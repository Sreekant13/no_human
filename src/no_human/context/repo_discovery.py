"""Discover local git repositories under well-known roots.

Scans ``~/git/`` (depth 2) for directories containing ``.git``, returning a
``{name: path}`` mapping the orchestrator injects into compact instructions so
the agent knows which repos are available locally (C1, R1.3).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("no_human.context")

_DEFAULT_ROOTS = (Path.home() / "git",)
_MAX_REPOS = 50
_MAX_DEPTH = 2


def discover_local_repos(
    *,
    roots: tuple[Path, ...] = _DEFAULT_ROOTS,
    max_depth: int = _MAX_DEPTH,
    max_repos: int = _MAX_REPOS,
) -> dict[str, str]:
    """Return ``{repo_name: absolute_path}`` for local git repos.

    Uses ``os.scandir`` for performance. Stops after *max_repos* to bound the
    compact-instructions section size.
    """
    found: dict[str, str] = {}
    for root in roots:
        if not root.is_dir():
            continue
        _scan(root, 0, max_depth, found, max_repos)
        if len(found) >= max_repos:
            break
    log.info("discovered %d local repos", len(found))
    return found


def _scan(
    directory: Path,
    depth: int,
    max_depth: int,
    found: dict[str, str],
    max_repos: int,
) -> None:
    """Recursively scan *directory* up to *max_depth* for git repos."""
    if depth > max_depth or len(found) >= max_repos:
        return
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except (OSError, PermissionError):
        return
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        child = Path(entry.path)
        if (child / ".git").is_dir():
            found[child.name] = str(child)
            if len(found) >= max_repos:
                return
        elif depth < max_depth:
            _scan(child, depth + 1, max_depth, found, max_repos)
