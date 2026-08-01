"""Local git operations: branch, commit, push, diff. Never merge (§3.2).

Commits are made under a *distinct* agent identity (not the user's), and the
agent is structurally prevented from committing onto or pushing to a protected
branch (never_push_to).
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


# Patterns stripped from commit messages to prevent AI attribution leaking
# into the repo history (the agent identity is set via git config, not trailers).
_AI_ATTRIBUTION = re.compile(
    r"\n*Co-authored-by:.*(?:Claude|anthropic|OpenAI|Copilot).*",  # term-ok: matches real third-party trailers verbatim
    re.IGNORECASE,
)


def _sanitize_commit_message(msg: str) -> str:
    """Strip AI attribution trailers from a commit message."""
    return _AI_ATTRIBUTION.sub("", msg).rstrip()


class GitError(RuntimeError):
    pass


class ProtectedBranch(GitError):
    """Raised on any attempt to operate directly on a never_push_to branch."""


@dataclass
class CommitResult:
    branch: str
    sha: str
    files_changed: int
    insertions: int
    deletions: int


def _branch_protected(branch: str, never_push_to: list[str]) -> bool:
    b = branch.removeprefix("origin/").removeprefix("refs/heads/")
    return any(fnmatch.fnmatch(b, pat) for pat in never_push_to)


class GitRepo:
    def __init__(
        self,
        path: Path,
        *,
        identity_name: str = "no_human",
        identity_email: str = "no-human@acme.com",
        never_push_to: list[str] | None = None,
    ):
        self.path = Path(path).expanduser().resolve()
        self.identity_name = identity_name
        self.identity_email = identity_email
        self.never_push_to = never_push_to or ["main", "master", "release/*"]
        if not (self.path / ".git").exists():
            raise GitError(f"not a git repository: {self.path}")

    def _run(self, *args: str, check: bool = True) -> str:
        # Per-call identity injection keeps the agent's authorship distinct from
        # the user's global git config.
        cmd = [
            "git",
            "-c", f"user.name={self.identity_name}",
            "-c", f"user.email={self.identity_email}",
            *args,
        ]
        proc = subprocess.run(
            cmd, cwd=self.path, capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD")

    def head_sha(self) -> str:
        return self._run("rev-parse", "HEAD")

    def default_branch(self, *, local_only: bool = False) -> str:
        """Best-effort: resolve the remote's actual default branch (origin/HEAD).

        C3: a local checkout's current branch can lag the remote's real default
        (e.g. an old clone still on "master" after the remote moved to "main").
        Returns "" if it can't be determined (no origin, detached, offline).

        ``local_only`` skips the ``git remote show origin`` fallback, which does
        NETWORK I/O. Callers serving an HTTP request must pass it: PR-001's GUI
        half needs this value while a person is typing in the composer, and a
        request handler that can block on a slow or unreachable remote would
        hang the board — offline, that fallback is a timeout, not an answer.
        The symbolic-ref path below is a local ref read and always cheap.
        """
        ref = self._run("symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False)
        if ref:
            return ref.rsplit("/", 1)[-1]
        if local_only:
            return ""
        out = self._run("remote", "show", "origin", check=False)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("HEAD branch:"):
                branch = line.split(":", 1)[1].strip()
                return "" if branch == "(unknown)" else branch
        return ""

    def checkout(self, branch: str) -> str:
        """Switch to an existing branch (no reset). Used to resume a parked
        task's feature branch."""
        self._run("checkout", branch)
        return branch

    def create_branch(self, name: str, *, base: str | None = None) -> str:
        if _branch_protected(name, self.never_push_to):
            raise ProtectedBranch(f"refusing to create protected branch: {name}")
        # Single command: create/reset `name` at `base` and check it out. Using
        # `base` only as a start-point (not `git checkout base` first) keeps this
        # worktree-safe — checking out the base branch directly fails when it is
        # already checked out in the primary worktree.
        if base:
            self._run("checkout", "-B", name, base)
        else:
            self._run("checkout", "-B", name)
        return name

    # Build/cache artifacts we never commit even when a repo lacks a .gitignore.
    _EPHEMERAL = (
        ":(exclude,glob)**/__pycache__/**",
        ":(exclude,glob)**/*.py[co]",
        ":(exclude,glob)**/.pytest_cache/**",
        ":(exclude,glob)**/node_modules/**",
        ":(exclude,glob)**/.DS_Store",
        ":(exclude,glob)**/.no_human/**",
        ":(exclude,glob)**/.windsurf/**",  # term-ok: real on-disk IDE config dir
        ":(exclude,glob)**/.devin/**",  # term-ok: real on-disk IDE config dir
        ":(exclude,glob)**/.claude/**",
        ":(exclude,glob)**/HANDOVER.md",
        ":(exclude,glob)**/PLAN.md",
        ":(exclude,glob)**/.venv/**",
        ":(exclude,glob)**/.venv*/**",
        ":(exclude,glob)**/venv/**",
        ":(exclude,glob)**/.tox/**",
        ":(exclude,glob)**/.mypy_cache/**",
        ":(exclude,glob)**/.ruff_cache/**",
        ":(exclude,glob)**/dist/**",
        ":(exclude,glob)**/build/**",
        ":(exclude,glob)**/*.egg-info/**",
        ":(exclude,glob)**/.coverage",
        ":(exclude,glob)**/.env",
        ":(exclude,glob)**/.idea/**",
        ":(exclude,glob)**/.vscode/**",
    )

    def stage_all(self) -> None:
        self._run("add", "-A", "--", ".", *self._EPHEMERAL)

    def has_changes(self) -> bool:
        return bool(self._run("status", "--porcelain", "--", ".", *self._EPHEMERAL))

    def uncommitted_source_files(
        self, coder_touched: set[str] | None = None,
    ) -> list[str]:
        """Non-ephemeral SOURCE files still uncommitted after a commit — a
        completeness guard for the committed state.

        A coder-created file left OUT of the commit yields a broken PR — e.g.
        App.jsx importing an uncommitted favicon.js — that passes every gate
        because tests run against the WORKTREE (which still holds the file),
        not the committed tree. Returns the leftover source paths (empty when
        the commit captured everything), so the caller can fail the attempt
        instead of opening a broken PR."""
        out = self._run("status", "--porcelain", check=False).strip()
        leftovers: list[str] = []
        for line in out.splitlines():
            rel = line[3:].strip() if len(line) > 3 else ""
            if rel.startswith('"') and rel.endswith('"'):
                rel = rel[1:-1]
            if " -> " in rel:            # rename: "old -> new"
                rel = rel.split(" -> ", 1)[1]
            if not rel or self._is_ephemeral_path(rel):
                continue
            if Path(rel).suffix.lower() in self._CODE_EXTS:
                leftovers.append(rel)
            elif coder_touched and rel in coder_touched:
                # B2 #7: a non-code leftover the coder ITSELF touched
                # (Dockerfile, yaml, requirements, proto) is intentional work
                # missing from the commit — the favicon incident class in its
                # non-code shape. Untouched non-code leftovers stay ignored:
                # they are test side-effects, exactly why _CODE_EXTS exists.
                leftovers.append(rel)
        return leftovers

    def commit_all(self, message: str) -> CommitResult:
        branch = self.current_branch()
        if _branch_protected(branch, self.never_push_to):
            raise ProtectedBranch(
                f"refusing to commit on protected branch: {branch}"
            )
        self.stage_all()
        self._run("commit", "-m", _sanitize_commit_message(message))
        return CommitResult(branch=branch, sha=self.head_sha(), **self._diffstat())

    # Code-only extensions safe to auto-stage for *untracked* files.
    # Deliberately excludes data formats (.json, .yaml, .txt, .xml, .csv)
    # that may be test side-effects (e.g. alert-state.json generated by a
    # test run). Modified *tracked* files are always staged regardless of
    # extension — if the file was already in the repo, the agent must have
    # modified it intentionally.
    _CODE_EXTS = frozenset((
        ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts",
        ".java", ".kt", ".go", ".rs", ".rb", ".c", ".cpp", ".h", ".cs",
        ".swift", ".scala", ".sh", ".bash", ".zsh", ".sql", ".html", ".css",
        ".scss", ".vue", ".svelte", ".gradle",
    ))

    # Ephemeral path components/files that must never be committed even when the
    # agent's edited-file list or the untracked scan surfaces them. This mirrors
    # _EPHEMERAL, but is applied in PYTHON: `git add -- <literal paths>
    # :(exclude,glob)…` silently DROPS explicitly-listed *untracked* files when
    # exclude pathspecs are mixed in (a git pathspec gotcha — it dropped every
    # coder-created file, favicon.js and the whole *.test.mjs suite, from PR #2).
    # So we filter the curated list here and `git add` it clean.
    _EPHEMERAL_DIRS = frozenset((
        "__pycache__", ".pytest_cache", "node_modules", ".no_human", ".windsurf",  # term-ok: real on-disk IDE config dir
        ".devin", ".claude", "venv", ".tox", ".mypy_cache", ".ruff_cache",  # term-ok: real on-disk IDE config dir
        "dist", "build", ".idea", ".vscode",
    ))
    _EPHEMERAL_FILES = frozenset((
        ".DS_Store", ".coverage", ".env", "HANDOVER.md", "PLAN.md",
    ))

    @classmethod
    def _is_ephemeral_path(cls, rel: str) -> bool:
        """True if a repo-relative path is an ephemeral artifact (venv, caches,
        build output, agent-owned dirs, editor cruft) — mirrors _EPHEMERAL."""
        p = Path(rel)
        for part in p.parts:
            if part in cls._EPHEMERAL_DIRS:
                return True
            if part.startswith(".venv") or part.endswith(".egg-info"):
                return True
        if p.name in cls._EPHEMERAL_FILES:
            return True
        return p.suffix.lower() in (".pyc", ".pyo")

    def commit_paths(self, paths: list[str], message: str) -> CommitResult:
        """Stage and commit only the listed *paths* — files the agent
        intentionally wrote/edited.  Test side-effects (e.g. state files
        mutated by running vitest) are left unstaged.

        Falls back to :meth:`commit_all` if none of the paths carry changes
        (the agent may have done all its work via Bash tool, which we can't
        track file-by-file).

        Additionally, any modified *tracked* files and untracked source files
        are staged alongside the explicitly listed paths. This covers files
        the agent created or modified via Bash/Run tools (which the
        orchestrator cannot track file-by-file).
        """
        branch = self.current_branch()
        if _branch_protected(branch, self.never_push_to):
            raise ProtectedBranch(
                f"refusing to commit on protected branch: {branch}"
            )
        # Resolve paths relative to the repo root.
        from pathlib import Path
        repo_root = Path(self.path).resolve()
        rel_paths: list[str] = []
        for p in paths:
            abs_p = Path(p).resolve()
            try:
                rel = str(abs_p.relative_to(repo_root))
            except ValueError:
                continue  # outside the repo — skip
            rel_paths.append(rel)
        # Also include modified tracked files — these are always intentional
        # (the agent must have touched them, even if via Bash).
        modified = self._run(
            "diff", "--name-only", check=False
        ).strip().splitlines()
        rel_paths.extend(m.strip() for m in modified if m.strip())
        # Include untracked source files (e.g. new .py created via Bash).
        untracked = self._run(
            "ls-files", "--others", "--exclude-standard", check=False
        ).strip().splitlines()
        for u in untracked:
            u = u.strip()
            if not u:
                continue
            ext = Path(u).suffix.lower()
            if ext in self._CODE_EXTS:
                rel_paths.append(u)
        # Dedupe and drop ephemeral artifacts. We MUST filter in Python rather
        # than pass :(exclude) pathspecs to `git add` alongside the literal
        # paths: git silently drops explicitly-listed untracked files when
        # exclude pathspecs are mixed in (the bug that stripped favicon.js and
        # every *.test.mjs from PR #2). With a pre-filtered list, a plain add
        # stages exactly what we intend.
        rel_paths = [r for r in dict.fromkeys(rel_paths)
                     if not self._is_ephemeral_path(r)]
        if rel_paths:
            self._run("add", "--", *rel_paths)
        # If no files were actually staged (e.g. agent only used Bash to
        # create files), fall back to stage_all so the commit isn't empty.
        staged = self._run("diff", "--cached", "--name-only", check=False).strip()
        if not staged:
            self.stage_all()
        self._run("commit", "-m", _sanitize_commit_message(message))
        return CommitResult(branch=branch, sha=self.head_sha(), **self._diffstat())

    def commits_ahead(self, base: str) -> int:
        """How many commits this branch adds on top of *base*.

        A resumed attempt (`nh reply`) starts from a [WIP-BLOCKED] checkpoint that
        is already committed, so `has_changes()` is False while the branch still
        carries the whole change. Asking git which is true is the only honest way
        to tell "nothing was done" from "it was done last time".
        """
        out = self._run("rev-list", "--count", f"{base}..HEAD", check=False).strip()
        try:
            return int(out)
        except ValueError:
            return 0

    def head_commit(self, base: str) -> CommitResult:
        """Describe HEAD as a commit against *base* — for work already committed.

        Three-dot (merge-base..HEAD), matching the range the reviewer reads. A
        two-dot ``base HEAD`` would report commits that landed on *base* since we
        branched as deletions of ours.
        """
        return CommitResult(
            branch=self.current_branch(), sha=self.head_sha(),
            **self._diffstat(f"{base}...HEAD"),
        )

    def _diffstat(self, *refs: str) -> dict[str, int]:
        out = self._run("diff", "--shortstat", *(refs or ("HEAD~1", "HEAD")),
                        check=False)
        files = insertions = deletions = 0
        for part in out.split(","):
            part = part.strip()
            if "file" in part:
                files = int(part.split()[0])
            elif "insertion" in part:
                insertions = int(part.split()[0])
            elif "deletion" in part:
                deletions = int(part.split()[0])
        return {"files_changed": files, "insertions": insertions, "deletions": deletions}

    def diff(self, ref: str = "HEAD~1") -> str:
        return self._run("diff", ref, "HEAD", check=False)

    def changed_files(self, ref: str = "HEAD~1") -> list[str]:
        out = self._run("diff", "--name-only", ref, "HEAD", check=False)
        return [f for f in out.splitlines() if f]

    def fetch(self, remote: str = "origin", *, prune: bool = True,
              timeout: int = 30) -> None:
        """Fetch latest refs from ``remote``.

        Called before deriving a base branch so we never branch off stale
        state (e.g. remote moved because a PR was merged or another task
        pushed).  Best-effort: a fetch failure is logged, never fatal —
        offline work still functions on the last-known refs.
        """
        args = ["fetch", remote]
        if prune:
            args.append("--prune")
        try:
            subprocess.run(
                ["git", *args],
                cwd=self.path, capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass  # best-effort; offline work must still function

    def remote_url(self, remote: str = "origin") -> str | None:
        out = self._run("remote", "get-url", remote, check=False)
        return out or None

    def push(self, branch: str | None = None, *, remote: str = "origin",
             set_upstream: bool = True) -> str:
        """Push ``branch`` to ``remote`` and return the SHA that was pushed.

        Resolved from the branch ref right BEFORE the push runs — not from
        HEAD afterwards, and not by the caller re-resolving HEAD later. A
        long-running task can sit for minutes waiting on CI/PR review, during
        which HEAD can move out from under this same working tree (another
        checkout, a later commit); re-resolving at that point silently swaps
        in whatever the tree now points at. That is exactly how a correctly
        landed PR got reported "lost": the receipt check compared the PR's
        head against a re-resolved `repo.head_sha()` that had drifted to
        main's tip, not the commit this push actually sent.
        """
        branch = branch or self.current_branch()
        if _branch_protected(branch, self.never_push_to):
            raise ProtectedBranch(f"refusing to push protected branch: {branch}")
        sha = self._run("rev-parse", branch)
        args = ["push"]
        if set_upstream:
            args += ["-u"]
        args += [remote, branch]
        self._run(*args)
        return sha

    # ----------------------------- worktrees ------------------------------- #
    # Phase 7 foundation: per-task isolation. A linked git worktree gives a task
    # its own working tree, index, and HEAD while sharing the repo's object store
    # — so concurrent tasks (even in the SAME repo) never clobber each other's
    # files or branch checkout. Cheaper than a full clone.

    def add_worktree(
        self, worktree_path: Path | str, *, base: str, branch: str | None = None,
        create: bool = True, detach: bool = False,
    ) -> "GitRepo":
        """Create a linked worktree at ``worktree_path``. Modes:
          - ``detach=True``: detached HEAD at ``base`` — the caller then creates
            the attempt branch inside (the orchestrator's per-task worktree).
          - ``create`` + ``branch``: new ``branch`` off ``base``.
          - ``create=False`` + ``branch``: attach an existing branch (resume).
        Returns a GitRepo rooted there with this repo's identity + protection
        rules. Refuses a protected branch name."""
        wt = Path(worktree_path).expanduser()
        wt.parent.mkdir(parents=True, exist_ok=True)
        if detach:
            self._run("worktree", "add", "--detach", str(wt), base)
        else:
            if not branch:
                raise GitError("add_worktree requires branch unless detach=True")
            if _branch_protected(branch, self.never_push_to):
                raise ProtectedBranch(
                    f"refusing to create worktree on protected branch: {branch}")
            if create:
                self._run("worktree", "add", "-b", branch, str(wt), base)
            else:
                self._run("worktree", "add", str(wt), branch)
        return GitRepo(
            wt,
            identity_name=self.identity_name,
            identity_email=self.identity_email,
            never_push_to=list(self.never_push_to),
        )

    def remove_worktree(self, worktree_path: Path | str, *, force: bool = True) -> None:
        """Remove a linked worktree (and prune its admin files). Best-effort:
        a missing/already-removed worktree is not an error."""
        wt = Path(worktree_path).expanduser()
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(wt))
        self._run(*args, check=False)
        self._run("worktree", "prune", check=False)

    def list_worktrees(self) -> list[str]:
        """Absolute paths of all worktrees (including the main one)."""
        out = self._run("worktree", "list", "--porcelain", check=False)
        return [ln.split(" ", 1)[1] for ln in out.splitlines()
                if ln.startswith("worktree ")]

    @contextmanager
    def worktree(
        self, worktree_path: Path | str, *, base: str, branch: str,
        create: bool = True, cleanup: bool = True,
    ) -> Iterator["GitRepo"]:
        """Scoped worktree: yields the worktree GitRepo and removes it on exit
        (set ``cleanup=False`` to preserve it across a park/resume boundary)."""
        wt = self.add_worktree(worktree_path, base=base, branch=branch, create=create)
        try:
            yield wt
        finally:
            if cleanup:
                self.remove_worktree(worktree_path)
