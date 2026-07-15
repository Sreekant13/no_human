"""PreToolUse safety guard — pure, testable policy (PLAN.md Part 10).

Blocks, before execution:
  - writes/edits to forbidden paths (.env, secrets/, *.key, *.pem, ...)
  - pushes/force-updates to protected branches (never_push_to) — which must
    include the PR's *base* branch, or "never merge" is trivially bypassed by
    pushing straight to it
  - merging a pull/merge request (`gh pr merge`, `glab mr merge`, and the
    equivalent REST call). This is constraint §3.2 — the agent never merges —
    and until 2026-07-10 nothing enforced it: only `git merge` was blocked,
    which is not how a PR gets merged.
  - destructive shell (`rm -rf`, history rewrites) — a circuit breaker that
    fires even under bypass permissions
  - interactive prompts (`AskUserQuestion`) — nobody is at the keyboard (§22)
  - background polling (`Monitor`, `TaskStop`, `ToolSearch`) in a read-only
    session — a planner does not need to busy-wait on its own subagents

Deliberately allowed (user, 2026-07-10): the agent may `git commit`, `git push`
its own branch, `git merge` another ref *into* that branch, and open a PR. Only
merging the PR is forbidden. A local merge cannot reach a protected branch
because the push is denied, so the branch-level `git merge` ban was protecting
nothing and blocked the legitimate "rebase/merge base into my branch" workflow.

The orchestrator wraps :func:`evaluate` in a Claude Agent SDK PreToolUse hook;
keeping the policy a pure function lets us unit-test it without the SDK.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

# Phase C — the measured whale: the coder re-reads 57.8k tokens EVERY turn
# (76 real attempts: 2.21M cache-read / 33.2 turns). The seed is ~6.4k of it;
# the rest is accumulated tool output, and an unbounded whole-file Read of a
# huge file is re-sent on every subsequent turn for the rest of the attempt.
# So a Read with no limit on a file above this many lines is REDIRECTED (never
# denied — a denial would just loop) to the scoped form the coder already
# knows: offset/limit, or Grep to find the relevant region first.
_READ_LINES_BUDGET = 2000
# Data/config/lock files often NEED whole-file context (a partial parse is
# useless) and are rarely re-read many times — exempt from the read redirect.
_WHOLE_READ_OK_EXTS = frozenset((
    "json", "yaml", "yml", "toml", "lock", "csv", "xml", "sql", "txt", "md",
))

# Tools that block on a human answer. Every no_human session is headless, so
# these silently return nothing and the agent fills the gap with a guess — we
# observed a planner proposer call AskUserQuestion, get no answer, and write
# "No answer given — I'll default to ...". Denying is only half the fix: the
# reason has to tell the agent what to do instead, or it just retries.
INTERACTIVE_TOOLS = {"AskUserQuestion"}

# Background-orchestration tools. A read-only session (planner, aggregator,
# reviewer) explores and reports; it never needs to run work in the background
# and poll for it. Observed in run 0305e5ce/087e2d3a: the `test-first` proposer
# used ToolSearch to discover Monitor and TaskStop, spawned two real research
# subagents, then spawned FIVE more subagents whose entire job was to wait for
# them ("idle wait for agent a422b247…", "idle until agent 2 completes"). It
# burned 100 events against the `minimal-first` lens's 22 for the same one plan
# draft, and gathered nothing with any of it.
#
# The `Agent` tool is deliberately NOT denied: it returns its result directly.
# The `risk-first` lens proves it — three subagents, zero Monitor/TaskStop calls,
# draft delivered.
BACKGROUND_TOOLS = {"Monitor", "TaskStop", "ToolSearch"}

_NO_POLLING_REASON = (
    "{tool} is unavailable in a read-only session. The Agent tool returns its "
    "result to you directly when the subagent finishes — do not spawn helpers "
    "to wait for it, and do not poll. Read, Grep, Glob, Bash and Agent are all "
    "you need; use them and write your report."
)

_NO_HUMAN_REASON = (
    "{tool} is unavailable: this session is headless and no human will ever "
    "answer it. Do not retry it and do not silently guess. If you cannot make "
    "verifiable progress without the answer, stop and emit a structured blocker "
    "report (BLOCKER_JSON_START/BLOCKER_JSON_END) with the question in the "
    "'question' field and your candidate answers in 'options'. Otherwise pick "
    "the most defensible option and state the assumption, and the evidence for "
    "it, explicitly in your output."
)

# Destructive shell patterns — matched against the full command string.
# D2 #2: deleting test files via the shell used to surface only at the
# END-of-attempt tamper gate — after the whole attempt's tokens were spent.
# Denied at tool time instead; `git mv` renames stay allowed. Covers rm /
# git rm on tests/ dirs and test_*/**.test.* file shapes.
_RM_TESTS = re.compile(
    r"\b(?:git\s+)?rm\b[^|;&]*?"
    # a tests/ dir, or a source test file — but NOT build/coverage artifacts
    # (dist/coverage/*.map, test_results.xml). Require a source extension.
    r"(?:\btests?/|/tests?/|"
    r"\btest_[\w-]+\.(?:py|js|mjs|cjs|jsx|ts|tsx|rb|go|rs|java)(?![\w.])|"
    r"[\w-]+\.test\.(?:js|mjs|cjs|jsx|ts|tsx)(?![\w.]))",
    re.IGNORECASE)
# The repo's no_human config is the operator's contract with the agent — the
# agent never edits it (add-or-tighten is a HUMAN action).
_NO_HUMAN_YML_WRITE = re.compile(
    r"(?:sed\s+-i|>\s*|>>\s*|tee\s+)[^|;&]*\.no_human\.ya?ml", re.IGNORECASE)

_RM_RF = re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|-rf|-fr)\b")
_GIT_DESTRUCTIVE = re.compile(
    r"\bgit\s+(push\s+.*--force|push\s+.*-f\b|reset\s+--hard\s+\S|"
    r"clean\s+-[a-z]*f|filter-branch|update-ref\s+-d)"
)

# Merging the PR — the one action that is always a human's (§3.2). `git merge`
# is NOT this: a PR is merged through the forge, and that is what must be denied.
_FORGE_MERGE = re.compile(
    r"\b(?:gh\s+pr\s+merge"           # gh pr merge 531 --squash
    r"|glab\s+mr\s+merge"             # glab mr merge 12
    r"|gh\s+api\b[^|;&]*?/(?:pulls|merge_requests)/\d+/merge"  # the REST call
    r"|glab\s+api\b[^|;&]*?/merge_requests/\d+/merge)"
)

# Any git/forge command that mutates history or a remote. A read-only session
# (planner, aggregator, reviewer) explores and reports; it never writes. Dropping
# the blanket `git merge` ban would otherwise have let a reviewer commit.
_GIT_WRITE = re.compile(
    r"\bgit\s+(?:commit|push|merge|rebase|cherry-pick|revert|am|apply|tag"
    r"|reset|restore|stash|branch|checkout|switch)\b"
)
_FORGE_WRITE = re.compile(
    r"\b(?:gh\s+pr\s+(?:create|merge|close|edit|ready|review)"
    r"|glab\s+mr\s+(?:create|merge|close|update))\b"
)


@dataclass
class GuardDecision:
    allow: bool
    reason: str = ""


def _path_forbidden(path: str, forbidden: list[str]) -> bool:
    norm = path.removeprefix("./").rstrip("/")
    base = norm.rsplit("/", 1)[-1]
    for pat in forbidden:
        p = pat.rstrip("/")
        if fnmatch.fnmatch(norm, p) or fnmatch.fnmatch(base, p):
            return True
        # directory prefix match: "secrets/" forbids "secrets/x/y"
        if pat.endswith("/") and (norm == p or norm.startswith(p + "/")):
            return True
        if "/" in norm and (norm.startswith(p + "/") or f"/{p}/" in f"/{norm}"):
            return True
    return False


def _branch_protected(branch: str, never_push_to: list[str]) -> bool:
    b = branch.strip().removeprefix("origin/").removeprefix("refs/heads/")
    return any(fnmatch.fnmatch(b, pat) for pat in never_push_to)


def _push_targets_protected(cmd: str, never_push_to: list[str]) -> bool:
    """Detect `git push ... <branch>` / `git checkout <protected>` to a guarded ref."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    # any token equal to a protected branch name in a push/checkout context
    if "push" in tokens or "checkout" in tokens or "switch" in tokens:
        for tok in tokens:
            if tok.startswith("-"):
                continue
            # handle refspecs like HEAD:main or local:remote
            for ref in re.split(r"[:]", tok):
                if _branch_protected(ref, never_push_to):
                    return True
    return False


def _line_count(path: str) -> int:
    """Lines in a file, or 0 when unknowable (missing/binary/unreadable) —
    the guard must never fail a tool call because it could not stat a file."""
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\n")
    except (OSError, ValueError):
        return 0


def evaluate(
    tool_name: str,
    tool_input: dict,
    *,
    forbidden_paths: list[str],
    never_push_to: list[str],
    readonly: bool = False,
) -> GuardDecision:
    """Return allow/deny for a single proposed tool call."""
    # 0. Interactive prompts — denied in every role, readonly or not.
    if tool_name in INTERACTIVE_TOOLS:
        return GuardDecision(False, _NO_HUMAN_REASON.format(tool=tool_name))

    # 1. Reviewer / read-only mode: block ALL writes, and the polling tools that
    #    let a planner invent a busy-wait loop instead of doing its job.
    if readonly and tool_name in WRITE_TOOLS:
        return GuardDecision(False, f"read-only session: {tool_name} blocked")
    if readonly and tool_name in BACKGROUND_TOOLS:
        return GuardDecision(False, _NO_POLLING_REASON.format(tool=tool_name))

    # 1b. Phase C: unbounded reads of huge files are redirected, not denied.
    # ONLY in the coder loop (readonly reviewer/researcher sessions are one-shot
    # — no re-read cost; the researcher is the agent that ABSORBS big reads) and
    # NOT for data/config files that genuinely need whole-file context (review #6).
    if (not readonly and tool_name == "Read"
            and not tool_input.get("limit")):
        path = str(tool_input.get("file_path") or "")
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        n = _line_count(path)
        if n > _READ_LINES_BUDGET and ext not in _WHOLE_READ_OK_EXTS:
            return GuardDecision(
                False,
                f"{path} is {n} lines — reading it whole puts all of it in "
                f"context and RE-SENDS it on every remaining turn. Read the "
                f"part you need (offset/limit), or Grep for the symbol first. "
                f"Whole-file reads are fine under {_READ_LINES_BUDGET} lines.")

    # 2. Writes to forbidden paths.
    if tool_name in WRITE_TOOLS:
        path = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or ""
        )
        if path and _path_forbidden(str(path), forbidden_paths):
            return GuardDecision(False, f"write to forbidden path blocked: {path}")
        if path and str(path).rstrip("/").endswith((".no_human.yml", ".no_human.yaml")):
            return GuardDecision(
                False,
                ".no_human.yml is the operator's contract with the agent — "
                "the agent never edits it. Propose the change in the PR body "
                "instead.")

    # 3. Shell command policy.
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        if readonly and (_GIT_WRITE.search(cmd) or _FORGE_WRITE.search(cmd)):
            return GuardDecision(
                False,
                f"read-only session: git/forge write blocked: {cmd}. Read the "
                "repo and report; you do not change it.",
            )
        if _RM_TESTS.search(cmd):
            return GuardDecision(
                False,
                f"deleting test files is blocked at tool time: {cmd}. Renames "
                "go through `git mv`; a genuine removal needs the human "
                "(state it in the PR body) — the tamper gate would fail this "
                "attempt at the end anyway, so this denial saves you the "
                "attempt.")
        if _NO_HUMAN_YML_WRITE.search(cmd):
            return GuardDecision(
                False,
                ".no_human.yml is the operator's contract with the agent — "
                "the agent never edits it.")
        if _RM_RF.search(cmd):
            return GuardDecision(False, f"destructive command blocked (rm -rf): {cmd}")
        if _GIT_DESTRUCTIVE.search(cmd):
            return GuardDecision(False, f"destructive git command blocked: {cmd}")
        if _FORGE_MERGE.search(cmd):
            return GuardDecision(
                False,
                "merging a pull/merge request is blocked — the agent never "
                "merges. Open the PR, push your fixes to its branch, and stop. "
                "A human merges it (`nh approve`).",
            )
        if re.search(r"\bgit\s+push\b", cmd) and _push_targets_protected(
            cmd, never_push_to
        ):
            return GuardDecision(
                False,
                f"push to protected branch blocked: {cmd}. Push to your own "
                "branch and open a PR instead — pushing to the base branch is "
                "merging without review.",
            )

    return GuardDecision(True)
