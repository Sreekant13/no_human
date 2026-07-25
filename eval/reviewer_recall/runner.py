"""Reviewer-recall runner — SCRUM-29.

Scores the fresh-context reviewer against the held-out seeded-defect corpus
(``eval/reviewer_recall/cases/``) per ``docs/REVIEWER_RECALL_METHOD.md``.
Scoring is entirely mechanical (no LLM judge) so the number is stable across
runs.

CLI entry point: ``nh bench report --reviewer-recall`` (wired in
``src/no_human/cli/commands.py::_run_reviewer_recall_report``) loads this
module by file path and calls :func:`run_and_report`. That CLI file is the
ONLY module outside this ``eval/`` tree allowed to reference
``eval/reviewer_recall`` — ``tests/test_reviewer_recall_guard.py`` pins this,
because nothing under ``eval/reviewer_recall/`` may ever be imported by
prompt-construction, few-shot, tuning, or intake code (method doc, "Corpus
design").
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

CASES_DIR = Path(__file__).resolve().parent / "cases"
METHOD_DOC = "docs/REVIEWER_RECALL_METHOD.md"
DEFAULT_MODEL = "claude-opus-4-8"


@dataclass
class Finding:
    file: str = ""
    line: int = 0
    text: str = ""
    blocking: bool = True


@dataclass
class ReviewOutcome:
    """What a reviewer invocation produced for one case."""

    status: str  # "PASS" | "FAIL"
    findings: list[Finding] = field(default_factory=list)


@dataclass
class CaseSpec:
    case_id: str
    dir: Path
    base_ref: str
    diff_text: str
    truth: dict[str, Any]

    @property
    def is_control(self) -> bool:
        return self.truth.get("class") == "control"


@dataclass
class CaseResult:
    case_id: str
    cls: str
    is_control: bool
    outcome: ReviewOutcome
    caught: bool | None = None       # seeded cases only
    clean_pass: bool | None = None   # control cases only
    reason: str = ""


@dataclass
class RecallReport:
    results: list[CaseResult]
    model: str
    run_date: str
    method_doc: str = METHOD_DOC


ReviewerFn = Callable[[Path, str, CaseSpec], Awaitable[ReviewOutcome]]


def load_cases(cases_dir: Path = CASES_DIR) -> list[CaseSpec]:
    """Iterate ``cases_dir`` and parse each ``<id>/{base.ref,change.diff,truth.json}``."""
    cases: list[CaseSpec] = []
    for entry in sorted(cases_dir.iterdir()):
        if not entry.is_dir():
            continue
        base_ref = (entry / "base.ref").read_text().strip()
        diff_text = (entry / "change.diff").read_text()
        truth = json.loads((entry / "truth.json").read_text())
        cases.append(CaseSpec(
            case_id=entry.name, dir=entry, base_ref=base_ref,
            diff_text=diff_text, truth=truth,
        ))
    return cases


def _run(cmd: list[str], *, cwd: Path, input_bytes: bytes | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, input=input_bytes, check=True, capture_output=True)


def prepare_case_repo(repo_root: Path, case: CaseSpec, workdir: Path) -> Path:
    """Check out ``case.base_ref`` with NO descendant history, then apply
    ``case.diff_text``.

    Per the method doc: most cases derive from real merged diffs, and a
    checkout that still carries the descendant commit would let the reviewer
    do git archaeology (``git log --all``, diffing against the later "real"
    fix) and find the plant mechanically. ``git archive`` exports the tree at
    ``base_ref`` with zero history, then we seed a brand-new single-commit
    repo from it — a stricter filter than a shallow clone: there is no other
    ref, remote, or ancestor/descendant commit to discover at all.
    """
    target = workdir / case.case_id
    target.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", case.base_ref], cwd=repo_root,
        check=True, capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x"], cwd=target, input=archive, check=True,
                   capture_output=True)
    _run(["git", "init", "-q"], cwd=target)
    _run(["git", "add", "-A"], cwd=target)
    _run([
        "git", "-c", "user.email=reviewer-recall@no-human.local",
        "-c", "user.name=reviewer-recall", "commit", "-q", "-m", "base",
    ], cwd=target)
    _run(["git", "apply", "--whitespace=nowarn"], cwd=target,
        input_bytes=case.diff_text.encode())
    return target


def score_case(case: CaseSpec, outcome: ReviewOutcome) -> CaseResult:
    """Mechanical scoring per docs/REVIEWER_RECALL_METHOD.md, "Scoring".

    Seeded case caught iff: verdict FAIL (or a blocking finding exists) +
    a blocking finding names the planted file + that finding's cited line
    falls within hunk_lines ±3 + the finding text contains >=1 truth.json
    keyword. Control case is a clean pass iff verdict is PASS, or FAIL with
    only non-blocking findings.
    """
    cls = case.truth.get("class", "")
    blocking = [f for f in outcome.findings if f.blocking]

    if case.is_control:
        clean = outcome.status == "PASS" or not blocking
        reason = ("clean pass" if clean else
                  f"false alarm: blocking finding on a clean diff ({blocking[0].file}:{blocking[0].line})")
        return CaseResult(case_id=case.case_id, cls=cls, is_control=True,
                          outcome=outcome, clean_pass=clean, reason=reason)

    truth = case.truth
    planted_file = truth.get("file")
    hunk = truth.get("hunk_lines") or [None, None]
    hunk_lo, hunk_hi = hunk[0], hunk[1]
    keywords = [k.lower() for k in (truth.get("keywords") or [])]

    if outcome.status != "FAIL" and not blocking:
        return CaseResult(case_id=case.case_id, cls=cls, is_control=False,
                          outcome=outcome, caught=False,
                          reason="verdict PASS and no blocking finding — defect not flagged")

    if not blocking:
        return CaseResult(case_id=case.case_id, cls=cls, is_control=False,
                          outcome=outcome, caught=False,
                          reason="FAIL verdict but no blocking finding")

    file_matches = [f for f in blocking if f.file == planted_file]
    for f in file_matches:
        in_hunk = (hunk_lo is None or hunk_hi is None or
                  (hunk_lo - 3) <= f.line <= (hunk_hi + 3))
        if not in_hunk:
            continue
        text_lower = f.text.lower()
        if keywords and not any(kw in text_lower for kw in keywords):
            continue
        return CaseResult(case_id=case.case_id, cls=cls, is_control=False,
                          outcome=outcome, caught=True,
                          reason=f"caught by finding at {f.file}:{f.line}")

    if not file_matches:
        reason = "missed — no blocking finding names the planted file"
    elif not any(
        (hunk_lo is None or hunk_hi is None or (hunk_lo - 3) <= f.line <= (hunk_hi + 3))
        for f in file_matches
    ):
        reason = "missed — cited line falls outside hunk_lines ±3"
    else:
        reason = "missed — finding text matches no truth.json keyword"
    return CaseResult(case_id=case.case_id, cls=cls, is_control=False,
                      outcome=outcome, caught=False, reason=reason)


async def run_case(
    repo_root: Path, case: CaseSpec, reviewer_fn: ReviewerFn, workdir: Path,
) -> CaseResult:
    """Checkout + apply + invoke + score a single case."""
    try:
        case_repo = prepare_case_repo(repo_root, case, workdir)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="ignore")[:400]
        outcome = ReviewOutcome(status="ERROR", findings=[])
        return CaseResult(
            case_id=case.case_id, cls=case.truth.get("class", ""),
            is_control=case.is_control, outcome=outcome,
            caught=None if case.is_control else False,
            clean_pass=False if case.is_control else None,
            reason=f"case setup failed: {stderr or exc}",
        )
    outcome = await reviewer_fn(case_repo, case.diff_text, case)
    return score_case(case, outcome)


def _default_reviewer_fn(model: str) -> ReviewerFn:
    """Wraps the existing fresh-context reviewer (no_human.review.reviewer)."""

    async def _fn(repo_path: Path, diff_text: str, case: CaseSpec) -> ReviewOutcome:
        from no_human.core.task import Task
        from no_human.review.reviewer import AdversarialReviewer

        task = Task.new(f"reviewer-recall probe: {case.case_id}",
                        repo_path=str(repo_path))
        reviewer = AdversarialReviewer(model=model)
        decision = await reviewer.review(task, repo_path=repo_path,
                                         diff_override=diff_text, before_ref="HEAD")
        blocking_ids = {id(item) for item in decision.blocking_items}
        findings = [
            Finding(file=item.file or "", line=item.line or 0,
                    text=f"{item.label}: {item.comment or item.evidence}",
                    blocking=id(item) in blocking_ids)
            for item in decision.failed_items
        ]
        return ReviewOutcome(status="PASS" if decision.passed else "FAIL",
                             findings=findings)

    return _fn


async def run_all(
    repo_root: Path,
    *,
    cases_dir: Path = CASES_DIR,
    reviewer_fn: ReviewerFn | None = None,
    model: str = DEFAULT_MODEL,
    run_date: str | None = None,
) -> RecallReport:
    fn = reviewer_fn or _default_reviewer_fn(model)
    cases = load_cases(cases_dir)
    results: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="nh-reviewer-recall-") as tmp:
        workdir = Path(tmp)
        for case in cases:
            results.append(await run_case(repo_root, case, fn, workdir))
    return RecallReport(results=results, model=model,
                        run_date=run_date or _today())


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def render_report(report: RecallReport) -> str:
    """Render exactly the method-doc format — recall + denominator, per-class
    breakdown, specificity ratio, model, run date, method-doc path. Never a
    bare percentage (the denominator always accompanies it)."""
    seeded = [r for r in report.results if not r.is_control]
    controls = [r for r in report.results if r.is_control]

    caught = sum(1 for r in seeded if r.caught)
    total = len(seeded)
    pct = f" ({caught / total:.0%})" if total else ""

    by_class: dict[str, list[CaseResult]] = {}
    for r in seeded:
        by_class.setdefault(r.cls, []).append(r)
    class_parts = ", ".join(
        f"{cls} {sum(1 for r in rs if r.caught)}/{len(rs)}"
        for cls, rs in sorted(by_class.items())
    )
    class_suffix = f"  [{class_parts}]" if class_parts else ""

    clean = sum(1 for r in controls if r.clean_pass)

    lines = [
        f"reviewer recall: {caught}/{total}{pct}{class_suffix}",
        f"specificity:     {clean}/{len(controls)} clean diffs passed",
        f"model: {report.model} · run date: {report.run_date} · "
        f"method: {report.method_doc}",
    ]
    return "\n".join(lines)


def run_and_report(
    repo_root: Path | str | None = None,
    *,
    reviewer_fn: ReviewerFn | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """CLI entry point: `nh bench report --reviewer-recall` calls this.

    Synchronous wrapper: runs the async per-case pipeline and returns the
    rendered report text (never a bare percentage).
    """
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    report = asyncio.run(run_all(root, reviewer_fn=reviewer_fn, model=model))
    return render_report(report)
