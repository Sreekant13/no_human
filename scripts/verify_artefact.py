#!/usr/bin/env python3
"""Open a built artefact and check what is INSIDE it, not that it built.

WHY THIS EXISTS
---------------
On 2026-08-05 a signed, notarized DMG was found to contain none of that week's
UI. It had been built from a checkout 44 commits behind main. Every step of the
pipeline reported success, because every step was successful — it was a clean
build of the wrong tree. `packaging/build-installer.sh` already asserted the
board file EXISTED; nothing asserted it was the CURRENT board.

The lesson generalises past this bug: a build pipeline can only tell you it did
what it was told. Only opening the artefact tells you what it contains. So this
script never inspects the repo to decide whether a build is good — it reads the
artefact's own bytes and compares them to a commit you name.

It is deliberately platform-neutral: it takes a DIRECTORY (a mounted .dmg, an
unpacked .app, an extracted NSIS payload, a Windows resources tree). Mounting
and unpacking are the caller's job, so the same check covers macOS and Windows.

FAIL CLOSED. EVERY TIME.
------------------------
The first version of this file failed OPEN in three places, and an independent
reviewer demonstrated all three end to end against a board reading "THIS IS THE
WRONG BOARD - 44 commits stale":

  * `if claimed and claimed != actual` — an ABSENT or EMPTY `board_sha256`
    skipped the content check entirely and printed OK. `build-installer.sh`
    produced exactly that stamp on any host without `shasum`, because the
    failure of a command substitution inside `echo` does not propagate under
    `set -euo pipefail`.
  * `not expected.startswith(got[:12])` with an empty `commit` — `"x".startswith("")`
    is True, so an empty commit matched every expectation.
  * `if expected and …` with `--expect-commit ""` — the natural CI line
    `--expect-commit "$(git rev-parse HEAD)"` degraded to "no check at all" the
    moment that substitution failed.

The rule this file now follows without exception: **a field this verifier reads
and does not find, or finds empty, or finds malformed, is not "nothing to
check" — it is provenance that cannot be established, and that is a FAILURE.**
A gate whose degraded state is "pass" is not a gate. The same rule applies to
its arguments: an empty expectation is a usage error, never a skipped check.

EXIT CODES — A WEAKENED CHECK IS NOT A PASS
-------------------------------------------
A second reviewer showed that `--allow-unknown-commit` and `--allow-dirty` both
returned 0 while printing their caveats to stderr, against a board reading
"THIS IS THE WRONG BOARD - 44 commits stale". A caller that reads `$?` — which
is every caller that is a script, including `packaging/make-dmg.sh` — could not
tell that apart from a fully verified artefact. Prose in stderr is not a signal
a pipeline can act on. So:

    0  verified: the artefact's commit was compared against a named commit and
       matched, and the tree it was built from was clean.
    1  FAILED.
    2  usage error.
    3  OK, but PROVENANCE NOT VERIFIED — every check that could run passed, and
       at least one check was deliberately weakened by a flag. `make-dmg.sh`
       accepts 3 for an unsigned build and refuses it for a signed one.

A WEAKENED CHECK IS NOT ONLY A FLAG — IT CAN BE THE QUESTION ITSELF
-------------------------------------------------------------------
A third reviewer showed that the caveat above still missed the case that caused
the incident. `--allow-dirty` downgrades only when the stamp says `dirty=yes`;
the stale checkout was CLEAN. With `--repo` pointing at the checkout that built
the artefact — which is what `make-dmg.sh` does for every unsigned build — the
default comparison is `stamp.commit == HEAD of the checkout that wrote
stamp.commit`. That is a tautology, and it printed `verify-artefact: OK — built
from 0ca24000e3ac, clean tree` with rc=0 over a board reading "THIS IS THE WRONG
BOARD".

This file cannot detect that on its own: a CONSUMER holding their own clone runs
the identical command line and is doing a genuine check. Only the caller knows
which it is, so the caller says: `--repo-built-this-artefact` declares the
comparison self-referential, and a self-referential comparison exits 3, never 0.
It is still RUN — a stamp naming a commit this checkout never built is a
FAILURE, because that is the one thing the comparison genuinely establishes.

WHAT THE DIGEST COVERS
----------------------
`board_sha256` hashes `<sha256 of the file's bytes>  <path relative to dist/>`
lines, not bare content hashes. The first version hashed a sorted multiset of
content hashes with the filenames stripped, which meant SWAPPING two files'
paths inside the board left the digest identical — the reviewer demonstrated a
board whose index.html and assets file had been exchanged passing with rc=0.
Any rename, move or swap inside `dist/` was invisible. The shell half in
`build-installer.sh` builds byte-identical lines and sorts them under `LC_ALL=C`
so the two sides agree; if they ever disagree, every real artefact fails its own
check.

Parity is pinned as a TRIANGLE, not as a direct comparison, and the earlier
version of this paragraph got that wrong. It named
`test_the_shell_digest_equals_the_python_one` as pinning shell-against-Python.
That test asserts the SHELL output against the test module's own independent
`_digest()` reference — never against `board_digest` below — because a test that
recomputes its expectation from the code under test proves nothing. The Python
side is pinned against that same reference by the artefact tests. So the two
implementations are tied to a common third statement of the format rather than
to each other, and a change to either one breaks its own leg of the triangle.
The distinction is not academic: a review demonstrated it by stripping the path
back out of the production digest line, and that test plus all four
hostile-tree variants (spaces, dotfiles, subdirectories, non-ASCII names, an
empty file, duplicate contents, under both hashers and both collations) stayed
green while the artefact tests went red.

WHAT THE STAMP MAY CARRY
------------------------
The stamp is a new channel INTO a signed, notarized, third-party-distributed
artefact, so its contents are a disclosure decision and not a convenience. The
first version wrote `branch=`, which put a live branch name — the reviewer's
worked example named a customer and their ticket — into a file handed to
strangers. `build-installer.sh`'s only content guard greps for `$HOME` and does
not catch it, and no term scan runs over a built bundle. The commit SHA is an
opaque 40-hex identifier and is fine; a branch name is free text a human chose,
and free text authored by a human is the thing that leaks. `branch` is GONE.

`STAMP_FIELDS` is therefore a closed set, and an unrecognised key FAILS: that is
what stops a future convenience field from quietly becoming the next channel.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

# A closed set. Adding to it is a disclosure decision — see the module docstring.
STAMP_FIELDS = ("commit", "dirty", "board_sha256")

RC_OK = 0
RC_FAILED = 1
RC_USAGE = 2
RC_UNVERIFIED = 3  # every check that ran passed; at least one was weakened

_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_EXPECT_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")


def _read_stamp(bundle: Path) -> tuple[dict[str, str], list[str]]:
    """Return (fields, problems). Any doubt about the stamp is a problem."""
    try:
        found = sorted(bundle.rglob("BUILD_STAMP"))
    except OSError as exc:  # an unreadable directory on the way down
        return {}, [f"cannot search the artefact for BUILD_STAMP: {exc}"]

    if not found:
        return {}, [
            "no BUILD_STAMP in the artefact — it was built before stamping "
            "existed, or by something other than packaging/build-installer.sh. "
            "Its provenance CANNOT be established; rebuild rather than trust it."
        ]
    if len(found) > 1:
        listed = ", ".join(str(p.relative_to(bundle)) for p in found)
        return {}, [
            f"{len(found)} BUILD_STAMP files in the artefact ({listed}) — which "
            "one describes it is ambiguous, so none of them is provenance"
        ]

    stamp = found[0]
    if not stamp.is_file() or stamp.is_symlink():
        return {}, [
            f"BUILD_STAMP at {stamp} is not a regular file — provenance cannot "
            "be read from it"
        ]
    try:
        text = stamp.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, [f"BUILD_STAMP at {stamp} cannot be read: {exc}"]

    fields: dict[str, str] = {}
    problems: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f"BUILD_STAMP has a line that is not key=value: {line!r}")
            continue
        k, _, v = line.partition("=")
        fields[k.strip()] = v.strip()

    unknown = sorted(set(fields) - set(STAMP_FIELDS))
    if unknown:
        problems.append(
            f"BUILD_STAMP carries field(s) this verifier does not know: "
            f"{', '.join(unknown)}. The stamp ships to third parties, so an "
            "unrecognised field is unreviewed content in a signed artefact")

    for key in STAMP_FIELDS:
        if key not in fields:
            problems.append(
                f"BUILD_STAMP has no {key!r} field — provenance is broken. This "
                "is NOT 'nothing to check': the build that wrote this stamp "
                "could not record it, so nothing here can be trusted")
        elif not fields[key]:
            problems.append(
                f"BUILD_STAMP's {key!r} is EMPTY — the build could not compute "
                "it and wrote a blank rather than failing. Provenance is broken")

    if fields.get("commit") and not _COMMIT_RE.match(fields["commit"]):
        problems.append(
            f"BUILD_STAMP's 'commit' is not a 40-hex sha: {fields['commit']!r}")
    if fields.get("board_sha256") and not _SHA256_RE.match(fields["board_sha256"]):
        problems.append(
            "BUILD_STAMP's 'board_sha256' is not a 64-hex digest: "
            f"{fields['board_sha256']!r}")
    if fields.get("dirty") and fields["dirty"] not in ("yes", "no"):
        problems.append(
            f"BUILD_STAMP's 'dirty' is neither 'yes' nor 'no': {fields['dirty']!r}")

    return fields, problems


def _board_root(bundle: Path) -> tuple[Path | None, list[str]]:
    """The one bundled board directory, or a problem saying why there isn't one.

    The first version took the FIRST `dist/` containing an index.html that
    `rglob` happened to yield. `rglob` walks in directory order, so a decoy at
    `AAA/dist` sorted ahead of the real board and won. There is exactly one
    board in a well-formed artefact; more than one is ambiguous and ambiguity
    is a failure, not a coin toss.
    """
    try:
        roots = sorted(
            p for p in bundle.rglob("dist")
            if p.is_dir() and not p.is_symlink() and (p / "index.html").is_file()
        )
    except OSError as exc:
        return None, [f"cannot search the artefact for a bundled board: {exc}"]

    if not roots:
        return None, [
            "no bundled board found (looked for a dist/ with index.html "
            "anywhere under the artefact)"]
    if len(roots) > 1:
        listed = ", ".join(str(p.relative_to(bundle)) for p in roots)
        return None, [
            f"{len(roots)} candidate board directories ({listed}) — which one "
            "the stamp describes is ambiguous, so the digest proves nothing"]
    return roots[0], []


def _board_files(root: Path) -> tuple[list[Path], list[str]]:
    """Regular files under the board, matching the shell's `find -type f`.

    The stamp's digest comes from `find … -type f`, which EXCLUDES symlinks;
    `Path.is_file()` FOLLOWS them, so the two disagreed the moment a symlink
    appeared and the Python digest changed while the shell one did not. Symlinks
    are skipped here so the two sides compute the same thing — and their presence
    is reported, because a skipped file is an unverified file.
    """
    files: list[Path] = []
    links: list[Path] = []
    try:
        for p in sorted(root.rglob("*")):
            if p.is_symlink():
                links.append(p)
            elif p.is_file():
                files.append(p)
    except OSError as exc:
        return [], [f"cannot list the bundled board: {exc}"]

    problems: list[str] = []
    if links:
        listed = ", ".join(str(p.relative_to(root)) for p in links[:5])
        problems.append(
            f"the bundled board contains {len(links)} symlink(s) ({listed}) — "
            "the stamp's digest is computed with `find -type f`, which skips "
            "them, so their contents ship UNVERIFIED")
    return files, problems


def _read_files(files: list[Path]) -> tuple[dict[Path, bytes], list[str]]:
    """Read every board file once. An unreadable file is a failure, not a crash."""
    blobs: dict[Path, bytes] = {}
    problems: list[str] = []
    for p in files:
        try:
            blobs[p] = p.read_bytes()
        except OSError as exc:
            problems.append(f"cannot read bundled board file {p}: {exc}")
    return blobs, problems


def board_digest(root: Path, blobs: dict[Path, bytes]) -> str:
    """sha256 over `<sha256 of the bytes>  <path relative to dist/>` lines.

    THE PATH IS PART OF THE DIGEST, and that is the whole point. The first
    version hashed `sorted(sha256(b) for b in blobs.values())` — a multiset of
    content hashes with the names thrown away — and its shell twin did the same
    with `awk '{print $1}'`. Exchanging two files' paths inside the board
    therefore produced an IDENTICAL digest: a reviewer swapped index.html with
    an asset, leaving the board serving the stale file at the entry point, and
    the verifier printed OK. Every rename, move and swap inside `dist/` was
    invisible to a check whose entire job is "are these the right bytes".

    The line format is fixed by parity with `build-installer.sh`, which builds
    the same bytes from `find -type f` and sorts them under `LC_ALL=C`:

      * two spaces between digest and path — what both `shasum -a 256` and
        `sha256sum` print, so the shape stays familiar in a debug dump;
      * paths relative to the board root, POSIX separators, no `./` prefix;
      * sorted as BYTES, not as text. `LC_ALL=C sort` orders by byte value, and
        a UTF-8 locale's collation does not — with the path now inside the line,
        the collation is load-bearing, where a line of pure hex was immune to
        it. `os.fsencode` + sorting `bytes` is the exact Python equivalent.
      * a trailing newline after the last line, because the shell pipes
        newline-terminated lines into the hasher.
    """
    lines = sorted(
        hashlib.sha256(blobs[p]).hexdigest().encode("ascii") + b"  "
        + os.fsencode(p.relative_to(root).as_posix())
        for p in sorted(blobs)
    )
    return hashlib.sha256(b"\n".join(lines) + b"\n").hexdigest()


# `git -C <repo>` does not mean <repo> if the environment says otherwise: GIT_DIR
# overrides it outright, and GIT_ALTERNATE_OBJECT_DIRECTORIES makes another
# repository's commits resolve inside it — both driven. make-dmg.sh clears these
# before invoking this script, so the release path is already covered; this
# repeats it because verify_artefact.py is also a standalone tool (CI, and an
# operator checking a downloaded DMG), and there its caller's environment is
# whatever it happens to be. The same list, and what is deliberately NOT cleared,
# is documented at the head of make-dmg.sh's verification block.
_GIT_ENV_REDIRECTS = (
    "GIT_NAMESPACE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_INDEX_FILE", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS", "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_REDIRECTS}
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify_artefact.py",
        description="Assert a BUILT artefact's contents match the commit it claims.")
    ap.add_argument("bundle", type=Path,
                    help="a mounted/unpacked artefact directory")
    ap.add_argument("--expect-commit", default=None,
                    help="the sha the artefact must have been built from "
                         "(default: HEAD of --repo). May be abbreviated to 7+ "
                         "hex characters. An EMPTY value is a usage error, not "
                         "a skipped check.")
    ap.add_argument("--repo", type=Path, default=Path("."),
                    help="source repo the expectation comes from")
    ap.add_argument("--allow-unknown-commit", action="store_true",
                    help="permit --repo not to yield the artefact's commit — "
                         "a published export has its own fresh history, and a "
                         "release tarball has no history at all. The commit "
                         "check is then SKIPPED, said so loudly, and the exit "
                         "code is 3, never 0.")
    ap.add_argument("--require", action="append", default=[], metavar="STR",
                    help="a string the bundled board MUST contain "
                         "(repeatable) — e.g. a feature shipped this week")
    ap.add_argument("--forbid", action="append", default=[], metavar="STR",
                    help="a string the bundled board must NOT contain "
                         "(repeatable) — e.g. a feature removed this week")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="accept an artefact built from a dirty tree. A dirty "
                         "tree corresponds to NO commit, so this too downgrades "
                         "the verdict to 'provenance NOT verified' and exits 3.")
    ap.add_argument("--repo-built-this-artefact", action="store_true",
                    help="declare that --repo IS the checkout this artefact was "
                         "built from. The HEAD comparison is then a TAUTOLOGY — "
                         "it can only catch tampering after the build, never "
                         "which source the build used — so the verdict is "
                         "downgraded to 'provenance NOT verified' and exits 3. "
                         "Contradicts --expect-commit.")
    args = ap.parse_args(argv)

    # Two ways of saying where the expectation comes from, and they disagree:
    # one says "from the build's own checkout, so it proves nothing about the
    # source", the other names an external source. Silently honouring either one
    # would make the OTHER's guarantee a lie. Ambiguity is a usage error.
    if args.repo_built_this_artefact and args.expect_commit is not None:
        print("verify-artefact: usage error\n  --repo-built-this-artefact and "
              "--expect-commit contradict each other: the first says the "
              "expectation comes from the checkout that BUILT this artefact "
              "(which proves nothing about which source that was), the second "
              "names an external source to hold it against. Pass one.",
              file=sys.stderr)
        return RC_USAGE

    # An empty expectation must be an ERROR, not a skip. `--expect-commit
    # "$(git rev-parse HEAD)"` is the natural CI line, and the whole check used
    # to evaporate the moment that substitution came back empty.
    if args.expect_commit is not None and not _EXPECT_RE.match(args.expect_commit):
        what = ("EMPTY" if not args.expect_commit.strip()
                else repr(args.expect_commit))
        print(f"verify-artefact: usage error\n  --expect-commit is {what} — it "
              "must be 7 to 40 hex characters. An unusable expectation is an "
              "error, never a skipped check.", file=sys.stderr)
        return RC_USAGE

    bundle: Path = args.bundle
    if not bundle.is_dir():
        print(f"verify-artefact: FAILED\n  not a directory: {bundle}",
              file=sys.stderr)
        return RC_FAILED

    problems: list[str] = []
    notices: list[str] = []

    root, board_problems = _board_root(bundle)
    problems.extend(board_problems)
    files: list[Path] = []
    if root is not None:
        files, list_problems = _board_files(root)
        problems.extend(list_problems)
    blobs, read_problems = _read_files(files)
    problems.extend(read_problems)

    stamp, stamp_problems = _read_stamp(bundle)
    problems.extend(stamp_problems)

    # ---- the commit the artefact claims ------------------------------------
    got = stamp.get("commit", "")
    provenance_checked = False
    if not got:
        pass  # already a problem from _read_stamp; nothing to compare against
    elif args.expect_commit is not None:
        expected = args.expect_commit.lower()
        if not got.startswith(expected):
            problems.append(
                f"artefact was built from {got[:12]}, expected {expected[:12]} "
                "— this is the stale-artefact failure; rebuild it")
        else:
            provenance_checked = True
    else:
        head = _git(args.repo, "rev-parse", "HEAD")
        if head.returncode != 0:
            # A consumer who downloaded a release ZIP or tarball has no `.git`
            # at all, so this branch — not the cat-file one below — is the one
            # they land on. The first version reached for `git rev-parse HEAD`
            # before ever looking at --allow-unknown-commit, so the documented
            # escape hatch was unreachable for them: `rc=1  cannot resolve the
            # expected commit from --repo .: fatal: not a git repository`, with
            # nothing they could do about it. Absent history is exactly the
            # condition the flag exists to acknowledge.
            msg = (f"cannot resolve the expected commit from --repo "
                   f"{args.repo}: "
                   f"{head.stderr.strip() or 'git rev-parse HEAD failed'}")
            if args.allow_unknown_commit:
                notices.append(
                    msg + " — so there is no source here to compare the "
                    "artefact against (an unpacked release tarball has no "
                    "history)")
            else:
                problems.append(
                    msg + ". Pass --allow-unknown-commit to check only the "
                    "artefact's INTERNAL integrity, or --expect-commit <sha>")
        elif got == head.stdout.strip():
            if args.repo_built_this_artefact:
                # THE CLEAN-TREE HOLE. `make-dmg.sh` runs every UNSIGNED build
                # through here with --repo pointing at the checkout that just
                # built the DMG, so this branch is reached by construction: the
                # stamp says HEAD because HEAD is where the stamp came from.
                # A clone 45 commits behind main satisfies it exactly as well as
                # the tip does, and a reviewer walked a board reading "THIS IS
                # THE WRONG BOARD" through it — clean tree, rc=0, flat `OK`.
                # --allow-dirty did not save it: that only downgrades when
                # `dirty=yes`, and the incident's checkout was CLEAN.
                #
                # So the caller declares the comparison self-referential and it
                # can no longer return "verified". What it still proves is real,
                # and is SMALLER than three copies of this sentence used to say.
                #
                # They said "the DMG was not edited between being built and
                # being packaged", full stop. The seventh review drove the
                # counterexample: `board_sha256` is computed over the located
                # `dist/` directory and NOTHING ELSE (see board_digest and
                # _board_root below), so an `_internal/evil.so` planted beside
                # the frozen server, a `migrations/003_backdoor.sql` and a
                # replaced `app.asar` all verify at a flat `OK`, rc=0. The
                # frozen server's own code, the migrations, the Electron code
                # and the app bundle's metadata are all OUTSIDE this digest.
                # What is inside it is the BOARD.
                notices.append(
                    f"the expected commit came from --repo {args.repo}, which "
                    "the caller declared IS the checkout that built this "
                    "artefact — so this comparison is a tautology. It proves "
                    "the BOARD was not modified after it was built (the stamp's "
                    "digest covers the bundled board and nothing else — not the "
                    "frozen server's code, not migrations, not the Electron "
                    "app); it says NOTHING about WHICH SOURCE that checkout was "
                    "on, and a checkout 45 commits behind its remote matches "
                    "itself perfectly. Pass --expect-commit <sha> resolved from "
                    "the release remote to actually establish provenance")
            else:
                provenance_checked = True
        elif _git(args.repo, "cat-file", "-e", f"{got}^{{commit}}").returncode != 0:
            # The artefact's commit is not an object in this repo AT ALL, so
            # this is not a stale artefact — it is the wrong repository to ask.
            # `scripts/build_public_export.py` gives the published repo a fresh
            # `git init` with ONE commit, so a public consumer running the
            # default `--repo .` lands here every time, and telling them
            # "rebuild it" would be a guaranteed false alarm with no value they
            # could pass to --expect-commit instead.
            msg = (f"the artefact's commit {got[:12]} is not an object in "
                   f"--repo {args.repo} — this is NOT the stale-artefact "
                   "failure, you are checking against a different repository "
                   "(a published export has its own fresh history)")
            if args.allow_unknown_commit:
                notices.append(msg)
            else:
                problems.append(
                    msg + ". Pass --allow-unknown-commit to check only the "
                    "artefact's INTERNAL integrity, or --expect-commit <sha>")
        else:
            problems.append(
                f"artefact was built from {got[:12]}, expected "
                f"{head.stdout.strip()[:12]} — this is the stale-artefact "
                "failure; rebuild it")

    if stamp.get("dirty") == "yes":
        if not args.allow_dirty:
            problems.append(
                "built from a DIRTY tree, so it corresponds to no commit "
                "(pass --allow-dirty for a local build)")
        else:
            # `make-dmg.sh` passes --allow-dirty for EVERY unsigned build, and
            # the comment there says why the check runs at all: "a
            # friend-shareable DMG can be just as stale". So this is the path
            # that most needs the caveat, and it was the one printing a flat
            # `OK` with rc=0 — a stale board plus dirty=yes plus a
            # self-consistent digest read exactly like a verified release.
            # `dirty=yes` is a signal the stamp went to the trouble of
            # recording; discarding it here is what made it worthless.
            notices.append(
                "built from a DIRTY tree, so the commit in its stamp does NOT "
                "describe these bytes — --allow-dirty was given, so this was "
                "accepted, but no source has been matched to this artefact")

    # ---- the board's own bytes, against the digest the build recorded ------
    claimed = stamp.get("board_sha256", "")
    if claimed and root is not None and not read_problems:
        actual = board_digest(root, blobs)
        if claimed != actual:
            problems.append(
                "the bundled board does not match its own stamp "
                f"(stamp {claimed[:12]}, actual {actual[:12]}) — the "
                "artefact was modified after it was built")

    blob = b"".join(blobs[p] for p in files if p in blobs)
    for needle in args.require:
        if needle.encode() not in blob:
            problems.append(f"bundled board is MISSING required string: {needle!r}")
    for needle in args.forbid:
        if needle.encode() in blob:
            problems.append(f"bundled board CONTAINS forbidden string: {needle!r}")

    if problems:
        print("verify-artefact: FAILED", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return RC_FAILED

    verified = provenance_checked and not notices
    if notices:
        print("verify-artefact: PROVENANCE NOT VERIFIED", file=sys.stderr)
        for n in notices:
            print(f"  {n}", file=sys.stderr)
        print("  The provenance check was SKIPPED or weakened. What follows is "
              "the artefact's INTERNAL integrity only: it says the board "
              "matches the digest this artefact stamped into itself, NOT that "
              "it was built from any particular source. Exit code 3, not 0, so "
              "a caller reading $? can tell this apart from a verified build.",
              file=sys.stderr)

    verdict = "OK" if verified else "OK (provenance NOT verified)"
    print(f"verify-artefact: {verdict} — built from {got[:12]}, "
          f"{'dirty' if stamp.get('dirty') == 'yes' else 'clean'} tree, "
          f"{len(files)} board file(s) matching the stamped digest, "
          f"{len(args.require)} required and {len(args.forbid)} forbidden "
          "string(s) checked")
    return RC_OK if verified else RC_UNVERIFIED


if __name__ == "__main__":
    raise SystemExit(main())
