"""Materialise each case's base file content into ``cases/<id>/base/``.

Run this ONCE per new case, from a checkout that still has the history
``base.ref`` points at::

    python eval/reviewer_recall/materialize_base.py            # all cases
    python eval/reviewer_recall/materialize_base.py <case-id>  # one case

Why this exists: ``prepare_case_repo`` used to rebuild each case with
``git archive <base.ref>``, which pins the corpus to this repo's 897-commit
history. no_human ships as a fresh ``git init`` with a single commit, so every
case would ERROR at ``git archive`` in the published repo. Materialising the
base content into the case directory makes the corpus self-contained: the
runner never touches repo history again, and ``base.ref`` survives only as
provenance.

Only the files a case's ``change.diff`` actually touches are materialised — a
diff cannot apply to files it does not name, and ``prepare_case_repo``'s scratch
repo is only ever read for citation verification (the reviewer runs single-turn
with no tools when the runner passes ``diff_override``, so it never explores the
tree). Files the diff CREATES (``--- /dev/null``) have no base blob and are
skipped.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases"
REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_DIR_NAME = "base"
MANIFEST_NAME = "base.manifest"


def blob_sha1(data: bytes) -> str:
    """git's blob object id for ``data``, computed without git.

    The manifest records these so the byte-identity pin on ``base/`` keeps
    running after the export, when ``base.ref`` resolves nothing and
    ``git cat-file`` can no longer be the reference. Computed here rather than
    shelled out so the test that checks it shares no code with the extractor.
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def diff_base_paths(diff_text: str) -> list[str]:
    """Repo-relative paths the diff modifies and therefore needs at base.

    Skips files the diff creates: their pre-image is ``/dev/null``, so there is
    no base blob to materialise and ``git apply`` must find them absent.
    """
    paths: list[str] = []
    pre: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            pre = None
        elif line.startswith("--- "):
            pre = line[4:].strip()
        elif line.startswith("+++ "):
            post = line[4:].strip()
            if pre == "/dev/null" or post == "/dev/null":
                continue
            if post.startswith("b/"):
                post = post[2:]
            if post not in paths:
                paths.append(post)
    return paths


def materialise(case_dir: Path, repo_root: Path = REPO_ROOT) -> list[str]:
    """Extract the case's base blobs and write ``base.manifest`` beside them.

    The manifest is ``<git blob sha1>  <repo-relative path>`` per line, sorted.
    It is the provenance record: each SHA is the object id of the blob at
    ``base.ref``, so it pins the materialised bytes to the commit the case was
    cut from even in a repo that no longer has that commit.
    """
    base_ref = (case_dir / "base.ref").read_text().strip()
    diff_text = (case_dir / "change.diff").read_text()
    out_root = case_dir / BASE_DIR_NAME
    written: list[str] = []
    manifest: list[str] = []
    for rel in diff_base_paths(diff_text):
        blob = subprocess.run(
            ["git", "cat-file", "blob", f"{base_ref}:{rel}"],
            cwd=repo_root, check=True, capture_output=True,
        ).stdout
        dest = out_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        written.append(rel)
        manifest.append(f"{blob_sha1(blob)}  {rel}")
    (case_dir / MANIFEST_NAME).write_text("\n".join(sorted(manifest)) + "\n")
    return written


def main(argv: list[str]) -> int:
    wanted = set(argv[1:])
    total = 0
    for case_dir in sorted(CASES_DIR.iterdir()):
        if not case_dir.is_dir():
            continue
        if wanted and case_dir.name not in wanted:
            continue
        written = materialise(case_dir)
        total += len(written)
        print(f"{case_dir.name}: {len(written)} file(s)")
    print(f"total: {total} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
