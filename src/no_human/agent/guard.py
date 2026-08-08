"""PreToolUse safety guard — pure, testable policy (PLAN.md Part 10).

Blocks, before execution:
  - writes/edits to forbidden paths (.env, secrets/, *.key, *.pem, ...)
  - pushes/force-updates to protected branches (never_push_to) — which must
    include the PR's *base* branch, or "never merge" is trivially bypassed by
    pushing straight to it
  - merging a pull/merge request (`gh pr merge`, `glab mr merge`, the
    equivalent REST call, and the product's own `nh merge-stack run`, which
    shells `gh pr merge` per ready PR). This is constraint §3.2 — the agent
    never merges — and until 2026-07-10 nothing enforced it: only `git merge`
    was blocked, which is not how a PR gets merged. The `nh merge-stack run`
    spelling was the same hole again, open until 2026-08-08 (P3 gap G6).
  - destructive shell (`rm -rf`, history rewrites) — a circuit breaker that
    fires even under bypass permissions
  - git that overwrites or discards WORKING-TREE content the agent did not
    create (`git stash`, `git restore`, `git checkout -- <path>`, `git clean
    -fd`, `git checkout-index -f`, ...) — in every session, coder included
  - interactive prompts (`AskUserQuestion`) — nobody is at the keyboard (§22)
  - background polling (`Monitor`, `TaskStop`, `ToolSearch`) in a read-only
    session — a planner does not need to busy-wait on its own subagents

Deliberately allowed (user, 2026-07-10): the agent may `git commit`, `git push`
its own branch, `git merge` another ref *into* that branch, and open a PR. Only
merging the PR is forbidden. A local merge cannot reach a protected branch
because the push is denied, so the branch-level `git merge` ban was protecting
nothing and blocked the legitimate "rebase/merge base into my branch" workflow.

The orchestrator wraps :func:`evaluate` in a Claude Agent SDK PreToolUse hook;
keeping the policy a pure function lets us unit-test it without the SDK.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

# Read the platform through a constant, never an inline `os.name` test, so the
# Windows branch below is reachable from a test on any host.
_IS_WINDOWS = os.name == "nt"

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

# Phase C — the measured whale: the coder re-reads 57.8k tokens EVERY turn
# (76 real attempts: 2.21M cache-read / 33.2 turns). The seed is ~6.4k of it;
# the rest is accumulated tool output, and an unbounded whole-file Read of a
# huge file is re-sent on every subsequent turn for the rest of the attempt.
# So a Read with no limit on a file above this many lines is REDIRECTED (never
# denied — a denial would just loop) to the scoped form the coder already
# knows: offset/limit, or Grep to find the relevant region first.
_READ_LINES_BUDGET = 2000
# Data/config/lock files often NEED whole-file context (a partial parse is
# useless) and are rarely re-read many times — exempt from the read redirect.
_WHOLE_READ_OK_EXTS = frozenset((
    "json", "yaml", "yml", "toml", "lock", "csv", "xml", "sql", "txt", "md",
))

# Tools that block on a human answer. Every no_human session is headless, so
# these silently return nothing and the agent fills the gap with a guess — we
# observed a planner proposer call AskUserQuestion, get no answer, and write
# "No answer given — I'll default to ...". Denying is only half the fix: the
# reason has to tell the agent what to do instead, or it just retries.
INTERACTIVE_TOOLS = {"AskUserQuestion"}

# Background-orchestration tools. A read-only session (planner, aggregator,
# reviewer) explores and reports; it never needs to run work in the background
# and poll for it. Observed in run 0305e5ce/087e2d3a: the `test-first` proposer
# used ToolSearch to discover Monitor and TaskStop, spawned two real research
# subagents, then spawned FIVE more subagents whose entire job was to wait for
# them ("idle wait for agent a422b247…", "idle until agent 2 completes"). It
# burned 100 events against the `minimal-first` lens's 22 for the same one plan
# draft, and gathered nothing with any of it.
#
# The `Agent` tool is deliberately NOT denied: it returns its result directly.
# The `risk-first` lens proves it — three subagents, zero Monitor/TaskStop calls,
# draft delivered.
BACKGROUND_TOOLS = {"Monitor", "TaskStop", "ToolSearch"}

_NO_POLLING_REASON = (
    "{tool} is unavailable in a read-only session. The Agent tool returns its "
    "result to you directly when the subagent finishes — do not spawn helpers "
    "to wait for it, and do not poll. Read, Grep, Glob, Bash and Agent are all "
    "you need; use them and write your report."
)

_NO_HUMAN_REASON = (
    "{tool} is unavailable: this session is headless and no human will ever "
    "answer it. Do not retry it and do not silently guess. If you cannot make "
    "verifiable progress without the answer, stop and emit a structured blocker "
    "report (BLOCKER_JSON_START/BLOCKER_JSON_END) with the question in the "
    "'question' field and your candidate answers in 'options'. Otherwise pick "
    "the most defensible option and state the assumption, and the evidence for "
    "it, explicitly in your output."
)

# Destructive shell patterns — matched against the full command string.
# D2 #2: deleting test files via the shell used to surface only at the
# END-of-attempt tamper gate — after the whole attempt's tokens were spent.
# Denied at tool time instead; `git mv` renames stay allowed. Covers rm /
# git rm on tests/ dirs and test_*/**.test.* file shapes.
_RM_TESTS = re.compile(
    r"\b(?:git\s+)?rm\b[^|;&]*?"
    # a tests/ dir, or a source test file — but NOT build/coverage artifacts
    # (dist/coverage/*.map, test_results.xml). Require a source extension.
    r"(?:\btests?/|/tests?/|"
    r"\btest_[\w-]+\.(?:py|js|mjs|cjs|jsx|ts|tsx|rb|go|rs|java)(?![\w.])|"
    r"[\w-]+\.test\.(?:js|mjs|cjs|jsx|ts|tsx)(?![\w.]))",
    re.IGNORECASE)
# The repo's no_human config is the operator's contract with the agent — the
# agent never edits it (add-or-tighten is a HUMAN action).
_NO_HUMAN_YML_WRITE = re.compile(
    r"(?:sed\s+-i|>\s*|>>\s*|tee\s+)[^|;&]*\.no_human\.ya?ml", re.IGNORECASE)

_RM_RF = re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|-rf|-fr)\b")
_GIT_DESTRUCTIVE = re.compile(
    r"\bgit\s+(push\s+.*--force|push\s+.*-f\b|reset\s+--hard\s+\S|"
    r"clean\s+-[a-z]*f|filter-branch|update-ref\s+-d)"
)

# A live product server/runner launched from an agent session. `nh serve` /
# `nh start` run against the OPERATOR's ~/.no_human (config, DB, credentials)
# no matter which checkout they start from — live incident 2026-07-24: a coder
# verifying serve flags launched a real `nh serve` from its worktree and its
# Jira poller mass-imported 16 duplicate tasks into the production board.
# `nh watch` runs a real task; `nh bench run` runs the real pipeline. CLI
# behavior is tested through the CliRunner suite, never a live process. The
# command position (start of string or after a separator/path) keeps prose
# like `echo nh serve …` allowed.
_LIVE_SERVER = re.compile(
    r"(?:^|[|;&]\s*|`|\$\(|^\s*|/|\bsudo\s+|\benv\s+[^|;&]*?\s)"
    r"nh\s+(?:serve|start|watch|bench\s+run)\b"
)

# Merging the PR — the one action that is always a human's (§3.2). `git merge`
# is NOT this: a PR is merged through the forge, and that is what must be denied.
_FORGE_MERGE = re.compile(
    r"\b(?:gh\s+pr\s+merge"           # gh pr merge 7004 --squash
    r"|glab\s+mr\s+merge"             # glab mr merge 12
    r"|gh\s+api\b[^|;&]*?/(?:pulls|merge_requests)/\d+/merge"  # the REST call
    r"|glab\s+api\b[^|;&]*?/merge_requests/\d+/merge)"
)

# The product's OWN spelling of the same act: `nh merge-stack run` shells
# `gh pr merge` for every READY PR in the stack (cli/commands.py,
# `merge_stack_run`). It is the OPERATOR's command — a human drives the stack —
# so in an agent session it is denied in EVERY mode, exactly like
# `_FORGE_MERGE` above. Proven live 2026-08-08 (P3 gap G6): the guard returned
# allow=True for `nh merge-stack run --yes` while denying every direct
# spelling. Unlike `_LIVE_SERVER`, this is NOT anchored to a command position:
# `uv run nh merge-stack run` and `sh -c "nh merge-stack run"` are the same
# merge one wrapper deeper, and for the merge family a prose false positive
# (an echo or a commit message quoting the full literal command) costs one
# denial with a stated alternative, while a miss merges a PR — the same
# polarity `_FORGE_MERGE` already accepts for `gh pr merge` in a quoted
# string. `plan` and `link` only read/record order and are not matched.
_MERGE_STACK_RUN = re.compile(r"(?<![\w.-])nh\s+merge-stack\s+run\b")

# Any git/forge command that mutates history or a remote. A read-only session
# (planner, aggregator, reviewer) explores and reports; it never writes. Dropping
# the blanket `git merge` ban would otherwise have let a reviewer commit.
_GIT_WRITE = re.compile(
    r"\bgit\s+(?:commit|push|merge|rebase|cherry-pick|revert|am|apply|tag"
    r"|reset|restore|stash|branch|checkout|switch)\b"
)
_FORGE_WRITE = re.compile(
    r"\b(?:gh\s+pr\s+(?:create|merge|close|edit|ready|review)"
    r"|glab\s+mr\s+(?:create|merge|close|update))\b"
)


@dataclass
class GuardDecision:
    allow: bool
    reason: str = ""


def _path_forbidden(path: str, forbidden: list[str]) -> bool:
    # Every rule below is written in `/` terms, but on Windows the SDK hands us
    # `C:\repo\secrets\key.pem` — so `norm.rsplit("/")` returned the WHOLE path
    # as the basename and no prefix rule could ever match. A guard that stops
    # matching is a guard that fails OPEN, so normalise the separator first.
    # Only on Windows: a backslash is a legal (if rare) character in a POSIX
    # filename and rewriting it there would change what the guard matches.
    if _IS_WINDOWS:
        path = path.replace("\\", "/")
    norm = path.removeprefix("./").rstrip("/")
    base = norm.rsplit("/", 1)[-1]
    for pat in forbidden:
        p = pat.rstrip("/")
        if fnmatch.fnmatch(norm, p) or fnmatch.fnmatch(base, p):
            return True
        # directory prefix match: "secrets/" forbids "secrets/x/y"
        if pat.endswith("/") and (norm == p or norm.startswith(p + "/")):
            return True
        if "/" in norm and (norm.startswith(p + "/") or f"/{p}/" in f"/{norm}"):
            return True
    return False


def _branch_protected(branch: str, never_push_to: list[str]) -> bool:
    b = branch.strip().removeprefix("origin/").removeprefix("refs/heads/")
    return any(fnmatch.fnmatch(b, pat) for pat in never_push_to)


def _push_targets_protected(cmd: str, never_push_to: list[str]) -> bool:
    """Detect `git push ... <branch>` / `git checkout <protected>` to a guarded ref."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    # any token equal to a protected branch name in a push/checkout context
    if "push" in tokens or "checkout" in tokens or "switch" in tokens:
        for tok in tokens:
            if tok.startswith("-"):
                continue
            # handle refspecs like HEAD:main or local:remote
            for ref in re.split(r"[:]", tok):
                if _branch_protected(ref, never_push_to):
                    return True
    return False


# --------------------------------------------------------------------------- #
# `git push` analysis — defence in depth under the pre-push hook.
#
# The matcher above is lexical, and lexical analysis cannot resolve shell
# expansion: `git push origin $(echo main)`, `B=main; git push origin $B` and
# `git push origin `echo main`` all reach `main` while carrying no token that
# reads as `main`. The real fix is the per-worktree pre-push hook installed by
# `vcs/push_hook.py`, which reads the ref git ALREADY RESOLVED. What is added
# here is the lexical half of that pair: an argv we cannot resolve, or one that
# disarms the hook, is refused instead of waved through.
# --------------------------------------------------------------------------- #

#: Command separators. Splitting before quote-parsing can cut inside a quoted
#: string; a fragment produced that way simply fails the "argv[0] is git" test.
_CMD_SEP = re.compile(r"(?:\|\||&&|[;\n|&])")

#: Leading words that prefix a real command without being it.
_WRAPPERS = frozenset({"env", "sudo", "nohup", "command", "exec", "time", "builtin"})

#: `$` and backtick: command substitution, variable and arithmetic expansion.
#: Any of them in a `git push` argv means the resolved ref is not knowable here.
_UNRESOLVABLE = re.compile(r"[$`]")

#: `--no-verify` skips pre-push hooks outright; `-c core.hooksPath=...` and
#: `--git-dir`/`--exec-path` relocate or neuter them. A push that disables the
#: enforcement point below this one is refused at this one.
_HOOK_DISARM = re.compile(
    r"--no-verify\b|core\.hookspath\b|--git-dir\b|--exec-path\b", re.IGNORECASE
)


def _looks_like_git_push(text: str) -> bool:
    return bool(re.search(r"\bgit\b.*\bpush\b", text, re.DOTALL))


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """argv with leading `VAR=value` assignments and `_WRAPPERS` words removed.

    A wrapper may carry its own flags and flag VALUES (`env -i`, `sudo -u me`),
    and their option grammars differ per tool, so no attempt is made to parse
    them: when what follows a wrapper still starts with a flag, the argv is
    taken from the first token that names `git` or a shell runner — the only
    argv[0] values any caller of this function acts on. Skipping the unparsed
    middle can only widen what is analysed, never allow more.
    """
    i = 0
    saw_wrapper = False
    while i < len(tokens) and (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[i])
        or tokens[i] in _WRAPPERS
    ):
        saw_wrapper = saw_wrapper or tokens[i] in _WRAPPERS
        i += 1
    argv = tokens[i:]
    if saw_wrapper and argv and argv[0].startswith("-"):
        for j, tok in enumerate(argv):
            name = PurePosixPath(tok).name
            if name == "git" or name in _SHELL_RUNNERS:
                return argv[j:]
    return argv


def _git_push_invocations(cmd: str, _depth: int = 0) -> list[tuple[str, list[str]]]:
    """Every `git ... push ...` invocation in ``cmd``, as (segment, argv).

    Recurses up to two levels into quoted arguments so `sh -c "git push origin
    $B"` is analysed rather than dismissed because argv[0] is `sh`. Bounded
    depth — a guard must not become a parser with unbounded work.
    """
    found: list[tuple[str, list[str]]] = []
    for seg in _CMD_SEP.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        argv = _strip_wrappers(tokens)
        if argv and PurePosixPath(argv[0]).name == "git" and "push" in argv:
            found.append((seg, argv))
            continue
        if _depth < 2:
            # `sh -c "..."`, `bash -lc "..."`, `xargs git push ...` etc.
            for tok in argv[1:] if argv else []:
                if _looks_like_git_push(tok):
                    found.extend(_git_push_invocations(tok, _depth + 1))
    return found


def _push_denial_reason(cmd: str, never_push_to: list[str]) -> str | None:
    """Why this command's `git push` must be denied, or None to allow it."""
    for seg, argv in _git_push_invocations(cmd):
        if _HOOK_DISARM.search(seg):
            return (
                f"push blocked: {seg}. It disables or relocates the pre-push "
                "hook (--no-verify / core.hooksPath / --git-dir / --exec-path), "
                "which is the check that enforces protected branches after git "
                "has resolved the refspec. Push with a plain `git push "
                "<remote> <branch>`."
            )
        if _UNRESOLVABLE.search(seg):
            return (
                f"push blocked: {seg}. The branch is produced by shell "
                "expansion (`$...` or a backtick), so this guard cannot tell "
                "which ref it resolves to and refuses rather than guess. Push "
                "a literal branch name — e.g. `git push origin "
                "my-task-branch` — not a substituted or variable one."
            )
        if _push_targets_protected(" ".join(argv), never_push_to):
            return (
                f"push to protected branch blocked: {seg}. Push to your own "
                "branch and open a PR instead — pushing to the base branch is "
                "merging without review."
            )
    return None


# --------------------------------------------------------------------------- #
# Destructive WORKING-TREE git — denied in EVERY session, the coder included.
#
# `_GIT_WRITE` above is applied only when `readonly` is set, so until now the
# CODER could run `git stash`, `git restore`, `git checkout -- <path>` and
# `git clean -fd` with nothing between it and the working tree but a sentence
# in its prompt. Observed 2026-08-06, benchmark spec ns-1746bea3: the coder was
# told "Do NOT run any git command", ran `git stash` then `git stash pop`,
# popped a PRE-EXISTING unrelated stash, and left 1041 lines of a foreign app
# in the working tree; its own `git reset -- <paths>` remediation only unstaged
# them. The independent judge failed the spec. The guard never fired.
#
# THE PROPERTY ENCODED HERE — not a list of the six verbs that happened to hurt:
#   never let git overwrite or discard working-tree content the agent did not
#   create in this session.
# A denylist of names cannot hold that property: the seventh spelling
# (`git checkout-index -f`, `git read-tree -u`, `git sparse-checkout set`,
# `git submodule update --force`, and whatever git adds next) is destructive on
# arrival and unlisted by construction. So the polarity is INVERTED. A git
# subcommand runs only if it is on `_GIT_WORKTREE_SAFE` — the subcommands that
# *cannot* clobber the tree, because they only read, only touch the index/refs,
# or only ADD content. Anything else is denied, including subcommands nobody
# here has heard of. A missing entry costs the agent one denial with a stated
# alternative; a missing DENYLIST entry costs a wiped working tree.
#
# That default-deny covers every git SUBCOMMAND — but only for invocations this
# analysis can SEE, and the analysis is lexical, with the same limits as the
# `git push` section above. What it follows: compound commands, `VAR=x` and
# wrapper prefixes (wrapper FLAGS included — `env -i git stash`,
# `sudo -u me git stash`), absolute paths to git, git's own global options, and
# nested shell runners (`sh -c "git stash"`, `xargs git restore`) two levels
# deep. What it cannot follow: an argv[0] the shell assembles at run time —
# `g=git; $g stash`, `$(which git) stash`, a shell alias/function, or an
# interpreter (`python -c "shutil.rmtree(...)"`, which needs no git at all).
# A SUBCOMMAND produced by expansion (`git $(echo stash)`) IS refused, because
# default-deny finds no safe subcommand to allow; a laundered argv[0] is out of
# scope here, exactly as it is for push, where the pre-push hook is the
# post-resolution backstop. This rule's backstop is narrower: the session's
# worktree is disposable and the PR diff is reviewed, so what a laundered
# invocation can destroy is bounded to the session's own copy.
#
# A dozen subcommands are genuinely dual-purpose — the same name is the coder's
# legitimate workflow AND the destructive operation — so they get a form rule
# rather than a verdict (`_GIT_DUAL_RULES`):
#   checkout   `-b feature/x`, `feature/x`  ALLOWED · `-- path`, `.`, `-f`  DENIED
#   switch     `-c x`, `x`                  ALLOWED · `--discard-changes`   DENIED
#   reset      (no flag)/`--soft`/`--mixed` ALLOWED · `--hard/--merge/--keep` DENIED
#   clean      `-n`/`--dry-run`             ALLOWED · every deleting form    DENIED
#   worktree   `list`                       ALLOWED · add/remove/move        DENIED
#   apply      a patch                      ALLOWED · `-R`/`--reverse`       DENIED
#   merge / rebase / cherry-pick / revert / am / pull
#              the operation, `--continue`  ALLOWED · `--abort`/`--skip`/
#                                                     `--autostash`          DENIED
# --------------------------------------------------------------------------- #

#: git's OWN options, which sit before the subcommand. `git -C /repo stash` and
#: `git --git-dir=x stash` must resolve to the subcommand `stash`, not to `-C`.
#: These take a separate value word, so the word after them is skipped too.
_GIT_GLOBAL_OPT_WITH_ARG = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--super-prefix", "--config-env", "--attr-source",
})

#: Subcommands that cannot overwrite or discard working-tree content: they read
#: (status/log/diff/...), or they write only the index, refs or objects
#: (add/commit/push/tag/...), or they add content that did not exist.
#: merge/rebase/cherry-pick/revert/am/pull are NOT here: the operations refuse
#: to START on a dirty tree, but their `--abort`/`--skip` forms restore the
#: pre-operation state with `reset --hard` semantics — PROVEN destructive:
#: after a conflicted merge, `git add survivor.txt; git merge --abort` deletes
#: survivor.txt from disk — and `--autostash` stash-pops around the operation,
#: which can drop or conflict uncommitted work. They carry a form rule in
#: `_GIT_DUAL_RULES` instead. `rm`/`mv` delete or move files, but that is a
#: deliberate authored edit — the same act as Write/Edit — and it is fenced
#: separately by `_RM_TESTS` and `_RM_RF`.
_GIT_WORKTREE_SAFE = frozenset({
    # inspection
    "status", "log", "diff", "show", "blame", "annotate", "describe",
    "shortlog", "whatchanged", "reflog", "rev-parse", "rev-list", "ls-files",
    "ls-tree", "ls-remote", "cat-file", "for-each-ref", "symbolic-ref",
    "name-rev", "merge-base", "diff-tree", "diff-files", "diff-index", "grep",
    "help", "version", "var", "count-objects", "check-ignore", "check-attr",
    "check-ref-format", "verify-commit", "verify-tag", "verify-pack",
    "patch-id", "range-diff", "cherry", "shortlog", "request-pull",
    # index / refs / objects only
    "add", "stage", "commit", "push", "branch", "tag", "fetch",
    "mv", "rm", "notes",
    "config", "remote", "init", "clone", "update-index", "update-ref",
    "hash-object", "mktree", "commit-tree", "write-tree", "format-patch",
    "bundle", "archive", "gc", "maintenance", "repack", "fsck", "stripspace",
    "interpret-trailers", "column", "mailinfo", "send-email",
})

#: Leading words of a nested shell we look INSIDE, so `sh -c "git stash"` is
#: analysed. Deliberately NOT "any token that mentions git" — that would deny
#: `echo "never run git stash"` and `git commit -m "revert the git stash"`,
#: which change nothing. argv[0] must actually be a runner.
_SHELL_RUNNERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "eval", "xargs", "timeout", "nice",
    "stdbuf", "script", "watch", "flock",
})

#: A checkout/switch/restore operand that names FILES rather than a ref. The
#: last clause is the load-bearing one: an operand that exists on disk is a
#: path, whatever it looks like — which is exactly the case that destroys work.
_PATHSPEC_GLOB = re.compile(r"[*?\[]")
_HAS_EXTENSION = re.compile(r"\.[A-Za-z0-9_]+$")


def _looks_like_pathspec(operand: str, cwd: str | None) -> bool:
    if operand in (".", "..", "*") or operand.startswith(("./", "../", "/")):
        return True
    if operand.endswith("/") or _PATHSPEC_GLOB.search(operand):
        return True
    if _HAS_EXTENSION.search(operand.rsplit("/", 1)[-1]):
        return True
    # Existence must be resolved in the SESSION'S WORKTREE — the cwd the
    # backend runs the command in — not this process's cwd, which is the
    # orchestrator's checkout and shares nothing with the session. When no cwd
    # is known, being wrong in one direction destroys work and in the other
    # costs one denial with a stated alternative, so: assume it is a path.
    if cwd is None:
        return True
    try:
        return os.path.exists(os.path.join(cwd, operand))
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False


def _git_subcommand(argv: list[str]) -> tuple[str, list[str]]:
    """(subcommand, remaining args) for a `git ...` argv, skipping git's own
    global options and their values. ("", []) when there is no subcommand."""
    i = 1
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("-"):
            return tok, argv[i + 1:]
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            i += 2
            continue
        i += 1
    return "", []


def _git_invocations(cmd: str, _depth: int = 0) -> list[tuple[str, list[str]]]:
    """Every `git ...` invocation in ``cmd``, as (segment, argv).

    Splits on shell separators so `cd /x && git stash` and `false || git reset
    --hard` are seen; strips `VAR=value` assignments and `_WRAPPERS` (their
    flags included) so `X=1 env git stash` and `env -i git stash` are seen;
    recurses up to two levels into nested shell runners so `sh -c "git stash"`
    is seen. Bounded depth — a guard must not become a parser with unbounded
    work.
    """
    found: list[tuple[str, list[str]]] = []
    for seg in _CMD_SEP.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        argv = _strip_wrappers(tokens)
        if not argv:
            continue
        name = PurePosixPath(argv[0]).name
        if name == "git":
            found.append((seg, argv))
        elif name in _SHELL_RUNNERS and _depth < 2:
            for j, tok in enumerate(argv[1:], start=1):
                # `sh -c "git stash"` — the command is one quoted token.
                if re.search(r"\bgit\s+\S", tok):
                    found.extend(_git_invocations(tok, _depth + 1))
                # `xargs git restore` / `timeout 30 git restore .` — the
                # command is the rest of THIS argv, already tokenised.
                elif PurePosixPath(tok).name == "git":
                    found.append((seg, argv[j:]))
                    break
    return found


def _checkout_clobbers(rest: list[str], cwd: str | None) -> bool:
    """`git checkout` in its overwrite-the-working-tree form.

    git's grammar is `checkout [<tree-ish>] [--] <pathspec>...` for the
    destructive form and `checkout [-b <new>] <branch>` for the one the coder
    needs. The discriminators, in order: a force/patch flag discards local
    modifications whatever the operands are; a `--` or `--pathspec-from-file`
    is the pathspec form by definition; a branch-creating flag is conclusively
    the safe form; two bare operands are `<tree-ish> <pathspec>`; one bare
    operand is destructive only if it names files rather than a ref — resolved
    against ``cwd``, the session's worktree.
    """
    flags = [t for t in rest if t.startswith("-")]
    if any(f in ("-f", "--force", "-p", "--patch", "--ours", "--theirs",
                 "--overlay", "--no-overlay", "--overwrite-ignore")
           or f.startswith("--pathspec-from-file")
           for f in flags):
        return True
    if "--" in rest:
        return True
    if any(f in ("-b", "-B", "--orphan", "-t", "--track") for f in flags):
        return False
    operands = [t for t in rest if not t.startswith("-")]
    if len(operands) >= 2:
        return True
    return bool(operands) and _looks_like_pathspec(operands[0], cwd)


def _switch_clobbers(rest: list[str], cwd: str | None) -> bool:
    return any(f in ("-f", "--force", "--discard-changes") for f in rest)


def _reset_clobbers(rest: list[str], cwd: str | None) -> bool:
    # --soft and --mixed (the default) never touch tracked files on disk;
    # --hard, --merge and --keep all write the working tree from a commit.
    return any(f in ("--hard", "--merge", "--keep") for f in rest)


def _clean_clobbers(rest: list[str], cwd: str | None) -> bool:
    # Only the dry run is not a deletion. `-n` may be bundled: `-nd`, `-ndx`.
    return not any(
        t == "--dry-run" or (t.startswith("-") and not t.startswith("--")
                             and "n" in t)
        for t in rest
    )


def _worktree_clobbers(rest: list[str], cwd: str | None) -> bool:
    operands = [t for t in rest if not t.startswith("-")]
    return not operands or operands[0] != "list"


def _apply_clobbers(rest: list[str], cwd: str | None) -> bool:
    # Applying a patch adds content; reversing one discards it.
    return any(f in ("-R", "--reverse") for f in rest)


def _sequencer_clobbers(rest: list[str], cwd: str | None) -> bool:
    """merge/rebase/cherry-pick/revert/am/pull. The OPERATION refuses to start
    on a dirty tree; what discards work is winding it back or wrapping it:
    `--abort`/`--skip` restore the pre-operation state with `reset --hard`
    semantics — after a conflicted merge, `git add survivor.txt; git merge
    --abort` deletes survivor.txt from disk — and `--autostash` (a flag on
    rebase, merge and pull) stash-pops around the operation, which can drop or
    conflict uncommitted work. `--continue`/`--quit` leave the tree alone.

    Matched as PREFIXES, not exact words: git's parse-options accepts any
    unique abbreviation, so `git merge --abor` performs the same destruction
    one character shorter. Every `--`-token at least three characters long
    that is a prefix of a denied option is denied too; git itself rejects the
    ambiguous ones, so the extra denials cost nothing. The CONFIG spelling of
    autostash (`-c rebase.autostash=true`, or `git config` then a plain
    `git rebase`) is not caught here — that is the config-side leg of the
    same behavior, out of this lexical rule's reach like every other
    non-argv spelling.
    """
    denied = ("--abort", "--skip", "--autostash")
    for f in rest:
        word = f.split("=", 1)[0]
        if word.startswith("--") and len(word) >= 3 and any(
                d.startswith(word) for d in denied):
            return True
    return False


#: subcommand -> predicate saying whether THIS invocation clobbers the tree.
_GIT_DUAL_RULES = {
    "checkout": _checkout_clobbers,
    "switch": _switch_clobbers,
    "reset": _reset_clobbers,
    "clean": _clean_clobbers,
    "worktree": _worktree_clobbers,
    "apply": _apply_clobbers,
    "merge": _sequencer_clobbers,
    "rebase": _sequencer_clobbers,
    "cherry-pick": _sequencer_clobbers,
    "revert": _sequencer_clobbers,
    "am": _sequencer_clobbers,
    "pull": _sequencer_clobbers,
}

_WORKTREE_ADVICE = (
    "You may not overwrite or discard working-tree content you did not create "
    "in this session. Leave other people's files alone: make your edits with "
    "Write/Edit, and if a change of yours is wrong, edit it back. Branching, "
    "committing, pushing and opening the PR are handled for you — you do not "
    "need git for them. If you genuinely cannot proceed without this, stop and "
    "emit a structured blocker report instead of running it."
)


def _git_worktree_denial(cmd: str, cwd: str | None = None) -> str | None:
    """Why this command's git invocation must be denied, or None to allow it.

    ``cwd`` is the SESSION's worktree — the directory the backend actually
    runs the command in — used to resolve whether a bare checkout operand
    names a file. This process's own cwd is the orchestrator's checkout and
    is never consulted.
    """
    for seg, argv in _git_invocations(cmd):
        sub, rest = _git_subcommand(argv)
        if not sub:
            continue
        rule = _GIT_DUAL_RULES.get(sub)
        if rule is not None:
            if rule(rest, cwd):
                return (
                    f"destructive working-tree git blocked: {seg}. "
                    f"`git {sub}` is allowed, but not in this form — this one "
                    f"writes over or throws away files in the working tree. "
                    + _WORKTREE_ADVICE
                )
            continue
        if sub not in _GIT_WORKTREE_SAFE:
            return (
                f"working-tree-unsafe git blocked: {seg}. `git {sub}` is not "
                f"one of the subcommands that provably cannot overwrite or "
                f"discard working-tree content, so it is denied by default "
                f"rather than allowed by omission. " + _WORKTREE_ADVICE
            )
    return None


def _line_count(path: str) -> int:
    """Lines in a file, or 0 when unknowable (missing/binary/unreadable) —
    the guard must never fail a tool call because it could not stat a file."""
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\n")
    except (OSError, ValueError):
        return 0


def evaluate(
    tool_name: str,
    tool_input: dict,
    *,
    forbidden_paths: list[str],
    never_push_to: list[str],
    readonly: bool = False,
    cwd: str | None = None,
) -> GuardDecision:
    """Return allow/deny for a single proposed tool call.

    ``cwd`` is the session's worktree — where the backend will actually run
    the command — so file-existence questions (is `git checkout notes` a
    branch switch or a wipe?) are answered about the right directory. Without
    it the guard answers those questions conservatively (deny).
    """
    # 0. Interactive prompts — denied in every role, readonly or not.
    if tool_name in INTERACTIVE_TOOLS:
        return GuardDecision(False, _NO_HUMAN_REASON.format(tool=tool_name))

    # 1. Reviewer / read-only mode: block ALL writes, and the polling tools that
    #    let a planner invent a busy-wait loop instead of doing its job.
    if readonly and tool_name in WRITE_TOOLS:
        return GuardDecision(False, f"read-only session: {tool_name} blocked")
    if readonly and tool_name in BACKGROUND_TOOLS:
        return GuardDecision(False, _NO_POLLING_REASON.format(tool=tool_name))

    # 1b. Phase C: unbounded reads of huge files are redirected, not denied.
    # ONLY in the coder loop (readonly reviewer/researcher sessions are one-shot
    # — no re-read cost; the researcher is the agent that ABSORBS big reads) and
    # NOT for data/config files that genuinely need whole-file context (review #6).
    if (not readonly and tool_name == "Read"
            and not tool_input.get("limit")):
        path = str(tool_input.get("file_path") or "")
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        n = _line_count(path)
        if n > _READ_LINES_BUDGET and ext not in _WHOLE_READ_OK_EXTS:
            return GuardDecision(
                False,
                f"{path} is {n} lines — reading it whole puts all of it in "
                f"context and RE-SENDS it on every remaining turn. Read the "
                f"part you need (offset/limit), or Grep for the symbol first. "
                f"Whole-file reads are fine under {_READ_LINES_BUDGET} lines.")

    # 2. Writes to forbidden paths.
    if tool_name in WRITE_TOOLS:
        path = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or ""
        )
        if path and _path_forbidden(str(path), forbidden_paths):
            return GuardDecision(False, f"write to forbidden path blocked: {path}")
        if path and str(path).rstrip("/").endswith((".no_human.yml", ".no_human.yaml")):
            return GuardDecision(
                False,
                ".no_human.yml is the operator's contract with the agent — "
                "the agent never edits it. Propose the change in the PR body "
                "instead.")

    # 3. Shell command policy.
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        if readonly and (_GIT_WRITE.search(cmd) or _FORGE_WRITE.search(cmd)):
            return GuardDecision(
                False,
                f"read-only session: git/forge write blocked: {cmd}. Read the "
                "repo and report; you do not change it.",
            )
        if _RM_TESTS.search(cmd):
            return GuardDecision(
                False,
                f"deleting test files is blocked at tool time: {cmd}. Renames "
                "go through `git mv`; a genuine removal needs the human "
                "(state it in the PR body) — the tamper gate would fail this "
                "attempt at the end anyway, so this denial saves you the "
                "attempt.")
        if _NO_HUMAN_YML_WRITE.search(cmd):
            return GuardDecision(
                False,
                ".no_human.yml is the operator's contract with the agent — "
                "the agent never edits it.")
        if _RM_RF.search(cmd):
            return GuardDecision(False, f"destructive command blocked (rm -rf): {cmd}")
        if _GIT_DESTRUCTIVE.search(cmd):
            return GuardDecision(False, f"destructive git command blocked: {cmd}")
        # Applies to EVERY session, coder included — see the block comment on
        # `_git_worktree_denial`. `_GIT_WRITE` above only ever ran for readonly.
        worktree_reason = _git_worktree_denial(cmd, cwd)
        if worktree_reason:
            return GuardDecision(False, worktree_reason)
        if _FORGE_MERGE.search(cmd):
            return GuardDecision(
                False,
                "merging a pull/merge request is blocked — the agent never "
                "merges. Open the PR, push your fixes to its branch, and stop. "
                "A human merges it (`nh approve`).",
            )
        if _MERGE_STACK_RUN.search(cmd):
            return GuardDecision(
                False,
                "`nh merge-stack run` merges PRs — it drives `gh pr merge` for "
                "every ready PR in the stack — and the agent never merges, in "
                "any session mode. It is the operator's command. Open or "
                "update your PR and stop; a human runs the merge stack.",
            )
        if _LIVE_SERVER.search(cmd):
            return GuardDecision(
                False,
                "launching a live no_human server/runner (`nh serve`/`start`/"
                "`watch`/`bench run`) is blocked in agent sessions — it runs "
                "against the operator's real ~/.no_human config, database, and "
                "credentials regardless of checkout. Test CLI behavior through "
                "the test suite (CliRunner), never a live process.",
            )
        # Kept as-is: catches `git push` spelled in ways argv analysis does not
        # reach (inside a heredoc, an alias, a quoted fragment of a larger
        # script). The argv analysis below is additive, never a replacement.
        if re.search(r"\bgit\s+push\b", cmd) and _push_targets_protected(
            cmd, never_push_to
        ):
            return GuardDecision(
                False,
                f"push to protected branch blocked: {cmd}. Push to your own "
                "branch and open a PR instead — pushing to the base branch is "
                "merging without review.",
            )
        push_reason = _push_denial_reason(cmd, never_push_to)
        if push_reason:
            return GuardDecision(False, push_reason)

    return GuardDecision(True)
