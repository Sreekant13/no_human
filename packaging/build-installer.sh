#!/usr/bin/env bash
# Build the no-source nh server bundle: React board -> PyInstaller onedir freeze.
#
# Output: packaging/dist/nh-server/{nh,_internal/,web/dist/}
# The board MUST sit at the bundle root (not under _internal): the server
# resolves it with Path(__file__).resolve().parents[3]/"web"/"dist", and under a
# frozen onedir build __file__ is <bundle>/_internal/no_human/api/app.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/packaging/dist"
BUNDLE="${OUT}/nh-server"

# `git -C "${ROOT}"` does not mean ${ROOT} if the environment says otherwise:
# GIT_DIR overrides it outright (driven: `GIT_DIR=$O/.git git -C R rev-parse
# HEAD` prints O's HEAD). This file asks git two questions that end up INSIDE
# the artefact — the stamped commit and the dirty flag — so both would then
# describe a repository that did not build it. Cleared here, and again at the
# top of the stamp section below, because the tests lift that section out and
# execute it on its own; the duplication is what keeps the shipped ordering and
# the tested ordering the same. Full reasoning, and what is deliberately NOT
# cleared, is in packaging/make-dmg.sh at the head of the verification block.
unset GIT_NAMESPACE GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR \
      GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
      GIT_INDEX_FILE GIT_GRAFT_FILE GIT_SHALLOW_FILE \
      GIT_REPLACE_REF_BASE GIT_NO_REPLACE_OBJECTS GIT_CEILING_DIRECTORIES \
      GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT

# The board is always rebuilt below, so it matches ROOT's SOURCE — but nothing
# checked that ROOT's source was current. On 2026-08-05 a signed, notarized DMG
# was found to contain none of the UI shipped that week: it had been built from
# a checkout sitting 44 commits behind main on an old branch. Every step
# "succeeded"; the artefact was simply built from the wrong commit, and the only
# way to discover it was to mount the DMG and grep the bundle.
#
# This cannot be a soft warning. A release build is exactly the moment nobody
# re-reads the log, and the failure is invisible in the output — it looks like a
# clean build, because it IS a clean build of the wrong tree.
if git -C "${ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "${ROOT}" fetch -q origin main 2>/dev/null || true
  _ref="$(git -C "${ROOT}" rev-parse --verify -q origin/main || git -C "${ROOT}" rev-parse --verify -q main || true)"
  if [ -n "${_ref}" ]; then
    _behind="$(git -C "${ROOT}" rev-list --count HEAD.."${_ref}" 2>/dev/null || echo 0)"
    if [ "${_behind}" -gt 0 ]; then
      echo "FAIL: ${ROOT} is ${_behind} commit(s) behind ${_ref} — this build would" >&2
      echo "      ship stale code. Update the checkout, or set NH_ALLOW_STALE_BUILD=1" >&2
      echo "      to build a deliberate point-in-time artefact." >&2
      echo "      HEAD: $(git -C "${ROOT}" rev-parse --abbrev-ref HEAD) $(git -C "${ROOT}" rev-parse --short HEAD)" >&2
      [ "${NH_ALLOW_STALE_BUILD:-0}" = "1" ] || exit 1
      echo "      (NH_ALLOW_STALE_BUILD=1 — proceeding anyway)" >&2
    fi
  fi
fi

echo "==> building the board (web/dist is gitignored, so always rebuild)"
(cd "${ROOT}/web" && npm run build)

echo "==> freezing the server"
(cd "${ROOT}" && .venv/bin/pyinstaller --noconfirm \
  --distpath "${OUT}" --workpath "${OUT}/.work" \
  packaging/nh-server.spec)

# Runtime data the server locates via Path(__file__).resolve().parents[3], which
# under a frozen onedir build is the bundle root. Both are load-bearing:
# migrations/*.sql builds the schema on first run (without it the server aborts
# with "no such table: tasks"), and web/dist is the board.
echo "==> placing runtime data at the bundle root"
rm -rf "${BUNDLE}/web" "${BUNDLE}/migrations"
mkdir -p "${BUNDLE}/web"
cp -R "${ROOT}/web/dist" "${BUNDLE}/web/dist"
cp -R "${ROOT}/migrations" "${BUNDLE}/migrations"

echo "==> stripping build-machine provenance"
# PyInstaller copies the installed distribution's .dist-info verbatim, and for an
# EDITABLE install pip records the source checkout's ABSOLUTE PATH in
# direct_url.json:
#     {"url":"file:///Users/<user>/git/<employer>/.../no_human","dir_info":{"editable":true}}
# That shipped inside the .app in the Mac DMG — the maintainer's home directory
# and the employer-named parent directory, in an artifact handed to third
# parties. It is exactly what P1 exists to prevent, and no export rule could
# catch it: the DMG is BUILT, not exported, so EXPORT_CLASSIFICATION.txt never
# sees this file. Found by an independent reviewer, not by a test.
#
# ONLY direct_url.json is removed, NOT the .dist-info directory. Deleting the
# whole directory was tried first, on the assumption that a frozen app resolves
# no distributions -- it does: `nh --version` then dies in
# importlib/metadata/__init__.py:397 `from_name`. direct_url.json is the sole
# file carrying the path (verified by grep over the built bundle), and it is
# pip-install provenance that nothing reads at runtime.
find "${BUNDLE}/_internal" -path "*.dist-info/direct_url.json" -print -delete || true

echo "==> verifying"
py_count="$(find "${BUNDLE}" -name '*.py' | wc -l | tr -d ' ')"
if [ "${py_count}" != "0" ]; then
  echo "FAIL: ${py_count} .py files in the bundle (it must ship no source)" >&2
  find "${BUNDLE}" -name '*.py' >&2
  exit 1
fi
test -f "${BUNDLE}/web/dist/index.html" || {
  echo "FAIL: board missing at ${BUNDLE}/web/dist/index.html" >&2; exit 1; }

# ── BUILD STAMP ─────────────────────────────────────────────────────────────
# The environment scrub from the top of this file, repeated: this block is
# lifted out and executed on its own by tests/test_verify_artefact.py, so a
# scrub that lived only above would be shipped-but-untested. `unset` of an
# already-unset name is a no-op, so running it twice costs nothing.
unset GIT_NAMESPACE GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR \
      GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
      GIT_INDEX_FILE GIT_GRAFT_FILE GIT_SHALLOW_FILE \
      GIT_REPLACE_REF_BASE GIT_NO_REPLACE_OBJECTS GIT_CEILING_DIRECTORIES \
      GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
#
# WHY. On 2026-08-05 a signed, notarized DMG was found to contain none of that
# week's UI: it had been built from a checkout 44 commits behind main. Every
# build step reported success, and NOTHING in the artefact recorded which commit
# it came from -- `__version__` is a static "0.1.0" -- so the only way to detect
# it was to mount the DMG and grep its board bundle for feature strings.
#
# A stamp turns "which commit is this?" from an archaeology problem into a file
# read. `scripts/verify_artefact.py` reads it back OUT of the built artefact and
# refuses a distribution step when it does not match the source it claims.
# The guard above stops a stale BUILD; this stamp catches a stale ARTEFACT,
# including one built correctly and then shipped months later.
#
# EVERY VALUE IS COMPUTED, CHECKED, AND ONLY THEN WRITTEN. The first version of
# this block wrote `echo "board_sha256=$(find … | shasum …)"` directly. Under
# `set -euo pipefail` the failure of a command substitution INSIDE an `echo`
# argument does not propagate -- `echo` itself succeeds -- so on a host with no
# `shasum` (the Linux/Windows parity this stamp is meant to cover) the block
# exited 0, the build carried on, and it shipped `board_sha256=` -- an empty
# digest, which the verifier then read as "no digest to check" and passed. An
# artefact that satisfies its own gate regardless of its contents is worse than
# no gate, because it is believed. A value that cannot be computed now ABORTS
# the build; it is never written blank.
#
# `branch` is NOT recorded, deliberately. It was, and it is a leak: a branch
# name is free text a human chose (`fix/<tracker-id>-<customer>-outage`) being
# written into a signed, notarized artefact handed to third parties. The $HOME
# check below would not catch it and no term scan runs over a built bundle. The
# commit sha is an opaque 40-hex identifier and carries the same provenance.
_hashcmd=""
if command -v shasum >/dev/null 2>&1; then _hashcmd="shasum -a 256"
elif command -v sha256sum >/dev/null 2>&1; then _hashcmd="sha256sum"; fi
if [ -z "${_hashcmd}" ]; then
  echo "FAIL: neither shasum nor sha256sum is available, so the board digest" >&2
  echo "      cannot be computed. Refusing to stamp an artefact with an empty" >&2
  echo "      digest: it would pass verify_artefact.py whatever it contains." >&2
  exit 1
fi

_commit="$(git -C "${ROOT}" rev-parse HEAD 2>/dev/null || true)"
if ! printf '%s' "${_commit}" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "FAIL: cannot resolve HEAD of ${ROOT} — the artefact would carry no" >&2
  echo "      provenance at all. Build from a git checkout." >&2
  exit 1
fi

# A dirty tree means the artefact does not correspond to ANY commit. Recorded
# rather than refused: a developer building locally is legitimate, but the
# verifier must be able to tell that apart from a release.
if _status="$(git -C "${ROOT}" status --porcelain 2>/dev/null)"; then
  if [ -n "${_status}" ]; then _dirty="yes"; else _dirty="no"; fi
else
  echo "FAIL: cannot read the working-tree state of ${ROOT}" >&2
  exit 1
fi

# THE PATH IS PART OF THE DIGEST.
#
# The first version was `find … -exec ${_hashcmd} {} \; | awk '{print $1}' |
# sort | ${_hashcmd}`. That `awk` THREW THE FILENAMES AWAY, leaving a sorted
# multiset of content hashes, and scripts/verify_artefact.py did the same with
# `blobs.values()`. So exchanging two files' paths inside the board produced a
# byte-identical digest: a reviewer swapped index.html with an asset — leaving
# the board serving the stale file at its entry point — and the check printed
# `OK — 2 board file(s) matching the stamped digest`, rc=0. Every rename, move
# and swap inside dist/ was invisible to the one check whose job is "are these
# the right bytes, in the right places".
#
# Each file therefore contributes `<sha256 of its bytes>  <path relative to
# dist/>`, and the digest is the hash of those lines. Three details are parity
# constraints with the Python half, not style — if the two ever disagree, every
# real artefact fails its own gate:
#
#  * the per-file hash is taken from STDIN (`${_hashcmd} < file`), so the line
#    is built here rather than by the hasher. shasum and sha256sum print the
#    same `<hash>  <file>` shape for ordinary names but differ on names
#    containing backslashes or newlines, where GNU coreutils switches to an
#    escaped form. Reading stdin sidesteps that entirely: both print `<hash>  -`.
#  * `LC_ALL=C sort` — byte order. With a path in the line the collation is
#    load-bearing; a line of pure hex was immune to it, which is why the old
#    version survived the he_IL.UTF-8 vs LC_ALL=C parity check.
#  * `-type f` excludes symlinks; scripts/verify_artefact.py skips them for the
#    same reason, so the two sides hash the same set.
#
# A per-file hash that does not come out as 64 hex aborts the build from inside
# the loop: `set -o pipefail` carries that failure out through the pipeline, so
# a half-read board can never be silently hashed into a plausible-looking digest.
# shellcheck disable=SC2086  # _hashcmd is a command plus its flags, on purpose
_hash_board() (
  cd "${BUNDLE}/web/dist" || exit 1
  find . -type f -print0 | while IFS= read -r -d '' _f; do
    _h="$(${_hashcmd} < "${_f}" | awk '{print $1}')"
    if ! printf '%s' "${_h}" | grep -Eq '^[0-9a-f]{64}$'; then
      echo "FAIL: could not hash board file ${_f} (got '${_h}')" >&2
      exit 1
    fi
    printf '%s  %s\n' "${_h}" "${_f#./}"
  done | LC_ALL=C sort | ${_hashcmd} | awk '{print $1}'
)
_board_sha256=""
_board_sha256="$(_hash_board)" || _board_sha256=""
if ! printf '%s' "${_board_sha256}" | grep -Eq '^[0-9a-f]{64}$'; then
  echo "FAIL: the board digest came out as '${_board_sha256}', which is not a" >&2
  echo "      sha256. Refusing to write an unusable stamp." >&2
  exit 1
fi

_stamp="${BUNDLE}/BUILD_STAMP"
{
  echo "commit=${_commit}"
  echo "dirty=${_dirty}"
  echo "board_sha256=${_board_sha256}"
} > "${_stamp}"
echo "==> build stamp: $(tr '\n' ' ' < "${_stamp}")"
sql_count="$(find "${BUNDLE}/migrations" -name '*.sql' | wc -l | tr -d ' ')"
test "${sql_count}" -gt 0 || {
  echo "FAIL: no migrations in ${BUNDLE}/migrations" >&2; exit 1; }

# no_human.ci_gate is a post-PR gate wired to one specific CI estate. It is not
# general-purpose and must not ship.
# nh-server.spec excludes it; this is the check that the exclusion took, since
# a new import elsewhere pulls the package back into the freeze silently.
# Searching the bundle for the name cannot do this job: pure modules live in
# the zlib-compressed PYZ, so the string is absent from the bytes while the
# module is inside. PyInstaller's own module table is the readable inventory.
pyz_toc="$(find "${OUT}/.work/nh-server" -name 'PYZ-*.toc' | head -1)"
test -n "${pyz_toc}" || {
  echo "FAIL: no PYZ table under ${OUT}/.work/nh-server - cannot read what was frozen" >&2
  exit 1; }
gate_modules="$(grep -o 'no_human\.ci_gate[A-Za-z0-9_.]*' "${pyz_toc}" | sort -u || true)"
if [ -n "${gate_modules}" ]; then
  echo "FAIL: no_human.ci_gate is frozen into the bundle (D1: it must not ship)" >&2
  echo "${gate_modules}" >&2
  exit 1
fi
# Same check for the private half of the term inventory: hex-encoded
# terms plus their replacement mapping. Never distributable in any artifact.
private_terms="$(grep -o 'no_human\.eval\._vendor_terms_private' "${pyz_toc}" | sort -u || true)"
if [ -n "${private_terms}" ]; then
  echo "FAIL: no_human.eval._vendor_terms_private is frozen into the bundle (it must never ship)" >&2
  exit 1
fi

# No absolute build path may survive anywhere in the bundle. This is the check
# the DMG never had: the identity guard in the test suite SKIPS whenever
# desktop/dist is absent, which is every clean clone and every CI run, so it was
# dark exactly when it mattered. Checked here, where the artifact is made.
# -a, and the bare path as well as the file:// form. The first version of this
# gate used `grep -rl "file://${HOME}"` and a known-positive control exposed two
# holes in it: grep SKIPS BINARY FILES without -a, so a path baked into the
# frozen executable or a .so was invisible -- and PyInstaller is exactly the kind
# of tool that bakes build paths into binaries. The file:// prefix is also only
# how pip happens to write it; RECORD-style or compiled-in paths are bare.
# Verified with planted controls in both forms before being trusted.
leaked="$(grep -ral -e "file://${HOME}" -e "${HOME}/" "${BUNDLE}" 2>/dev/null || true)"
if [ -n "${leaked}" ]; then
  echo "FAIL: the bundle records this machine's build path (P1: no maintainer trace ships)" >&2
  echo "${leaked}" >&2
  exit 1
fi

echo "OK: ${BUNDLE} ($(du -sh "${BUNDLE}" | cut -f1)), 0 .py files, no ci_gate"
