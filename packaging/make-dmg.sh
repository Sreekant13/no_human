#!/usr/bin/env bash
# Package the built .app into a shareable DMG.
#
# Two things here are deliberate, both forced by what actually happens on a
# managed Mac:
#
#  * NOT electron-builder's dmg target. It drives Finder over AppleScript to lay
#    out the window; Finder keeps the volume busy and the build dies in
#    `hdiutil detach`.
#  * NOT `hdiutil create -srcfolder <dir-containing-.app>` either: that fails
#    with "Resource busy" on an app bundle (an empty image and a plain folder
#    both succeed, so it is the bundle specifically).
#
# So: create a read-write image, copy into it, FORCE-detach, convert compressed.
# The force is required because an endpoint-security agent can hold a volume
# containing an app bundle — a plain `detach` returns
# "couldn't unmount diskN - Operation not permitted".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${ROOT}/desktop/dist/mac-arm64/no_human.app"
OUT="${ROOT}/packaging/dist"
RW="${OUT}/.no_human-rw.dmg"
VOL="no_human"

# ONE source of truth for "is this build shippable" — desktop/signing.cjs, the
# same module electron-builder.config.cjs uses to decide whether to sign. A
# second copy of the rule here is how you get a signed .app inside a DMG named
# as if it were unsigned, or worse, the reverse.
read_plan() { (cd "${ROOT}/desktop" && node -e '
  const { signingPlan } = require("./signing.cjs");
  const v = signingPlan(process.env)[process.argv[1]];
  process.stdout.write(v === undefined || v === null ? "" : String(v));
' "$1"); }

SIGN_MODE="$(read_plan mode)"
ARTIFACT_TAG="$(read_plan artifactTag)"
VERSION="$(node -p "require('${ROOT}/desktop/package.json').version")"

# The filename carries the verdict. An operator cannot upload
# "no_human-0.1.0-UNSIGNED.dmg" to a release page believing it is shippable.
DMG="${OUT}/no_human-${VERSION}${ARTIFACT_TAG}.dmg"

test -d "${APP}" || { echo "FAIL: no .app at ${APP} — build it first" >&2; exit 1; }
mkdir -p "${OUT}"
rm -f "${RW}" "${DMG}"

# Size the image from the app plus headroom, so this keeps working as it grows.
size_mb=$(( $(du -sm "${APP}" | cut -f1) + 120 ))
hdiutil create -size "${size_mb}m" -fs HFS+ -volname "${VOL}" -ov "${RW}" >/dev/null

# Refuse to start if the name is taken: macOS would silently mount this image as
# "/Volumes/no_human 1" and the copy below would target the WRONG volume.
if [ -e "/Volumes/${VOL}" ]; then
  echo "FAIL: /Volumes/${VOL} is already mounted — detach it first" >&2
  exit 1
fi

# Take the mountpoint from the attach output (column 3) rather than assuming it.
attach_out="$(hdiutil attach "${RW}" -nobrowse -noverify)"
dev="$(printf '%s\n' "${attach_out}" | grep '/Volumes/' | head -1 | awk '{print $1}')"
mnt="$(printf '%s\n' "${attach_out}" | grep '/Volumes/' | head -1 | cut -f3-)"
cleanup() { hdiutil detach "${dev}" -force >/dev/null 2>&1 || true; }
trap cleanup EXIT
test -d "${mnt}" || { echo "FAIL: could not determine mountpoint" >&2; exit 1; }

cp -R "${APP}" "${mnt}/"
ln -s /Applications "${mnt}/Applications"   # drag-to-install affordance

hdiutil detach "${dev}" -force >/dev/null
trap - EXIT
hdiutil convert "${RW}" -format UDZO -o "${DMG}" >/dev/null
rm -f "${RW}"

# ---------------------------- sign / notarize ---------------------------- #
# electron-builder signs the .app; the DMG it is wrapped in is built here, so
# the container has to be signed, notarized and stapled here too. Stapling is
# what lets the DMG validate OFFLINE — without it, a user on a bad network sees
# Gatekeeper fail even though notarization succeeded.
if [ "${SIGN_MODE}" = "signed" ]; then
  echo "==> signing the DMG"
  # CSC_NAME is the identity when set; otherwise let codesign resolve the sole
  # Developer ID Application identity in the keychain.
  ident="${CSC_NAME:-Developer ID Application}"
  codesign --force --sign "${ident}" --timestamp "${DMG}"

  echo "==> notarizing (this waits on Apple and can take several minutes)"
  if [ -n "${APPLE_API_KEY:-}" ]; then
    xcrun notarytool submit "${DMG}" --wait \
      --key "${APPLE_API_KEY}" --key-id "${APPLE_API_KEY_ID}" \
      --issuer "${APPLE_API_ISSUER}"
  elif [ -n "${APPLE_KEYCHAIN_PROFILE:-}" ]; then
    xcrun notarytool submit "${DMG}" --wait \
      --keychain-profile "${APPLE_KEYCHAIN_PROFILE}"
  else
    xcrun notarytool submit "${DMG}" --wait \
      --apple-id "${APPLE_ID}" --password "${APPLE_APP_SPECIFIC_PASSWORD}" \
      --team-id "${APPLE_TEAM_ID}"
  fi

  echo "==> stapling"
  xcrun stapler staple "${DMG}"

  echo "==> verifying the SIGNATURE (all three must pass, or this is not shippable)"
  # These three answer "who built it" and "has Apple seen it". They do NOT
  # answer "what is in it" — the stale DMG passed all three. That question is
  # asked below, unconditionally, for signed and unsigned builds alike.
  codesign -dv --verbose=2 "${DMG}" 2>&1 | sed 's/^/    /'
  spctl -a -t open --context context:primary-signature -v "${DMG}" 2>&1 | sed 's/^/    /'
  xcrun stapler validate "${DMG}" 2>&1 | sed 's/^/    /'
else
  # Loud, and impossible to mistake for a release. This is what every build
  # without signing credentials in its environment produces — signingPlan()
  # decides from CSC_LINK/CSC_NAME alone.
  # `tr` rather than bash 4's ${x^^}: macOS ships bash 3.2 and this script
  # runs under /usr/bin/env bash, which is usually that one.
  mode_upper="$(printf '%s' "${SIGN_MODE}" | tr '[:lower:]' '[:upper:]')"
  cat >&2 <<EOF

  ────────────────────────────────────────────────────────────────────────
  ${DMG##*/} is ${mode_upper}.
  macOS Gatekeeper will REJECT it on any machine that downloads it, and the
  app cannot auto-update (Squirrel.Mac requires a signature).
  This is what a build with no Developer ID certificate available to it
  produces — see docs/DISTRIBUTION.md. Do not publish this artifact.
  ────────────────────────────────────────────────────────────────────────

EOF
fi

# ---------------------- what is INSIDE the DMG --------------------------- #
# WHY THIS IS HERE, AND WHY IT IS NOT OPTIONAL.
#
# The signature block above announces "all three must pass, or this is not
# shippable" — and the stale DMG passed all three. It was correctly signed,
# genuinely notarized and properly stapled; it simply contained a board built
# from a checkout 44 commits behind main. codesign proves WHO built it, spctl
# and stapler prove APPLE SAW it. Nothing in that trio, or anywhere else in
# this pipeline, ever asked WHICH SOURCE it came from.
#
# This is the last moment the artefact exists as one file about to be handed to
# a person, and it is the only place with both the DMG and the repo in hand. So
# the check lives here, after stapling (which rewrites the file), and it runs
# for unsigned builds too — a friend-shareable unsigned DMG can be just as
# stale, and the failure this exists to catch is not a signing failure.
#
# It is wired into the script rather than documented as a step because the
# repo's own scrub.yml already wrote down what happens otherwise: "A gate
# nobody is forced to run is the state the tip-only gates were already in."
#
# AND WHY THE EXPECTATION IS NOT `${ROOT}`'s OWN HEAD.
#
# The first version of this block ran the verifier with `--repo "${ROOT}"` and
# nothing else, so it compared the artefact's stamp against `git rev-parse HEAD`
# of THE SAME CHECKOUT build-installer.sh had just read that stamp from. That
# comparison is a tautology: it can only ever fail if someone edited the BOARD
# between building and packaging it.
#
# THAT SENTENCE SAID "the DMG", HERE AND IN TWO OTHER FILES, AND IT WAS FALSE.
# The seventh review drove it: `board_sha256` covers the files under the bundled
# `dist/` and NOTHING ELSE, so an `_internal/evil.so` planted beside the frozen
# server — with `migrations/003_backdoor.sql` and a replaced `app.asar` for
# company — verifies at a flat `OK`, rc=0. Everything outside `dist/` is outside
# the digest: the frozen server's code and its `_internal/` tree, `migrations/`,
# the Electron code, `app.asar`, and the .app's own metadata.
#
# WIDENING THE DIGEST IS OUT OF SCOPE, and not merely because nobody asked.
# `npm run dist:bundled` runs build-installer.sh — which writes the stamp — and
# only THEN electron-builder, which SIGNS the .app and in doing so rewrites the
# Mach-O binaries under `_internal/`. A digest taken over that tree at stamp
# time would disagree with itself on every signed build, which is the opposite
# of small and safe. What covers that tree is the .app's own codesign seal; the
# DMG's signature and `spctl` assessment are checked below.
#
# A reviewer built a DMG from a clone sitting
# 45 commits behind main, on a branch with no origin/main at all, and this block
# printed `verify-artefact: OK — built from 8d8a33130a12, clean tree` with rc=0.
# The property it was actually testing — "does the artefact match the checkout
# that made it" — is real, but it is not the property every WHY block on this
# branch claims, which is "WHICH SOURCE did it come from".
#
# The only thing standing between that clone and a shipped DMG was the
# behind-origin/main check in build-installer.sh, and that check is defeated
# three ways, all of them demonstrated: `git fetch … || true` swallows an
# unreachable remote, a stale local `main` is accepted as the fallback ref, and
# NH_ALLOW_STALE_BUILD=1 waives it outright.
#
# So a SIGNED build now resolves the expectation from the REMOTE, here, and
# every step of that resolution fails closed:
#
#   * not a git checkout            -> FAIL
#   * no remote named `origin`      -> FAIL
#   * ${ROOT}'s HEAD will not resolve, or the self-reference probe cannot be
#                                      created                        -> FAIL
#   * ${ROOT} itself does not advertise that probe, i.e. the probe is
#     blind and its silence would prove nothing                       -> FAIL
#   * `origin` advertises that probe, i.e. it shares ${ROOT}'s refs   -> FAIL
#   * the probe cannot be put to `origin` at all (unreachable)        -> FAIL
#   * the fetch does not succeed    -> FAIL. An unreachable remote is not
#                                      permission to skip the check; `|| true`
#                                      on a gate input is the bug.
#   * FETCH_HEAD is not a commit    -> FAIL
#   * the artefact's stamped commit != that tip -> the verifier FAILS
#
# WHAT THE UNSIGNED PATH DOES, AND WHAT IT CANNOT DO.
#
# An unsigned build must keep working offline: a point-in-time artefact for a
# friend is a legitimate thing to make, and requiring a reachable remote for it
# would only teach people to skip this script. So it keeps the LOCAL comparison
# against ${ROOT} — and that comparison is a tautology, because ${ROOT} is the
# checkout build-installer.sh just wrote the stamp from.
#
# It says so now. An earlier version of this comment claimed the unsigned
# verdict "comes back as rc=3 … never as a flat OK" because of --allow-dirty.
# THAT WAS FALSE, and shipped twice — here and in docs/INSTALLER.md.
# --allow-dirty downgrades only when the stamp says `dirty=yes`, and the
# checkout that caused the incident was CLEAN. A reviewer drove it: unsigned,
# clean, 45 commits behind, board reading "THIS IS THE WRONG BOARD" ->
# `verify-artefact: OK — built from 0ca24000e3ac, clean tree` / `OK: x.dmg
# [unsigned]`, rc=0, no caveat anywhere.
#
# So the tautology is DECLARED rather than hoped about: --repo-built-this-artefact
# tells the verifier the expectation comes from the build's own checkout, which
# makes the verdict rc=3 for every unsigned build, clean or dirty, and puts
# "provenance NOT verified" in the final line an operator reads. The verifier
# still RUNS the comparison — a stamp naming a commit ${ROOT} never built is a
# failure — it just can no longer call the result verified.
#
# THE LIMIT, stated because the last version of this comment overclaimed:
# nothing here establishes what an unsigned DMG was built from. Only the signed
# path does that, and only a signed DMG is distributable.
#
# WHY THE REMOTE AND BRANCH ARE LITERALS.
#
# They were `${NH_RELEASE_REMOTE:-origin}` and `${NH_RELEASE_BRANCH:-main}`, and
# this comment said there was "deliberately no NH_ALLOW_STALE_BUILD equivalent
# here". There were two, and the same reviewer drove them:
#
#   NH_RELEASE_REMOTE=$ROOT NH_RELEASE_BRANCH=HEAD   # signed, 45 behind
#   verify-artefact: OK — built from d7f611faad97 ; OK: x.dmg [signed]  rc=0
#
# Both pointed the release gate back at the artefact's own checkout — the exact
# tautology the signed path was rewritten to escape — while every fail-closed
# step above reported success. They are gone. A release comes from origin/main
# or it is not a release.
#
# What that does NOT cover. The previous version of this paragraph said
# removing those two variables "stops the ENVIRONMENT from redirecting the
# gate", and that anyone redirecting it "can still repoint `origin`" by
# rewriting `.git/config`. BOTH HALVES WERE FALSE, and the sixth review drove
# each of them separately:
#
#   * `GIT_NAMESPACE=rel`, an ENVIRONMENT variable and no config write at all,
#     blinded the self-reference check below — so removing NH_RELEASE_REMOTE and
#     NH_RELEASE_BRANCH plainly did not stop the environment.
#   * `transfer.hideRefs` in a GLOBAL gitconfig walked a signed 45-behind DMG
#     end to end, rc=0, without touching ${ROOT}'s `.git/config` and without
#     repointing `origin` — so `.git/config` was never where the boundary ran.
#
# Deleting two named variables is not the same as stopping the environment.
#
# What is true now is narrower and is stated as a mechanism rather than a
# boundary: the gate CLEARS the GIT_* variables that redirect it (the unset
# below), and CALIBRATES its self-reference probe against a class of config it
# cannot clear — with the limits of that calibration written out where it is
# made, not summarised here. What remains outside is anyone who can repoint
# `origin` — via `.git/config`, or via a global or system gitconfig `insteadOf` —
# at a DIFFERENT repository serving the same commits, and anyone who can choose
# the SSH COMMAND for an ssh `origin` (`GIT_SSH_COMMAND`, or `core.sshCommand` in
# a global config), which is a strictly stronger position: the ssh command is
# what ANSWERS, so it can blind the probe as well as redirect the fetch. Both
# spellings driven; see the GIT_SSH_COMMAND note in the scrub's list below.
#
# A THIRD SHAPE, and it is the one that shows why the first two read too
# narrowly: A `url` VALUE CAN BE A COMMAND.
#
#   [remote "origin"]                  # in a global config, alongside
#       url = ext::git -c uploadpack.hideRefs=refs/nh-self-probe \
#             upload-pack ${ROOT}
#   [protocol "ext"]
#       allow = always
#
# points `origin` at THIS VERY REPOSITORY, uses no ssh, and BLINDS the probe
# rather than redirecting it. Driven here: 45 commits behind, board reading
# "THIS IS THE WRONG BOARD" -> `verify-artefact: OK` / `OK: …x.dmg [signed]`,
# rc=0. Driven identically against f209d60e, so it is PRE-EXISTING and not a
# consequence of asking by url; the capability it needs is the one the two
# shapes above already declare — writing a config this gate reads on purpose —
# so the posture is unchanged and nothing here narrows the disclosure. It is
# named separately because it is neither of them, and because it refutes the
# natural reading of the sweep below, that only `url` and `pushurl` NAME a
# repository: a `url` need not name a repository at all. NOT CLOSED.
#
# One shape is checked below — `origin` resolving to ${ROOT}'s OWN REPOSITORY —
# because it reproduces the tautology exactly while looking like a normal fetch.
#
# THE EXACT SCOPE OF THAT CHECK. THREE successive versions of this paragraph
# were refuted by driven counterexamples, so read the boundary as stated, not as
# remembered — and note that what it now rests on is not a comparison of paths
# at all.
#
# It plants `refs/nh-self-probe/<pid>` in ${ROOT} and asks whether the release
# remote advertises it. The question is asked BY URL and never by the remote's
# NAME — `git ls-remote -- "${probe_url}"`, where ${probe_url} is the raw,
# first-listed `remote.origin.url` — and that is not a detail of spelling: every
# `remote.<name>.*` key applies to the name form and to nothing else, and one of
# them was driven blinding a by-name probe while the by-url positive control
# sailed through. The whole argument, and the key-by-key sweep of what it gives
# up, is at `probe_url` below rather than summarised here.
# A remote that answers is sharing this
# checkout's refs, and the resolution of the URL is git's own, so what is caught
# is every spelling git accepts: ${ROOT} by an absolute path, by a path relative
# to ${ROOT}, as `${ROOT}/.git`, through a symlink, through a
# `url.<x>.insteadOf` rewrite, as a LINKED WORKTREE of ${ROOT} in either
# direction, and as a `file://` URL in any of its forms — with an empty
# authority, with a host (git ignores the host: `file://example.com${ROOT}`
# resolves), with userinfo or a port, and with percent-escapes (git decodes
# them; the filesystem does not).
#
# The `file://` shape is the FIFTH review's finding, and it is why the
# derivation is gone rather than patched a fourth time: this check has had
# THREE path derivations (7afd79f4, c2868265, 59c623c0) and four distinct
# defects in them — the third carried two, the authority and the percent-escape
# — and every one failed the same fail-open way: the arithmetic yielded
# something unresolvable and the code read that as "not self-referential".
#
# It does NOT catch a remote that is a separate REF NAMESPACE merely holding the
# same COMMITS — a bare mirror, a `--mirror` clone, a BUNDLE FILE used as
# `origin`, a second clone, or a `cp -al` hardlink farm of ${ROOT}. (The mirror
# and the bundle are the sixth review's additions to this list; both were driven
# walking a stale signed build through, and a bundle is worth naming separately
# because it is a single file rather than something that looks like a
# repository.) An earlier version of this paragraph justified that with "those have
# their own object store and their own refs", which is FALSE and was refuted:
# `cp -al` gives the copy the SAME INODES as ${ROOT}, for the object files and
# for `refs/heads/main` alike (driven: identical inode numbers, and the copy
# serves ${ROOT}'s stale main). The boundary is narrower than that clause
# claimed, and it is what the probe actually rests on: a ref CREATED in ${ROOT}
# after such a copy exists never appears in it — git writes a ref by rename, so
# the copy keeps the old inode and does not track the update — so the probe
# cannot see itself there, and nothing else in this script could distinguish it
# from a real remote serving those commits either. Staleness relative to a
# mirror of a stale tree is not detectable from inside this script.
#
# `hideRefs` USED TO BE PARKED HERE, AND THAT WAS WRONG. The last version of
# this paragraph filed `uploadpack.hideRefs` under the residual, on the grounds
# that it "sits inside the `.git/config` trust boundary" and "anyone who can set
# it can repoint `origin`". The sixth review refuted both clauses by driving it:
# `transfer.hideRefs` in a GLOBAL gitconfig — not ${ROOT}'s `.git/config`, and
# not a repoint of anything — blinded the probe and shipped a signed 45-behind
# DMG. A knob in a file the gate never looks at is not inside a boundary drawn
# around a file it does. It is now a FAILURE, caught by the probe's positive
# control below, and it is no longer on this list.
#
# Note the distinction that was got wrong once: "same commits" is the limit,
# "same repository" is NOT — a worktree is the latter.
echo "==> verifying the CONTENTS against the source this build claims"

# THE GATE'S GIT MUST NOT TAKE ITS ANSWER FROM THE ENVIRONMENT.
#
# The sixth review's finding, and the reason it took six rounds to surface:
# every test above ran with GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM pinned to
# /dev/null and no GIT_* redirects set, while the real build inherits the
# operator's whole environment. The harness was CLEANER THAN THE REAL THING, so
# this entire class was invisible to it.
#
# Three members of the class were driven against the previous commit. Their
# results differ, and are recorded separately rather than averaged into one
# claim:
#
#   GIT_NAMESPACE=rel        BLINDS THE PROBE. `git ls-remote origin` answers
#                            EMPTY with rc=0 for an origin that IS ${ROOT}, and
#                            the code below read that emptiness as "not
#                            self-referential". GIT_NAMESPACE is NOT in git's
#                            `local_repo_env` scrub list, so the `upload-pack`
#                            the local transport spawns inherits it and
#                            advertises only `refs/namespaces/rel/*`; the probe
#                            was there and nothing was asked to show it.
#                            HONEST LIMIT: the reviewer who found this reported
#                            it walking a signed 45-behind DMG all the way to
#                            rc=0. THAT DID NOT REPRODUCE HERE (git 2.49.0):
#                            the same namespace also hides `refs/heads/main`,
#                            so the fetch below fails and the build stops. The
#                            BLINDING reproduced exactly; the end-to-end ship
#                            did not, and is not claimed.
#   transfer.hideRefs        WALKS A BUILD THROUGH, end to end, driven here:
#     = refs/nh-self-probe   set in a GLOBAL gitconfig, origin = ${ROOT}, HEAD
#     (in a global config)   45 behind, board reading "THIS IS THE WRONG BOARD"
#                            -> `verify-artefact: OK` / `OK: …x.dmg [signed]`,
#                            rc=0. It hides the probe from `upload-pack` while
#                            leaving `refs/heads/main` advertised, so unlike the
#                            namespace it does not trip anything downstream.
#   GIT_DIR=<other>/.git     overrides `git -C "${ROOT}"` outright (driven:
#                            `GIT_DIR=$O/.git git -C R rev-parse HEAD` prints
#                            O's HEAD). Every git below would then resolve
#                            HEAD, plant the probe and read `origin` in a
#                            repository that did not build this artefact. No
#                            end-to-end ship was constructed for it; the
#                            redirection is the defect and is enough.
#
# So the gate clears them rather than inheriting them. This is the FIRST thing
# the block does, and it is inside the block on purpose: the tests lift this
# block out and execute it, so a scrub placed at the top of the file would be
# real but untested. make-dmg.sh runs no git before this point.
#
# WHAT IS CLEARED, AND ON WHAT EVIDENCE. Driven to an observable effect on this
# gate's own commands: GIT_NAMESPACE (above), GIT_DIR (above),
# GIT_ALTERNATE_OBJECT_DIRECTORIES (driven: makes another repository's commits
# resolve in ${ROOT}, and `HEAD^{commit}` / `FETCH_HEAD^{commit}` are object
# lookups), and GIT_CONFIG_COUNT, which REPOINTS `origin` from the environment
# alone: `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.<other>.insteadOf
# GIT_CONFIG_VALUE_0=${ROOT}` makes `git ls-remote origin` answer from <other>
# (driven). Note the near miss, recorded because the obvious version of this
# claim is WRONG: setting `remote.origin.url` that way does NOT repoint
# anything — git treats the remote's url as multi-valued, appends the env one,
# and uses the first (driven: `config --get-all` shows both, `remote get-url`
# still answers ${ROOT}'s). It is `insteadOf` that does it. Clearing the COUNT
# is sufficient for all of them: git ignores GIT_CONFIG_KEY_n/VALUE_n without
# it (driven). The remainder — GIT_WORK_TREE,
# GIT_COMMON_DIR, GIT_OBJECT_DIRECTORY, GIT_INDEX_FILE, GIT_GRAFT_FILE,
# GIT_SHALLOW_FILE, GIT_REPLACE_REF_BASE, GIT_NO_REPLACE_OBJECTS,
# GIT_CEILING_DIRECTORIES, GIT_CONFIG, GIT_CONFIG_PARAMETERS — is cleared
# because it redirects the same three things (where the repo is, which objects
# exist, what the config says) and NOT because a bypass was demonstrated for
# each; say so rather than imply nine more driven exploits.
#
# WHAT IS DELIBERATELY NOT CLEARED, so the omissions are decisions and not
# oversights:
#
#   * GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM. `url.<x>.insteadOf` in a global
#     config is a legitimate way `origin` resolves, and blanking it here would
#     make this gate resolve `origin` differently from every other git in the
#     build. A HOSTILE global config is handled instead by the probe's positive
#     control below, which fails closed — that is the general answer, and it
#     covers configs this list could not enumerate.
#   * GIT_SSH_COMMAND. It selects the ssh binary, and clearing it would break
#     legitimate custom ssh setups.
#
#     THE PREVIOUS SENTENCE HERE WAS FALSE AND THE SEVENTH REVIEW DROVE IT. It
#     read: "Redirecting it reaches a DIFFERENT repository, which is the residual
#     already declared below, not a blinding of this check." The first half
#     ("no effect on a local-path origin") was separately re-confirmed and is
#     kept. The second half is refuted: on an ssh origin the ssh command is what
#     ANSWERS THE PROBE, so it can hide the probe ref while still advertising
#     `refs/heads/main`. Driven, with `origin = ssh://localhost${ROOT}` and a
#     wrapper running `git -c uploadpack.hideRefs=refs/nh-self-probe upload-pack
#     "${ROOT}"` -> `verify-artefact: OK` / `OK: …x.dmg [signed]`, rc=0, 45
#     commits behind, board reading "THIS IS THE WRONG BOARD". The positive
#     control cannot calibrate it, because the control asks ${ROOT} over a LOCAL
#     PATH and the ssh command is not on that transport.
#
#     It is therefore a BLINDING and it is filed under the residual DELIBERATELY,
#     not by mistake: `core.sshCommand` in a GLOBAL gitconfig does the identical
#     thing with no environment variable at all (driven, same result), so adding
#     GIT_SSH_COMMAND to the scrub would close one spelling of it while the gate
#     goes on reading that config on purpose. Pinned, both spellings, by
#     test_the_declared_residual_an_ssh_redirect_still_walks_through, which
#     asserts the build DOES walk through — if that test ever fails, this
#     paragraph is what needs rewriting.
#   * GIT_PROTOCOL. git sets it for its own children; there is no read of it
#     here that changes an answer, and none was demonstrated.
unset GIT_NAMESPACE GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR \
      GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
      GIT_INDEX_FILE GIT_GRAFT_FILE GIT_SHALLOW_FILE \
      GIT_REPLACE_REF_BASE GIT_NO_REPLACE_OBJECTS GIT_CEILING_DIRECTORIES \
      GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT

RELEASE_REMOTE="origin"
RELEASE_BRANCH="main"
expect_flag=""
local_flag="--repo-built-this-artefact"
if [ "${SIGN_MODE}" = "signed" ]; then
  local_flag=""
  if ! git -C "${ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
    echo "FAIL: ${ROOT} is not a git checkout, so there is no canonical source" >&2
    echo "      to hold this release against. Refusing to sign off blind." >&2
    exit 1
  fi
  echo "    a signed release must match ${RELEASE_REMOTE}/${RELEASE_BRANCH}; resolving it"

  # The remote's URL is a gate input like any other: absent means the release
  # tip cannot be established, which is a FAILURE.
  if ! remote_url="$(git -C "${ROOT}" remote get-url "${RELEASE_REMOTE}" 2>/dev/null)" \
     || [ -z "${remote_url}" ]; then
    echo "FAIL: ${ROOT} has no remote named '${RELEASE_REMOTE}', so there is no" >&2
    echo "      canonical source to hold this release against." >&2
    exit 1
  fi

  # EVERY QUESTION BELOW IS ASKED BY URL, NEVER BY REMOTE NAME. The seventh
  # review's F1: the probe's positive control asked `${ROOT}` — a URL — while the
  # probe itself asked `${RELEASE_REMOTE}` — a NAME. The whole `remote.<name>.*`
  # config family applies to the name form and to nothing else, so it could act on
  # one and not the other, which is precisely the asymmetry a control exists to
  # rule out. Driven against f209d60e, in two directions, with `remote.origin.*`
  # set in a GLOBAL gitconfig the gate deliberately does not clear:
  #
  #   uploadpack = "git -c uploadpack.hideRefs=refs/nh-self-probe upload-pack"
  #       BLINDS THE PROBE while the control passes. origin = ${ROOT}, HEAD 45
  #       behind, board reading "THIS IS THE WRONG BOARD" -> `verify-artefact:
  #       OK` / `OK: …x.dmg [signed]`, rc=0. The `transfer.hideRefs` bypass the
  #       sixth review fixed, one config key later.
  #   uploadpack = "git upload-pack '${ROOT}' #"
  #       REDIRECTS THE RELEASE-TIP FETCH. git appends the repository path to
  #       that string and runs it through a shell for a local transport, so the
  #       `#` comments the real path out and ${ROOT} is served in its place;
  #       FETCH_HEAD comes back as the artefact's own stale commit. Nothing is
  #       self-referential here — `origin` is a genuine remote — so the probe is
  #       right to stay silent. On fd0e40b1 this was caught only by ACCIDENT:
  #       the by-name probe went through the same hostile `uploadpack` and so
  #       saw itself. Moving the probe to a url and leaving the fetch on the
  #       name would have turned that accident into a shipped stale DMG, which
  #       is why both move together.
  #
  # WHAT THIS DOES NOT CLOSE, swept key by key rather than claimed as a class —
  # this check's own history is a run of comments that generalised further than
  # their code. Of `remote.<name>.*`, only `url` and `pushurl` decide WHAT
  # ANSWERS, and `pushurl` is push-side. (Not "NAME a repository", which is how
  # this sentence read and which is false: an `ext::` value is a COMMAND, not a
  # repository name — see the third residual shape at the head of this block.)
  # `uploadpack` was the whole of the fetch-side
  # attack surface and both of its halves are above. `receivepack`, `mirror`,
  # `push`, `skipDefaultUpdate`, `skipFetchAll` and `followRemoteHEAD` do not act
  # on a fetch of an explicit refspec into FETCH_HEAD; `fetch`, `tagOpt`, `prune`
  # and `pruneTags` shape refspecs and remote-tracking refs, and this fetch
  # supplies its own refspec and reads FETCH_HEAD; `proxy`, `proxyAuthMethod`,
  # `serverOption`, `promisor`, `partialclonefilter` and `vcs` change HOW the
  # transport runs, not WHICH repository answers. What is genuinely GIVEN UP by
  # not going through the remote name is that last group: a build machine whose
  # release remote needs a PER-REMOTE proxy, or a foreign-VCS helper named by
  # `remote.origin.vcs` rather than by a url scheme, now fails to reach it — and
  # fails CLOSED, with the unreachable-remote message, rather than answering
  # wrongly. That is the trade, taken deliberately.
  #
  # And `remote.<name>.*` is not the only family: `url.<base>.insteadOf` in a
  # global config still rewrites, by design, and can still point `origin` at a
  # DIFFERENT repository serving the same commits. That is the residual declared
  # at the head of this block, unchanged and not narrowed by this fix.
  #
  # WHICH url, because two of the three obvious spellings are wrong and each was
  # driven:
  #
  #   * NOT `git remote get-url`'s answer. It reports the url with `insteadOf`
  #     ALREADY applied, so handing it back to git rewrites a SECOND time. With
  #     `url.A.insteadOf = raw` and `url.B.insteadOf = A`, `ls-remote origin`
  #     reaches A and `ls-remote "$(git remote get-url origin)"` reaches B — the
  #     probe would be asking about a repository the fetch never touches.
  #   * NOT `git config --get`. It answers the LAST value of a multi-valued key;
  #     git fetches from the FIRST.
  #
  # So: the RAW, first-listed `remote.<name>.url`, with git's own resolver
  # applying `insteadOf` exactly once — the same string, through the same code,
  # as `git fetch ${RELEASE_REMOTE}` would have used. `insteadOf` therefore still
  # works, and nothing legitimate is lost by it.
  #
  # `git remote get-url` ANSWERS THE REMOTE'S NAME when the remote has no url at
  # all (driven: with only `remote.origin.pushurl` set it prints `origin` and
  # exits 0), so the guard above does not fire and this one must.
  #
  # The `|| probe_url=""` is not the `|| true` shape this branch exists to
  # delete: `git config --get-all` exits 1 for a key that is not set, and under
  # `set -e` that would abort with no message at all. The emptiness is CHECKED on
  # the next line and is a FAILURE there.
  probe_url="$(git -C "${ROOT}" config --get-all \
                   "remote.${RELEASE_REMOTE}.url" 2>/dev/null \
                 | awk 'NR == 1')" || probe_url=""
  if [ -z "${probe_url}" ]; then
    echo "FAIL: remote '${RELEASE_REMOTE}' has no configured url in ${ROOT}, so" >&2
    echo "      there is no canonical source to hold this release against." >&2
    exit 1
  fi
  # A remote that IS this checkout makes the fetch a mirror of the tree that
  # built the artefact: it succeeds, FETCH_HEAD is a real commit, and the answer
  # is still "the artefact matches itself".
  #
  # THE URL IS NOT PARSED. Three successive versions of this check DERIVED a
  # filesystem path from the URL and compared repository identity at that path.
  # Four URLs that git resolves perfectly well defeated them — the third
  # version carried the last two defects at once:
  #
  #   1. `[ -d "${remote_path}" ]` in the SHELL's cwd, while the fetch is
  #      `git -C "${ROOT}"` — `npm run dist:bundled` runs this script from
  #      ${ROOT}/desktop, so `origin = .` was a directory that existed neither
  #      there nor as a checkout;
  #   2. `--absolute-git-dir`, which is PER-WORKTREE — a linked worktree of
  #      ${ROOT} answers `${ROOT}/.git/worktrees/<name>` while ${ROOT} answers
  #      `${ROOT}/.git`, yet the two share one object store and one
  #      `refs/heads/main`;
  #   3. `${remote_url#file://}`, which strips the SCHEME but not the
  #      AUTHORITY — `file://localhost${ROOT}` left `localhost/…`, which has no
  #      leading slash, so it was joined onto ${ROOT} as if it were relative;
  #   4. percent-escapes, which git DECODES for a `file://` URL and the
  #      filesystem does not — `file:///…/%52oot` is ${ROOT} to git and a
  #      non-existent directory to `[ -d ]`. (git ignores the authority
  #      entirely, so `file://example.com${ROOT}` resolves too.)
  #
  # Every one of those failed the SAME way: the arithmetic produced something
  # unresolvable, the `&&` chain fell through, and "cannot tell" was read as
  # "not self-referential" — a fail-open on a gate input. Each shipped a signed,
  # 45-commits-behind DMG with a flat `OK: …x.dmg [signed]`, rc=0.
  #
  # So the question is put to the REMOTE instead, in the only vocabulary that
  # cannot be got wrong: plant a ref nothing else can have, and ask whether the
  # remote advertises it. `git ls-remote -- "${probe_url}"` hands git the RAW
  # url and lets git's own resolver do all of it — URL syntax, `insteadOf`
  # rewrites (applied exactly once), symlinks, percent-escapes, relative paths
  # against ${ROOT}, `.git` dirs and linked worktrees all included — so there is
  # no derivation left to be wrong about.
  # A separate repository holding the same COMMITS cannot have a ref created in
  # this repository seconds ago, which is exactly the boundary this check is
  # supposed to draw.
  probe_ref="refs/nh-self-probe/$$"
  probe_cleanup() {
    git -C "${ROOT}" update-ref -d "${probe_ref}" >/dev/null 2>&1 || true
  }
  # The probe points at an object ${ROOT} already has, so nothing is written to
  # the object store and nothing becomes newly reachable. The EXIT trap covers
  # the window between creating the ref and reading the answer — including the
  # `exit 1` inside it — and the ref is deleted before the verdict below, so no
  # later exit path can leave it behind either. Pinned, both ways out, by
  # test_the_self_reference_probe_leaves_no_ref_behind.
  if ! root_head="$(git -C "${ROOT}" rev-parse --verify -q 'HEAD^{commit}')" \
     || ! printf '%s' "${root_head}" | grep -Eq '^[0-9a-f]{40}$'; then
    echo "FAIL: cannot resolve ${ROOT}'s own HEAD, so the self-reference check" >&2
    echo "      has nothing to plant. Its input is missing, which is a FAILURE —" >&2
    echo "      a gate that skips itself when it cannot see is not a gate." >&2
    exit 1
  fi
  trap probe_cleanup EXIT
  if ! git -C "${ROOT}" update-ref "${probe_ref}" "${root_head}"; then
    echo "FAIL: cannot create ${probe_ref} in ${ROOT}, so the self-reference" >&2
    echo "      check cannot be made. That is a FAILURE, not a skip." >&2
    exit 1
  fi
  # THE PROBE'S SILENCE MEANS NOTHING UNTIL THE PROBE IS SHOWN TO WORK.
  #
  # An empty `probe_seen` has TWO readings — "origin is not this repository" and
  # "the probe cannot be seen at all" — and the code below acts on the first.
  # The sixth review found the second. Two spellings make `ls-remote` answer
  # EMPTY with rc=0 for an origin that IS ${ROOT}: `GIT_NAMESPACE` in the
  # environment, and `transfer.hideRefs = refs/nh-self-probe` in a GLOBAL
  # gitconfig. The second one shipped a signed, 45-commits-behind, wrong-board
  # DMG at rc=0 when driven; the first stopped at the fetch on this git version
  # (see the scrub's note at the head of this block) — but both blinded the
  # probe, which is the defect, and the fetch is not this check's safety net.
  #
  # The unset above deletes the environment one. The config one cannot be
  # deleted the same way: that config is read ON PURPOSE, because `insteadOf`
  # in it is a legitimate way `origin` resolves (see the GIT_CONFIG_GLOBAL note
  # at the head of this block). Nor could a list of knobs be kept ahead of
  # whatever the next `hideRefs`-shaped one turns out to be.
  #
  # So the instrument is CALIBRATED instead of trusted: ask the same question of
  # ${ROOT} itself, a remote that trivially is this repository and must
  # therefore advertise the probe. If it does not, this check cannot see, and a
  # check that cannot see must FAIL rather than report clean.
  #
  # THE CONTROL AND THE PROBE MUST BE THE SAME KIND OF QUESTION. An earlier
  # version of this paragraph ended "that is the whole class, not the two
  # spellings of it", and the seventh review refuted it in one config key: the
  # control asked a URL and the probe asked a NAME, so `remote.origin.uploadpack`
  # blinded the probe while the control passed, and a signed 45-behind DMG
  # shipped at rc=0. Both are urls now (see the `probe_url` note above); that
  # removes the ASYMMETRY, which is a different and smaller claim than removing
  # the class, and the sweep of what remains is written out up there rather than
  # summarised here.
  #
  # ITS LIMITS, stated so it is not read as more. Two of them:
  #
  #   * The control exercises the `upload-pack` THIS machine spawns for a LOCAL
  #     PATH — because ${ROOT} is one. Production `origin` is https, and no
  #     control here runs against an https server; the reviewer's point, and it
  #     stands. What the control establishes is that the LOCAL upload-pack path
  #     is not hiding the probe, which is the transport every driven bypass so
  #     far has used, and which is the transport a self-referential `origin` must
  #     use, since ${ROOT} is on this filesystem. It establishes nothing about a
  #     genuinely remote server's advertisement — but such a server is a
  #     different repository, which has no probe to hide.
  #   * It cannot see a config keyed on ${remote_url}'s spelling but not on
  #     ${ROOT}'s literal one — an `insteadOf` rewrite is the example — because
  #     the two ask different url strings by construction. Such a rewrite points
  #     `origin` at a different repository, which is the declared residual.
  if ! control_seen="$(git -C "${ROOT}" ls-remote -- "${ROOT}" \
                           "${probe_ref}" 2>/dev/null)" \
     || [ -z "${control_seen}" ]; then
    echo "FAIL: the self-reference probe is blind — ${ROOT} did not advertise" >&2
    echo "      ${probe_ref}, a ref that exists in it right now. Something is" >&2
    echo "      hiding it (transfer.hideRefs / uploadpack.hideRefs in a global" >&2
    echo "      or system gitconfig is the known cause), so an empty answer" >&2
    echo "      from ${RELEASE_REMOTE} would prove nothing. Unanswerable is a" >&2
    echo "      FAILURE, not a skip." >&2
    exit 1
  fi
  # `--` BEFORE THE URL, AND IT IS LOAD-BEARING. The url comes from CONFIG, and
  # without the separator a value beginning with `-` reaches git as an OPTION
  # rather than as a repository. Measured on git 2.49.0, not assumed:
  #
  #   [remote "origin"]                              # in a GLOBAL gitconfig,
  #       url = --upload-pack=touch <file>           # which lands FIRST in
  #                                                  # `config --get-all`
  #
  #   without `--`  git runs `touch <file> refs/nh-self-probe/<pid>` for the
  #                 local transport. The file IS CREATED — arbitrary command
  #                 execution inside a signed release build — and the gate then
  #                 exits 1 with the SAME rc and the SAME stderr as a clean
  #                 refusal (driven: identical once the run's own temp path,
  #                 which differs between any two runs, is normalised).
  #   with `--`     `fatal: strange pathname '--upload-pack=…' blocked`, nothing
  #                 runs, and the gate fails closed on the same message.
  #
  # THE PREVIOUS VERSION OF THIS NOTE WAS FALSE. It read "(git 2.49 also refuses
  # such a pathname on its own; the separator does not depend on that.)" Both
  # halves are wrong, and in the same way: WITHOUT the separator there is no
  # pathname for git to refuse — the value is consumed as an option long before
  # any pathname check — so that refusal is reachable ONLY BECAUSE the separator
  # makes git treat the value as the repository. It depends on it entirely.
  # (What git does check on its own is the string AFTER `insteadOf`: a rewrite
  # that PRODUCES a `-`-leading url is blocked either way, because the rewritten
  # string never passes through argv. Also measured.)
  #
  # f209d60e could not be reached this way: it asked `${RELEASE_REMOTE}` — a
  # literal — so no config value was ever an ARGUMENT, and git resolved the url
  # internally where it is not option-parsed (driven: rc=1, nothing ran). The
  # by-url pivot is what created this surface, and this separator is the whole
  # of the mitigation. Pinned by
  # test_a_configured_url_that_is_a_git_OPTION_cannot_execute_a_command, whose
  # own observer is test_the_url_separator_mutation_is_actually_caught — the
  # assertion has to be the side effect, because the exit code and every message
  # are identical with and without it, which is why this survived 140 tests.
  if ! probe_seen="$(git -C "${ROOT}" ls-remote -- "${probe_url}" \
                         "${probe_ref}" 2>/dev/null)"; then
    echo "FAIL: cannot reach ${RELEASE_REMOTE} to ask whether it is this very" >&2
    echo "      repository, so neither that check nor the release tip below can" >&2
    echo "      be established. That is a FAILURE, not a skip." >&2
    exit 1
  fi
  probe_cleanup
  trap - EXIT
  if [ -n "${probe_seen}" ]; then
    echo "FAIL: ${RELEASE_REMOTE} resolves to this very repository (${remote_url})." >&2
    echo "      It advertised ${probe_ref}, a ref created here moments ago, so" >&2
    echo "      it shares this checkout's refs — whether it IS ${ROOT}, a linked" >&2
    echo "      worktree of it, or another spelling of the same path. Fetching" >&2
    echo "      it would compare the artefact against the tree that built it," >&2
    echo "      which is the tautology this gate exists to end." >&2
    exit 1
  fi

  # `refs/heads/${RELEASE_BRANCH}`, NOT the bare name. `git fetch origin main`
  # is unqualified, and a TAG named `main` on the release remote resolves ahead
  # of the branch: `* tag main -> FETCH_HEAD`. A reviewer pushed one and walked a
  # signed build 45 commits behind through this gate with no env var and no
  # change to this checkout — flat OK, rc=0, [signed]. A release branch is a
  # BRANCH; say so in the refspec.
  #
  # BY URL, for the reason given at `probe_url` above: `remote.origin.uploadpack`
  # moves the SERVER SIDE of a by-name fetch, and was driven serving ${ROOT} to a
  # fetch whose `origin` was a genuine current remote — FETCH_HEAD came back as
  # the artefact's own stale commit and `--expect-commit` was satisfied by it.
  #
  # The `--` here is the same guard as at the `ls-remote` above, and it is
  # DEFENCE IN DEPTH rather than a second live check: this line is only reached
  # when that `ls-remote` succeeded on the same ${probe_url}, and a `-`-leading
  # value never gets that far. Driven, all three mutants against the same
  # payload — removing it from the `ls-remote` alone, or from both, executes the
  # command; removing it from THIS line alone changes nothing observable and no
  # test dies. That survivor is deliberate and is recorded rather than papered
  # over: it exists for a future edit that reorders or drops the question above.
  if ! git -C "${ROOT}" fetch -q -- "${probe_url}" \
         "refs/heads/${RELEASE_BRANCH}"; then
    echo "FAIL: cannot reach ${RELEASE_REMOTE} to resolve ${RELEASE_BRANCH}, so the" >&2
    echo "      commit this release is supposed to be CANNOT be established." >&2
    echo "      That is a FAILURE, not a skip: a gate that passes when its own" >&2
    echo "      input is missing is how the stale DMG shipped." >&2
    exit 1
  fi
  # FETCH_HEAD, not the local remote-tracking ref: it is what the fetch above
  # just returned, rather than a `origin/main` that may not have been updated
  # since the clone.
  #
  # STATED EXACTLY, because the earlier wording ("so a stale local main cannot
  # stand in for it") was refuted: FETCH_HEAD is only as good as WHAT WAS
  # FETCHED FROM. When `origin` was a linked worktree of ${ROOT}, the fetch
  # succeeded and handed back ${ROOT}'s own stale `refs/heads/main` — FETCH_HEAD
  # *was* the stale local main. This line does not establish otherwise; the
  # self-reference check above is what makes that case unreachable.
  release_sha="$(git -C "${ROOT}" rev-parse --verify -q 'FETCH_HEAD^{commit}' || true)"
  if ! printf '%s' "${release_sha}" | grep -Eq '^[0-9a-f]{40}$'; then
    echo "FAIL: ${RELEASE_REMOTE}/${RELEASE_BRANCH} did not resolve to a commit" >&2
    echo "      (got '${release_sha}'). Provenance cannot be established." >&2
    exit 1
  fi
  echo "    ${RELEASE_REMOTE}/${RELEASE_BRANCH} = ${release_sha}"
  expect_flag="--expect-commit ${release_sha}"
fi

if [ -e "/Volumes/${VOL}" ]; then
  echo "FAIL: /Volumes/${VOL} is already mounted — detach it first" >&2
  exit 1
fi
v_out="$(hdiutil attach "${DMG}" -nobrowse -noverify -readonly)"
v_dev="$(printf '%s\n' "${v_out}" | grep '/Volumes/' | head -1 | awk '{print $1}')"
v_mnt="$(printf '%s\n' "${v_out}" | grep '/Volumes/' | head -1 | cut -f3-)"
vcleanup() { hdiutil detach "${v_dev}" -force >/dev/null 2>&1 || true; }
trap vcleanup EXIT
test -d "${v_mnt}" || { echo "FAIL: could not mount ${DMG} to verify it" >&2; exit 1; }

PY="${ROOT}/.venv/bin/python"
[ -x "${PY}" ] || PY="$(command -v python3)"

# A release must correspond to a commit. A local build legitimately does not —
# the filename already says it is not shippable — so only there is a dirty tree
# accepted. Everything else (the commit, the board digest) is checked in both.
allow_dirty=""
[ "${SIGN_MODE}" = "signed" ] || allow_dirty="--allow-dirty"

verify_rc=0
# shellcheck disable=SC2086  # each is one optional flag, or nothing
"${PY}" "${ROOT}/scripts/verify_artefact.py" "${v_mnt}" \
  --repo "${ROOT}" ${allow_dirty} ${local_flag} ${expect_flag} || verify_rc=$?

hdiutil detach "${v_dev}" -force >/dev/null
trap - EXIT

# rc=3 is "every check that ran passed, and at least one was weakened" — for an
# unsigned build that is --repo-built-this-artefact (always) and --allow-dirty
# (when the tree was dirty), and it is the honest verdict. For a signed one it
# must never happen (no weakening flag is passed), and if it somehow does, a
# release with unverified provenance is exactly what this block exists to stop.
#
# The verdict must survive into the LAST line, not just this note. The reviewer's
# demonstration ended `OK: …x.dmg [unsigned]` — an operator scrolling to the
# bottom saw a bare OK over a board 45 commits stale. The tag carries it now, the
# same way the FILENAME carries the signing verdict.
#
# WHY THE COLLAPSE TO 0 IS DELIBERATE, AND WHY THE FILENAME CARRIES THIS.
#
# A reviewer flagged the shape: docs/INSTALLER.md argues rc=3 exists precisely
# because "prose is not a signal a pipeline can act on", and then this script
# turns 3 into 0 and leaves the verdict as text. Decided, not left open: the
# collapse STAYS, because the distinction is already machine-readable one level
# up, in the filename — and not as an approximation of the verdict but as the
# SAME predicate.
#
#   * SIGN_MODE and ARTIFACT_TAG are two reads of ONE signingPlan() call in
#     desktop/signing.cjs. artifactTag is "" if and only if mode is "signed";
#     the other two modes get "-UNNOTARIZED" / "-UNSIGNED". All three are
#     pinned in desktop/signing.test.mjs.
#   * ${local_flag} — the only thing that makes rc=3 reachable at all, with
#     --allow-dirty — is set if and only if SIGN_MODE is not "signed".
#   * the collapse below is guarded on SIGN_MODE != "signed", so a SIGNED build
#     that somehow returns 3 is NOT collapsed: it falls into the FAIL block and
#     exits 1. Pinned by test_rc3_from_a_SIGNED_build_is_never_collapsed.
#
# So `exit 0` from this script means: verified against origin/main if the
# artefact's name has no tag, provenance-not-verified if it has one. There is no
# third state, and a caller reading `$?` alongside a filename it must already
# read — it has to, to decide distributability — loses nothing.
#
# The alternative, propagating 3 out of make-dmg.sh, was rejected for a concrete
# reason: a developer with no signing credentials in their environment gets an
# unsigned build from EVERY `npm run dist:bundled`, so this script would exit
# non-zero on every one of them. (An earlier version of this paragraph said
# "every build is unsigned until the Apple Developer membership exists". That is
# no longer true — the notarization credentials are stored and were validated by
# Apple — and it was never the load-bearing part: signingPlan() reads the
# ENVIRONMENT alone, so the argument is about a developer's machine, not about
# the state of an account.) A tool that always fails teaches people to append
# `|| true` — which is the exact fail-open shape (`git fetch … || true`) this
# branch exists to delete. That trade would buy a signal nobody reads at the
# cost of the habit that shipped the stale DMG.
#
# WHAT WOULD RETIRE THIS ARGUMENT: it rests entirely on the filename carrying
# the mode. If make-dmg.sh ever emits an artefact whose name does not, the
# reasoning above is void and rc must be propagated instead.
provenance_tag=""
if [ "${verify_rc}" = "3" ] && [ "${SIGN_MODE}" != "signed" ]; then
  echo "NOTE: provenance for ${DMG##*/} is NOT verified (rc=3) — it matches the" >&2
  echo "      checkout that built it and nothing more, and that checkout may" >&2
  echo "      itself be stale. Unsigned builds are not distributable anyway;" >&2
  echo "      see the banner above." >&2
  provenance_tag="; provenance NOT verified"
  verify_rc=0
fi

if [ "${verify_rc}" != "0" ]; then
  # Name what was ACTUALLY compared. An unsigned build is never held against
  # ${RELEASE_REMOTE}/${RELEASE_BRANCH}, and saying it was is the same class of
  # untrue-in-one-branch claim that put this block through a third review.
  held_against="${RELEASE_REMOTE}/${RELEASE_BRANCH}"
  [ "${SIGN_MODE}" = "signed" ] || held_against="the checkout that built it (${ROOT})"
  echo "" >&2
  echo "FAIL: ${DMG##*/} does not match ${held_against} (verify_artefact.py rc=${verify_rc})." >&2
  echo "      This is the failure that shipped once already: a clean, correctly" >&2
  echo "      signed build of the WRONG TREE. Do not distribute this file." >&2
  exit 1
fi

echo "OK: ${DMG} ($(du -sh "${DMG}" | cut -f1)) [${SIGN_MODE}${provenance_tag}]"
