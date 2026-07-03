"""Repo-wiki generator — OpenWiki-inspired, no external dependency.

Spawns a bounded, read-only Agent SDK session (mirroring ``onboard.AgentDeriver``)
that inspects a repo's structure, code, and git history, then produces:

  - ``.no_human/wiki/architecture.md``
  - ``.no_human/wiki/modules.md``
  - ``.no_human/wiki/conventions.md``

A single delimited block is written into the repo's ``CLAUDE.md`` (created if
absent) pointing agents to the wiki — never the full content.  Regeneration
replaces the block atomically; user content outside the delimiters is preserved.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

# Delimiters for the managed block in CLAUDE.md.
_WIKI_BLOCK_START = "<!-- no_human:wiki -->"
_WIKI_BLOCK_END = "<!-- /no_human:wiki -->"

WIKI_DIR = Path(".no_human") / "wiki"
WIKI_FILES = ("architecture.md", "modules.md", "conventions.md")

_CLAUDE_MD_BLOCK = (
    f"{_WIKI_BLOCK_START}\n"
    "## Auto-generated repo wiki\n\n"
    "See `.no_human/wiki/` for architecture, modules, and conventions docs.\n"
    "These files are regenerated automatically — edit with care.\n"
    f"{_WIKI_BLOCK_END}"
)


@dataclass
class WikiResult:
    """Output of a wiki generation run."""
    repo_path: str = ""
    files_written: list[str] = field(default_factory=list)
    commit_sha: str = ""
    error: str = ""


PROMPT = (
    "You are documenting the repository at {repo}. Do NOT modify anything — "
    "only read files (grep/glob/read).\n\n"
    "Inspect the repo's structure, key source files, configuration, CI, tests, "
    "README, and recent git history (git log -20 --oneline). Produce a concise "
    "developer wiki with three sections.\n\n"
    "Respond with ONLY a fenced ```json block:\n"
    '{{"architecture": "<markdown content for architecture.md — '
    'high-level design, major components, data flow, key abstractions>", '
    '"modules": "<markdown content for modules.md — '
    'directory layout, what each top-level module/package does, entry points>", '
    '"conventions": "<markdown content for conventions.md — '
    'coding style, testing patterns, naming, CI/CD, contribution rules>"}}'
)


class WikiGenerator:
    """Generate repo wiki docs via a bounded Agent SDK session.

    Mirrors ``onboard.AgentDeriver``: read-only, turn-bounded, structured JSON
    output parsed from a fenced block.
    """

    def __init__(self, backend: Any, *, max_turns: int = 12):
        self.backend = backend
        self.max_turns = max_turns

    async def generate(self, repo_path: str | Path) -> WikiResult:
        """Run the wiki generation session and write files."""
        repo = Path(repo_path).expanduser().resolve()
        if not repo.is_dir():
            return WikiResult(repo_path=str(repo), error=f"not a directory: {repo}")

        commit_sha = _git_head(repo)

        result = await self.backend.run(
            PROMPT.format(repo=repo),
            cwd=repo,
            max_turns=self.max_turns,
            effort="low",
        )

        text = result.final_text or ""
        parsed = _parse_wiki_json(text)
        if parsed is None:
            return WikiResult(
                repo_path=str(repo),
                commit_sha=commit_sha,
                error="failed to parse wiki JSON from agent output",
            )

        written = _write_wiki_files(repo, parsed)
        _update_claude_md(repo)

        return WikiResult(
            repo_path=str(repo),
            files_written=written,
            commit_sha=commit_sha,
        )


# --------------------------------------------------------------------------- #
# Pure helpers (testable without an Agent SDK backend)                         #
# --------------------------------------------------------------------------- #


def _parse_wiki_json(text: str) -> dict[str, str] | None:
    """Extract the wiki JSON from agent output."""
    blocks = _JSON_BLOCK.findall(text)
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Require at least one of the expected keys.
    if not any(k in data for k in ("architecture", "modules", "conventions")):
        return None
    return data


def _write_wiki_files(repo: Path, data: dict[str, str]) -> list[str]:
    """Write wiki markdown files to ``<repo>/.no_human/wiki/``."""
    wiki_dir = repo / WIKI_DIR
    wiki_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in WIKI_FILES:
        key = name.removesuffix(".md")
        content = data.get(key, "")
        if not content:
            continue
        path = wiki_dir / name
        path.write_text(content if content.endswith("\n") else content + "\n")
        written.append(str(path.relative_to(repo)))
    return written


def _update_claude_md(repo: Path) -> None:
    """Insert or replace the wiki pointer block in ``CLAUDE.md``."""
    claude_md = repo / "CLAUDE.md"
    if claude_md.exists():
        text = claude_md.read_text()
    else:
        text = ""
    new_text = upsert_wiki_block(text)
    claude_md.write_text(new_text)


def upsert_wiki_block(text: str) -> str:
    """Replace or append the ``<!-- no_human:wiki -->`` block.

    Preserves all user content outside the delimiters. On the first run,
    appends the block. On subsequent runs, replaces the existing block
    in place — never duplicates.
    """
    if _WIKI_BLOCK_START in text:
        # Replace existing block.
        pattern = re.compile(
            re.escape(_WIKI_BLOCK_START) + r".*?" + re.escape(_WIKI_BLOCK_END),
            re.DOTALL,
        )
        return pattern.sub(_CLAUDE_MD_BLOCK, text, count=1)
    # Append (with a blank line separator if the file has content).
    if text and not text.endswith("\n"):
        text += "\n"
    if text:
        text += "\n"
    text += _CLAUDE_MD_BLOCK + "\n"
    return text


def _git_head(repo: Path) -> str:
    """Return HEAD SHA or empty string."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=repo,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
