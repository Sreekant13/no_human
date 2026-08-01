#!/usr/bin/env python3
"""Verify the repository tree against RELEASE_MANIFEST.txt.

The manifest pins every tracked file's content with a SHA-256, like a lockfile
for the tree itself: a review of any change is a review of a visible manifest
row. This script checks, in both directions, that the checkout and the manifest
agree:

  * every tracked file is listed, with a matching hash;
  * every listed file exists in the tree, with a matching hash;
  * the manifest itself is the one file that carries no row (it cannot pin its
    own content).

`--write` regenerates the manifest from the current tree — run it after adding,
removing or editing files, and commit the result with the change it reflects.

Two problem classes, two exit codes, because they mean different things:

  * a **hash mismatch** (or a row pointing at a file that is gone) says the tree
    is not the tree the manifest was reviewed against. Always exit 1.
  * a **tracked file with no row** says the manifest is incomplete. On the
    released tree that is a defect; in a working repository it is the normal
    state — this repo carries ~93 of them on purpose, because `--write` would
    otherwise sweep private files into a RELEASE inventory.

Collapsing the two made the exit code carry no signal here: the script was
already 1 from the unlisted paths, so a genuinely bad merge resolution — a real
hash mismatch — changed nothing an automated caller could see, and only reading
the body revealed it. Unlisted paths are therefore WARNINGS by default and do
not set the exit code; `--strict` restores the old behaviour and is what CI runs
on the released tree, where completeness is a hard invariant.

Standard library + `git` only. Exit 0 when the tree matches, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

MANIFEST_NAME = "RELEASE_MANIFEST.txt"

HEADER = """\
# RELEASE_MANIFEST.txt — the release file inventory.
#
# One row per file: `<sha256>  <path>`. CI verifies a checkout against this
# file with scripts/check_release_manifest.py, so every content change is
# visible here, like a lockfile. Rows are sorted by path.
"""


def repo_root() -> Path:
    return Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent, text=True).strip())


def tracked_files(root: Path) -> list[str]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=root, text=True)
    return sorted(p for p in out.split("\0") if p)


def hash_path(root: Path, rel: str) -> str:
    """SHA-256 of the file's bytes; for a symlink, of its target string."""
    p = root / rel
    if p.is_symlink():
        return hashlib.sha256(os.readlink(p).encode("utf-8")).hexdigest()
    return hashlib.sha256(p.read_bytes()).hexdigest()


def parse_manifest(text: str) -> dict[str, str]:
    """``{path: sha256}``. A row that cannot be parsed is an error, not a skip."""
    out: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.rstrip("\n")
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        digest, sep, path = s.partition("  ")
        if not sep or not path or len(digest) != 64 or any(
                c not in "0123456789abcdef" for c in digest):
            raise SystemExit(
                f"{MANIFEST_NAME}:{lineno}: cannot parse {line!r} — every row "
                "is `<sha256>  <path>`")
        if path in out:
            raise SystemExit(f"{MANIFEST_NAME}:{lineno}: {path} is listed twice")
        out[path] = digest
    return out


def write_manifest(root: Path) -> int:
    rows = {rel: hash_path(root, rel) for rel in tracked_files(root)
            if rel != MANIFEST_NAME}
    body = "".join(f"{digest}  {rel}\n" for rel, digest in sorted(rows.items()))
    (root / MANIFEST_NAME).write_text(HEADER + body, encoding="utf-8")
    print(f"{MANIFEST_NAME}: wrote {len(rows)} row(s)")
    return 0


def check_manifest(root: Path, *, strict: bool = False) -> int:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        print(f"FAIL: {MANIFEST_NAME} not found at the repository root",
              file=sys.stderr)
        return 1
    listed = parse_manifest(manifest_path.read_text(encoding="utf-8"))
    tracked = tracked_files(root)

    # `problems` fail the run; `unlisted` only fails it under --strict. Kept as
    # two lists rather than one tagged list so that no future edit can append a
    # real mismatch to the warning bucket by accident.
    problems: list[str] = []
    unlisted: list[str] = []
    if MANIFEST_NAME in listed:
        problems.append(f"{MANIFEST_NAME} lists itself; it cannot pin its own "
                        "content")
    tracked_set = set(tracked) - {MANIFEST_NAME}
    listed_set = set(listed) - {MANIFEST_NAME}

    for rel in sorted(tracked_set - listed_set):
        unlisted.append(f"{rel}: tracked but not listed — run "
                        f"`python scripts/check_release_manifest.py --write`")
    for rel in sorted(listed_set - tracked_set):
        problems.append(f"{rel}: listed but not in the tree")
    for rel in sorted(listed_set & tracked_set):
        actual = hash_path(root, rel)
        if actual != listed[rel]:
            problems.append(
                f"{rel}: content differs from the manifest "
                f"(listed {listed[rel][:12]}…, actual {actual[:12]}…)")

    if unlisted:
        label = "FAIL" if strict else "WARN"
        print(f"{label}: {len(unlisted)} tracked file(s) not listed in "
              f"{MANIFEST_NAME}:", file=sys.stderr)
        for u in unlisted:
            print(f"  {u}", file=sys.stderr)
        if not strict:
            print("  (warning only — re-run with --strict to fail on these; "
                  "the exit code below reflects hash mismatches only)",
                  file=sys.stderr)

    if problems:
        print(f"FAIL: the tree does not match {MANIFEST_NAME}:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    if unlisted and strict:
        return 1
    matched = len(listed_set & tracked_set)
    suffix = f" ({len(unlisted)} tracked file(s) unlisted)" if unlisted else ""
    print(f"OK: {matched} file(s) match {MANIFEST_NAME}{suffix}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_release_manifest.py",
        description="Verify (or regenerate) the release file inventory.")
    ap.add_argument("--write", action="store_true",
                    help="regenerate the manifest from the current tree")
    ap.add_argument("--root", type=Path, default=None,
                    help="repository root (default: the repo this script is in)")
    ap.add_argument("--strict", action="store_true",
                    help="also fail on tracked files with no manifest row "
                         "(the released tree's invariant; a working repository "
                         "legitimately carries these)")
    args = ap.parse_args(argv)
    root = (args.root or repo_root()).resolve()
    if args.write:
        return write_manifest(root)
    return check_manifest(root, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
