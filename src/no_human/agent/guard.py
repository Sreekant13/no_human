"""PreToolUse safety guard — pure, testable policy (PLAN.md Part 10).

Blocks, before execution:
  - writes/edits to forbidden paths (.env, secrets/, *.key, *.pem, ...)
  - pushes/force-updates to protected branches (never_push_to)
  - destructive shell (`rm -rf`, history rewrites) — a circuit breaker that
    fires even under bypass permissions
  - `git merge` of any kind — the agent never merges (§3.2)

The orchestrator wraps :func:`evaluate` in a Claude Agent SDK PreToolUse hook;
keeping the policy a pure function lets us unit-test it without the SDK.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

# Destructive shell patterns — matched against the full command string.
_RM_RF = re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|-rf|-fr)\b")
_GIT_DESTRUCTIVE = re.compile(
    r"\bgit\s+(push\s+.*--force|push\s+.*-f\b|reset\s+--hard\s+\S|"
    r"clean\s+-[a-z]*f|filter-branch|update-ref\s+-d)"
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


def evaluate(
    tool_name: str,
    tool_input: dict,
    *,
    forbidden_paths: list[str],
    never_push_to: list[str],
    readonly: bool = False,
) -> GuardDecision:
    """Return allow/deny for a single proposed tool call."""
    # 0. Reviewer / read-only mode: block ALL writes unconditionally.
    if readonly and tool_name in WRITE_TOOLS:
        return GuardDecision(False, f"read-only session: {tool_name} blocked")

    # 1. Writes to forbidden paths.
    if tool_name in WRITE_TOOLS:
        path = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or ""
        )
        if path and _path_forbidden(str(path), forbidden_paths):
            return GuardDecision(False, f"write to forbidden path blocked: {path}")

    # 2. Shell command policy.
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        if _RM_RF.search(cmd):
            return GuardDecision(False, f"destructive command blocked (rm -rf): {cmd}")
        if _GIT_DESTRUCTIVE.search(cmd):
            return GuardDecision(False, f"destructive git command blocked: {cmd}")
        if re.search(r"\bgit\s+merge\b", cmd):
            return GuardDecision(
                False, "git merge blocked — the agent never merges (human-only)"
            )
        if re.search(r"\bgit\s+push\b", cmd) and _push_targets_protected(
            cmd, never_push_to
        ):
            return GuardDecision(False, f"push to protected branch blocked: {cmd}")

    return GuardDecision(True)
