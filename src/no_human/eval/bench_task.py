"""North-star benchmark task specs (plan i-need-yo-to-wobbly-falcon, Task A2).

A ``BenchTask`` freezes ONE real historical conversation into a replayable task
spec. The no-cheating rule is structural: the builder copies ONLY the initial
user request into the spec — the rest of the source session is consumed solely
for the ``original`` economics block (tokens/duration/corrections), never for
content. Optional hand-curated fields (acceptance criteria, held-out tests) are
leak-linted against the source session's assistant text so a curator cannot
smuggle the original solution into the spec.

Specs live as YAML under ``eval/northstar_tasks/`` (git-tracked, PR-reviewed
like golden tasks). The runner (Task A3) replays them through the real
Orchestrator in a push-proof sandbox.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..history.extractor import Transcript

NORTHSTAR_DIR = Path(__file__).resolve().parents[3] / "eval" / "northstar_tasks"
# The raw built corpus contains VERBATIM operator conversations (titles,
# requests, real repo paths — personal and enterprise). It is never committed:
# `generated/` is gitignored. Only hand-curated specs (the core subset, moved
# up into eval/northstar_tasks/ and PR-reviewed like golden tasks) are tracked.
GENERATED_DIR = NORTHSTAR_DIR / "generated"

_WORD = re.compile(r"[a-z0-9_]+")


@dataclass
class BenchTask:
    id: str
    title: str
    request: str                       # verbatim initial user message ONLY
    source: dict[str, Any] = field(default_factory=dict)
    repo: dict[str, Any] = field(default_factory=dict)   # {path, pin, branch}
    original: dict[str, Any] = field(default_factory=dict)
    acceptance_criteria: list[str] = field(default_factory=list)
    holdout: str = ""
    subset: str = "full"               # "core" | "full"
    runnable: bool = True
    skip_reason: str = ""
    expect_escalation: bool = False
    path: Path | None = None

    @staticmethod
    def from_dict(data: dict[str, Any], *, path: Path | None = None) -> "BenchTask":
        return BenchTask(
            id=data["id"],
            title=data["title"],
            request=data.get("request", ""),
            source=dict(data.get("source", {}) or {}),
            repo=dict(data.get("repo", {}) or {}),
            original=dict(data.get("original", {}) or {}),
            acceptance_criteria=list(data.get("acceptance_criteria", []) or []),
            holdout=data.get("holdout", "") or "",
            subset=data.get("subset", "full"),
            runnable=bool(data.get("runnable", True)),
            skip_reason=data.get("skip_reason", "") or "",
            expect_escalation=bool(data.get("expect_escalation", False)),
            path=path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "request": self.request,
            "source": self.source, "repo": self.repo, "original": self.original,
            "acceptance_criteria": self.acceptance_criteria,
            "holdout": self.holdout, "subset": self.subset,
            "runnable": self.runnable, "skip_reason": self.skip_reason,
            "expect_escalation": self.expect_escalation,
        }


def load_bench_tasks(directory: Path = NORTHSTAR_DIR, *,
                     subset: str | None = None) -> list[BenchTask]:
    directory = Path(directory)
    tasks: list[BenchTask] = []
    if not directory.exists():
        return tasks
    for p in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        data = yaml.safe_load(p.read_text()) or {}
        if data:
            tasks.append(BenchTask.from_dict(data, path=p))
    if subset:
        tasks = [t for t in tasks if t.subset == subset]
    return sorted(tasks, key=lambda t: t.id)


# ----------------------------- leak lint ---------------------------------- #

def _ngrams(text: str, n: int = 6) -> set[tuple[str, ...]]:
    words = _WORD.findall((text or "").lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def find_leaks(spec: BenchTask, assistant_text: str, *, n: int = 6) -> list[str]:
    """Curated fields must not share an n-gram with the source session's
    assistant output — that would smuggle the original solution into the
    benchmark. Returns the offending fields (empty = clean). The request
    itself is exempt: it is user-authored by definition."""
    reference = _ngrams(assistant_text, n)
    if not reference:
        return []
    leaks: list[str] = []
    for label, text in (
        ("acceptance_criteria", "\n".join(spec.acceptance_criteria)),
        ("holdout", spec.holdout),
    ):
        if _ngrams(text, n) & reference:
            leaks.append(label)
    return leaks


# ------------------------------ builder ----------------------------------- #

def _first_request(t: Transcript) -> str:
    for m in t.messages:
        if m.role == "user" and m.content.strip():
            return m.content
    return ""


def _pin_for(repo_path: Path, branch: str, started: str) -> str:
    """Best-effort commit pin: the tip of ``branch`` as of the session start.
    Empty string when unresolvable (caller decides HEAD-fallback vs skip)."""
    if not started:
        return ""
    try:
        out = subprocess.run(
            ["git", "rev-list", "-1", f"--before={started}",
             branch or "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def build_bench_tasks(
    transcripts: list[Transcript], *, out_dir: Path = GENERATED_DIR,
) -> list[Path]:
    """Turn transcripts into spec YAMLs. Structural no-leak guarantee: only
    ``_first_request`` (the initial user message) is copied; assistant text
    never reaches the spec. Dedupes resumed/continued sessions by the hash of
    that first request. Non-runnable conversations are still written (with
    ``runnable: false`` + reason) so coverage accounting stays honest."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    seen_requests: set[str] = set()

    for t in transcripts:
        request = _first_request(t)
        if not request.strip():
            continue
        req_hash = hashlib.sha256(request.strip().encode()).hexdigest()
        if req_hash in seen_requests:
            continue  # resumed/continued session — same task
        seen_requests.add(req_hash)

        tid = f"ns-{hashlib.sha256(t.cascade_id.encode()).hexdigest()[:8]}"
        # Claude Code carries cwd; Windsurf/Devin carry workspace URIs — the
        # original 89-conversation corpus is ALL workspaces-shaped.
        cwd = getattr(t, "cwd", "") or ""
        if not cwd:
            from ..history.analyzer import _project_from_workspaces
            cwd = _project_from_workspaces(getattr(t, "workspaces", []) or [])
        branch = getattr(t, "git_branch", "") or ""
        runnable, skip_reason, pin = True, "", ""

        repo_path = Path(cwd) if cwd else None
        if repo_path is None or not repo_path.is_dir():
            runnable, skip_reason = False, f"repo missing: {cwd or '(unknown cwd)'}"
        elif not (repo_path / ".git").exists():
            runnable, skip_reason = False, f"not a git repo: {cwd}"
        else:
            pin = _pin_for(repo_path, branch, getattr(t, "started", ""))
            if not pin:
                pin = "HEAD"  # advisory fallback; curator may flip runnable

        started = getattr(t, "started", "") or ""
        ended = getattr(t, "ended", "") or ""
        wall = 0.0
        if started and ended:
            from datetime import datetime
            try:
                _iso = "%Y-%m-%dT%H:%M:%S.%f%z"
                s = datetime.strptime(started.replace("Z", "+0000"), _iso)
                e = datetime.strptime(ended.replace("Z", "+0000"), _iso)
                wall = max(0.0, (e - s).total_seconds())
            except ValueError:
                wall = 0.0

        # Title from the REQUEST (user-authored), never from the transcript
        # title — auto-generated titles summarize the whole conversation and
        # can encode the eventual solution approach (review finding: an
        # unaudited leak channel straight into the coder's Task).
        req_title = " ".join(request.strip().split())[:100]
        spec = BenchTask(
            id=tid,
            title=req_title,
            request=request,
            source={
                "kind": ("windsurf" if getattr(t, "source", "") == "windsurf"
                         else "claude_code"),
                "session": t.cascade_id,
                "label": getattr(t, "source", "") or "",
            },
            repo={"path": cwd, "pin": pin, "branch": branch},
            original={
                "tokens": dict(getattr(t, "usage", {}) or {}),
                "wall_clock_s": round(wall, 1),
                "user_messages": sum(1 for m in t.messages if m.role == "user"),
                "corrections": int(getattr(t, "corrections", 0) or 0),
            },
            runnable=runnable,
            skip_reason=skip_reason,
        )
        p = out_dir / f"{tid}.yaml"
        p.write_text(yaml.safe_dump(spec.to_dict(), sort_keys=False,
                                    allow_unicode=True, width=100))
        written.append(p)
    return written
