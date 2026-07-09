"""Independent adversarial reviewer (PLAN.md Part 4.4, §3.3).

A fresh-context Agent SDK session told to *find faults and refute "done."*
Runs as ``claude-opus-4-8`` (different model from the implementer) with a
read-only guard so it can inspect the repo but cannot modify it.

Contract:
  - Returns a pass/fail ``ReviewDecision`` with evidence-backed ``ChecklistItem``s.
  - Never emits a numeric score (1–10). The result is always bool.
  - If the session produces no parseable structured block, the decision is
    fail-closed (not pass-through) — the absence of evidence is not evidence of
    passing.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agent.claude_backend import AgentResult
from ..review.selfcheck import ChecklistItem
from ..core.jsonparse import loads_lenient
from ..core.task import Task

_REVIEW_JSON = re.compile(r"REVIEW_JSON_START\s*(.*?)\s*REVIEW_JSON_END", re.DOTALL)

_REVIEW_TURNS = 10
_DIFF_CAP = 60_000  # chars — ~15K tokens, fits in 200K context alongside test output
_FILES_CAP = 80_000  # chars — full text of the changed files, ~20K tokens
_CODE_REVIEW_DIFF_CAP = 120_000  # code_review tasks: ~30K tokens, fits in 200K context
_CODE_REVIEW_TURNS = 15
_CODE_REVIEW_TIMEOUT = 600  # seconds — larger diffs need more time
_OUTPUT_CAP = 4000


class ReviewerUnavailable(RuntimeError):
    """No reviewer is wired, so the review gate cannot run.

    Raised instead of returning a passing decision: the gate must fail closed,
    never silently become a rubber stamp (CLAUDE.md #3).
    """


@dataclass
class ReviewDecision:
    passed: bool
    checklist: list[ChecklistItem] = field(default_factory=list)
    raw_output: str = ""
    suggested_next: str | None = None
    stages: dict[str, Any] | None = None

    @property
    def failed_items(self) -> list[ChecklistItem]:
        return [i for i in self.checklist if not i.passed]

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "passed": self.passed,
            "items": [
                {
                    "label": i.label, "passed": i.passed,
                    "evidence": i.evidence,
                    "file": i.file, "line": i.line,
                    "comment": i.comment,
                    "severity": i.severity,
                }
                for i in self.checklist
            ],
            "raw_output": self.raw_output or None,
        }
        if self.stages:
            d["stages"] = self.stages
        if self.suggested_next:
            d["suggested_next"] = self.suggested_next
        return d


def _git_diff(repo_path: Path, before: str = "HEAD~1", after: str = "HEAD") -> tuple[str, int]:
    """Return (truncated_diff, total_length)."""
    proc = subprocess.run(
        ["git", "diff", f"{before}..{after}", "--stat", "--patch", "--no-color"],
        cwd=repo_path, capture_output=True, text=True,
    )
    raw = proc.stdout or ""
    return raw[:_DIFF_CAP], len(raw)


def _changed_paths(repo_path: Path, before: str, after: str) -> list[str]:
    """Paths that exist at `after`. Deletions are skipped (no text to show);
    for a rename, the new path is returned."""
    proc = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{before}..{after}"],
        cwd=repo_path, capture_output=True, text=True, errors="replace",
    )
    paths: list[str] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or parts[0].startswith("D"):
            continue
        paths.append(parts[-1])
    return paths


def _file_text(repo_path: Path, after: str, path: str) -> str | None:
    """Full text of `path` at `after`, or None if unreadable or binary."""
    proc = subprocess.run(
        ["git", "show", f"{after}:{path}"], cwd=repo_path, capture_output=True,
    )
    if proc.returncode != 0:
        return None
    if b"\x00" in proc.stdout:  # binary — never inline it
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _full_file_context(
    repo_path: Path, before: str, after: str, *, cap: int = _FILES_CAP,
) -> tuple[str, list[str]]:
    """Return (rendered_block, paths_omitted_because_they_did_not_fit).

    A diff shows only changed hunks, so a declaration a few lines above a hunk
    is invisible to the reviewer. It then "finds" an undefined symbol that is
    defined in plain sight. Attaching the full text of each changed file closes
    that blind spot.

    Files are included **whole or not at all**. Truncating a file mid-way would
    reintroduce the very bug this prevents: the cut could land above the
    declaration being checked, and the reviewer cannot tell the difference
    between "not present" and "not shown". Smallest-first packing fits the most
    files; whatever does not fit is named so the reviewer can read it with its
    tools.
    """
    texts: dict[str, str] = {}
    for p in _changed_paths(repo_path, before, after):
        t = _file_text(repo_path, after, p)
        if t is not None:
            texts[p] = t

    included: list[tuple[str, str]] = []
    omitted: list[str] = []
    used = 0
    for p, t in sorted(texts.items(), key=lambda kv: (len(kv[1]), kv[0])):
        entry = len(t) + len(p) + 32  # +header
        if used + entry > cap:
            omitted.append(p)
            continue
        used += entry
        included.append((p, t))

    included.sort(key=lambda kv: kv[0])
    block = "".join(
        f"--- {p} (full text @ {after}) ---\n{t}\n" for p, t in included
    )
    return block, sorted(omitted)


_INVOCATION_ERROR_RE = re.compile(
    r"error: unrecognized arguments"
    r"|no tests ran"
    r"|no tests collected"
    r"|ModuleNotFoundError"
    r"|ImportError"
    r"|command not found",
    re.IGNORECASE,
)


def _annotated_test_output(test_output: str) -> str:
    """Wrap test_output for the review prompt, prepending a note if it looks
    like the test runner crashed (invocation error) rather than tests failing."""
    body = test_output or "(no test output provided)"
    if test_output and _INVOCATION_ERROR_RE.search(test_output):
        return (
            "Test results:\n"
            "NOTE: The test runner encountered an invocation error (not a test failure). "
            "The test command itself failed before any tests could run. This is a test "
            "infrastructure issue. Evaluate the diff on its own merits — do not reject "
            "solely because tests couldn't execute.\n"
            f"```\n{body}\n```\n"
        )
    return f"Test results:\n```\n{body}\n```\n"


def _build_review_prompt(
    task: Task,
    diff: str,
    test_output: str,
    held_out_output: str,
    *,
    diff_total_len: int = 0,
    profile_context: str = "",
    confirmed_rules: str = "",
    full_files: str = "",
    omitted_files: list[str] | None = None,
    allow_tools: bool = True,
) -> str:
    criteria = "\n".join(f"  - {c}" for c in task.acceptance_criteria) or "  (none stated)"
    held_section = (
        f"\nHeld-out test results (tests the implementer never saw):\n{held_out_output}\n"
        if held_out_output else ""
    )
    profile_section = (
        f"\nProject profile (use these conventions as a baseline):\n{profile_context}\n"
        if profile_context else ""
    )
    rules_section = (
        f"\nConfirmed rules from past experience (the team learned these the hard way):\n"
        f"{confirmed_rules}\n"
        if confirmed_rules else ""
    )
    # Prompt ordering: STABLE protocol first → VOLATILE task/diff last (Phase 2a).
    is_truncated = diff_total_len > len(diff)

    # A diff shows only changed hunks. Telling the reviewer the diff is
    # "complete and authoritative" and forbidding it from reading files made it
    # flag symbols as undefined that were declared a few lines outside a hunk —
    # a false positive that cost a full attempt on two separate runs. In gate
    # mode the reviewer always keeps its read-only tools.
    if allow_tools:
        tool_policy = (
            "You MAY use read/search tools (Read, Grep, Glob) to inspect any file in\n"
            "the repository. Do NOT modify any files.\n"
            "MUST: before asserting that a symbol is undefined, missing, unassigned,\n"
            "or orphaned, read the full file and confirm it. A declaration outside\n"
            "the changed hunks is still present in the code.\n\n"
        )
        tool_rule = (
            "  - You MAY use read/search tools; you MUST use them before claiming a\n"
            "    symbol is undefined, missing, or orphaned.\n"
            "  - Do NOT modify any files.\n"
        )
    else:
        tool_policy = (
            "CRITICAL: Do NOT use any tools. Do NOT run any commands. Do NOT read any files.\n"
            "Everything you need is provided below. Respond with text ONLY.\n\n"
        )
        tool_rule = "  - Do NOT use tools, run commands, or read files.\n"

    if is_truncated:
        diff_section = (
            f"Diff (TRUNCATED from {diff_total_len:,} to {len(diff):,} chars — use your"
            f" read tools to inspect any file whose diff is cut off):\n```\n{diff}\n```\n\n"
        )
    else:
        diff_section = (
            "Diff (every changed hunk; code outside these hunks is NOT shown here):\n"
            f"```\n{diff}\n```\n\n"
        )

    files_section = ""
    if full_files:
        files_section = (
            "Full text of the changed files — authoritative. Check here before\n"
            "claiming a symbol is not defined:\n"
            f"{full_files}\n"
        )
    if omitted_files:
        files_section += (
            "Changed files NOT included in full (too large): "
            + ", ".join(omitted_files)
            + " — read them with your tools before making any claim about them.\n\n"
        )

    next_pass = 4
    rules_pass = ""
    if confirmed_rules:
        rules_pass = (
            f"\nPASS {next_pass}: RULE ADHERENCE — for each confirmed rule listed above,\n"
            "  check whether the diff violates it. Cite the rule text, the violating\n"
            "  file:line, and a concrete explanation. Only flag rules that are actually\n"
            "  relevant to the changed files — do not flag rules about unrelated areas.\n"
        )
        next_pass += 1

    scope_pass = (
        f"\nPASS {next_pass}: SCOPE — is this the SMALLEST change that solves the task?\n"
        "  This is a large diff. Check for: unnecessary abstractions or indirection\n"
        "  that aren't required by the acceptance criteria; 'while I'm here' changes\n"
        "  unrelated to the task; premature generalization (e.g. building a framework\n"
        "  when a simple function suffices). Fail if the change could be significantly\n"
        "  smaller while still meeting every criterion.\n"
        if diff.count("\n") > 150 else ""
    )

    return (
        # ── stable prefix (review protocol, rules, output format) ──
        "You are a Staff Software Engineer performing an independent code review.\n"
        "Your ONLY job is to find flaws. Do NOT trust the implementer's work.\n"
        "Try to REFUTE the claim that this task is 'done.' Be adversarial.\n\n"
        + tool_policy
        + "Review the diff in TWO stages. Each stage produces checklist items,\n"
        "and EVERY item must cite concrete evidence (a file:line from the diff,\n"
        "a line of command/test output, or a specific failing input).\n"
        "An item with no cited evidence is not a valid finding.\n\n"
        "STAGE 1 — SPEC COMPLIANCE:\n"
        "  Does the code actually meet each acceptance criterion? Trace the\n"
        "  changed code against every criterion. Does it return what it claims?\n"
        "  Are the tests real (not asserting trivia)?\n"
        "  SCOPE CHECK: does the diff address EXACTLY what the task asked, or\n"
        "  did the implementer do something different (easier, tangential, or\n"
        "  over-engineered)? Flag any drift from the stated acceptance criteria.\n\n"
        "STAGE 2 — CODE QUALITY:\n"
        "  ARCHITECTURE — is this the right approach or a workaround? Does it\n"
        "  follow the existing patterns/conventions shown in the profile? Any\n"
        "  layering, coupling, or abstraction problems?\n"
        "  EDGE CASES — error handling, empty/null/boundary inputs, security\n"
        "  (injection, auth, secrets), concurrency, performance.\n"
        "  EXTERNAL INTEGRATIONS — for any call to an external system (CI/build\n"
        "  API, VCS/PR API, webhooks, cloud/k8s), is the full state space handled\n"
        "  (not just the happy path)? Does any call destructively replace state\n"
        "  when it should merge/add? Are repeated calls (comments, artifacts,\n"
        "  triggers) idempotent?\n\n"
        "For each finding, cite the specific file:line from the diff.\n\n"
        "Limit your output to at most 5 checklist items. If you find more than 5\n"
        "issues, consolidate the least critical ones into a single 'minor issues'\n"
        "item. Focus your detailed items on the most impactful findings.\n\n"
        "Rules:\n"
        + tool_rule
        + "  - Pass/fail only. No numeric scores.\n"
        "  - 'passed: true' means ALL criteria are demonstrably met.\n\n"
        "Output EXACTLY this format (and NOTHING after it):\n\n"
        "REVIEW_JSON_START\n"
        '{"passed": true_or_false,\n'
        ' "stages": {"spec_compliance": {"passed": true_or_false},\n'
        '            "code_quality": {"passed": true_or_false}},\n'
        ' "suggested_next": "one-sentence hint for the next attempt" or null,\n'
        ' "items": [\n'
        '  {"label": "short label", "passed": true_or_false,\n'
        '   "evidence": "detailed explanation of the finding",\n'
        '   "file": "path/to/file.py", "line": 42,\n'
        '   "comment": "PR comment written in a natural, human voice"}\n'
        "]}\n"
        "REVIEW_JSON_END\n\n"
        "For each item:\n"
        "  - 'file' must be the path exactly as shown in the diff header (e.g. 'src/foo.py')\n"
        "  - 'line' must be a line number from the RIGHT side of the diff (new file)\n"
        "  - 'comment' must read like a real engineer wrote it in a code review.\n"
        "    Write in first person, be direct, vary your sentence structure.\n"
        "    No bullet lists, no bold text, no headers, no markdown formatting.\n"
        "    Don't start with 'This', 'The', or 'I noticed'. Just say what's wrong\n"
        "    and what you'd do instead, the way you'd talk to a colleague.\n"
        "  - For general observations with no specific line, set file to '' and line to 0\n"
        "  - 'suggested_next' helps the implementing agent focus its retry — set to null if passed\n\n"
        f"{profile_section}"
        f"{rules_section}\n"
        # ── volatile task-specific content ──
        f"Task: {task.title}\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        + diff_section
        + files_section
        + _annotated_test_output(test_output)
        + f"{held_section}"
        + rules_pass
        + scope_pass
    )


def _build_code_review_prompt(
    task: Task,
    diff: str,
    diff_total_len: int,
    *,
    pr_comments: str = "",
    profile_context: str = "",
    confirmed_rules: str = "",
) -> str:
    """Build the prompt for dedicated code-review tasks (not the gate prompt).

    Key differences from the gate prompt:
    - Constructive tone (not adversarial "refute done")
    - Encourages reading full files via tools when diff is truncated
    - Includes prior PR comments so the reviewer can verify they were addressed
    - Requests severity classification per finding
    - Uses task description as context when acceptance_criteria is empty
    """
    criteria = "\n".join(f"  - {c}" for c in task.acceptance_criteria)
    if not criteria:
        # Fall back to description as implicit acceptance criteria
        desc = (task.description or "").strip()
        if desc:
            criteria = f"  (derived from description) {desc}"
        else:
            criteria = "  (none stated — review for general correctness and quality)"

    truncation_notice = ""
    if diff_total_len > len(diff):
        truncation_notice = (
            f"\n** NOTE: The diff was truncated from {diff_total_len:,} to "
            f"{len(diff):,} characters. Use your tools to read full file contents "
            f"for any file where the diff is cut off. Do NOT flag findings about "
            f"'missing code' unless you have read the full file and confirmed it. **\n"
        )

    comments_section = ""
    if pr_comments:
        comments_section = (
            f"\nExisting PR comments (check whether each was addressed in the diff):\n"
            f"{pr_comments}\n"
        )

    profile_section = (
        f"\nProject profile (use these conventions as a baseline):\n{profile_context}\n"
        if profile_context else ""
    )
    rules_section = (
        f"\nConfirmed rules from past experience (the team learned these the hard way):\n"
        f"{confirmed_rules}\n"
        if confirmed_rules else ""
    )

    # Prompt ordering: STABLE protocol first → VOLATILE task/diff last (Phase 2a).
    return (
        # ── stable prefix (protocol, rules, output format) ──
        "You are a Staff Software Engineer performing a thorough code review of a PR.\n"
        "Your job is to provide constructive, evidence-based feedback that helps the\n"
        "author improve the code. Be rigorous but fair — acknowledge good patterns\n"
        "while flagging real issues.\n\n"
        "If the diff appears truncated for any file, use your read tools to open\n"
        "the full file and understand the complete change before making judgments.\n"
        "Never flag 'missing code' or 'orphaned constant' without first reading\n"
        "the full file to confirm.\n\n"
        "Review the diff in THREE explicit passes:\n\n"
        "PASS 1: CORRECTNESS — does the code actually meet each acceptance\n"
        "  criterion? Trace the changed code against every criterion. Does it\n"
        "  return what it claims? Are the tests real (not asserting trivia)?\n"
        "PASS 2: ARCHITECTURE — is this the right approach or a workaround? Does\n"
        "  it follow the existing patterns/conventions shown in the profile? Any\n"
        "  layering, coupling, or abstraction problems?\n"
        "PASS 3: EDGE CASES — error handling, empty/null/boundary inputs,\n"
        "  security (injection, auth, secrets), concurrency, performance. For any\n"
        "  call to an external system (CI/build API, VCS/PR API, webhooks), verify\n"
        "  the full state space is handled, no call destructively replaces state\n"
        "  when it should merge/add, and repeated calls are idempotent.\n\n"
        "For each finding, cite the specific file:line from the diff or file.\n\n"
        "Rules:\n"
        "  - You MAY use read/search tools to inspect full files for context.\n"
        "  - Do NOT modify any files. This is a read-only review.\n"
        "  - Pass/fail only. No numeric scores.\n"
        "  - Classify each finding with a severity: critical, high, medium, low, nit.\n"
        "  - 'passed: true' means ALL criteria are demonstrably met and no\n"
        "    critical/high issues remain.\n\n"
        "Output EXACTLY this format (and NOTHING after it):\n\n"
        "REVIEW_JSON_START\n"
        '{"passed": true_or_false, "items": [\n'
        '  {"label": "short label", "passed": true_or_false,\n'
        '   "severity": "critical|high|medium|low|nit",\n'
        '   "evidence": "detailed explanation of the finding",\n'
        '   "file": "path/to/file.py", "line": 42,\n'
        '   "comment": "PR comment written in a natural, human voice"}\n'
        "]}\n"
        "REVIEW_JSON_END\n\n"
        "For each item:\n"
        "  - 'file' must be the path exactly as shown in the diff header (e.g. 'src/foo.py')\n"
        "  - 'line' must be a line number from the RIGHT side of the diff (new file)\n"
        "  - 'comment' must read like a real engineer wrote it in a code review.\n"
        "    Write in first person, be direct, vary your sentence structure.\n"
        "    No bullet lists, no bold text, no headers, no markdown formatting.\n"
        "    Don't start with 'This', 'The', or 'I noticed'. Just say what's wrong\n"
        "    and what you'd do instead, the way you'd talk to a colleague.\n"
        "  - For general observations with no specific line, set file to '' and line to 0\n\n"
        f"{profile_section}"
        f"{rules_section}\n"
        # ── volatile task-specific content ──
        f"Task: {task.title}\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"Diff:\n```\n{diff}\n```\n"
        f"{truncation_notice}"
        f"{comments_section}"
    )


def _parse_review_output(text: str) -> ReviewDecision:
    m = _REVIEW_JSON.search(text or "")
    if not m:
        return ReviewDecision(
            passed=False,
            checklist=[ChecklistItem(
                "structured output present",
                False,
                "reviewer produced no parseable REVIEW_JSON block — fail closed",
            )],
            raw_output=text or "",
        )
    try:
        data = loads_lenient(m.group(1))
    except json.JSONDecodeError as exc:
        return ReviewDecision(
            passed=False,
            checklist=[ChecklistItem("json parse", False, str(exc))],
            raw_output=text,
        )
    items = [
        ChecklistItem(
            label=str(i.get("label", "?")),
            passed=bool(i.get("passed", False)),
            evidence=str(i.get("evidence", "")),
            file=str(i.get("file", "")),
            line=int(i.get("line", 0) or 0),
            comment=str(i.get("comment", "")),
            severity=str(i.get("severity", "")),
        )
        for i in (data.get("items") or [])
    ]
    # Reviewer's explicit "passed" field AND all items must agree.
    all_pass = all(i.passed for i in items)
    passed = bool(data.get("passed", False)) and all_pass
    # D4: extract two-stage verdicts and suggested_next (backward-compatible).
    stages = data.get("stages") if isinstance(data.get("stages"), dict) else None
    suggested_next = data.get("suggested_next") if isinstance(data.get("suggested_next"), str) else None
    return ReviewDecision(
        passed=passed, checklist=items, raw_output=text,
        suggested_next=suggested_next, stages=stages,
    )


class AdversarialReviewer:
    """Fresh-context reviewer session — read-only, told to refute 'done.'"""

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-8",
        backend: Any | None = None,
        on_event: Callable | None = None,
    ):
        if backend is not None:
            self._backend = backend
        else:
            from ..agent.claude_backend import ClaudeBackend
            self._backend = ClaudeBackend(
                model=model,
                readonly=True,
            )
        # The model actually bound to this reviewer, read back from the backend
        # so an injected one reports itself rather than the default kwarg.
        self.model = getattr(self._backend, "model", model)
        self._on_event = on_event

    async def review(
        self,
        task: Task,
        *,
        repo_path: Path,
        test_output: str = "",
        held_out_output: str = "",
        before_ref: str = "HEAD~1",
        after_ref: str = "HEAD",
        diff_override: str | None = None,
        profile_context: str = "",
        confirmed_rules: str = "",
        mode: str = "gate",
        pr_comments: str = "",
    ) -> ReviewDecision:
        # Code review mode: higher diff cap, different prompt, multi-turn agent.
        if mode == "code_review":
            cap = _CODE_REVIEW_DIFF_CAP
            raw = diff_override or ""
            diff = raw[:cap]
            prompt = _build_code_review_prompt(
                task,
                diff,
                len(raw),
                pr_comments=pr_comments,
                profile_context=profile_context,
                confirmed_rules=confirmed_rules,
            )
            return await self._agent_review(
                prompt, repo_path,
                max_turns=_CODE_REVIEW_TURNS,
                timeout=_CODE_REVIEW_TIMEOUT,
            )

        # Gate mode (default): original adversarial review.
        full_files, omitted_files = "", []
        if diff_override:
            # Caller supplied the diff; there are no refs to read files from, and
            # _fast_review runs single-turn with no tools.
            diff = diff_override[:_DIFF_CAP]
            diff_total_len = len(diff_override)
        else:
            diff, diff_total_len = _git_diff(repo_path, before_ref, after_ref)
            full_files, omitted_files = _full_file_context(
                repo_path, before_ref, after_ref,
            )
        prompt = _build_review_prompt(
            task,
            diff,
            test_output[:_OUTPUT_CAP],
            held_out_output[:_OUTPUT_CAP],
            diff_total_len=diff_total_len,
            profile_context=profile_context,
            confirmed_rules=confirmed_rules,
            full_files=full_files,
            omitted_files=omitted_files,
            allow_tools=not diff_override,
        )

        # When the diff is already provided, use a single-turn call (no tools).
        # The model has everything it needs in the prompt — no repo exploration.
        if diff_override:
            return await self._fast_review(prompt, repo_path)

        # Full agent session for post-implementation reviews (needs to read files).
        return await self._agent_review(prompt, repo_path)

    async def _fast_review(self, prompt: str, repo_path: Path) -> ReviewDecision:
        """Single-turn review — diff already in prompt, no tools needed."""
        try:
            result: AgentResult = await asyncio.wait_for(
                self._backend.run(
                    prompt,
                    cwd=repo_path,
                    max_turns=1,
                    effort="medium",
                    on_event=self._on_event,
                ),
                timeout=180,
            )
        except asyncio.TimeoutError:
            return ReviewDecision(
                passed=False,
                checklist=[ChecklistItem("timeout", False,
                    "reviewer timed out after 180s — fail closed")],
            )
        return _parse_review_output(result.final_text or "")

    async def _agent_review(
        self, prompt: str, repo_path: Path,
        *, max_turns: int = _REVIEW_TURNS, timeout: int = 300,
    ) -> ReviewDecision:
        """Multi-turn review — model can explore the repo with read-only tools."""
        all_text_parts: list[str] = []
        original_on_event = self._on_event

        def _capture_event(event):
            if event.text:
                all_text_parts.append(event.text)
            if original_on_event:
                original_on_event(event)

        try:
            result: AgentResult = await asyncio.wait_for(
                self._backend.run(
                    prompt,
                    cwd=repo_path,
                    max_turns=max_turns,
                    effort="medium",
                    on_event=_capture_event,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ReviewDecision(
                passed=False,
                checklist=[ChecklistItem("timeout", False,
                    f"reviewer timed out after {timeout}s — fail closed")],
            )
        # Try final_text first, then all captured text.
        decision = _parse_review_output(result.final_text or "")
        if not decision.passed and decision.checklist and \
                decision.checklist[0].label == "structured output present":
            full_text = "\n".join(all_text_parts)
            decision = _parse_review_output(full_text)
        return decision
