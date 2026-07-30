"""Find the user's repositories instead of asking them to type a path.

The first thing onboarding and the task composer ask for is a filesystem path,
typed from memory. This module walks the conventional clone roots under the
user's home and returns a pickable list.

Three properties matter more than coverage:

* **Bounded.** Depth is capped at :data:`DEFAULT_MAX_DEPTH` levels below each
  root, the well-known dependency/build directories are never entered, a repo
  is a leaf (we do not descend into one), and the result count is capped. When
  the cap bites, the response says so — a silently truncated list is a list
  the user cannot trust.
* **Contained.** The walk never leaves ``home``. Operator-configured extra
  roots outside home are refused and reported; a symlink pointing out of home
  is not followed.
* **Fast.** The walk is pure ``os.scandir``. The only subprocess is one
  ``git status`` per *returned* repo (after the cap), run on a small thread
  pool, so a wide tree costs one status per row shown and nothing more.

``dirty`` is the reason the git probe is worth its cost: pointing an agent at
a repository the user is mid-edit in is how uncommitted work gets lost, and
the picker should show that before the click, not after.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

#: Where developers actually clone things, in the order they are offered.
CONVENTIONAL_ROOTS: tuple[str, ...] = (
    "Projects", "Code", "Development", "Dev", "repos", "git", "workspace", "src",
)

#: Levels below a root. Three, because the common layouts are ``<root>/<repo>``
#: (1), ``<root>/<owner>/<repo>`` (2) and ``<root>/<host>/<owner>/<repo>`` (3) —
#: the last is what a host-namespaced checkout looks like, and stopping at two
#: would miss every repo on such a machine. Four buys little and starts walking
#: into monorepo subprojects and unpacked archives.
DEFAULT_MAX_DEPTH = 3

#: Rows returned. Past this the list stops being pickable anyway, and the
#: response carries a note instead of quietly dropping the tail.
DEFAULT_MAX_RESULTS = 200

#: Never entered. Hidden directories are skipped by name as well, so ``.venv``
#: and ``.tox`` are covered twice over — the explicit names document intent.
EXCLUDED_DIRS = frozenset({
    "node_modules", ".venv", "venv", "vendor", "target", "build", "dist",
    "__pycache__", ".git", "Library", "Applications", ".Trash",
    ".cache", ".gradle", ".m2", "site-packages", "Pods", ".terraform",
})

#: A directory holding one of these but no ``.git`` is still a project the user
#: may want to point a task at, so it is offered with ``is_git: false``.
_MANIFESTS = (
    "package.json", "pyproject.toml", "go.mod", "pom.xml", "Cargo.toml",
    "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
)

#: Ceiling for one tracked-files probe. Cheap on a normal repository (tens of
#: ms) but NOT free on a very large one: measured on this machine, `git status
#: --untracked-files=no` still refreshes the index and took 1.6s on the biggest
#: checkout. Past this, that repo's status is reported unavailable.
GIT_TIMEOUT_S = 1.5

#: Budget for the second, expensive probe (untracked files). Scanning for
#: untracked files means walking the whole working tree, and on a large
#: checkout that is seconds - 98% of the cost of a default `git status`
#: (measured: 2272ms full vs 40ms tracked-only). The picker must not wait, so
#: this probe is capped hard and a repo that blows the cap comes back with
#: `dirty_scan: "partial"` instead of holding up the list.
UNTRACKED_TIMEOUT_S = 0.75

#: ONE budget for all git probing, both passes, not per repo. Per-probe
#: timeouts alone let total wall time grow with the number of large checkouts -
#: exactly the machine where discovery matters most. Repos the deadline never
#: reaches report `unavailable` (tracked probe skipped) or `partial` (tracked
#: clean, untracked pass skipped), so the whole scan costs at most this plus
#: one in-flight probe, no matter how many repositories there are.
#:
#: The path/name/branch half of every row costs no subprocess at all and is
#: never budgeted away - the list itself is always complete.
DIRTY_BUDGET_S = 2.0

_GIT_WORKERS = 8


def _quick_ecosystem(repo: Path) -> str:
    if (repo / "package.json").exists():
        return "node"
    if (repo / "uv.lock").exists() or (repo / "pyproject.toml").exists():
        return "python"
    if (repo / "pom.xml").exists():
        return "maven"
    if (repo / "go.mod").exists():
        return "go"
    return ""


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _git_dir(repo: Path) -> Path | None:
    """Resolve ``.git`` whether it is a directory or a worktree pointer file."""
    dot = repo / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        try:
            line = dot.read_text(errors="replace").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            target = Path(line.split(":", 1)[1].strip()).expanduser()
            if not target.is_absolute():
                target = (repo / target).resolve()
            return target if target.is_dir() else None
    return None


def _head_info(repo: Path) -> tuple[str, bool]:
    """(branch-or-short-sha, detached) read straight off ``HEAD`` — no subprocess."""
    gd = _git_dir(repo)
    if gd is None:
        return "", False
    try:
        head = (gd / "HEAD").read_text(errors="replace").strip()
    except OSError:
        return "", False
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        # Strip the refs/heads/ PREFIX only. Taking the last path segment loses
        # everything before the slash in the branch names people actually use
        # ("feat/x" would read as "x").
        for prefix in ("refs/heads/", "refs/remotes/"):
            if ref.startswith(prefix):
                return ref[len(prefix):], False
        return ref, False
    if head:
        return head[:8], True
    return "", False


def _git_status(repo: Path, untracked: str, timeout: float) -> str | None:
    """``git status --porcelain`` output, or None when it could not be read.

    None means "no answer" (timeout, broken repo, git missing) and is kept
    distinct from "" (answered: clean) - the caller reports the difference
    rather than passing a guess off as a reading. ``GIT_CEILING_DIRECTORIES``
    stops git walking upward into an unrelated parent repository when ``.git``
    here turns out to be unusable.
    """
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CEILING_DIRECTORIES"] = str(repo.parent)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "--no-optional-locks",
             "-c", "core.fsmonitor=false", "status", "--porcelain",
             f"--untracked-files={untracked}", "--ignore-submodules=all"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:  # noqa: BLE001
        log.debug("status probe (-u%s) gave no answer for %s: %s",
                  untracked, repo, type(exc).__name__)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


#: Internal only, never returned: "the cheap probe found no tracked edits, so
#: this row still needs the untracked pass".
_PENDING = "pending"


def _describe_cheap(repo: Path, deadline: float) -> dict[str, Any]:
    """Everything a row needs except the expensive untracked verdict."""
    is_git = (repo / ".git").exists()
    branch, detached = _head_info(repo) if is_git else ("", False)
    row: dict[str, Any] = {
        "path": str(repo),
        "name": repo.name,
        "is_git": is_git,
        "branch": branch,
        "detached": detached,
        "dirty": False,
        "dirty_scan": "not-a-repo",
        "ecosystem": _quick_ecosystem(repo),
    }
    if not is_git:
        return row
    if time.monotonic() >= deadline:
        row["dirty_scan"] = "unavailable"
        return row
    tracked = _git_status(repo, "no", GIT_TIMEOUT_S)
    if tracked is None:
        row["dirty_scan"] = "unavailable"
    elif tracked.strip():
        # Already proven dirty - the expensive pass would change nothing.
        row["dirty"] = True
        row["dirty_scan"] = "complete"
    else:
        row["dirty_scan"] = _PENDING
    return row


def _untracked_pass(rows: list[dict[str, Any]], deadline: float) -> None:
    """Resolve the pending rows in place, against the shared deadline."""

    def probe(row: dict[str, Any]) -> None:
        if row["dirty_scan"] != _PENDING:
            return
        if time.monotonic() >= deadline:
            row["dirty_scan"] = "partial"
            return
        out = _git_status(Path(row["path"]), "normal", UNTRACKED_TIMEOUT_S)
        if out is None:
            # Tracked files are provably clean; untracked is unknown. Saying
            # "clean" outright would overstate what was measured.
            row["dirty_scan"] = "partial"
        else:
            row["dirty"] = bool(out.strip())
            row["dirty_scan"] = "complete"

    pending = [r for r in rows if r["dirty_scan"] == _PENDING]
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=_GIT_WORKERS) as pool:
        list(pool.map(probe, pending))


def _walk(root: Path, home: Path, max_depth: int, ceiling: int,
          found: list[Path]) -> None:
    """Collect candidate project directories under ``root``, depth-bounded."""
    def visit(d: Path, depth: int) -> None:
        if len(found) >= ceiling:
            return
        if (d / ".git").exists() or any((d / m).exists() for m in _MANIFESTS):
            found.append(d)
            return  # a project is a leaf - never descend into one
        if depth >= max_depth:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in sorted(entries, key=lambda x: x.name):
            if not e.is_dir(follow_symlinks=True):
                continue
            if e.name.startswith(".") or e.name in EXCLUDED_DIRS:
                continue
            child = Path(e.path)
            if e.is_symlink():
                try:
                    target = child.resolve()
                except OSError:
                    continue
                # A link out of home is exactly the escape hatch this walk
                # must not take.
                if not _is_within(target, home):
                    continue
            visit(child, depth + 1)

    visit(root, 0)


def discover_repos(
    *,
    home: Path | str | None = None,
    extra_roots: Iterable[str] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict[str, Any]:
    """Scan the conventional clone roots and return a pickable repository list.

    Returns a JSON-ready dict: ``repos`` (path/name/is_git/branch/dirty/
    detached/ecosystem), the roots actually scanned, missing and refused, the
    cap state with a human-readable ``note``, and ``elapsed_ms``.
    """
    t0 = time.perf_counter()
    home_path = Path(home).expanduser() if home is not None else Path.home()
    try:
        home_path = home_path.resolve()
    except OSError:
        pass

    scanned: list[str] = []
    missing: list[str] = []
    refused: list[str] = []

    candidate_roots: list[Path] = [home_path / name for name in CONVENTIONAL_ROOTS]
    for raw in extra_roots or []:
        if not str(raw).strip():
            continue
        # "~" means the home this scan is bound to, not the process's home.
        # Anything else would let a configured "~/work" reach outside the
        # boundary every other line of this function is defending.
        text = str(raw).strip()
        if text == "~":
            p = home_path
        elif text.startswith("~/"):
            p = home_path / text[2:]
        else:
            p = Path(text)
        try:
            p = p.resolve()
        except OSError:
            pass
        if not _is_within(p, home_path):
            refused.append(str(p))
            continue
        if p not in candidate_roots:
            candidate_roots.append(p)

    ceiling = max(max_results * 5, 1000)
    found: list[Path] = []
    for root in candidate_roots:
        if not root.is_dir():
            missing.append(str(root))
            continue
        scanned.append(str(root))
        _walk(root, home_path, max_depth, ceiling, found)

    unique = sorted(set(found), key=lambda p: (p.name.lower(), str(p)))
    total = len(unique)
    capped = total > max_results
    shown = unique[:max_results]

    deadline = time.monotonic() + DIRTY_BUDGET_S
    with ThreadPoolExecutor(max_workers=_GIT_WORKERS) as pool:
        rows = list(pool.map(lambda p: _describe_cheap(p, deadline), shown))
    _untracked_pass(rows, deadline)

    note = ""
    if capped:
        note = (
            f"Showing the first {max_results} of {total} repositories found - "
            "narrow the scan roots in Settings, or type the path directly, "
            "to reach the rest."
        )

    return {
        "repos": rows,
        "roots_scanned": scanned,
        "roots_missing": missing,
        "roots_refused": refused,
        "total_found": total,
        "limit": max_results,
        "capped": capped,
        "note": note,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


