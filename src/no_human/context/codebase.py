"""Codebase context via agentic grep/glob + git log (no vector index — PLAN 16#2)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from ..core.task import Task
from .base import ContextChunk, keywords


class CodebaseSource:
    name = "codebase"

    def __init__(self, max_files: int = 8, max_commits: int = 10):
        self.max_files = max_files
        self.max_commits = max_commits

    async def gather(self, task: Task) -> list[ContextChunk]:
        if not task.repo_path or not Path(task.repo_path).exists():
            return []
        return await asyncio.to_thread(self._gather_sync, task)

    def _gather_sync(self, task: Task) -> list[ContextChunk]:
        repo = Path(task.repo_path)
        chunks: list[ContextChunk] = []
        terms = keywords(task)

        file_hits = self._search(repo, terms)
        for path, lines in list(file_hits.items())[: self.max_files]:
            chunks.append(ContextChunk(
                source="codebase",
                title=str(path.relative_to(repo)),
                content="\n".join(lines[:20]),
                ref=str(path),
                score=float(len(lines)),
            ))

        log = self._git_log(repo)
        if log:
            chunks.append(ContextChunk(
                source="codebase", title="recent commits",
                content=log, ref=str(repo), score=0.0,
            ))
        return chunks

    # Vendored / generated trees that pollute relevance — never search these.
    _EXCLUDE_DIRS = (
        ".git", "node_modules", "dist", "build", "target", ".venv", "venv",
        "vendor", "__pycache__", ".idea", ".gradle", "coverage", "out",
    )
    _EXCLUDE_GLOBS = ("*.min.js", "*.map", "*.lock", "*.snap", "*.bundle.js")

    def _search(self, repo: Path, terms: list[str]) -> dict[Path, list[str]]:
        """Return {file: matched lines} ranked by hit count, using rg or grep."""
        if not terms:
            return {}
        pattern = "|".join(t.replace("+", r"\+") for t in terms)
        hits: dict[Path, list[str]] = {}
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-i", "--max-columns", "200",
                   "--max-count", "5", "-e", pattern]
            for d in self._EXCLUDE_DIRS:
                cmd += ["--glob", f"!**/{d}/**"]
            for g in self._EXCLUDE_GLOBS:
                cmd += ["--glob", f"!{g}"]
            cmd.append(str(repo))
        else:
            cmd = ["grep", "-rniE", pattern]
            for d in self._EXCLUDE_DIRS:
                cmd.append(f"--exclude-dir={d}")
            for g in self._EXCLUDE_GLOBS:
                cmd.append(f"--exclude={g}")
            cmd.append(str(repo))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        for line in proc.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            fp = Path(parts[0])
            hits.setdefault(fp, []).append(f"{parts[1]}: {parts[2].strip()[:200]}")
        # rank files by number of matches (most relevant first)
        return dict(sorted(hits.items(), key=lambda kv: len(kv[1]), reverse=True))

    def _git_log(self, repo: Path) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline", "-n", str(self.max_commits)],
            capture_output=True, text=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
