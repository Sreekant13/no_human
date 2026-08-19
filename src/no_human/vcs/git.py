"""Local git operations: branch, commit, push, diff. Never merge (§3.2).

Commits are made under a *distinct* agent identity (not the user's), and the
agent is structurally prevented from committing onto or pushing to a protected
branch (never_push_to).
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .outbound_scrub import scrub_outbound
from .push_hook import install_pre_push_guard


# Patterns stripped from commit messages to prevent AI attribution leaking
# into the repo history (the agent identity is set via git config, not trailers).
_AI_ATTRIBUTION = re.compile(
    r"\n*Co-authored-by:.*(?:Claude|anthropic|OpenAI|Copilot).*",  # term-ok: matches real third-party trailers verbatim
    re.IGNORECASE,
)


def _sanitize_commit_message(msg: str) -> str:
    """Strip AI attribution trailers from a commit message."""
    return scrub_outbound(_AI_ATTRIBUTION.sub("", msg).rstrip(), "commit_message")


class GitError(RuntimeError):
    pass


#: Stderr signatures of git LOCK CONTENTION — another process (the coder's own
#: git subprocess, an IDE, a parallel bench worker on the same volume) briefly
#: holds a lock this invocation needs. Transient by definition, so the ONLY
#: class of git failure that is retried; anything else (conflict, bad ref,
#: protected branch) raises on the first attempt exactly as before. Two specs
#: of main-6cec2140 (2026-08-07) crashed on this class: a `git add` and a
#: `git checkout -B <agent-branch> bench-base` that both failed on locks and
#: were booked as spec crashes.
_TRANSIENT_GIT_STDERR = (
    "index.lock",
    "could not lock",
    "cannot lock ref",
    "unable to create",
    "resource temporarily unavailable",
)

#: Backoffs before each retry (also the retry COUNT). Module-level so tests
#: zero them. Short: a held git lock clears in milliseconds; this only has to
#: outlive a peer process's single index write.
_GIT_RETRY_BACKOFFS_S: tuple[float, ...] = (0.5, 2.0)


def is_transient_git_failure(stderr: str) -> bool:
    """True iff *stderr* carries a lock-contention signature (see above)."""
    low = (stderr or "").lower()
    return any(sig in low for sig in _TRANSIENT_GIT_STDERR)


class ProtectedBranch(GitError):
    """Raised on any attempt to operate directly on a never_push_to branch."""


class PushBehindRemote(GitError):
    """The local branch is an ANCESTOR of its own pushed tip — behind, not diverged.

    Same git error string as the rebase case (`(non-fast-forward)` /
    `(fetch first)`), opposite remedy: forcing here destroys an earlier
    attempt's reviewed, already-published commits (the remote holds commits
    this tree does not). Distinct type so the delivery retry can never route
    it into `--force-with-lease`.
    """


#: `git push` rejection vocabulary that means "not a fast-forward" — a
#: rebased branch OR a branch behind its own remote tip both say this; the
#: two are distinguished by ancestry, not by this string (see
#: `GitRepo.remote_branch_relation`). Mirrors the orchestrator's
#: `_is_non_fast_forward`, deliberately narrow: it only gates the extra
#: `ls-remote` classification call, never a force decision on its own.
def _looks_like_non_fast_forward(text: str) -> bool:
    low = (text or "").lower()
    if "rejected" not in low:
        return False
    return "non-fast-forward" in low or "fetch first" in low


def _behind_message(branch: str, remote: str, sha: str) -> str:
    """Shared text for `PushBehindRemote`, raised from both the pre-push
    classification (force requested up front) and the post-push failure
    path (plain push rejected non-fast-forward). Same wording either way —
    other tests assert on "behind" and the branch name in this string."""
    return (
        f"refusing to force-push {branch!r}: local is BEHIND its "
        f"own tip on {remote!r} (local {sha[:8]} is an ancestor "
        f"of the remote tip) — the remote holds commits this "
        f"tree does not, most likely from an earlier attempt's "
        f"push. Forcing would destroy them. This attempt cannot "
        f"publish {sha[:8]} onto {branch!r} without human sync."
    )


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
        # Lock-contention retry, INLINE in both runners rather than a shared
        # helper taking `cmd`: the egress-allowlist scanner resolves the git
        # subcommand from the literal ["git", ...] construction at the
        # subprocess.run site, and a helper parameter turns every call into
        # an unclassifiable `exec:git <dynamic>` (test_egress_allowlist).
        proc = subprocess.run(
            cmd, cwd=self.path, capture_output=True, text=True
        )
        if check:
            for backoff in _GIT_RETRY_BACKOFFS_S:
                if proc.returncode == 0 or not is_transient_git_failure(proc.stderr):
                    break
                time.sleep(backoff)
                proc = subprocess.run(
                    cmd, cwd=self.path, capture_output=True, text=True
                )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    def _run_porcelain_lines(self, *args: str, check: bool = True) -> list[str]:
        """Like `_run`, but for `status --porcelain` output specifically.

        `_run`'s `proc.stdout.strip()` is a whole-output strip, not a
        per-line one: on multi-line porcelain output (`" M calc.py\\n??
        scratch.txt\\n"`) it eats only the leading space off the FIRST
        line, silently shortening its 3-char `XY ` status prefix to 2 —
        every prefix-sliced caller (`line[3:]`) then truncates that one
        entry's path by a character (`"calc.py"` -> `"alc.py"`). Splitting
        on `"\\n"` and dropping only the trailing newline (never a global
        `.strip()`) keeps every line's prefix width intact."""
        cmd = [
            "git",
            "-c", f"user.name={self.identity_name}",
            "-c", f"user.email={self.identity_email}",
            *args,
        ]
        proc = subprocess.run(
            cmd, cwd=self.path, capture_output=True, text=True
        )
        if check:
            # Same inline lock-contention retry as `_run` (see the note there).
            for backoff in _GIT_RETRY_BACKOFFS_S:
                if proc.returncode == 0 or not is_transient_git_failure(proc.stderr):
                    break
                time.sleep(backoff)
                proc = subprocess.run(
                    cmd, cwd=self.path, capture_output=True, text=True
                )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return [line for line in proc.stdout.split("\n") if line]

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

    def resolve_commitish(self, name: str) -> str | None:
        """`name` if it resolves to a commit here, else `origin/name`, else None.

        A branch NAME the caller derived (a project's default branch, say) may
        have no local ref — single-branch clone, renamed remote default — while
        `origin/<name>` is present and current. Git does not DWIM that inside
        `worktree add` or `checkout -B <new> <base>`, so callers resolve first.
        `^{commit}` makes --verify an EXISTENCE check, not a shape check."""
        for cand in (name, f"origin/{name}"):
            if self._run("rev-parse", "--verify", "--quiet",
                         f"{cand}^{{commit}}", check=False):
                return cand
        return None

    def create_branch(self, name: str, *, base: str | None = None) -> str:
        if _branch_protected(name, self.never_push_to):
            raise ProtectedBranch(f"refusing to create protected branch: {name}")
        # Single command: create/reset `name` at `base` and check it out. Using
        # `base` only as a start-point (not `git checkout base` first) keeps this
        # worktree-safe — checking out the base branch directly fails when it is
        # already checked out in the primary worktree.
        if base:
            # A derived base with no local ref falls through to origin/<base>
            # (review finding F1's second site). A base that resolves NOWHERE
            # still goes to git verbatim so the failure is loud and names it.
            self._run("checkout", "-B", name, self.resolve_commitish(base) or base)
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

    def reset_workspace(self) -> list[str]:
        """Discard EVERYTHING uncommitted in this checkout — staged/unstaged
        changes to tracked files AND untracked files/dirs — so a `checkout`/
        `checkout -B` right after this call can never fail with "Your local
        changes would be overwritten by checkout". Agent worktrees only:
        callers must prove ownership first (`core/worktree.is_agent_worktree`)
        before calling this — it is destructive and unconditional.

        Deliberately `clean -fd`, never `-x`: ignored artifacts (`.venv`,
        `node_modules`, `dist`, caches) are expensive to rebuild and never
        block a checkout, so they are left alone. `-e .no_human` preserves
        the agent's own scratch/plan sidecar.

        Runs entirely with `check=False` — a locked index or unreadable tree
        must not raise here; the `checkout`/`checkout -B` that follows is
        left to surface that failure loudly, as it does today. Returns the
        relative paths that were dirty before the reset (empty on an
        already-clean tree — idempotent)."""
        status_lines = self._run_porcelain_lines(
            "status", "--porcelain", "--", ".", *self._EPHEMERAL, check=False)
        discarded: list[str] = []
        for line in status_lines:
            rel = line[3:].strip() if len(line) > 3 else ""
            if rel.startswith('"') and rel.endswith('"'):
                rel = rel[1:-1]
            if " -> " in rel:            # rename: "old -> new"
                rel = rel.split(" -> ", 1)[1]
            if rel:
                discarded.append(rel)
        self._run("reset", "--hard", check=False)
        self._run("clean", "-fd", "-e", ".no_human", check=False)
        return discarded

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
        instead of opening a broken PR.

        A guard must not share BOTH predicates with the code path it guards.
        `commit_paths` decides what to stage using `_CODE_EXTS` and the
        edit-hook-populated `coder_touched` set; a guard built on only those
        same two predicates is blind in exactly the region where the commit
        is blind — which is why a Bash-created, non-code file (a harness's
        own generated report, written beside the harness it came from) was
        silently dropped 20 times across 15 tasks and flagged by this guard
        zero of those times. The third predicate below
        (`_dirs_newly_added_by_head`) is derived from the COMMIT ITSELF —
        which directories HEAD just brought into existence — an input
        `commit_paths` never consults when deciding what to stage, so it can
        catch the class the other two predicates cannot."""
        out = self._run("status", "--porcelain", check=False).strip()
        newly_added_dirs = self._dirs_newly_added_by_head()
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
            elif (coder_touched and rel in coder_touched) or (
                str(Path(rel).parent) in newly_added_dirs
            ):
                # B2 #7: a non-code leftover the coder ITSELF touched
                # (Dockerfile, yaml, requirements, proto) is intentional work
                # missing from the commit — the favicon incident class in its
                # non-code shape. A leftover sitting in a directory HEAD just
                # created is the same incident class again, caught without
                # relying on the edit hook at all (see docstring above).
                # Untouched non-code leftovers in PRE-EXISTING directories
                # stay ignored: they are test side-effects, exactly why
                # _CODE_EXTS exists (see its comment).
                leftovers.append(rel)
        return leftovers

    def _dir_absent_from_tree(self, rel_dir: str, treeish: str) -> bool:
        """True if *rel_dir* has no entries in *treeish* — i.e. *treeish*
        does not (yet) know this directory exists.

        The repo root (`""`/`"."`) always exists, so it is never "absent".
        An unresolvable *treeish* (unborn repo, root commit with no parent)
        gives no evidence either way, so this degrades to False — the
        conservative, exclude-leaning answer used everywhere else in this
        class — rather than raising or guessing "new"."""
        if not rel_dir or rel_dir == ".":
            return False
        if not self._run("rev-parse", "--verify", treeish, check=False).strip():
            return False
        out = self._run("ls-tree", treeish, "--", rel_dir + "/", check=False).strip()
        return out == ""

    def _dirs_newly_added_by_head(self) -> set[str]:
        """Directories that HEAD's commit brought into existence — present
        among the files HEAD touched, absent from HEAD^'s tree.

        Root commit (no HEAD^ to diff against): every directory among HEAD's
        files counts as newly added — there is no prior tree it could have
        existed in. Unborn repo (no HEAD at all): returns the empty set, the
        same conservative degrade as `_dir_absent_from_tree`."""
        names = self._run(
            "show", "--pretty=format:", "--name-only", "HEAD", check=False
        )
        dirs = {str(Path(n).parent) for n in names.splitlines() if n.strip()}
        dirs.discard(".")
        if not dirs:
            return dirs
        if not self._run("rev-parse", "--verify", "HEAD^", check=False).strip():
            return dirs  # root commit: no HEAD^, so all of HEAD's dirs are new
        return {d for d in dirs if self._dir_absent_from_tree(d, "HEAD^")}

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
        rejected_non_code: list[str] = []
        for u in untracked:
            u = u.strip()
            if not u:
                continue
            ext = Path(u).suffix.lower()
            if ext in self._CODE_EXTS:
                rel_paths.append(u)
            else:
                rejected_non_code.append(u)
        # Stage non-code untracked files too, but only when they are almost
        # certainly a DELIVERABLE rather than a test side-effect. A file is a
        # deliverable when the same commit is (a) newly creating the
        # directory it sits in — this repo had no such directory before —
        # AND (b) already staging another file into that same directory: the
        # coder built a new directory for its work and filled it (the
        # measured case: a new eval directory holding a harness module plus
        # the markdown report that running it produces — dropped silently 20
        # times across 15 tasks, because a generated report carries no code
        # extension). A test side-effect
        # instead lands in a PRE-EXISTING directory (`teams/test/alert-
        # state.json`) or in a directory holding nothing else the commit is
        # staging (`data/state.json`) — running the suite does not also
        # author source next to its own state file. Both halves are
        # load-bearing: dropping (b) would commit `data/state.json`, which
        # `test_commit_paths_excludes_untracked_json_side_effects` pins as
        # excluded. This is suffix-agnostic, so it covers extensionless
        # deliverables (Dockerfile, Makefile) for free.
        staged_dirs = {str(Path(r).parent) for r in rel_paths}
        for u in rejected_non_code:
            if self._is_ephemeral_path(u):
                continue
            d = str(Path(u).parent)
            if d in staged_dirs and self._dir_absent_from_tree(d, "HEAD"):
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

    def is_ancestor(self, sha: str, descendant: str) -> bool:
        """True when *sha* is reachable from *descendant* (it is its own).

        Used to tell a review verdict that judged THIS branch head from one
        that judged another attempt's commit. An unknown/garbage ref is False,
        never an exception: "cannot prove it" and "not an ancestor" are the
        same answer for a claim we are deciding whether to print.
        """
        if not sha or not descendant:
            return False
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, descendant],
            cwd=self.path, capture_output=True, text=True,
        )
        return proc.returncode == 0

    def remote_branch_relation(self, branch: str, *, remote: str = "origin",
                                timeout: int = 30) -> str:
        """How local ``branch`` relates to its own tip on ``remote``.

        Returns ``"behind"`` (local is an ancestor of the remote tip — the
        remote holds commits this tree does not, e.g. an earlier attempt's
        push), ``"diverged"`` (neither is an ancestor of the other, e.g. a
        local rebase of an already-pushed branch), ``"up_to_date"``, or
        ``"unknown"`` when the relation cannot be determined (branch never
        pushed, remote unreachable, or its object isn't available locally —
        fail-open to today's behaviour: nothing here BLOCKS a force, only a
        *proven* ancestry does).

        Deliberately does NOT touch ``refs/remotes/<remote>/<branch>`` — that
        is the ref `push`'s ``--force-with-lease`` is judged against, and
        refreshing it here would make the lease vacuous (see `push`'s
        docstring). The remote tip is read with `ls-remote` (no ref written),
        and if its object is missing locally it is fetched into a private
        namespace (`refs/no_human/push-check/<branch>`), never the tracking
        ref — this requires ``--refmap=`` on that fetch: git applies the
        remote's *configured* fetch refspec (`+refs/heads/*:refs/remotes/
        <remote>/*`) to update tracking refs IN ADDITION to an explicit
        refspec argument, so a plain ``git fetch <remote> <explicit-dest>``
        silently updates the tracking ref anyway (confirmed empirically);
        only an empty ``--refmap=`` disables that and writes exclusively to
        the private destination.
        """
        local = self._run("rev-parse", branch, check=False)
        if not local:
            return "unknown"
        ls = subprocess.run(
            ["git", "ls-remote", remote, f"refs/heads/{branch}"],
            cwd=self.path, capture_output=True, text=True, timeout=timeout,
        )
        if ls.returncode != 0 or not ls.stdout.strip():
            return "unknown"
        remote_sha = ls.stdout.split()[0]
        if remote_sha == local:
            return "up_to_date"
        have_obj = subprocess.run(
            ["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"],
            cwd=self.path, capture_output=True, text=True,
        )
        if have_obj.returncode != 0:
            private_ref = f"refs/no_human/push-check/{branch}"
            fetched = subprocess.run(
                ["git", "fetch", "--refmap=", remote,
                 f"+refs/heads/{branch}:{private_ref}"],
                cwd=self.path, capture_output=True, text=True, timeout=timeout,
            )
            if fetched.returncode != 0:
                return "unknown"
            have_obj = subprocess.run(
                ["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"],
                cwd=self.path, capture_output=True, text=True,
            )
            if have_obj.returncode != 0:
                return "unknown"
        return "behind" if self.is_ancestor(local, remote_sha) else "diverged"

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
             set_upstream: bool = True, force_with_lease: bool = False) -> str:
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

        ``force_with_lease`` exists for ONE caller: a delivery retry whose plain
        push was rejected non-fast-forward because the agent REBASED its own
        already-pushed branch. That is not a corner case — `git reflog` on a
        stranded branch reads ``rebase (finish): refs/heads/no-human/<id> onto
        <new main>``, and `agent/guard.py` deliberately permits that rebase as
        "the legitimate rebase/merge base into my branch workflow". A rebased
        branch cannot be fast-forwarded; force is the ONLY correct push, and
        without it the attempt's reviewed, green work is thrown away. Measured
        2026-08-11: 81 rejections in one week, and 0 of the 7 tasks that ever
        hit one reached `done`.

        Two safety properties, both load-bearing:

        * The protected-branch check above runs FIRST and is untouched, so this
          can never force main/master/release — constraint #2 is unaffected.
        * The lease is judged against the remote-tracking ref, and there must
          be NO fetch of that ref beforehand. The tracking ref records the
          remote as of our last contact — the push earlier in this same
          attempt. If the remote still matches it, only OUR own rebase moved
          the branch and the force is safe; if it does not (someone else
          pushed — a human fixup, another attempt), git refuses with
          ``stale info`` and that refusal reaches the caller as the ordinary
          push failure, which the delivery retry escalates honestly. A fetch
          here would refresh the tracking ref to the remote's current value,
          making the comparison always match — the lease degrades to a plain
          ``--force`` and would destroy exactly the commit it exists to
          protect. Proven both ways by tests: the benign own-rebase delivers,
          the unseen third-party commit is refused, and a missing tracking
          ref refuses rather than guesses.

        A THIRD safety property, ahead of the lease itself: ``--force-with-
        lease`` does NOT protect a branch that is merely BEHIND its remote.
        The lease only checks that the remote-tracking ref still matches the
        actual remote — and a behind branch's tracking ref is perfectly
        current (nothing has touched it), so the lease is satisfied and the
        force proceeds anyway, destroying the remote's commits. The flag's
        name suggests otherwise; only ancestry (`remote_branch_relation`)
        tells a behind branch apart from a legitimately rebased one. Because
        a force is requested up front here, no `GitError` will ever be
        raised for git to reject — the failure-path classification below
        never runs — so this ancestry check has to happen BEFORE the push,
        not after. It is gated on `force_with_lease` so an ordinary,
        non-forcing push keeps its current cost: no extra `ls-remote`
        round-trip.
        """
        branch = branch or self.current_branch()
        if _branch_protected(branch, self.never_push_to):
            raise ProtectedBranch(f"refusing to push protected branch: {branch}")
        sha = self._run("rev-parse", branch)
        args = ["push"]
        if force_with_lease:
            try:
                relation = self.remote_branch_relation(branch, remote=remote)
            except Exception:
                # Fail OPEN: an unreachable remote, an unpushed branch, or an
                # auth failure must never silently disable delivery — only a
                # *proven* "behind" blocks (same contract as "unknown" below).
                relation = "unknown"
            if relation == "behind":
                raise PushBehindRemote(_behind_message(branch, remote, sha))
            # "unknown" / "diverged" / "up_to_date" all fall through and
            # force, exactly as today.
            # NO fetch here — see the docstring: refreshing the tracking ref
            # is what would make the lease vacuous.
            args += ["--force-with-lease"]
        if set_upstream:
            args += ["-u"]
        args += [remote, branch]
        try:
            self._run(*args)
        except GitError as exc:
            if _looks_like_non_fast_forward(str(exc)) and \
                    self.remote_branch_relation(branch, remote=remote) == "behind":
                raise PushBehindRemote(_behind_message(branch, remote, sha)) from exc
            raise
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
        # Second enforcement point. Every agent worktree comes through here
        # (the orchestrator's `_acquire_worktree` and the `worktree()` scope
        # both call it), so this is where the pre-push hook goes: the string
        # matcher in `agent/guard.py` cannot resolve `git push origin $(echo
        # main)`, and the hook -- which reads the ref git already resolved --
        # does not have to. Failure to install is fatal on purpose: a worktree
        # handed to an agent without it has only the string matcher.
        install_pre_push_guard(wt, list(self.never_push_to))
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
