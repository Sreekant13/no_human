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

# Lines that are plumbing, not real human input — never treat as rules.
_NOISE_PREFIXES = (
    "<local-command-caveat>", "<command-name>", "<command-message>",
    "<command-args>", "<command-stdout>", "<command-stderr>",
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


def _parse_session(path: Path) -> Transcript | None:
    """Parse one session JSONL into a Transcript (or None if too thin/noisy)."""
    messages: list[Message] = []
    created = ""
    title = ""
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
                text = _text_from_content(msg.get("content")).strip()
                if role == "user" and _is_noise(text):
                    continue
                if not text:
                    continue
                if not created:
                    created = o.get("timestamp", "")
                if role == "user" and not title:
                    title = text.replace("\n", " ")[:80]
                messages.append(Message(role=role, content=text, step_type=t))
    except OSError:
        return None

    user_msgs = [m for m in messages if m.role == "user"]
    if len(user_msgs) < 2:
        return None  # trivial/probe session — nothing to learn
    return Transcript(
        cascade_id=f"cc:{path.stem}",
        title=title or f"Claude Code session {path.stem[:8]}",
        created=created,
        messages=messages,
        step_count=len(messages),
    )


def extract_claude_code_transcripts(
    *, days: int = 30, limit: int = 80, root: Path | None = None,
) -> list[Transcript]:
    """Read recent Claude Code sessions into Transcripts.

    Bounded: only sessions modified within ``days``, the ``limit`` most recent,
    and skips throwaway sessions whose project path is a temp dir (agent/CI
    worktrees, not the user's real work)."""
    base = root or CLAUDE_PROJECTS
    if not base.is_dir():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    files: list[Path] = []
    for proj in base.iterdir():
        if not proj.is_dir():
            continue
        name = proj.name.lower()
        if "tmp" in name or "scratchpad" in name or "worktree" in name:
            continue  # throwaway agent/CI sessions, not the user's history
        for f in proj.glob("*.jsonl"):
            try:
                if f.stat().st_mtime >= cutoff:
                    files.append(f)
            except OSError:
                continue
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    out: list[Transcript] = []
    for f in files[:limit]:
        tr = _parse_session(f)
        if tr is not None:
            out.append(tr)
    log.info("Claude Code: %d transcripts from %d recent session files",
             len(out), min(len(files), limit))
    return out
