"""Reviewer worktree integrity guard (task d115e22f).

The reviewer session keeps Bash enabled (see `docs/security.md`), so a shell
redirect (``echo x > calc.py``) is not a tool call `agent/guard.py`'s
PreToolUse hook ever sees. A compromised or careless review session could
therefore write into the very worktree the gate is about to judge, and the
gate would then judge the tree the reviewer LEFT rather than the tree it was
GIVEN.

This module is the pure snapshot/diff/targeted-revert machinery
``Orchestrator._run_reviewer`` wraps around its single ``reviewer.review(...)``
call. It never touches `agent/guard.py`, the reviewer's toolset, or anything
about WHAT the reviewer is allowed to call — it only asks, mechanically,
"did the tree change under the reviewer, and if so, undo exactly that."

Module contract: every public function either returns a complete,
trustworthy result or raises :class:`WorktreeCheckFailed`. There is no
"probably fine" return value — "could not tell" must never be read as
"nothing happened," because that is the exact failure this module replaces
(the previous attempt's check failed OPEN: guard machinery that couldn't run
was treated as if the reviewer had written nothing).

Second re-scope (this revision): `git status` — the instrument `snapshot`/
`compare` were originally built on — is structurally blind to the `.git`
subtree; it only ever reports on the tracked/untracked WORKTREE. A Bash-
enabled reviewer can plant an executable hook (e.g. `post-checkout`) there
invisibly to that instrument, and this module's own `revert()` calls
(`git checkout`, `git reset`) would then EXECUTE it. `_git_dir_inventory`
closes the detection gap; the `-c core.hooksPath=`/`-c core.fsmonitor=false`
flags on every `revert()` git call close the execution gap. Neither one does
the other's job — both are required.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_DEFAULT_TIMEOUT_SECONDS = 30.0


class WorktreeCheckFailed(RuntimeError):
    """Snapshot, diff or revert could not be performed with confidence.

    Callers must treat this as fail-closed: route it to the
    ``ReviewerUnavailable`` escalation channel, never fall back to trusting
    the reviewer's decision as-is.
    """


@dataclass(frozen=True)
class Snapshot:
    head: str
    entries: dict[str, str]
    git_entries: dict[str, tuple[int, int, int, str]]


@dataclass(frozen=True)
class Delta:
    added: list[str]
    modified: list[str]
    deleted: list[str]

    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)


def _run_git(repo_path: Path, *args: str, timeout: float) -> str:
    # Variadic, matching `vcs/push_hook.py:_git` / `vcs/pr_watcher.py:_git_rc`
    # / `vcs/git.py:GitRepo._run` — not a `list[str]` parameter. The
    # egress-allowlist scanner (tests/test_egress_allowlist.py) resolves a
    # git subcommand only from literal words it can see AT THE CALL SITE; a
    # single `args: list[str]` parameter hides every call site's literal
    # subcommand behind one opaque list value and the whole module collapses
    # to one undifferentiated `exec:git <dynamic>` channel. `*args` lets each
    # call site spell its subcommand as a literal positional word, so
    # `_run_git(repo_path, "rev-parse", ref, timeout=t)` resolves to the
    # already-classified-LOCAL `exec:git rev-parse`, same as every other git
    # wrapper in this tree.
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeCheckFailed(
            f"git {' '.join(args)} timed out after {timeout}s") from exc
    except (FileNotFoundError, PermissionError, OSError, TypeError) as exc:
        raise WorktreeCheckFailed(
            f"git {' '.join(args)} could not run: {exc}") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().replace("\n", " ")[:400]
        raise WorktreeCheckFailed(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {stderr}")
    return proc.stdout or ""


def _content_hash(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "deleted"
    except (PermissionError, OSError) as exc:
        raise WorktreeCheckFailed(f"could not read {path}: {exc}") from exc
    return hashlib.sha256(data).hexdigest()


def _parse_porcelain_z(raw: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain=v1 -z` output into (path, XY) pairs,
    skipping the extra "from" token a rename/copy entry carries."""
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        out.append((path, xy))
        if xy[0] in ("R", "C") and i < len(tokens):
            i += 1  # skip the rename/copy source path
    return out


# --- .git-subtree inventory (part A) ---------------------------------------
#
# `git status` never reports on paths under `.git` itself, so the porcelain
# snapshot above is structurally blind to a planted hook. This second
# instrument walks the `.git` subtree directly.
#
# Volatile exclusion list — kept short, each entry justified, matched on the
# path *relative to the root it was found under* (so "index" excludes both
# the admin dir's and the common dir's index file, not a directory prefix):
_VOLATILE_GIT_EXACT = frozenset({
    # Rewritten by every read-only `git status`/`git diff` the reviewer
    # legitimately runs (stat-cache refresh). Not an execution surface.
    "index",
    # Transient lock created/removed around index writes. Never executed.
    "index.lock",
    # Bookkeeping ref written by `git fetch`. Pure data, never executed.
    "FETCH_HEAD",
    # Bookkeeping ref written by `reset`/`merge`/`rebase`. Never executed.
    "ORIG_HEAD",
    # A flat snapshot of every ref in the repo, rewritten by `git gc`/
    # `pack-refs` and by ordinary push/fetch traffic anywhere in the SHARED
    # object store — same "not this review's business" rationale as the
    # `refs/` directory prune below, just packed into one file instead of a
    # directory. Never executed; the ref this worktree actually has checked
    # out is independently covered by the `HEAD` comparison in `compare()`.
    "packed-refs",
})
# The reflog: appended on every ref update by ordinary git use. Never
# executed.
_VOLATILE_GIT_PREFIX = "logs/"

# Whole subtrees PRUNED before descent — `_iter_git_paths` never lists their
# contents at all, as opposed to `_VOLATILE_GIT_EXACT`/`_VOLATILE_GIT_PREFIX`
# above, which still walk and stat every path and only filter afterwards.
# These are shared, high-churn, high-volume subtrees where that walk itself
# is the cost:
_SKIPPED_GIT_DIR_PREFIXES = frozenset({
    # `--git-common-dir` is shared by EVERY linked worktree of this repo, and
    # itself contains `worktrees/<id>/` for each one — including this
    # worktree's own admin dir, which `_git_dir_inventory` already walks
    # separately via `--absolute-git-dir`. Walking it again unrestricted
    # would (a) re-discover this worktree's own files a second time under a
    # different relative path (`worktrees/<id>/index` vs `index`), which the
    # exclusion lists above — matched exactly, on purpose, so they stay short
    # and auditable — would not recognise, false-positiving on ordinary index
    # churn from a read-only `git status`; and (b) pick up every OTHER
    # worktree's admin dir sharing this common dir, so an unrelated
    # concurrent task's git activity would trip THIS review's guard. Neither
    # is a real execution surface for `repo_path`: each worktree's own
    # `core.hooksPath` already points only inside its own admin dir
    # (`push_hook.py`), never another worktree's.
    "worktrees/",
    # Git's content-addressed object store. An existing object's bytes are
    # immutable — it is identified BY their sha256, so an in-place
    # "modification" is a cryptographic contradiction — the only possible
    # write is an ADDITION, and the shared object store gains new objects
    # constantly from every OTHER concurrent task's linked worktree, not just
    # this review. Walking and content-hashing it anyway was both a real
    # perf cost (measured on this checkout: 150MB across 2628 files, read
    # three times per review — snapshot, compare, and revert's own internal
    # compare — and growing with repo history) and a false-positive source
    # unrelated to what THIS reviewer wrote. Whatever the reviewer's own
    # commits added here is still caught without walking a single object:
    # `compare()` tracks `HEAD` via `git rev-parse` independently of this
    # inventory, and `revert()` refuses outright the moment HEAD moved.
    "objects/",
    # Every ref in the repo, not just the one this worktree has checked
    # out — shared across every worktree, and moved by concurrent unrelated
    # pushes/fetches/branch activity on OTHER branches this review never
    # touched. The ref THIS worktree has checked out is independently
    # covered by the `HEAD` sha comparison in `compare()`, so pruning
    # `refs/` loses no coverage of what the reviewer did while removing the
    # same shared-store concurrency false-positive vector as `objects/`
    # above (`refs/stash`, previously listed as an exact exception here, is
    # now covered by this prune instead).
    "refs/",
})

# Deliberately NOT excluded — a later reader must not "tidy" these in:
# `hooks/`, `no_human-hooks/` (the direct hook execution surface this guard
# exists to catch; see `push_hook.py`'s `_MIRRORED_HOOKS` shim chain),
# `config` / `config.worktree` / `info/attributes` (a second exec-on-checkout
# surface via smudge/clean/textconv filters and `alias.*`; hardening that is
# out of scope, but detecting a write to it is not), and the `.git` pointer
# file itself (rewriting it repoints the whole admin dir for a linked
# worktree).


def _is_volatile_git_path(rel: str) -> bool:
    return rel in _VOLATILE_GIT_EXACT or rel.startswith(_VOLATILE_GIT_PREFIX)


def _resolve_git_root(repo_path: Path, arg: str, *, timeout: float) -> Path:
    raw = _run_git(repo_path, "rev-parse", arg, timeout=timeout).strip()
    if not raw:
        raise WorktreeCheckFailed(
            f"git rev-parse {arg} returned nothing in {repo_path}")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_path / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorktreeCheckFailed(
            f"could not resolve git dir from {arg}={raw!r}: {exc}") from exc


def _stat_entry(path: Path) -> tuple[int, int, int, str]:
    try:
        st = path.stat()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise WorktreeCheckFailed(f"could not stat {path}: {exc}") from exc
    return (stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns, _content_hash(path))


def _symlink_entry(path: Path) -> tuple[int, int, int, str]:
    # A symlinked hook is executed by git exactly like a regular file at that
    # path (`ln -s /tmp/payload post-checkout`) — the earlier revision of
    # this guard skipped `path.is_symlink()` paths entirely, which made a
    # symlinked hook invisible to the inventory. We must NOT follow the link
    # (reading whatever it points at could touch an arbitrary path outside
    # the repo, hang on a FIFO/device, or simply be a dangling target that
    # raises on `.stat()`); instead the link itself — its mode and target
    # string — is the thing being inventoried, via `lstat`/`readlink`, which
    # never dereference.
    try:
        st = path.lstat()
        target = str(path.readlink())
    except OSError as exc:
        raise WorktreeCheckFailed(f"could not read symlink {path}: {exc}") from exc
    digest = hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest()
    return (stat.S_IMODE(st.st_mode), len(target), st.st_mtime_ns, digest)


def _iter_git_paths(
    root: Path, skip_dir_prefixes: frozenset[str]
) -> Iterator[tuple[Path, bool]]:
    """Depth-first walk of *root*, yielding `(path, is_symlink)` for every
    symlink and regular file underneath it.

    A symlink is always a LEAF here, never descended through — matching
    `_symlink_entry`'s never-dereference contract, and closing the
    symlinked-hook gap an earlier revision of this guard had (a symlinked
    directory under `.git` is exactly as inventoried, by link identity, as a
    symlinked file). A directory whose root-relative path (with a trailing
    "/") is in *skip_dir_prefixes* is PRUNED — its contents are never even
    `iterdir()`-ed — rather than walked and filtered afterwards; this is
    what keeps the walk cheap for a subtree the size of `objects/`. A
    special file (socket, FIFO, device) is silently skipped: it is neither a
    symlink nor `is_file()` nor `is_dir()`.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError as exc:
            raise WorktreeCheckFailed(f"could not walk {current}: {exc}") from exc
        for child in children:
            try:
                is_link = child.is_symlink()
                is_regular_file = (not is_link) and child.is_file()
                is_dir = (not is_link) and not is_regular_file and child.is_dir()
            except OSError as exc:
                raise WorktreeCheckFailed(f"could not stat {child}: {exc}") from exc
            if is_dir:
                rel_dir = f"{child.relative_to(root).as_posix()}/"
                if rel_dir in skip_dir_prefixes:
                    continue
                stack.append(child)
                continue
            if is_link or is_regular_file:
                yield child, is_link


def _walk_git_root(
    root: Path, label: str, *, skip_dir_prefixes: frozenset[str] = frozenset()
) -> dict[str, tuple[int, int, int, str]]:
    entries: dict[str, tuple[int, int, int, str]] = {}
    if not root.is_dir():
        return entries
    for path, is_link in sorted(_iter_git_paths(root, skip_dir_prefixes)):
        rel = path.relative_to(root).as_posix()
        if _is_volatile_git_path(rel):
            continue
        if is_link:
            # Inventoried by link identity (mode + target), never by
            # dereferencing — see `_symlink_entry`. This is what closes the
            # symlinked-hook gap: `hooks/post-checkout -> /tmp/payload` is a
            # write under `.git` like any other and must be caught here.
            entries[f"{label}/{rel}"] = _symlink_entry(path)
        else:
            entries[f"{label}/{rel}"] = _stat_entry(path)
    return entries


def _git_dir_inventory(repo_path: Path, *, timeout: float) -> dict[str, tuple[int, int, int, str]]:
    """Recursive inventory of the `.git` subtree: `(mode, size, mtime_ns,
    sha256)` per path, covering both the per-worktree admin dir
    (`git rev-parse --absolute-git-dir`) and the shared common dir
    (`git rev-parse --git-common-dir`) — for a *linked* worktree these
    differ, and the hooks that actually fire live under the common dir
    (`push_hook.py:24-31`), not the admin dir alone. Mode is part of the
    tuple so a `chmod +x` on a byte-identical existing file (e.g. a
    `hooks/*.sample`) is still a detected delta.
    """
    repo_path = Path(repo_path)
    admin_dir = _resolve_git_root(repo_path, "--absolute-git-dir", timeout=timeout)
    common_dir = _resolve_git_root(repo_path, "--git-common-dir", timeout=timeout)
    entries: dict[str, tuple[int, int, int, str]] = {}
    entries.update(
        _walk_git_root(admin_dir, "admin", skip_dir_prefixes=_SKIPPED_GIT_DIR_PREFIXES))
    if common_dir != admin_dir:
        entries.update(
            _walk_git_root(
                common_dir, "common", skip_dir_prefixes=_SKIPPED_GIT_DIR_PREFIXES))
    pointer = repo_path / ".git"
    try:
        is_pointer_file = pointer.is_file() and not pointer.is_symlink()
    except OSError as exc:
        raise WorktreeCheckFailed(f"could not stat {pointer}: {exc}") from exc
    if is_pointer_file:
        # Linked worktree: `.git` is a text file (`gitdir: <admin>/...`), not
        # a directory. It lives in the WORKTREE, so neither root walk above
        # reaches it, yet rewriting it repoints the whole admin dir.
        entries["pointer"] = _stat_entry(pointer)
    return entries


def _is_git_subtree_path(path: str) -> bool:
    return path.startswith(".git/")


def snapshot(repo_path: Path, *, timeout: float) -> Snapshot:
    """Capture HEAD plus a content-hashed record of every dirty/untracked
    path, plus a full `.git`-subtree inventory. Clean tracked paths are
    deliberately NOT recorded — `compare` classifies added/modified/deleted
    from git's own status codes on the AFTER snapshot, not from this module's
    memory of what the baseline tree looked like, so a file that was clean at
    snapshot time and dirtied during the review is still correctly reported
    as "modified," never "added."
    """
    try:
        repo_path = Path(repo_path)
    except TypeError as exc:
        raise WorktreeCheckFailed(f"not a usable repo path: {repo_path!r}") from exc
    head = _run_git(repo_path, "rev-parse", "HEAD", timeout=timeout).strip()
    if not head or len(head) < 7 or any(c not in "0123456789abcdef" for c in head):
        raise WorktreeCheckFailed(
            f"HEAD did not resolve to a commit sha in {repo_path}: {head!r}")
    raw = _run_git(
        repo_path,
        "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no",
        timeout=timeout,
    )
    entries: dict[str, str] = {}
    for path, xy in _parse_porcelain_z(raw):
        entries[path] = f"{xy}:{_content_hash(repo_path / path)}"
    git_entries = _git_dir_inventory(repo_path, timeout=timeout)
    return Snapshot(head=head, entries=entries, git_entries=git_entries)


def _bucket(xy: str) -> str:
    if xy == "??":
        return "added"
    if "D" in xy:
        return "deleted"
    if "A" in xy:
        return "added"
    return "modified"


def compare(repo_path: Path, before: Snapshot, *, timeout: float) -> Delta:
    """Re-snapshot and diff structurally against *before*.

    A changed HEAD (e.g. the reviewer ran `git commit`) is reported as a
    synthetic ``HEAD:<old>-><new>`` modification entry even when it leaves
    the working tree itself perfectly clean — `revert` refuses that case
    outright rather than guessing at undoing a commit.

    Any added/removed/changed `.git`-subtree entry (see `_git_dir_inventory`)
    is reported through the SAME added/modified/deleted lists, prefixed
    `.git/`, so it rides the one `reviewer_wrote` event + verdict-discard
    path the worktree-file delta already uses — one code path, one event
    kind, one discard.
    """
    repo_path = Path(repo_path)
    after = snapshot(repo_path, timeout=timeout)
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    for path in sorted(set(before.entries) | set(after.entries)):
        b = before.entries.get(path)
        a = after.entries.get(path)
        if a == b:
            continue
        if a is None:
            # No longer dirty/untracked. Only interesting if the path is
            # actually gone from disk — a reviewer "un-dirtying" a
            # pre-existing dirty file leaves no residual effect to report.
            if not (repo_path / path).exists():
                deleted.append(path)
            continue
        a_status = a.split(":", 1)[0]
        bucket = _bucket(a_status)
        if bucket == "added" and b is not None:
            # Content changed under a path that was ALREADY untracked before
            # the session — not a newly-created file, so it is reported (and
            # reverted) as a modification, not an addition.
            bucket = "modified"
        {"added": added, "modified": modified, "deleted": deleted}[bucket].append(path)
    for path in sorted(set(before.git_entries) | set(after.git_entries)):
        b = before.git_entries.get(path)
        a = after.git_entries.get(path)
        if a == b:
            continue
        display = f".git/{path}"
        if a is None:
            deleted.append(display)
        elif b is None:
            added.append(display)
        else:
            modified.append(display)
    if before.head != after.head:
        modified.append(f"HEAD:{before.head}->{after.head}")
    return Delta(added=sorted(added), modified=sorted(modified), deleted=sorted(deleted))


def revert(repo_path: Path, before: Snapshot, delta: Delta, *, timeout: float) -> None:
    """Targeted revert of exactly the WORKTREE paths *delta* names — never
    `git reset --hard` / `git clean -fdx` / `git stash`.

    `.git`-subtree entries in *delta* (prefixed `.git/`, see `compare`) are
    deliberately NOT touched here: there is no generic way to restore
    hook-directory content from a hash-only inventory, and the security
    consequence — a rigged verdict — is already neutralised by the caller
    discarding it once this function returns. This function's job is to
    restore the checked-out worktree without ever executing a repo-local
    hook while doing so; it must not choke on paths `git reset`/`git
    checkout` were never going to understand. (Parked follow-up, not
    implemented: physically removing a planted `.git`-subtree file.)

    Every git call below passes `-c core.hooksPath=<empty dir>` (so a
    planted hook cannot fire) and `-c core.fsmonitor=false` (same rationale
    as `repo_discovery.py:306` — fsmonitor spawns a user-configured
    process). Both are written literally at each call site, not routed
    through a shared `cmd`-taking helper, to keep the egress-allowlist
    scanner's `["git", ...]`-literal classification intact
    (`src/no_human/vcs/git.py:167-172`), and never through `GitRepo._run`,
    whose own prefix has no hooks override and must keep firing the
    load-bearing `pre-push` hook.

    Verifies itself by re-diffing against *before* once done; a residual
    difference in the WORKTREE portion raises rather than returning quietly,
    since a silent partial revert is exactly the "probably fine" outcome
    this module refuses to produce.
    """
    repo_path = Path(repo_path)
    if any(entry.startswith("HEAD:") for entry in delta.modified):
        raise WorktreeCheckFailed(
            "the reviewer session moved HEAD (e.g. committed) — that cannot "
            "be undone by a targeted revert without rewriting history")
    worktree_added = [p for p in delta.added if not _is_git_subtree_path(p)]
    worktree_modified = [p for p in delta.modified if not _is_git_subtree_path(p)]
    worktree_deleted = [p for p in delta.deleted if not _is_git_subtree_path(p)]

    empty_hooks_dir = tempfile.mkdtemp(prefix="no_human-revert-hooks-")
    try:
        for path in worktree_added:
            full = repo_path / path
            try:
                if full.is_symlink() or full.exists():
                    full.unlink()
            except OSError as exc:
                raise WorktreeCheckFailed(
                    f"could not remove reviewer-created path {path}: {exc}") from exc
            _run_git(
                repo_path,
                "-c", f"core.hooksPath={empty_hooks_dir}", "-c", "core.fsmonitor=false",
                "reset", "--", path,
                timeout=timeout)
        for path in [*worktree_modified, *worktree_deleted]:
            _run_git(
                repo_path,
                "-c", f"core.hooksPath={empty_hooks_dir}", "-c", "core.fsmonitor=false",
                "checkout", before.head, "--", path,
                timeout=timeout)
    finally:
        shutil.rmtree(empty_hooks_dir, ignore_errors=True)

    residual = compare(repo_path, before, timeout=timeout)
    residual_worktree = Delta(
        added=[p for p in residual.added if not _is_git_subtree_path(p)],
        modified=[p for p in residual.modified if not _is_git_subtree_path(p)],
        deleted=[p for p in residual.deleted if not _is_git_subtree_path(p)],
    )
    if not residual_worktree.is_empty():
        raise WorktreeCheckFailed(
            "revert did not restore the reviewed baseline; residual delta: "
            f"added={residual_worktree.added} modified={residual_worktree.modified} "
            f"deleted={residual_worktree.deleted}")


def guard_config(config: dict | None) -> float:
    """Timeout (seconds) for every git call this module makes, from
    `pipeline.reviewer_worktree_guard.timeout_seconds`.

    Mirrors `review_routing.routing_config`'s tolerance of the
    `pipeline: None` deep-merge shape and of a malformed value — a bad
    config value here must not silently widen the check into "wait forever"
    or "never wait," so both fall back to the documented default.
    """
    section = (config or {}).get("pipeline") or {}
    guard = section.get("reviewer_worktree_guard") or {}
    raw = guard.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_TIMEOUT_SECONDS
    return value
