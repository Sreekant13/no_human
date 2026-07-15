"""Independent adversarial reviewer (PLAN.md Part 4.4, §3.3).

A fresh-context Agent SDK session told to *find faults and refute "done."*
Runs as ``claude-opus-4-8`` (different model from the implementer) with a
read-only guard so it can inspect the repo but cannot modify it.

Contract:
  - Returns a pass/fail ``ReviewDecision`` with evidence-backed ``ChecklistItem``s.
  - Never emits a numeric score (1–10). The result is always bool.
  - If the session produces no parseable structured block, the gate has not run.
    It never passes — the absence of evidence is not evidence of passing — and
    it no longer returns a *failing* decision either: after one bounded retry it
    raises :class:`ReviewerUnavailable` so the task escalates. A failing
    decision would be fed to the coder as a finding to fix, spending one of its
    bounded attempts on a defect nobody ever found (task 84251cb2, attempt 13).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agent.claude_backend import AgentResult
from ..review.selfcheck import ChecklistItem
from ..core.jsonparse import loads_lenient
from ..core.task import Task

log = logging.getLogger(__name__)

_REVIEW_JSON = re.compile(r"REVIEW_JSON_START\s*(.*?)\s*REVIEW_JSON_END", re.DOTALL)

# 10 was set when the reviewer could not read files. D16 gave it read-only tools,
# and it now spends most turns fetching the code it cites — the grounding that
# kills false positives. On task 84251cb2 it exhausted 10 turns exploring a
# 1300-line Jenkinsfile and never emitted its verdict, which cost the coder its
# last bounded attempt for a defect that did not exist.
_REVIEW_TURNS = 30
_REVIEW_TIMEOUT = 600
# Constraint #4: retry only on infra failures, and boundedly. A reviewer that
# never reaches a verdict is an infra failure, not a finding.
_REVIEW_INFRA_RETRIES = 1
# On a *timeout* (hung/saturated reviewer, not turn-starved) the retry's window
# is halved down to this floor rather than granted another full one — a hang
# won't clear in a second full window, it just doubles how long a task sits
# blocked in review. Floored so the retry still gets a fair chance.
_REVIEW_MIN_RETRY_TIMEOUT = 120
# The sentinel `_parse_review_output` returns when no REVIEW_JSON block was found.
_NO_VERDICT_LABEL = "structured output present"
_DIFF_CAP = 60_000  # chars — ~15K tokens, fits in 200K context alongside test output
_FILES_CAP = 80_000  # chars — full text of the changed files, ~20K tokens
_CODE_REVIEW_DIFF_CAP = 120_000  # code_review tasks: ~30K tokens, fits in 200K context
_CODE_REVIEW_TURNS = 15
_CODE_REVIEW_TIMEOUT = 600  # seconds — larger diffs need more time
_OUTPUT_CAP = 4000


class ReviewerUnavailable(RuntimeError):
    """The review gate could not run: no reviewer is wired, or it reached no
    verdict after its bounded retries.

    Raised instead of returning a decision. A passing decision would make the
    gate a rubber stamp (CLAUDE.md #3); a *failing* one is subtler and just as
    wrong — its checklist becomes feedback the coder is told to act on, so a
    reviewer that merely ran out of turns costs the coder a bounded attempt for
    a defect nobody found.
    """


@dataclass
class ReviewDecision:
    passed: bool
    checklist: list[ChecklistItem] = field(default_factory=list)
    raw_output: str = ""
    suggested_next: str | None = None
    stages: dict[str, Any] | None = None
    # Blocking findings demoted because their file:line citation did not check
    # out ("label: reason" strings) — surfaced so the round shows the demotion.
    demoted_citations: list[str] = field(default_factory=list)
    # Token usage of the reviewer session(s) that produced this decision, so the
    # code_review attempt records real cost instead of 0 (f71107e9 read as a
    # 0-token "done" that hid a real review with 4 findings).
    tokens_used: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def failed_items(self) -> list[ChecklistItem]:
        return [i for i in self.checklist if not i.passed]

    @property
    def blocking_items(self) -> list[ChecklistItem]:
        """Failing findings the reviewer graded critical/high/medium, or left
        unclassified. These, and only these, fail the gate."""
        return [i for i in self.checklist if _is_blocking(i)]

    @property
    def advisory_items(self) -> list[ChecklistItem]:
        """Failing findings graded low/nit: surfaced to the human, never blocking."""
        return [i for i in self.failed_items if not _is_blocking(i)]

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


# The exact verdict contract _parse_review_output expects — shared by the main
# review prompt and the angle prompts so the two can never drift apart.
_VERDICT_FORMAT = (
    "Output EXACTLY this format (and NOTHING after it):\n\n"
    "REVIEW_JSON_START\n"
    '{"passed": true_or_false,\n'
    ' "stages": {"spec_compliance": {"passed": true_or_false},\n'
    '            "code_quality": {"passed": true_or_false}},\n'
    ' "suggested_next": "one-sentence hint for the next attempt" or null,\n'
    ' "items": [\n'
    '  {"label": "short label", "passed": true_or_false,\n'
    '   "severity": "critical|high|medium|low|nit",\n'
    '   "evidence": "detailed explanation of the finding",\n'
    '   "file": "path/to/file.py", "line": 42,\n'
    '   "comment": "PR comment written in a natural, human voice"}\n'
    "]}\n"
    "REVIEW_JSON_END\n\n"
    "For each item:\n"
    "  - 'file' must be the path exactly as shown in the diff header (e.g. 'src/foo.py')\n"
    "  - a finding graded critical/high/medium MUST cite the exact file and line of "
    "the defect; a citation that does not exist in the repo demotes the finding to advisory\n"
    "  - 'line' must be a line number from the RIGHT side of the diff (new file)\n"
    "  - 'comment' must read like a real engineer wrote it in a code review.\n"
    "    Write in first person, be direct, vary your sentence structure.\n"
    "    No bullet lists, no bold text, no headers, no markdown formatting.\n"
    "    Don't start with 'This', 'The', or 'I noticed'. Just say what's wrong\n"
    "    and what you'd do instead, the way you'd talk to a colleague.\n"
    "  - For general observations with no specific line, set file to '' and line to 0\n"
    "  - 'suggested_next' helps the implementing agent focus its retry — set to null if passed\n\n"
)


def _build_angle_prompt(task: Task, diff: str, focus: str,
                        diff_total_len: int = 0) -> str:
    """A dedicated single-concern prompt for an angle pass. Deliberately NOT
    the full adversarial template — gluing a 'security only' preface onto a
    prompt that also demands spec/scope checks produced self-contradicting
    instructions and mislabeled ordinary findings as [angle] ones."""
    criteria = "\n".join(f"  - {c}" for c in task.acceptance_criteria) or "  (none stated)"
    # B2 #20: angles run single-turn with no tools, so a diff truncated at
    # _DIFF_CAP is ALL they can see. Silently reviewing the head and passing
    # is a false-pass; tell the angle it is looking at a partial diff so it
    # scopes its verdict to what it saw rather than clearing the whole change.
    truncated = diff_total_len and diff_total_len > len(diff)
    trunc_note = (
        f"\n\nNOTE: this diff is TRUNCATED — you are seeing {len(diff):,} of "
        f"{diff_total_len:,} chars. Judge ONLY the part shown; a PASS means "
        "'nothing in your focus in the visible portion', not 'the whole change "
        "is clean'."
        if truncated else "")
    return (
        f"You are a focused code reviewer. {focus}\n"
        "Report ONLY findings inside that focus; if there are none, pass.\n"
        "Findings must cite file:line from the diff below.\n\n"
        + _VERDICT_FORMAT
        + f"Task: {task.title}\n"
        f"Acceptance criteria (context only — do NOT review compliance):\n{criteria}\n\n"
        "The diff under review:\n```diff\n" + diff + "\n```" + trunc_note + "\n"
    )


def _build_review_prompt(
    task: Task,
    diff: str,
    test_output: str,
    held_out_output: str,
    *,
    diff_total_len: int = 0,
    profile_context: str = "",
    confirmed_rules: str = "",
    prior_rounds: str = "",
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
    # Review continuity. You are a fresh context, but this is not the first
    # round — without memory the gate oscillated live: round 14 demanded a
    # self-check be enforced, round 15 demanded the enforcement be gated,
    # rounds 16–17 demanded it all be removed as out of scope. Each round was
    # "right" in isolation; together they were an unbounded polish loop.
    continuity_section = (
        "\nREVIEW CONTINUITY — prior rounds and operator decisions:\n"
        f"{prior_rounds}\n"
        "Rules for continuity:\n"
        "  - Do NOT re-litigate a finding a prior round raised and the coder\n"
        "    addressed, and do NOT reverse a prior round's request, unless you\n"
        "    cite NEW evidence (file:line) that the resolution is wrong.\n"
        "  - Operator answers above are binding. A scope question they settle\n"
        "    is settled — it is not a finding of any severity.\n"
        "  - New findings in code untouched by prior rounds are always fair.\n"
        if prior_rounds else ""
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
        "Classify every finding with a severity, exactly one of:\n"
        "  critical — data loss, security hole, or the change cannot work at all\n"
        "  high     — a real defect that will fire in normal use\n"
        "  medium   — a real defect on a plausible path, or a missed acceptance\n"
        "             criterion\n"
        "  low      — works, but should be better: naming, duplication, style\n"
        "  nit      — cosmetic, or a preference\n"
        "Severity is a classification, never a score. critical/high/medium block\n"
        "the change; low/nit are recorded for the human and do not block.\n\n"
        "Judge SCOPE against the acceptance criteria above, not against your own\n"
        "taste. If the change does something the criteria do not ask for, that is\n"
        "'low' at most — unless it introduces a correctness, security or\n"
        "reliability defect, in which case grade the defect on its own merits. Do\n"
        "not demand work the criteria do not require.\n\n"
        "Limit your output to at most 5 checklist items. If you find more than 5\n"
        "issues, consolidate ONLY the low/nit ones into a single 'minor issues'\n"
        "item with severity 'nit'. NEVER put a medium-or-higher finding in that\n"
        "bucket — report it as its own item. Focus your detailed items on the most\n"
        "impactful findings.\n\n"
        "Rules:\n"
        + tool_rule
        + "  - Pass/fail only. No numeric scores.\n"
        "  - 'passed: true' means ALL criteria are demonstrably met.\n"
        "  - Set 'passed: true' when the only remaining findings are low/nit.\n"
        "  - Every item MUST carry a severity. An unclassified finding is treated\n"
        "    as blocking.\n\n"
        + _VERDICT_FORMAT
        + f"{profile_section}"
        f"{rules_section}"
        f"{continuity_section}\n"
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
        "  - a finding graded critical/high/medium MUST cite the exact file and line of "
        "the defect; a citation that does not exist in the repo demotes the finding to advisory\n"
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


# Severities that block the gate. Anything a reviewer leaves unclassified is
# blocking too: the gate degrades safe, never open.
ADVISORY_SEVERITIES = frozenset({"low", "nit"})


def _is_blocking(item: ChecklistItem) -> bool:
    """A failing finding blocks unless the reviewer graded it low or nit."""
    if item.passed:
        return False
    return (item.severity or "").strip().lower() not in ADVISORY_SEVERITIES


# ── C3-G1: tier-gated multi-angle review ────────────────────────────────────
# Complex-tier diffs get two extra single-turn angle passes (security,
# test-adequacy) run in parallel with the same parser/citation rules — the
# Qodo-2.0 recall pattern, gated by tier so a trivial helper never pays for
# it. Angles ADD findings; they can flip pass→fail, never fail→pass.
REVIEW_ANGLES: tuple[tuple[str, str], ...] = (
    ("security", "SECURITY ONLY: injection, secrets/credential exposure, "
     "path traversal, unsafe deserialization/subprocess/shell use, authz "
     "bypass, SSRF. Ignore style, scope, and general correctness."),
    ("tests", "TEST ADEQUACY ONLY: do the added/changed tests genuinely "
     "exercise the change (real assertions on behavior, failure cases, "
     "boundaries)? Flag tautological or mocked-to-green tests. Ignore style "
     "and general correctness."),
    # D2 #6 (cc10x failure-hunter): the bug class reviewers most often miss —
    # code that cannot fail loudly. Report-only, cite lines.
    ("silent-failure", "SILENT FAILURES ONLY: empty catch blocks, exceptions "
     "swallowed or logged-and-continued where the caller needs the failure, "
     "discarded return values that carry errors, bare except/except Exception "
     "with a pass, error paths that return a success-shaped value, retries "
     "that hide a permanent fault. For each: cite the file:line and say what "
     "the caller wrongly believes succeeded. Ignore style, scope, security, "
     "and test coverage — other passes own those."),
)


def _title_tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 2}


def merge_angle_findings(
    main: ReviewDecision,
    angles: list[tuple[str, ReviewDecision]],
) -> ReviewDecision:
    """Fold angle-pass findings into the main decision.

    A failed angle item is appended (label prefixed "[angle]") unless it
    duplicates an existing checklist item (token-set Jaccard >= 0.5). The
    decision can only get STRICTER: pass flips to fail when a new item is
    blocking; a fail never flips back.
    """
    appended: list[ChecklistItem] = []
    for name, d in angles:
        main.tokens_used += d.tokens_used
        main.cache_read_tokens += d.cache_read_tokens
        main.cache_creation_tokens += d.cache_creation_tokens
        main.demoted_citations.extend(d.demoted_citations)
        for item in d.failed_items:
            toks = _title_tokens(item.label)
            dup = any(
                toks and (len(toks & _title_tokens(ex.label))
                          / max(1, len(toks | _title_tokens(ex.label)))) >= 0.5
                for ex in main.checklist + appended
            )
            if dup:
                continue
            # "name:" not "[name]" — Rich console markup eats bracketed
            # prefixes in `nh review` output.
            item.label = f"{name}: {item.label}"
            appended.append(item)
    main.checklist.extend(appended)
    if any(_is_blocking(i) for i in appended):
        main.passed = False
    return main


def _reached_no_verdict(decision: ReviewDecision) -> bool:
    """True when the reviewer emitted no REVIEW_JSON block at all.

    That is the gate failing to run — not a finding against the diff.
    """
    return (
        not decision.passed
        and bool(decision.checklist)
        and decision.checklist[0].label == _NO_VERDICT_LABEL
    )


def _citation_fails(
    item: ChecklistItem, repo_path: Path, before_ref: str
) -> str | None:
    """Why the item's file:line citation does not check out, or None.

    None also for items with no citation at all — absence is not punished,
    only a citation that names something which does not exist (2411.03079:
    hallucinated locations are the reviewer-FP channel that survives severity
    grading). A file missing from the worktree but present at ``before_ref``
    is a deleted-file finding and verifies fine.
    """
    rel = (item.file or "").strip()
    if not rel:
        return None
    try:
        resolved = (repo_path / rel).resolve()
        inside = resolved.is_relative_to(repo_path.resolve())
    except (OSError, ValueError):
        return f"unresolvable path {rel!r}"
    if not inside:
        return f"{rel!r} escapes the repo"
    if resolved.is_file():
        if item.line > 0:
            try:
                with open(resolved, encoding="utf-8", errors="ignore") as fh:
                    n_lines = sum(1 for _ in fh)
            except OSError:
                return None  # our read failed — never demote on our own error
            if item.line > n_lines:
                return f"{rel} has {n_lines} lines, cited line {item.line}"
        return None
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{before_ref}:{rel}"],
        cwd=repo_path, capture_output=True,
    )
    if probe.returncode == 0:
        return None
    return f"{rel} not found in the worktree or at {before_ref}"


def _verify_citations(
    items: list[ChecklistItem], repo_path: Path, before_ref: str
) -> list[str]:
    """Demote blocking findings whose citations don't check out. Mutates items."""
    demoted: list[str] = []
    for item in items:
        if item.passed or not _is_blocking(item):
            continue
        reason = _citation_fails(item, repo_path, before_ref)
        if reason:
            item.severity = "low"
            item.evidence = (
                f"{item.evidence}\n[citation rule] cited location did not check "
                f"out ({reason}) — demoted to advisory. Re-raise with a "
                "verifiable file:line citation."
            ).strip()
            demoted.append(f"{item.label}: {reason}")
    return demoted


def _parse_review_output(
    text: str, repo_path: Path | None = None, before_ref: str = "HEAD~1",
) -> ReviewDecision:
    m = _REVIEW_JSON.search(text or "")
    if not m:
        return ReviewDecision(
            passed=False,
            checklist=[ChecklistItem(
                _NO_VERDICT_LABEL,
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
    # The citation rule runs BEFORE the verdict: a blocking finding whose
    # cited location does not exist is advisory, and must not fail the gate.
    demoted = _verify_citations(items, repo_path, before_ref) if repo_path else []
    stages = data.get("stages") if isinstance(data.get("stages"), dict) else None
    suggested_next = data.get("suggested_next") if isinstance(data.get("suggested_next"), str) else None
    return ReviewDecision(
        passed=_gate_verdict(items, data, stages),
        checklist=items, raw_output=text,
        suggested_next=suggested_next, stages=stages,
        demoted_citations=demoted,
    )


def _gate_verdict(
    items: list[ChecklistItem], data: dict[str, Any], stages: dict | None
) -> bool:
    """Decide the gate deterministically from the reviewer's own evidence.

    The old rule was ``reviewer.passed AND every item passed``. The gate prompt
    tells the reviewer to consolidate surplus findings into a single 'minor
    issues' item — so on any diff with more than five findings the reviewer
    manufactured a failing item, and the gate could never pass. That is why
    no_human had never opened a reviewed PR: not the tasks, the arithmetic.

    Now a *blocking* finding is one the reviewer graded critical/high/medium —
    or did not grade at all. low/nit findings are recorded and surfaced to the
    human, and never block. Every other way out fails closed:
      - no items at all: absence of evidence is not evidence of passing;
      - `spec_compliance` false: a missed acceptance criterion is never a nit;
      - reviewer says `passed: false` while flagging nothing: it disagrees with
        its own checklist, so trust the "no".
    """
    if not items:
        return False
    if stages and stages.get("spec_compliance", {}).get("passed") is False:
        return False
    if any(_is_blocking(i) for i in items):
        return False
    reviewer_passed = bool(data.get("passed", False))
    any_failing = any(not i.passed for i in items)
    return reviewer_passed or any_failing


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
        prior_rounds: str = "",
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
            prior_rounds=prior_rounds,
            full_files=full_files,
            omitted_files=omitted_files,
            allow_tools=not diff_override,
        )

        # When the diff is already provided, use a single-turn call (no tools).
        # The model has everything it needs in the prompt — no repo exploration.
        if diff_override:
            decision = await self._fast_review(prompt, repo_path, before_ref=before_ref)
        else:
            # Full agent session for post-implementation reviews (needs to read files).
            decision = await self._agent_review(prompt, repo_path, before_ref=before_ref)

        # C3-G1: complex-tier tasks get parallel single-turn angle passes.
        # Angles are ADDITIVE and best-effort: one that times out or crashes
        # is dropped with a visible note — it must never fail the gate by
        # itself (the fail-closed rule belongs to the MAIN review only).
        # B2 #11: angles can only ADD findings / keep a fail failed — they can
        # never flip fail→pass — so running them after a decided FAIL is pure
        # Opus cost. Short-circuit on the main verdict.
        if decision.passed and self._tier_wants_angles(task):
            angle_prompts = [
                (name, _build_angle_prompt(task, diff, focus,
                                           diff_total_len=diff_total_len))
                for name, focus in REVIEW_ANGLES
            ]
            results = await asyncio.gather(
                *(self._fast_review(pr, repo_path, before_ref=before_ref)
                  for _, pr in angle_prompts),
                return_exceptions=True,
            )
            angle_decisions: list[tuple[str, ReviewDecision]] = []
            for i, r in enumerate(results):
                name = angle_prompts[i][0]
                skipped = None
                if not isinstance(r, ReviewDecision):
                    skipped = str(r)[:120]
                elif any(i2.label == "timeout" for i2 in r.checklist):
                    skipped = "timed out"
                if skipped is not None:
                    log.warning("review angle %r skipped: %s", name, skipped)
                    decision.checklist.append(ChecklistItem(
                        f"{name} angle did not run ({skipped})", True,
                        "advisory — the extra angle pass was skipped; the main "
                        "review still gates"))
                    continue
                angle_decisions.append((name, r))
            if angle_decisions:
                decision = merge_angle_findings(decision, angle_decisions)
        return decision

    @staticmethod
    def _tier_wants_angles(task: Task) -> bool:
        """Angles run only for complex-tier tasks (the tier can only make the
        review STRICTER — tiers change effort, never safety)."""
        try:
            from ..core.complexity import is_complex
            return is_complex(task)
        except Exception:  # noqa: BLE001 — angles are additive, never required
            return False

    async def _fast_review(self, prompt: str, repo_path: Path,
                           *, before_ref: str = "HEAD~1") -> ReviewDecision:
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
        decision = _parse_review_output(result.final_text or "",
                                        repo_path=repo_path, before_ref=before_ref)
        decision.tokens_used = result.tokens_used
        decision.cache_read_tokens = getattr(result, "cache_read_tokens", 0)
        decision.cache_creation_tokens = getattr(result, "cache_creation_tokens", 0)
        return decision

    async def _agent_review(
        self, prompt: str, repo_path: Path,
        *, max_turns: int = _REVIEW_TURNS, timeout: int = _REVIEW_TIMEOUT,
        before_ref: str = "HEAD~1",
    ) -> ReviewDecision:
        """Multi-turn review — model can explore the repo with read-only tools.

        A reviewer that never reaches a verdict has not found a defect: the gate
        simply did not run. Returning its fail-closed decision is worse than it
        looks — the checklist item "reviewer produced no parseable REVIEW_JSON"
        is fed back to the *coder* as a finding to fix, and it spends one of the
        coder's bounded attempts on a defect that does not exist. That is what
        ended task 84251cb2's last attempt.

        So: retry once with a larger budget (constraint #4 — infra-only, bounded),
        then raise :class:`ReviewerUnavailable` so the task escalates honestly.
        No path here ever turns a missing verdict into a pass.
        """
        last_reason = "unknown"
        round_timeout = timeout
        for round_n in range(_REVIEW_INFRA_RETRIES + 1):
            budget = max_turns * (2 ** round_n)
            decision, reason = await self._review_once(
                prompt, repo_path, max_turns=budget, timeout=round_timeout,
                before_ref=before_ref,
            )
            if decision is not None:
                return decision
            last_reason = reason
            # A *timeout* means the reviewer is hung/saturated, not turn-starved,
            # so granting another full window just doubles the wall-time a task
            # sits blocked in review (a 50-line diff sat 20min in prod: 2×600s).
            # Halve the next round's window — the turn budget still doubles for
            # the turn-exhaustion case the retry actually exists for. Never turns
            # a missing verdict into a pass; only escalates a hang sooner.
            if reason.startswith("timed out"):
                round_timeout = max(_REVIEW_MIN_RETRY_TIMEOUT, round_timeout // 2)
            log.warning(
                "reviewer reached no verdict (%s) on round %d/%d (budget %d turns)",
                reason, round_n + 1, _REVIEW_INFRA_RETRIES + 1, budget,
            )

        raise ReviewerUnavailable(
            f"the reviewer reached no verdict after {_REVIEW_INFRA_RETRIES + 1} "
            f"rounds ({last_reason}). The review gate did not run, so this diff "
            "is unreviewed. Escalating rather than passing it — or blaming the "
            "coder for a finding that was never made."
        )

    async def _review_once(
        self, prompt: str, repo_path: Path, *, max_turns: int, timeout: int,
        before_ref: str = "HEAD~1",
    ) -> tuple[ReviewDecision | None, str]:
        """One reviewer session.

        Returns ``(decision, "")`` on a real verdict — pass or fail — and
        ``(None, reason)`` when the gate could not run at all.
        """
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
            return None, f"timed out after {timeout}s"

        # Try final_text first, then all captured text.
        decision = _parse_review_output(result.final_text or "",
                                        repo_path=repo_path, before_ref=before_ref)
        if _reached_no_verdict(decision):
            decision = _parse_review_output("\n".join(all_text_parts),
                                            repo_path=repo_path, before_ref=before_ref)
        if _reached_no_verdict(decision):
            reason = result.stop_reason or "no REVIEW_JSON block"
            if getattr(result, "is_error", False):
                reason = f"reviewer session error ({reason})"
            return None, reason
        decision.tokens_used = result.tokens_used
        decision.cache_read_tokens = getattr(result, "cache_read_tokens", 0)
        decision.cache_creation_tokens = getattr(result, "cache_creation_tokens", 0)
        return decision, ""
