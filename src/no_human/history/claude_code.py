"""Extract conversation transcripts from Claude Code's on-disk session logs.

Claude Code stores every session as JSONL under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` — one JSON object per
line. Unlike the Windsurf extractor (which RPCs a running IDE), this reads files
directly, so it works with no IDE running and covers the user's real Claude Code
history (the place they actually give corrections and rules today).

We extract the human's text messages (and assistant text for context), skipping
sidechains, tool plumbing, and local-command noise. The output is the same
``Transcript`` dataclass the analyzer/ingester already consume, so Claude Code
history flows through the identical learning pipeline as Windsurf.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .extractor import Message, Transcript

log = logging.getLogger("no_human.history")

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# The operator's machine holds TWO Claude config dirs: ~/.claude (enterprise)
# and ~/.claude-personal (personal). "All conversations" means both.
DEFAULT_ROOTS = (
    CLAUDE_PROJECTS,
    Path.home() / ".claude-personal" / "projects",
)

# Encoded-cwd project dirs that are machine-generated, not the operator's own
# conversations: no_human's eval/shadow sandboxes, pytest tmp repos, agent
# worktrees, scratchpads, and anything under a temp root. Substrings of the
# ENCODED path (cwd with "/" -> "-"), matched case-insensitively.
_MACHINE_DIR_MARKERS = (
    "tmp", "scratchpad", "worktree",       # original filter (agent/CI throwaways)
    "-var-folders-",                        # macOS $TMPDIR (…/var/folders/…)
    "nh-eval-", "nh-shadow-",               # no_human's own eval/shadow runs
    "pytest-of-",                           # pytest tmp_path factories
)


# no_human's OWN agent sessions (supervisor / coder / planner / intake /
# standalone reviewer / eval judges) often run with cwd = the operator's REAL
# repo, so the directory markers above miss them. Their first "user" message
# is a machine-authored prompt template — a session opening with one of these
# is no_human talking to itself, never the operator, and is corpus poison for
# both the benchmark and the learning miner. Matched against the start of the
# session's FIRST user message.
_MACHINE_PROMPT_PREFIXES = (
    # Sources: no_human's own prompt templates (grep '"You are ' in src/)
    # PLUS the operator's other automation that spawns Claude sessions
    # (incident-monitor's SRE prompts). Keep in sync when adding agent prompts.
    "You are the Supervisor of an autonomous coding agent",
    "You are the Supervisor reviewing an autonomous coding agent",
    "You are implementing a software task",       # "in/on the repo at" variants
    "You are planning an implementation task",
    "You are a relentless intake interviewer",
    "You are a Staff Software Engineer performing",   # independent + per-PR
    "You are an impartial evaluation judge",
    "You are a focused code reviewer",
    "You are a focused codebase researcher",
    "You are a spec quality evaluator",
    "You are diagnosing why an autonomous coding agent",
    "You are documenting the repository at",
    "You are mining a developer",
    "You are onboarding a repository at",
    "You are resuming a previously-blocked task",
    "You are synthesizing the BEST implementation plan",
    "You are an expert SRE analyzing a production",  # issue + incident variants
    "Summarize this context for a developer working on",
    "Fetch the diff for http",   # template emits "Fetch the diff for {url}"
)


def _root_label(root: Path) -> str:
    """Source label from the config dir the projects root lives in."""
    parent = root.parent.name
    if parent == ".claude-personal":
        return "cc:personal"
    if parent == ".claude":
        return "cc:enterprise"
    return f"cc:{parent or 'other'}"

# Lines that are plumbing, not real human input — never treat as rules.
_NOISE_PREFIXES = (
    "<local-command-caveat>", "<command-name>", "<command-message>",
    "<command-args>", "<command-stdout>", "<command-stderr>",
    "<local-command-stdout>", "<local-command-stderr>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>", "Caveat:",
    "[Request interrupted", "<system-reminder>",
)


def _text_from_content(content) -> str:
    """Pull human-readable text out of a message's content (str or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            # skip thinking / tool_use / tool_result — not user-authored prose
        return "\n".join(parts)
    return ""


def _is_noise(text: str) -> bool:
    t = text.lstrip()
    return not t or t.startswith(_NOISE_PREFIXES)


def _parse_session(
    path: Path, *, source: str = "", min_user_msgs: int = 2,
) -> Transcript | None:
    """Parse one session JSONL into a Transcript (or None if too thin/noisy).

    Besides the prose the learning pipeline consumes, this extracts the
    session's original economics for the north-star benchmark: per-bucket
    token usage summed over (non-sidechain) assistant lines, first/last
    message timestamps, the cwd/branch the session ran against, and the
    corrections count (non-noise user messages after the first).
    """
    messages: list[Message] = []
    created = ""
    title = ""
    usage: dict[str, int] = {}
    started = ""
    ended = ""
    cwd = ""
    git_branch = ""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("isSidechain") or o.get("isMeta"):
                    continue  # subagent / injected — not the user's conversation
                t = o.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = o.get("message") or {}
                role = msg.get("role", t)
                if role == "assistant":
                    u = msg.get("usage")
                    if isinstance(u, dict):
                        for bucket in ("input_tokens", "output_tokens",
                                       "cache_read_input_tokens",
                                       "cache_creation_input_tokens"):
                            v = u.get(bucket)
                            if isinstance(v, (int, float)):
                                usage[bucket] = usage.get(bucket, 0) + int(v)
                text = _text_from_content(msg.get("content")).strip()
                if role == "user" and _is_noise(text):
                    continue
                if not text:
                    continue
                ts = o.get("timestamp", "")
                if ts:
                    started = started or ts
                    ended = ts
                if not cwd and o.get("cwd"):
                    cwd = str(o["cwd"])
                if not git_branch and o.get("gitBranch"):
                    git_branch = str(o["gitBranch"])
                if not created:
                    created = ts
                if role == "user" and not title:
                    title = text.replace("\n", " ")[:80]
                messages.append(Message(role=role, content=text, step_type=t))
    except OSError:
        return None

    user_msgs = [m for m in messages if m.role == "user"]
    if len(user_msgs) < min_user_msgs:
        return None  # trivial/probe session — below the caller's floor
    if user_msgs and user_msgs[0].content.lstrip().startswith(
            _MACHINE_PROMPT_PREFIXES):
        return None  # no_human's own agent session, not the operator
    return Transcript(
        cascade_id=f"cc:{path.stem}",
        title=title or f"Claude Code session {path.stem[:8]}",
        created=created,
        messages=messages,
        step_count=len(messages),
        usage=usage,
        started=started,
        ended=ended,
        cwd=cwd,
        git_branch=git_branch,
        source=source,
        corrections=max(0, len(user_msgs) - 1),
    )


def extract_claude_code_transcripts(
    *,
    days: int = 30,
    limit: int = 80,
    roots: list[Path] | tuple[Path, ...] | None = None,
    min_user_msgs: int = 2,
) -> list[Transcript]:
    """Read recent Claude Code sessions into Transcripts.

    Reads EVERY config root (default: ~/.claude AND ~/.claude-personal — the
    operator's enterprise and personal histories). Bounded: only sessions
    modified within ``days``, the ``limit`` most recent overall, and skips
    machine-generated sessions (no_human's own eval/shadow/worktree runs,
    pytest tmp repos, anything under a temp root — see _MACHINE_DIR_MARKERS).
    ``min_user_msgs`` is the substance floor: the learning pipeline keeps the
    historical >=2 (needs a correction to learn from); the benchmark corpus
    passes 1 (a one-shot request is still a real task)."""
    bases = [Path(r) for r in (roots if roots is not None else DEFAULT_ROOTS)]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    files: list[tuple[Path, str]] = []
    for base in bases:
        if not base.is_dir():
            continue
        label = _root_label(base)
        for proj in base.iterdir():
            if not proj.is_dir():
                continue
            name = proj.name.lower()
            if any(marker in name for marker in _MACHINE_DIR_MARKERS):
                continue  # machine-generated, not the operator's history
            for f in proj.glob("*.jsonl"):
                try:
                    if f.stat().st_mtime >= cutoff:
                        files.append((f, label))
                except OSError:
                    continue
    files.sort(key=lambda pair: pair[0].stat().st_mtime, reverse=True)

    out: list[Transcript] = []
    for f, label in files[:limit]:
        tr = _parse_session(f, source=label, min_user_msgs=min_user_msgs)
        if tr is not None:
            out.append(tr)
    log.info("Claude Code: %d transcripts from %d recent session files "
             "across %d roots", len(out), min(len(files), limit), len(bases))
    return out
