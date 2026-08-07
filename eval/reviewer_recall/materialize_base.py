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

DE-IDENTIFICATION. A base blob comes from a PRE-SWEEP commit, so it can carry
terms that the tree-wide de-identification sweep has since removed from live
source — and ``eval/reviewer_recall/`` is published, so those are a public leak.
Every blob is therefore passed through ``scrub()`` on the way to disk, using the
substitution list in the private supplement (absent in the export, where this
script cannot run anyway because it needs history — those are two separate
refusals, :class:`HistoryUnavailable` and :class:`ScrubRulesUnavailable`, and
the history is probed first so neither is reported as the other). With no rules
loaded this FAILS CLOSED. A file whose bytes the scrub
changed carries the marker ``scrubbed`` as a third manifest column, declaring
that the fixture deliberately differs from ``base.ref``; the column does NOT
name the pre-scrub blob, because ``base.manifest`` ships (see
:func:`materialise`).
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent / "cases"
REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_DIR_NAME = "base"
MANIFEST_NAME = "base.manifest"

PRIVATE_TERMS_PATH = (
    REPO_ROOT / "src" / "no_human" / "eval" / "_vendor_terms_private.py"
)

#: Column 3 of ``base.manifest``. A fixed marker, never a hash — see
#: :func:`materialise` for why it stopped being the origin blob's object id.
SCRUBBED_MARKER = "scrubbed"

_NO_RULES = (
    "no de-identification rules are loaded: {path} is absent, or defines no "
    "BASE_FIXTURE_SCRUB entries. Materialising now would write the base "
    "fixtures UNSCRUBBED and regenerate base.manifest to agree with them, so "
    "this refuses instead.\n"
    "This is NOT the public export's situation. The export has no supplement, "
    "but it also has no pre-scrub history, and `materialise` probes for the "
    "history FIRST and raises HistoryUnavailable there. So reaching THIS error "
    "from `materialise` means a checkout that CAN resolve base.ref has lost "
    "the supplement — fix the checkout, do not re-run."
)

_NO_HISTORY = (
    "cannot run here: {root} does not have the commit base.ref names ({ref}). "
    "This script reads the PRE-SCRUB history to rebuild a fixture, and the "
    "public export is a fresh `git init` with a single commit, so it carries "
    "none of it. Nothing is wrong — this checkout simply is not one the "
    "materialiser can run in. Run it where the history is."
)


class ScrubRulesUnavailable(RuntimeError):
    """Raised rather than materialising fixtures with de-identification off.

    Fail closed. The failure mode this exists for is silent: with no rules
    loaded the old ``scrub`` returned its input untouched, so a re-materialise
    wrote pre-sweep bytes to disk and then re-pinned the manifest to them. The
    tree looked internally consistent and had never been de-identified.
    """


class HistoryUnavailable(RuntimeError):
    """Raised where ``base.ref`` cannot be resolved at all.

    Distinct from :class:`ScrubRulesUnavailable` on purpose, and probed FIRST,
    because the two collapse into each other otherwise: the public export is
    missing the supplement AND the history, so a bare scrub-rules check at the
    top of :func:`materialise` reports "your supplement is gone" in the one
    environment where that is expected and harmless. An independent review
    caught exactly that — the guard fired in a checkout that could never have
    run the materialiser, while its message asserted the opposite.
    """


def _load_scrub() -> list[tuple[bytes, bytes]]:
    """``(term, replacement)`` byte pairs, longest term first.

    Loaded BY PATH, not by importing ``no_human.eval`` — that package's
    ``__init__`` pulls the orchestrator and the Agent SDK, which is a great deal
    of machinery for a script whose job is ``git cat-file``.

    An absent supplement yields an EMPTY list, and this function does not raise:
    the public export drops the supplement and still has to be able to IMPORT
    this module, because the byte-identity test out there imports it for
    :func:`scrub`. The refusal therefore lives at the point of use —
    :func:`scrub` and :func:`materialise` both raise
    :class:`ScrubRulesUnavailable` on an empty list — so importing stays total
    while running with de-identification switched off is impossible.
    """
    if not PRIVATE_TERMS_PATH.exists():
        return []
    spec = importlib.util.spec_from_file_location(
        "_rr_vendor_terms_private", PRIVATE_TERMS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pairs = [(bytes.fromhex(h), r.encode()) for h, r in
             getattr(mod, "BASE_FIXTURE_SCRUB", [])]
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


SCRUB = _load_scrub()


def scrub(data: bytes) -> bytes:
    """Apply the de-identification substitutions to one blob's bytes.

    Longest term first so a term that is a prefix of another (a name and its
    truncation) cannot be half-replaced by the shorter rule.

    Raises :class:`ScrubRulesUnavailable` with no rules loaded, rather than
    returning ``data`` untouched and letting a caller believe it was scrubbed.
    """
    if not SCRUB:
        raise ScrubRulesUnavailable(_NO_RULES.format(path=PRIVATE_TERMS_PATH))
    for term, replacement in SCRUB:
        data = data.replace(term, replacement)
    return data


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

    The manifest is ``<git blob sha1>  <repo-relative path>`` per line, sorted,
    with an optional third column holding the marker :data:`SCRUBBED_MARKER`.

    Column 1 is always the blob id of the bytes ON DISK, so it is a byte-identity
    pin that keeps working after the export, when ``base.ref`` resolves nothing
    and ``git cat-file`` can no longer be the reference.

    Column 3 appears only when :func:`scrub` changed the blob, and says exactly
    that and nothing more. Column 1 alone would be a lie by omission once a file
    is de-identified — one manifest that quietly means two different things is
    how a scrubbed fixture silently reverts — so the declaration is worth a
    column even as a bare marker.

    IT USED TO BE THE ORIGIN BLOB'S OBJECT ID, and that was removed on
    2026-08-02. ``base.manifest`` is classified ``ship`` and all 31 recorded
    origin ids are reachable from ``main``: a shipped file was a working index
    into content that had deliberately not been de-identified. The history
    rewrite did not close it — blobs are CONTENT-addressed, so rewriting commits
    retires no blob id, and a rewrite retracts nothing from clones already taken.
    Re-checked against the real rewrite on 2026-08-04 and it held. See the corpus
    README for the full argument and its measurements.

    No ASSERTION was given up with it. ``base.ref`` is a full 40-hex commit id
    (enforced by ``test_every_base_ref_is_a_full_commit_id`` — 4 of the 20 were
    abbreviations until 2026-08-02, which made this very sentence false where it
    mattered most), and a commit id fixes its entire tree, so
    ``<base.ref>:<path>`` already determines the origin blob and therefore its
    hash: the deleted assertion re-checked a value it could derive. What the
    origin blob must CONTAIN is still asserted, and strictly more tightly than a
    hash equality, by ``tests/test_reviewer_recall_runner.py::
    test_scrubbed_fixtures_are_their_origin_blob_put_through_scrub``.

    A CAPABILITY was given up, and it is worth naming. The origin blobs
    themselves are reachable from ``main``, so in a fresh clone column 3 would
    have let that check run for all 31 files. Its replacement needs the
    ``base.ref`` COMMIT, and a DEFAULT clone resolves only 4 of the 20 cases —
    so the check now runs in full or skips, and in a default clone it skips.
    That is the trade: the column that made the check portable was the same
    column that published the index, and a verification aid is not worth a leak.

    WHAT WAS NOT GIVEN UP IS THE CONTENT, and the README says so at length —
    read it before quoting the "4 of 20" above as the size of the residual.
    Two corrections it records, both re-measured 2026-08-04 on a real network
    clone: ``git fetch origin '+refs/pull/*:refs/remotes/pull/*'`` takes that 4
    to 20 of 20, because every otherwise-unreachable ``base.ref`` commit sits on
    a PR ref; and removing this column removed a REDUNDANT path *within a clone*,
    not the bytes — all 31 ids it held are also named by the ``change.diff``
    shipped beside it, so distinct recoverable pre-scrub blobs went 48 -> 48
    while paths to them went 2 -> 1. Outside a clone the column was NOT
    redundant: ``change.diff`` abbreviates to 7 hex and GitHub's blob API
    rejects anything under 40 (422), so only this column published an id you
    could dereference remotely. The remaining mitigation is the publish TARGET
    having no PR refs (``git ls-remote <target>`` must print zero
    ``refs/pull/*``), not anything this function can do.
    """
    base_ref = (case_dir / "base.ref").read_text().strip()
    # History first, rules second — see `HistoryUnavailable`. Both refuse; the
    # order is what keeps them telling the truth about WHY.
    if subprocess.run(
        ["git", "cat-file", "-e", f"{base_ref}^{{commit}}"],
        cwd=repo_root, capture_output=True,
    ).returncode != 0:
        raise HistoryUnavailable(_NO_HISTORY.format(root=repo_root, ref=base_ref))
    if not SCRUB:
        raise ScrubRulesUnavailable(_NO_RULES.format(path=PRIVATE_TERMS_PATH))
    diff_text = (case_dir / "change.diff").read_text()
    out_root = case_dir / BASE_DIR_NAME
    written: list[str] = []
    manifest: list[str] = []
    for rel in diff_base_paths(diff_text):
        blob = subprocess.run(
            ["git", "cat-file", "blob", f"{base_ref}:{rel}"],
            cwd=repo_root, check=True, capture_output=True,
        ).stdout
        clean = scrub(blob)
        dest = out_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(clean)
        written.append(rel)
        line = f"{blob_sha1(clean)}  {rel}"
        if clean != blob:
            line += f"  {SCRUBBED_MARKER}"
        manifest.append(line)
    # A create-only case (every pre-image /dev/null) has no base blobs: its
    # manifest is EMPTY — written as the empty string, not a lone newline,
    # so a manifest reader's line loop sees zero lines rather than one blank
    # malformed one. No base/ directory is created for it either (git cannot
    # track an empty directory, so one could never round-trip anyway).
    text = "\n".join(sorted(manifest))
    (case_dir / MANIFEST_NAME).write_text(text + "\n" if text else "")
    return written


def main(argv: list[str]) -> int:
    import json

    wanted = set(argv[1:])
    total = 0
    print(f"scrub rules loaded: {len(SCRUB)}")
    for case_dir in sorted(CASES_DIR.iterdir()):
        if not case_dir.is_dir():
            continue
        if wanted and case_dir.name not in wanted:
            continue
        truth = json.loads((case_dir / "truth.json").read_text())
        if truth.get("external_base_ref"):
            # base.ref names a commit in a recorded-replay scratch repo, not
            # in this repository — there is no history here to extract from.
            # Such a case's base/ and manifest are built when the case is cut
            # and pinned by the same byte-identity test as every other case.
            print(f"{case_dir.name}: skipped (external base.ref)")
            continue
        written = materialise(case_dir)
        total += len(written)
        print(f"{case_dir.name}: {len(written)} file(s)")
    print(f"total: {total} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
