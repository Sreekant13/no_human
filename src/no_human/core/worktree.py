"""Centralized worktree teardown, and the startup janitor that reclaims what a
missed teardown left behind.

``run_task``'s ``finally`` block used to remove a worktree with a bare
``main_repo.remove_worktree`` call and nothing that checked whether it worked.
``git worktree remove`` runs with ``check=False`` (``vcs/git.py``), so a
failure — a dirty tree, a stale lock, a main repo that no longer exists —
was silently swallowed and the directory stayed on disk forever.
``_reap_dead_worktrees`` only ever reclaims leftovers of the ONE task about to
run again, so a task that reaches ``done``/``failed`` and never runs again
leaves its directory unreachable for the rest of time. That is the structural
leak measured 2026-08-12: 106 directories, 2.2GB, under
``~/.no_human/worktrees``.

``teardown_worktree`` is the one place both callers (``run_task``'s
``finally`` and ``_reap_dead_worktrees``) now route through: it adds the
missing post-condition check and an ``rmtree`` fallback, and it is loud (an
ERROR log line, never swallowed) when a directory survives both. It never
raises — teardown must not be able to fail a task or crash boot.

``sweep_stale_worktrees`` is the janitor: a STARTUP-ONLY sweep (never a
per-tick one) of every directory under the worktree root, run once before the
scheduler starts dispatching. A directory is reclaimed only when it is
PROVABLY dead — never on age or mtime — per the same conservative-skip rule
``doctor.py``'s W2.6 orphan check already uses: an owner pid that is still
alive always wins over whatever the task's status says, a task this store
does not know about is left alone unless its worktree's own git admin
directory (the ``gitdir:`` target inside ``.git``) has itself vanished (the
"deleted bench sandbox" class — 88 of the 106 measured), and anything that
cannot be read or parsed is skipped with a loud warning rather than guessed
at.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..testing import runner
from .task import TERMINAL_STATES

log = logging.getLogger("no_human.worktree")

# Worktree paths THIS PROCESS is currently running a task in. Only the reaper
# and the janitor read it, and only to answer one question: "is this
# directory, which carries my own pid, one I am using right now, or one my
# own earlier crashed run left behind?" — a question `os.kill(pid, 0)` cannot
# answer for our own pid.
#
# It is NOT a lock. Nothing waits on it, nothing is excluded by it, and two
# attempts of one task still start freely and run side by side; it only ever
# decides what is safe to DELETE. Serialising attempts would be the wrong
# fix: overlap is legitimate, the shared path was the defect.
_LIVE_WORKTREES: set[str] = set()


def is_agent_worktree(
    path, config, *, live: set[str] | None = None,
) -> bool:
    """Fail-closed containment predicate: True only for a directory this
    process's own worktree machinery could have produced — never a human's
    primary checkout, and never anything outside the configured worktree
    root. `reset_agent_workspace` is destructive (hard reset + clean), so
    this is the guard that stands between it and an operator's live tree.

    All of the following must hold, else False:
      1. ``path``'s PARENT is exactly the configured ``worktree_root`` —
         nothing outside the root is ever in scope, however it is shaped.
      2. ``path``'s directory NAME parses under ``config.worktree_owner``
         into the ``<task_id>.<owner_pid>.<token>`` shape this process
         mints for a per-run worktree, OR ``path`` is registered in
         ``live`` (this process is running a task in it right now — covers
         legacy/bare-task-id directories that still predate the shaped
         name).
      3. ``path/.git`` is a FILE, not a directory — the marker of a linked
         worktree. A primary checkout's ``.git`` is a directory; treating
         one as agent-owned would let this reset a human's tree.

    Any exception (unreadable path, a symlink that does not resolve, a
    non-existent directory) reads as False — "cannot prove it is ours"
    is not "safe to reset"."""
    from ..config import worktree_owner, worktree_root

    if live is None:
        live = _LIVE_WORKTREES
    try:
        p = Path(path).resolve()
        root = worktree_root(config).resolve()
        if p.parent != root:
            return False
        _, owner_pid = worktree_owner(p.name)
        shaped = len(p.name.split(".")) >= 3 and owner_pid is not None
        if not (shaped or str(path) in live or str(p) in live):
            return False
        return (p / ".git").is_file()
    except Exception:  # noqa: BLE001 — unreadable/unresolvable reads as "not ours"
        return False


def reset_agent_workspace(
    repo, config, *, task_id: str, live: set[str] | None = None,
) -> list[str] | None:
    """Hard-reset + clean an agent-owned worktree immediately before a
    branch decision (`checkout` / `checkout -B`), so a previous attempt's
    uncommitted leftovers can never crash the next one's startup.

    Returns the discarded paths (``[]`` on an already-clean tree), or
    ``None`` when the guard declined — never raises, so a reset failure
    (or a workspace this process does not own) can never fail a task; the
    caller's own branch operation remains the loud symptom if something is
    still wrong afterward."""
    if not is_agent_worktree(repo.path, config, live=live):
        log.info(
            "workspace reset SKIPPED for task %s — %s is not an "
            "agent-owned worktree", task_id[:8], repo.path,
        )
        return None
    try:
        discarded = repo.reset_workspace()
    except Exception as exc:  # noqa: BLE001 — reset must never fail a task
        log.error(
            "workspace reset FAILED for task %s at %s: %s",
            task_id[:8], repo.path, exc,
        )
        return None
    if discarded:
        log.warning(
            "workspace reset discarded %d uncommitted leftover(s) for task "
            "%s before branching (previous attempt's leftovers): %s",
            len(discarded), task_id[:8], ", ".join(discarded[:5]),
        )
    return discarded


def teardown_worktree(
    main_repo, wt_path: Path, *, task_id: str, live: set[str] | None = None,
) -> bool:
    """Remove one task's worktree directory. Never raises.

    ``main_repo`` may be ``None`` — the bench-sandbox case, where no repo is
    left to hold a registration for the directory at all; only the
    filesystem side is left to clean up.

    Returns ``True`` when the directory is confirmed gone, ``False`` when it
    survived both the git removal and the rmtree fallback — a caller may
    ignore the return value (teardown must never be allowed to fail a task),
    but the failure is always logged at ERROR, never silently dropped."""
    if live is None:
        live = _LIVE_WORKTREES
    wt_path = Path(wt_path)
    try:
        # Kill any test subprocess still running in this worktree BEFORE
        # removing it — rmtree'ing a `.venv` out from under a live pytest
        # subprocess is the xdist INTERNALERROR this order avoids. A kill
        # failure must not stop the removal.
        try:
            killed = runner.terminate_running(wt_path)
            if killed:
                log.warning(
                    "killed %d orphaned test proc(s) in %s before teardown",
                    killed, task_id[:8],
                )
        except Exception:  # noqa: BLE001 — teardown must never crash
            pass

        if main_repo is not None:
            try:
                main_repo.remove_worktree(wt_path)
            except Exception as exc:  # noqa: BLE001 — post-condition catches this
                log.warning(
                    "git worktree remove failed for %s: %s", task_id[:8], exc)

        # Post-condition check — this is the fix. `remove_worktree` runs with
        # `check=False`, so a git failure above was silent; only checking
        # whether the directory is actually gone catches it.
        if wt_path.exists():
            try:
                shutil.rmtree(wt_path, ignore_errors=False)
            except Exception:  # noqa: BLE001
                try:
                    shutil.rmtree(wt_path, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    pass

        if wt_path.exists():
            log.error(
                "worktree teardown FAILED for task %s: %s still on disk "
                "after git worktree remove + rmtree — reclaim by hand: "
                "git worktree remove --force %s", task_id[:8], wt_path, wt_path,
            )
            return False
        log.info("worktree teardown OK for task %s: %s", task_id[:8], wt_path)
        return True
    finally:
        live.discard(str(wt_path))


def _default_open_repo(task, config):
    """``GitRepo(task.repo_path)`` guarded against every way opening it can
    fail — an unreadable path, a repo that no longer exists on disk. The
    janitor must never let a bad repo path turn into an unhandled exception
    that aborts the whole sweep."""
    from ..vcs import GitRepo

    try:
        return GitRepo(
            Path(task.repo_path),
            identity_name=config["git"]["agent_identity_name"],
            identity_email=config["git"]["agent_identity_email"],
            never_push_to=config["git"]["never_push_to"],
        )
    except Exception:  # noqa: BLE001
        return None


def _read_gitdir_target(entry: Path) -> Path | None:
    """Parse the ``gitdir: <path>`` line out of a linked worktree's ``.git``
    file. Returns ``None`` when it is missing, unreadable, or unparseable —
    "cannot tell" must never be treated as "provably dead"."""
    try:
        text = (entry / ".git").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            return Path(target) if target else None
    return None


async def sweep_stale_worktrees(
    store, config, *, open_repo=None, live: set[str] | None = None,
) -> tuple[int, int]:
    """Startup-only janitor. Reclaims worktree directories left by tasks that
    have already reached a terminal status and will never run again — the
    class ``_reap_dead_worktrees`` structurally cannot reach, since it only
    ever runs scoped to the one task about to start.

    Returns ``(removed, skipped)``. Every skip is logged — an operator who
    disagrees can see exactly what the janitor declined and why."""
    from ..config import pid_alive, worktree_owner, worktree_root

    if live is None:
        live = _LIVE_WORKTREES
    root = worktree_root(config)
    if not root.is_dir():
        log.debug("worktree sweep: root %s does not exist, nothing to reclaim",
                  root)
        return (0, 0)

    resolve_repo = open_repo if open_repo is not None else (
        lambda task: _default_open_repo(task, config))

    removed = 0
    skipped = 0
    for entry in sorted(root.iterdir()):
        try:
            if not entry.is_dir():
                continue
            if str(entry) in live:
                continue  # a run in THIS process owns it right now

            task_id, owner_pid = worktree_owner(entry.name)
            if owner_pid is not None and pid_alive(owner_pid):
                log.info(
                    "worktree sweep: skipping %s — owner pid %d is alive",
                    entry, owner_pid)
                skipped += 1
                continue

            task = await store.get_task(task_id)
            if task is not None:
                if task.status not in TERMINAL_STATES:
                    log.info(
                        "worktree sweep: skipping %s — task %s is %s, not "
                        "terminal", entry, task_id[:8], task.status.value)
                    skipped += 1
                    continue
                try:
                    repo = resolve_repo(task)
                except Exception:  # noqa: BLE001
                    repo = None
                if teardown_worktree(repo, entry, task_id=task.id, live=live):
                    removed += 1
                else:
                    skipped += 1
                continue

            # Unknown task id: this store never heard of it (a different
            # install's leftover, or — the 88-directory class measured
            # 2026-08-12 — a deleted bench sandbox). Never age/mtime: the one
            # provable fact available is whether the worktree's own git admin
            # directory still exists. If it does, some repo somewhere might
            # still hold the registration; if it does not, no repo anywhere
            # can be using this worktree and there is no registration left to
            # prune, so the directory is provably reclaimable.
            gitdir_target = _read_gitdir_target(entry)
            if gitdir_target is None:
                log.warning(
                    "worktree sweep: skipping %s — unknown task id and no "
                    "readable .git gitdir; cannot prove it is dead", entry)
                skipped += 1
                continue
            if gitdir_target.exists():
                log.warning(
                    "worktree sweep: skipping %s — unknown task id but its "
                    "git admin dir %s still exists; leaving it alone",
                    entry, gitdir_target)
                skipped += 1
                continue
            if teardown_worktree(None, entry, task_id=task_id, live=live):
                removed += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 — one bad entry must not
            # abort the whole sweep.
            log.warning("worktree sweep: error inspecting %s: %s", entry, exc)
            skipped += 1
    return (removed, skipped)
