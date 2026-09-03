"""The proof ledger (#23): one file per claim source, committed on the task's
`nh-evidence/<task-id>` side branch, so every row of the PR body's Evidence
table can link to the record behind it.

Why a branch and not the PR body: a body is edited text; a blob at a commit
SHA is not. Every URL this module builds names the ledger COMMIT, never the
branch, so what a body points at cannot be rewritten later. Why a side branch
and not the task branch: `vcs/approve_merge.py`'s squash-land carries the task
branch's full diff into main, and an unclassified `.nh-evidence/` directory
must never get there (`tests/test_approve_merge.py::
test_squash_lands_an_nh_evidence_directory_committed_on_the_branch`). The
tamper guard already ignores this directory (`testing/tamper_guard.py`).

Every file opens by saying what it is — a harness-captured record, not
model-authored — because a reader who clicks a "proof" link must not mistake
the record for a verdict. Nothing here decides anything: the files are the
gate outputs as `PrEvidence` gathered them, rendered once.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..agent.verification_receipts import KINDS, md_inline_code
from ..vcs.git import GitError, GitRepo
from ..vcs.manifest_repair import commit_with_manifest_repair
from ..vcs.outbound_scrub import scrub_outbound
from .pr_evidence import PrEvidence

#: The side branch and the directory on it, both per task.
BRANCH = "nh-evidence/{task_id}"
DIRECTORY = ".nh-evidence/{task_id}"

#: Evidence-table row → ledger file. `proof_urls` keys are these row names.
FILES: dict[str, str] = {
    "verification": "verification.md",
    "review": "review.md",
    "tests": "tests.md",
    "verifiers": "verifiers.md",
    "tamper": "tamper.md",
    "merge_policy": "merge-policy.md",
    "assumptions": "assumptions.md",
    "readme": "README.md",
}


def deliver(repo: GitRepo, task_id: str, files: dict[str, bytes], message: str, *,
            on_repair: Callable[[list[str], str], None] | None = None) -> str:
    """Commit *files* under `.nh-evidence/<task_id>/` on `nh-evidence/<task_id>`,
    push, and return the new commit's SHA — "" when there was nothing to
    commit. Raises on git failure (callers treat delivery as advisory).

    Stacks: when the branch already exists (a UI-evidence commit opened it
    earlier in the same attempt) the new commit lands on its tip; otherwise
    the branch is cut from the current branch. The working tree is put back
    on the original branch either way — `_finalize`'s real push and
    `open_pr` must see the task branch, not this one.
    """
    if not files:
        return ""
    for rel in files:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ValueError(f"ledger path escapes the ledger directory: {rel!r}")
    if repo.has_changes():
        # `commit_paths` also sweeps modified tracked and untracked source
        # files into the commit; on a side branch that would publish and then
        # drop unrelated edits. The tree is clean at `_finalize`; if not, no ledger.
        raise GitError("working tree has uncommitted changes; ledger not delivered")
    original = repo.current_branch()
    branch = BRANCH.format(task_id=task_id)
    root = Path(repo.path) / DIRECTORY.format(task_id=task_id)
    try:
        try:
            repo.branch_sha(branch)
        except GitError:
            repo.create_branch(branch, base=original)
        else:
            repo.checkout(branch)
        paths: list[str] = []
        for rel, data in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            paths.append(str(dst))
        commit_with_manifest_repair(repo, paths, message, on_repair=on_repair)
        sha = repo.head_sha()
        repo.push(branch=branch, force_with_lease=True)
        return sha
    finally:
        with contextlib.suppress(Exception):
            repo.checkout(original)


def blob_url(owner: str, repo_name: str, sha: str, task_id: str, rel: str, *,
             line: int | None = None) -> str:
    """GitHub blob URL of a ledger file AT THE LEDGER COMMIT. With *line*,
    the plain (unrendered) view so the `#L<n>` anchor lands on that line."""
    path = quote(f"{DIRECTORY.format(task_id=task_id)}/{rel}", safe="/")
    url = f"https://github.com/{owner}/{repo_name}/blob/{sha}/{path}"
    return f"{url}?plain=1#L{line}" if line else url


def proof_urls(owner: str, repo_name: str, sha: str, task_id: str,
               files: dict[str, str] | dict[str, bytes],
               log_lines: dict[str, int] | None = None) -> dict[str, str]:
    """Row name → blob URL, for exactly the files that were delivered; plus
    `verification:<kind>` → the log opened on that kind's last command
    (*log_lines*, from `command_lines`)."""
    urls = {key: blob_url(owner, repo_name, sha, task_id, name)
            for key, name in FILES.items() if name in files}
    for kind, n in (log_lines or {}).items():
        urls[f"verification:{kind}"] = blob_url(
            owner, repo_name, sha, task_id, FILES["verification"], line=n)
    return urls


def command_lines(verification_md: str, rows: list[dict]) -> dict[str, int]:
    """kind → 1-based line of that kind's LAST recorded command in the
    ledger's `verification.md` (the line the body's fold summary names), or
    nothing for a kind whose command is not on the page (a command the
    appendix's entry cap left unlisted — a link then would land nowhere)."""
    lines = verification_md.split("\n")  # GitHub numbers "\n" lines only, unlike splitlines()
    out: dict[str, int] = {}
    for kind in (*KINDS, *sorted({str(r.get("kind")) for r in rows} - set(KINDS))):
        group = [r for r in rows if str(r.get("kind")) == kind]
        if not group:
            continue
        wanted = f"- {md_inline_code(str(group[-1].get('command', '')))}"
        hits = [i for i, line in enumerate(lines, 1) if line == wanted]
        if hits:
            out[kind] = hits[-1]
    return out


def _header(title: str, task_id: str, head_sha: str, what: str) -> str:
    return (f"# {title}\n\n_Harness-captured record for task `{task_id[:8]}`, "
            f"commit `{head_sha}` — not model-authored: no_human wrote this file "
            f"from {what}. It records what the gate produced; it is not a "
            f"verdict of the model that wrote the code._\n\n")


def _json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, sort_keys=True, default=str) + "\n```\n"


def render_files(evidence: PrEvidence, *, task_id: str, head_sha: str,
                 verification_md: str, review_md: str, assumptions_md: str) -> dict[str, str]:
    """The ledger's text files, from the same `PrEvidence` the body renders
    from. A gate that produced nothing gets NO file: a file would be a claim
    with nothing behind it. Every file goes through the outbound scrub."""
    files: dict[str, str] = {}
    files["verification.md"] = _header(
        "How I verified this — full log", task_id, head_sha,
        "the command receipts a PostToolUse observer recorded") + verification_md
    rv = evidence.review_verdict
    if rv and not rv.get("unmatched"):  # unmatched: the rounds judged another commit
        files["review.md"] = _header(
            "Independent review", task_id, head_sha,
            "the fresh-context reviewer's checklist on this commit") + (
            review_md or _json_block(evidence.review_verdict))
    if evidence.tests is not None:
        files["tests.md"] = _header(
            "Tests — the orchestrator's own run", task_id, head_sha,
            "the layered test run on the final tree") + _json_block(evidence.tests)
    if evidence.verifiers:
        files["verifiers.md"] = _header(
            "Verifiers", task_id, head_sha,
            "the deterministic verifier rules selected for this commit's files"
        ) + _json_block(evidence.verifiers)
    if evidence.tamper:
        files["tamper.md"] = _header(
            "Test-change guard", task_id, head_sha,
            "the tamper adjudicator's waivers") + _json_block(evidence.tamper)
    if evidence.merge_policy or evidence.merge_policy_error:
        files["merge-policy.md"] = _header(
            "Merge-ready policy", task_id, head_sha,
            "the repo's merge policy evaluated against this commit — advisory "
            "to the human, nothing merges on it") + _json_block(
            evidence.merge_policy or {"error": evidence.merge_policy_error})
    if assumptions_md:
        files["assumptions.md"] = _header(
            "Assumptions", task_id, head_sha,
            "the intake step's recorded questions and assumptions") + assumptions_md
    listing = "\n".join(f"- `{name}`" for name in sorted(files))
    files["README.md"] = _header(
        "Evidence ledger", task_id, head_sha,
        "this attempt's gate outputs") + (
        "These files back the pull request's **Evidence** table and its "
        "\"How I verified this\" section; the body links to them at this commit, "
        "so what it points at cannot change. `verification.md` carries the "
        "command log and, when commands were recorded, the list of what that "
        "log cannot attest.\n\n" + listing + "\n")
    return {name: scrub_outbound(text, f"evidence ledger {name}")
            for name, text in files.items()}
