"""Pure prompt-assembly blocks extracted from the orchestrator (EH4, step 1).

The implement-prompt god-method mixed ~500 LOC of string construction with
orchestrator state. These are the parts that are PURE — they depend only on
their arguments, so they can live here, be unit-tested directly, and keep the
orchestrator method thin. Extraction is byte-for-byte behaviour-preserving
(pinned by a golden-prompt test); the still-stateful parts (memories, context
digest, resume digest, plan) stay in the orchestrator for now.
"""

from __future__ import annotations

from typing import Any

from ..blockers import Blocker
from .task import Task


def build_resume_digest(task: Task) -> str:
    """Seed a resumed task's fresh session with the prior blocker report and
    any human reply (22.5) — not a stale, bloated context. Pure: reads only
    ``task.blocker`` and ``task.context``."""
    parts: list[str] = []
    if task.blocker:
        b = Blocker.from_dict(task.blocker)
        parts.append(
            "You are resuming a previously-blocked task. Prior diagnosis:\n"
            f"  category: {b.category.value}\n"
            f"  why: {b.root_cause_hypothesis}\n"
            f"  tried: {'; '.join(b.tried) if b.tried else '(none)'}"
        )
    ctx = task.context or {}
    replies = ctx.get("human_replies") or []
    if replies:
        latest = replies[-1]
        parts.append(
            "A human answered your blocking question:\n"
            f"  Q: {latest.get('question', '')}\n"
            f"  A: {latest.get('answer', '')}\n"
            "Use this answer; do NOT re-ask. Do not lower the bar."
        )
    feedback = ctx.get("send_back_feedback") or []
    if feedback:
        parts.append(
            "Reviewer/human send-back feedback to address:\n"
            + "\n".join(f"  - {f.get('message', '')}" for f in feedback[-3:])
        )
    review_fb = ctx.get("review_feedback") or []
    if review_fb:
        lines = []
        for f in review_fb:
            loc = f"{f.get('file', '')}:{f.get('line', 0)}" if f.get("file") else ""
            detail = f.get("comment") or f.get("evidence") or ""
            lines.append(f"  - {f.get('label', '')}{f' ({loc})' if loc else ''}: {detail}")
        parts.append(
            "The independent staff reviewer FAILED your previous attempt on "
            "these specific, cited findings. Fix each one — do NOT weaken, "
            "skip, or delete any test to satisfy the reviewer:\n"
            + "\n".join(lines)
        )
    suggested_next = ctx.get("review_suggested_next")
    if suggested_next:
        parts.append(
            f"Reviewer's suggested focus for this retry: {suggested_next}"
        )
    # R1.6: inject distilled attempt log so this attempt doesn't repeat.
    attempt_log = ctx.get("attempt_log") or []
    if attempt_log:
        parts.append(
            "Previous attempt outcomes (do NOT repeat the same approach):\n"
            + "\n".join(f"  - {entry}" for entry in attempt_log)
        )
    handoff = ctx.get("handoff")
    if handoff:
        summary = handoff.get("summary", "")
        files = handoff.get("changed_files", [])
        turns = handoff.get("turns_used", "?")
        wip = handoff.get("wip_sha", "")
        resume_lines = [
            f"The previous attempt ran out of turns ({turns} used) and left "
            f"partial work{' (committed as WIP-PARTIAL ' + wip[:8] + ')' if wip else ''}."
        ]
        if files:
            resume_lines.append(
                f"  Files already modified: {', '.join(files[:15])}"
            )
        if summary and not summary.startswith("Claude Code returned"):
            resume_lines.append(f"  Last status: {summary[:600]}")
        resume_lines.append(
            "CRITICAL: Your working tree ALREADY CONTAINS the partial implementation.\n"
            "  1. READ the files listed above to understand what is already done.\n"
            "  2. Do NOT redo work that is already complete.\n"
            "  3. Pick up where the previous attempt left off.\n"
            "  4. Focus remaining turns on completing unfinished acceptance criteria\n"
            "     and running the test suite."
        )
        parts.append("\n".join(resume_lines))
    ci_fail = ctx.get("ci_failure")
    if ci_fail:
        tests = ci_fail.get("failing_tests") or []
        parts.append(
            "The remote CI build for your previous attempt FAILED. Fix the "
            "actual failure — do NOT weaken, skip, or delete tests to go green.\n"
            f"  pipeline: {ci_fail.get('url', '')}\n"
            + (f"  failing tests: {', '.join(tests[:10])}\n" if tests else "")
            + "  details:\n"
            + "\n".join(f"    {ln}" for ln in
                        (ci_fail.get("detail", "")).splitlines()[:30])
        )
    # D3: inject test case plan from structured spec.
    spec = ctx.get("spec") or {}
    test_plan = spec.get("test_plan", "")
    if test_plan:
        parts.append(
            "Test plan from the spec — write tests that cover these:\n"
            + test_plan
        )
    # W3.5 (Devin playbook): the spec's out_of_scope is the FORBIDDEN list.
    out_of_scope = spec.get("out_of_scope")
    if out_of_scope:
        items = (out_of_scope if isinstance(out_of_scope, list)
                 else [out_of_scope])
        forbidden = "\n".join(f"  - {str(x)}" for x in items if str(x).strip())
        if forbidden:
            parts.append(
                "OUT OF SCOPE — do NOT do any of these (the spec forbids "
                "them; touching them fails review):\n" + forbidden)
    return "\n\n".join(parts)


def build_rules_block(
    test_cmd_str: str, integration_cmd_str: str, ci_name: str | None,
) -> str:
    """The implement-prompt Rules section. ``ci_name`` is the remote CI runner's
    name, or None when there is none (mirrors ``self.ci_runner``)."""
    return (
        "Rules:\n"
        "  CRITICAL — NEVER SKIP A TASK. Everything the user gives you, you CAN do.\n"
        "  Never claim inability or skip work because you assume you can't do it.\n"
        "  If you hit a real blocker, report it with evidence — but NEVER refuse\n"
        "  without trying first. Find a way.\n\n"
        "  - Verify with evidence: run commands, read their output; don't assert.\n"
        "    'I think it works' is NOT evidence. Run the command and show the output.\n"
        "  - Minimal, focused edits. No comments unless the WHY is non-obvious.\n"
        "  - Add or update tests for your change and run them.\n"
        + (f"  - Run unit tests with: {test_cmd_str}\n"
           f"    EARLY VERIFICATION: Run this command EARLY (within your first few tool\n"
           f"    calls) to confirm the test environment works. If it fails due to missing\n"
           f"    plugins, conftest issues, or argument errors, fix the invocation BEFORE\n"
           f"    spending turns on implementation. Do NOT wait until the end to discover\n"
           f"    the test suite is broken.\n"
           f"    You MUST run this command and confirm ALL tests pass before finishing.\n"
           f"    Paste the full output as evidence.\n"
           if test_cmd_str else
           "  - Run the project's test suite and confirm all tests pass before finishing.\n")
        + "  - REPRO MANIFEST: if this repo's tests run with pytest, write\n"
          "    .no_human/repro_tests.json — {\"tests\": [\"<pytest node ids>\"]} — listing\n"
          "    the test(s) that FAIL on the base code and PASS with your change (for a\n"
          "    bugfix: the reproduction; for a feature: its acceptance tests). The\n"
          "    harness runs them in both trees to prove the diff does what it claims.\n"
          "    The file is metadata: never commit it (.no_human/ is excluded anyway).\n"
        + (f"  - Integration tests run on GitLab CI after your branch is pushed. Your\n"
           f"    change must also pass integration tests. If you can run them locally\n"
           f"    with: {integration_cmd_str}\n"
           f"    do so and confirm they pass. Otherwise, ensure your changes are\n"
           f"    compatible with the integration test expectations.\n"
           if integration_cmd_str else "")
        + (f"  - Remote CI ({ci_name}) will run after local tests pass.\n"
           f"    Your change must pass both local tests AND the remote CI pipeline.\n"
           f"    If you know what the CI tests exercise, verify your changes are\n"
           f"    compatible. Do NOT assume local-only tests are sufficient.\n"
           if ci_name is not None and not integration_cmd_str else "")
        + "  - NEVER weaken, skip, or delete a test to make things pass.\n"
        "  - If, after verifying with evidence, you find the acceptance criteria are\n"
        "    ALREADY satisfied by the existing code: do NOT fabricate an edit, and do\n"
        "    NOT simply report success. Emit a blocker (category AMBIGUITY) citing\n"
        "    file:line for each criterion. A task that needs no code change is a\n"
        "    decision for a human, not a silent no-op — finishing with zero edits and\n"
        "    no blocker reads to the system as a failed attempt. This is never a way\n"
        "    to avoid finishing doable work.\n"
        "  - Do NOT run any git command — branching, committing, pushing and\n"
        "    opening the PR are handled for you. Just edit files and run tests.\n"
        "  - All imports MUST be at the top of the file. Never add imports in the\n"
        "    middle of a file — if you need to import, make a separate edit at the top.\n"
        "  - Before writing code for a CI or remote environment, verify what tools and\n"
        "    runtimes are available there. Never assume python3, jq, or specific versions.\n"
        "  - READ the existing code BEFORE making changes. Understand what is already\n"
        "    there; do not guess or speculate about the codebase.\n"
        "  - If you are stuck after 2 attempts at the same approach, STOP and rethink.\n"
        "    Try a fundamentally different approach, not a minor tweak.\n"
        "  - Fix root causes, not symptoms. If a test fails, understand WHY before\n"
        "    changing code. Chasing the error message leads to cascading wrong fixes.\n"
        "  - Make the SMALLEST change that solves the task. No speculative abstraction,\n"
        "    no 'while I'm here' extras, no premature generalization. If a one-line fix\n"
        "    works, ship it — don't build a framework.\n"
        "  - Do NOT create virtualenvs, install packages (pip install, npm install),\n"
        "    or generate build artifacts in the repo. Use the existing environment.\n"
        "    If dependencies are needed, add them to the project's dependency file\n"
        "    (requirements.txt, pyproject.toml, package.json, pom.xml, etc.).\n"
        "  Standing discipline (apply at every step, not just at the end):\n"
        "  - Verify everything. No assumptions — read the actual code before changing\n"
        "    it, run commands and cite their output. Don't trust any file:line reference\n"
        "    without confirming it yourself first — the codebase may have moved.\n"
        "  - Rank every decision 1–10 and only proceed on a 10. If a step isn't a 10,\n"
        "    stop and close the gap before moving to the next step.\n"
        "  - Devil's advocate before acting. For each change, explicitly write down what\n"
        "    could break, then address it before you make the change — not after.\n"
        "  - Review every change as a staff engineer would. No sloppy patches, no\n"
        "    unrequested abstractions, no scope creep.\n"
    )


def build_memories_block(
    memories: list[dict] | None, critical_cap: int, relevant_cap: int,
) -> str:
    """Format confirmed rules + skills for prompt injection (importance-tiered).
    Pure: takes the active memories and the char budgets. '' when none.

    - Critical (importance=high): full content, up to ``critical_cap``
    - Relevant (importance=med): compact one-liner, up to ``relevant_cap``
    - Long-tail (importance=low): title only, as on-demand lookup hint
    """
    if not memories:
        return ""
    critical: list[dict] = []
    relevant: list[dict] = []
    long_tail: list[dict] = []
    for m in memories:
        tags = m.get("tags") or []
        if "importance:high" in tags:
            critical.append(m)
        elif "importance:low" in tags:
            long_tail.append(m)
        else:
            relevant.append(m)

    parts: list[str] = []
    if critical:
        crit_lines: list[str] = []
        budget = critical_cap
        for m in critical:
            mem_type = m.get("type", "rule")
            title = m.get("title", "")
            content = m.get("content", "").strip()
            line = f"  - [{mem_type}] {title}: {content}"
            if budget - len(line) < 0:
                break  # hard cap: stop, don't truncate mid-rule
            crit_lines.append(line)
            budget -= len(line)
        if crit_lines:
            parts.append(
                "Critical rules (MUST follow — full content):\n"
                + "\n".join(crit_lines)
            )

    if relevant:
        rel_lines: list[str] = []
        budget = relevant_cap
        for m in relevant:
            mem_type = m.get("type", "rule")
            title = m.get("title", "")
            content = m.get("content", "").replace("\n", " ").strip()[:200]
            line = f"  - [{mem_type}] {title}: {content}"
            if budget - len(line) < 0:
                break
            rel_lines.append(line)
            budget -= len(line)
        if rel_lines:
            parts.append(
                "Relevant rules/skills:\n"
                + "\n".join(rel_lines)
            )

    if long_tail:
        tail_lines = [
            f"  - [{m.get('type', 'rule')}] {m.get('title', '')}"
            for m in long_tail[:20]
        ]
        parts.append(
            "Additional context (look up if relevant to your task):\n"
            + "\n".join(tail_lines)
        )

    if not parts:
        return ""
    return (
        "\nConfirmed rules/skills from past experience:\n"
        + "\n\n".join(parts)
        + "\n"
    )


def build_profile_block(prof: Any) -> str:
    """The 'Project profile (confirmed)' block, or '' when there is no profile.
    Tells the agent the repo's ecosystem/commands so it doesn't waste turns
    rediscovering the stack."""
    if not prof:
        return ""
    parts = [f"Ecosystem: {prof.ecosystem}" if prof.ecosystem else ""]
    if prof.test_cmd:
        parts.append(f"Unit test command: {prof.test_cmd}")
    if getattr(prof, "integration_test_cmd", ""):
        parts.append(f"Integration test command: {prof.integration_test_cmd}")
    if prof.install_cmd:
        parts.append(f"Install command: {prof.install_cmd}")
    if prof.lint_cmd:
        parts.append(f"Lint command: {prof.lint_cmd}")
    ci_conf = getattr(prof, "ci", {}) or {}
    if ci_conf.get("enabled"):
        ci_backend = ci_conf.get("backend", "gitlab")
        ci_project = ci_conf.get("project", "")
        parts.append(f"Remote CI: {ci_backend}" + (f" ({ci_project})" if ci_project else ""))
    return "Project profile (confirmed):\n" + "\n".join(f"  {p}" for p in parts if p) + "\n\n"
