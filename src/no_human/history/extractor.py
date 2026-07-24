"""Extract exact conversation transcripts from the Windsurf language server.  # term-ok: real IDE names

The IDE runs a ConnectRPC server (``exa.language_server_pb.LanguageServerService``)
inside a Go ``language_server_*`` process. We:

  1. Find the process via ``psutil``, read ``WINDSURF_CSRF_TOKEN`` from its env.  # term-ok: real third-party env var
  2. Probe its listening ports to find the ConnectRPC endpoint.
  3. Call ``GetAllCascadeTrajectories`` → list of conversations.
  4. Call ``GetCascadeTrajectorySteps`` per conversation → full step data.
  5. Extract user/assistant messages from the steps.

The result is a list of ``Transcript`` dataclasses — pure data, no file I/O.
Writing to markdown or feeding into the learning queue is the caller's concern.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("no_human.history")

SERVICE = "exa.language_server_pb.LanguageServerService"


@dataclass
class Message:
    role: str          # "user" or "assistant"
    content: str
    step_type: str     # original CORTEX_STEP_TYPE_*


@dataclass
class Transcript:
    cascade_id: str
    title: str
    created: str       # ISO 8601
    messages: list[Message] = field(default_factory=list)
    step_count: int = 0
    workspaces: list[str] = field(default_factory=list)
    # Original-session economics (north-star benchmark; populated by the
    # Claude Code extractor, empty/zero for sources that can't provide them).
    usage: dict[str, int] = field(default_factory=dict)  # 4 token buckets
    started: str = ""       # ISO 8601 first message timestamp
    ended: str = ""         # ISO 8601 last message timestamp
    cwd: str = ""           # working directory the session ran in
    git_branch: str = ""    # branch checked out at session time
    source: str = ""        # "cc:enterprise" | "cc:personal" | "windsurf" | ""  # term-ok: internal source tag names the real IDE
    corrections: int = 0    # non-noise user messages after the first — the
                            # babysitting signal the benchmark compares against


class IDENotRunningError(Exception):
    """Raised when no Windsurf language server process is found."""  # term-ok: real IDE names


class LanguageServerClient:
    """Thin ConnectRPC/JSON client for the local language server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 csrf_token: str = ""):
        self.host = host
        self.port = port
        self.csrf_token = csrf_token

    def call(self, method: str, body: dict[str, Any] | None = None) -> dict:
        url = f"http://{self.host}:{self.port}/{SERVICE}/{method}"
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": self.csrf_token,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            raise ConnectionError(
                f"{method} on {self.host}:{self.port} failed: {exc}"
            ) from exc


def _discover_client() -> LanguageServerClient:
    """Auto-discover the language server's ConnectRPC port and CSRF token.

    Iterates over all ``language_server_*`` processes, reads the
    ``WINDSURF_CSRF_TOKEN`` env var, then probes each LISTEN port until  # term-ok: real third-party env var
    ``GetAllCascadeTrajectories`` responds.
    """
    try:
        import psutil
    except ImportError:
        raise ImportError(
            "psutil is required for IDE history extraction. It is a declared "
            "dependency, so this environment is stale — reinstall no_human "
            "(uv tool install --force --editable .)"
        )

    candidates: list[tuple[str, int, str]] = []  # (host, port, token)

    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if "language_server" not in name and "language_" not in name:
            continue
        try:
            env = proc.environ()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        token = env.get("WINDSURF_CSRF_TOKEN")  # term-ok: real third-party env var
        if not token:
            continue
        try:
            conns = proc.net_connections()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        for conn in conns:
            if conn.status == "LISTEN" and conn.laddr:
                candidates.append((conn.laddr.ip, conn.laddr.port, token))

    if not candidates:
        raise IDENotRunningError(
            "No Windsurf language server found. Is the IDE running?"  # term-ok: real IDE names
        )

    for host, port, token in candidates:
        client = LanguageServerClient(host=host, port=port, csrf_token=token)
        try:
            result = client.call("GetAllCascadeTrajectories")
            if "trajectorySummaries" in result:
                log.info("connected to language server on %s:%d", host, port)
                return client
        except Exception:
            continue

    raise IDENotRunningError(
        f"Found {len(candidates)} candidate ports but none responded to "
        "GetAllCascadeTrajectories. The IDE may be starting up."
    )


def _discover_all_clients() -> list[LanguageServerClient]:
    """Discover ALL responding language servers across all IDE windows.

    Each Windsurf IDE window runs its own ``language_server_*`` process  # term-ok: real IDE names
    with a unique CSRF token. To collect conversations from every window, we
    iterate all language_server processes and return every client that responds
    to ``GetAllCascadeTrajectories``.

    Returns at least one client or raises ``IDENotRunningError``.
    """
    try:
        import psutil
    except ImportError:
        raise ImportError(
            "psutil is required for IDE history extraction. It is a declared "
            "dependency, so this environment is stale — reinstall no_human "
            "(uv tool install --force --editable .)"
        )

    # Group candidates by PID → (token, [(host, port), ...])
    pid_candidates: dict[int, tuple[str, list[tuple[str, int]]]] = {}

    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if "language_server" not in name and "language_" not in name:
            continue
        try:
            env = proc.environ()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        token = env.get("WINDSURF_CSRF_TOKEN")  # term-ok: real third-party env var
        if not token:
            continue
        try:
            conns = proc.net_connections()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        ports = [(conn.laddr.ip, conn.laddr.port)
                 for conn in conns if conn.status == "LISTEN" and conn.laddr]
        if ports:
            pid_candidates[proc.pid] = (token, ports)

    if not pid_candidates:
        raise IDENotRunningError(
            "No Windsurf language server found. Is the IDE running?"  # term-ok: real IDE names
        )

    clients: list[LanguageServerClient] = []
    seen_pids: set[int] = set()

    for pid, (token, ports) in pid_candidates.items():
        for host, port in ports:
            client = LanguageServerClient(host=host, port=port, csrf_token=token)
            try:
                result = client.call("GetAllCascadeTrajectories")
                if "trajectorySummaries" in result:
                    log.info("connected to language server pid=%d on %s:%d", pid, host, port)
                    if pid not in seen_pids:
                        clients.append(client)
                        seen_pids.add(pid)
                    break  # one responding port per PID is enough
            except Exception:
                continue

    if not clients:
        total = sum(len(p) for _, p in pid_candidates.values())
        raise IDENotRunningError(
            f"Found {total} candidate ports across {len(pid_candidates)} "
            "process(es) but none responded. The IDE may be starting up."
        )

    log.info("discovered %d language server(s)", len(clients))
    return clients


def _extract_messages(steps: list[dict]) -> list[Message]:
    """Pull user/assistant messages from raw trajectory steps."""
    messages: list[Message] = []
    for step in steps:
        stype = step.get("type", "")

        if stype == "CORTEX_STEP_TYPE_USER_INPUT":
            text = step.get("userInput", {}).get("userResponse", "")
            if text and text.strip():
                messages.append(Message("user", text.strip(), stype))

        elif stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            planner = step.get("plannerResponse", {})
            text = planner.get("response", "")
            if text and text.strip():
                messages.append(Message("assistant", text.strip(), stype))

    return messages


def extract_transcripts(
    *,
    days: int = 30,
    client: LanguageServerClient | None = None,
) -> list[Transcript]:
    """Extract all conversation transcripts from the last ``days`` days.

    Returns a list of ``Transcript`` objects sorted by creation time.
    If *client* is ``None``, auto-discovers ALL local language servers and
    merges conversations from every IDE window (deduped by cascade_id).
    """
    if client is not None:
        clients = [client]
    else:
        clients = _discover_all_clients()

    # Merge summaries from all servers, dedup by cascade_id.
    all_summaries: dict[str, tuple[dict, LanguageServerClient]] = {}
    for c in clients:
        try:
            sums = c.call("GetAllCascadeTrajectories").get("trajectorySummaries", {})
            for cid, summary in sums.items():
                if cid not in all_summaries:
                    all_summaries[cid] = (summary, c)
        except Exception:
            log.warning("failed to list trajectories from %s:%d", c.host, c.port)

    summaries_with_clients = all_summaries
    log.info("found %d total conversations across %d server(s)",
             len(summaries_with_clients), len(clients))

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    transcripts: list[Transcript] = []

    for cid, (summary, src_client) in summaries_with_clients.items():
        created = summary.get("createdTime", "")
        if created:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if dt < cutoff:
                continue

        title = summary.get("summary", "Untitled")
        step_count = summary.get("stepCount", 0)
        workspaces = [
            w.get("workspaceFolderAbsoluteUri", "")
            for w in summary.get("workspaces", [])
        ]

        log.info("fetching %s (%d steps)...", title, step_count)
        try:
            raw_steps = src_client.call(
                "GetCascadeTrajectorySteps", {"cascadeId": cid}
            ).get("steps", [])
        except Exception:
            log.warning("failed to fetch steps for %s, skipping", cid)
            continue

        messages = _extract_messages(raw_steps)
        if not messages:
            continue

        user_msgs = [m for m in messages if m.role == "user"]
        transcripts.append(Transcript(
            cascade_id=cid,
            title=title,
            created=created,
            messages=messages,
            step_count=step_count,
            workspaces=workspaces,
            source="windsurf",  # term-ok: internal source tag names the real IDE
            corrections=max(0, len(user_msgs) - 1),
        ))

    transcripts.sort(key=lambda t: t.created)
    log.info("extracted %d transcripts with messages", len(transcripts))
    return transcripts


# ────────────────────── markdown formatting ──────────────────────

def _sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:80] or "untitled"


def transcript_to_markdown(t: Transcript) -> str:
    """Render a single transcript as a markdown string."""
    lines = [
        f"# {t.title}",
        "",
        f"- **Cascade ID**: `{t.cascade_id}`",
        f"- **Created**: {t.created}",
        f"- **Messages**: {len(t.messages)}",
        "",
        "---",
        "",
    ]
    for i, msg in enumerate(t.messages, 1):
        role_label = "User" if msg.role == "user" else "Cascade"
        lines.append(f"## Message {i} — {role_label}")
        lines.append("")
        if msg.role == "user":
            for line in msg.content.split("\n"):
                lines.append(f"> {line}")
        else:
            lines.append(msg.content)
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def write_transcripts(transcripts: list[Transcript], output_dir: str) -> str:
    """Write transcripts as markdown files and an INDEX.md. Returns the index path."""
    from pathlib import Path

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    index_lines = [
        "# Conversation Transcripts Index\n",
        f"Extracted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        f"Conversations: {len(transcripts)}\n\n",
        "| # | Date | Title | Messages | File |",
        "|---|------|-------|----------|------|",
    ]

    for idx, t in enumerate(transcripts, 1):
        date_str = t.created[:10] if t.created else "unknown"
        fname = f"{date_str}_{_sanitize_filename(t.title)}.md"
        fpath = out / fname
        fpath.write_text(transcript_to_markdown(t), encoding="utf-8")
        index_lines.append(
            f"| {idx} | {date_str} | {t.title} | {len(t.messages)} "
            f"| [{fname}](./{fname}) |"
        )

    index_path = out / "INDEX.md"
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return str(index_path)
