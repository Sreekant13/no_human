"""Local git operations: branch, commit, push, diff. Never merge (§3.2).

Commits are made under a *distinct* agent identity (not the user's), and the
agent is structurally prevented from committing onto or pushing to a protected
branch (never_push_to).
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path


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

    def create_branch(self, name: str, *, base: str | None = None) -> str:
        if _branch_protected(name, self.never_push_to):
            raise ProtectedBranch(f"refusing to create protected branch: {name}")
        if base:
            self._run("checkout", base)
        self._run("checkout", "-B", name)
        return name

    # Build/cache artifacts we never commit even when a repo lacks a .gitignore.
    _EPHEMERAL = (
        ":(exclude,glob)**/__pycache__/**",
        ":(exclude,glob)**/*.py[co]",
        ":(exclude,glob)**/.pytest_cache/**",
        ":(exclude,glob)**/node_modules/**",
        ":(exclude,glob)**/.DS_Store",
    )

    def stage_all(self) -> None:
        self._run("add", "-A", "--", ".", *self._EPHEMERAL)

    def has_changes(self) -> bool:
        return bool(self._run("status", "--porcelain"))

    def commit_all(self, message: str) -> CommitResult:
        branch = self.current_branch()
        if _branch_protected(branch, self.never_push_to):
            raise ProtectedBranch(
                f"refusing to commit on protected branch: {branch}"
            )
        self.stage_all()
        self._run("commit", "-m", message)
        return CommitResult(branch=branch, sha=self.head_sha(), **self._diffstat())

    def _diffstat(self) -> dict[str, int]:
        out = self._run("diff", "--shortstat", "HEAD~1", "HEAD", check=False)
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

    def remote_url(self, remote: str = "origin") -> str | None:
        out = self._run("remote", "get-url", remote, check=False)
        return out or None

    def push(self, branch: str | None = None, *, remote: str = "origin",
             set_upstream: bool = True) -> str:
        branch = branch or self.current_branch()
        if _branch_protected(branch, self.never_push_to):
            raise ProtectedBranch(f"refusing to push protected branch: {branch}")
        args = ["push"]
        if set_upstream:
            args += ["-u"]
        args += [remote, branch]
        return self._run(*args)
