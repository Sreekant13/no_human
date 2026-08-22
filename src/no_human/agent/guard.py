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
  - spawning subagents (`Task`, `Agent`, `Workflow`, `CronCreate`,
    `RemoteTrigger`) in a read-only session — a spawned subagent's own
    toolset, not the parent's readonly gate, decides what it can do, so
    spawning one is a capability-laundering channel out of readonly
  - filesystem-wide scans (`find /`, `grep -r ... /`, `ls -R /`, ...) —
    exploration must be repo-scoped, in every session mode

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
import posixpath
from urllib.parse import unquote
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from . import venv_install_guard

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
BACKGROUND_TOOLS = {"Monitor", "TaskStop", "ToolSearch"}

# Spawn tools. `Agent`/`Task` were deliberately NOT denied here until this was
# fixed: the `risk-first` lens proved a legitimate readonly research pattern —
# three subagents, zero Monitor/TaskStop calls, draft delivered. But a spawned
# subagent gets its OWN toolset, not the parent's readonly gate — a reviewer
# session could spawn a subagent with Write/Edit and launder its way out of
# readonly (the lexical-guards-cannot-enforce-capability class; sibling hole
# already fixed in a21f124a7). `Workflow` explicitly orchestrates subagents;
# `CronCreate`/`RemoteTrigger` launch async work that escapes this session's
# control the same way. All are denied in readonly sessions — no
# backward-compatibility exemption for the risk-first pattern above: a readonly
# session that needs to delegate research is not truly readonly.
SPAWN_TOOLS = {"Task", "Agent", "Workflow", "CronCreate", "RemoteTrigger"}

_NO_POLLING_REASON = (
    "{tool} is unavailable in a read-only session. A subagent's result returns "
    "to you directly when it finishes — do not spawn helpers to wait for it, "
    "and do not poll. Read, Grep, Glob and Bash are all you need; use them and "
    "write your report."
)

_NO_SPAWN_REASON = (
    "{tool} is unavailable in a read-only session: a spawned subagent gets its "
    "own toolset, not this session's read-only gate, so spawning one is a way "
    "to launder capability out of read-only. Read, Grep, Glob and Bash are all "
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
# `_LIVE_SERVER` used to live here as a regex. It is now one branch of
# `_approve_denial`, which reads argv — see the block comment there.
# --------------------------------------------------------------------------- #
# Filesystem-wide scans (`find /`, `grep -r ... /`, `ls -R /`, ...). Intake
# exploration is meant to be repo-scoped — see `intake/grill.py` and
# `intake/evaluator.py` — but a model can still type the wrong command, and a
# whole-machine sweep burns the turn budget and never answers a repo question.
#
# Commands whose job is directory traversal.
_SCAN_EXECUTABLES = frozenset({"find", "grep", "egrep", "fgrep", "rg", "ls", "du", "fd", "tree"})

# Real temp roots this repo's own tests and agents legitimately scan —
# scanning them is not a filesystem-wide sweep even though they live under a
# system prefix (`/private/var/...` on macOS).
_SCAN_EXEMPT_PREFIXES = ("/tmp", "/var/folders", "/private/var/folders", "/private/tmp")

# First-level system directories. `/Users`/`/home` are included deliberately —
# a repo checkout usually lives under one of them, which is exactly why the
# cwd/exemption checks in `_operand_is_blocked_scan` run BEFORE this list is
# consulted: a path under the session's own cwd is allowed even though it is
# also, incidentally, under `/Users`.
_SYSTEM_ROOTS = (
    "/etc", "/usr", "/var", "/opt", "/System", "/Library", "/Applications",
    "/home", "/Users", "/private", "/Volumes", "/proc", "/dev",
)

_REPO_SCOPE_REASON = (
    "filesystem-wide scan blocked: {cmd}. Codebase exploration must be "
    "repo-scoped — the session's working directory IS the repository root. "
    "Run `find {root}/ -name '<glob>'` or `grep -r '<pattern>' {root}/` (or "
    "the Grep/Glob tools) instead. Scanning {target} reads the whole "
    "machine, burns your turn budget, and answers nothing about this task."
)

# Executables that ALWAYS traverse recursively when given a directory operand,
# regardless of flags: `find`/`fd`/`tree`/`du` walk by default, and unlike GNU
# grep, ripgrep has no non-recursive mode for a directory target — `rg TODO /`
# sweeps the machine with no `-r` anywhere on the command line. Grouping `rg`
# with `grep` under a flag-gated recursion test (an earlier version of this
# guard did) is a bypass: `rg` belongs here, not in `_GREP_LIKE` below.
_ALWAYS_RECURSIVE_SCAN = frozenset({"find", "fd", "tree", "du", "rg"})

# grep-family tools: recursive only with an explicit flag.
_GREP_LIKE = frozenset({"grep", "egrep", "fgrep"})
_RECURSIVE_SHORT_FLAG = re.compile(r"^-[A-Za-z]*[rR][A-Za-z]*$")

# grep/rg option flags that consume the NEXT token as a VALUE, not a path
# operand — `-e`/`--regexp` and `-f`/`--file` also mean the search pattern was
# supplied by the flag, so the usual "first positional is the pattern" rule
# must not also eat the first path operand (`grep -r -e TODO /` must still
# see `/` as a path operand and get blocked, not lose it to a phantom
# "pattern" that was already consumed by `-e`).
_GREP_VALUE_FLAGS = frozenset({
    "-e", "--regexp", "-f", "--file", "-m", "--max-count", "--include",
    "--exclude", "--exclude-dir", "-A", "--after-context", "-B",
    "--before-context", "-C", "--context",
})
_GREP_PATTERN_FLAGS = frozenset({"-e", "--regexp", "-f", "--file"})


def _is_scan_exe_name(name: str) -> bool:
    return name in _SCAN_EXECUTABLES


def _is_recursive_scan(name: str, args: list) -> bool:
    """Whether this invocation traverses a directory tree at all — a
    non-recursive `grep pattern file.txt` or `ls dir` never sweeps anything,
    so only recursive scans are worth checking for a filesystem-wide target."""
    if name in _ALWAYS_RECURSIVE_SCAN:
        return True
    if name == "ls":
        for a in args:
            if a == "--":
                break
            if a == "--recursive":
                return True
            if a.startswith("-") and not a.startswith("--") and "R" in a[1:]:
                return True
        return False
    if name in _GREP_LIKE:
        for a in args:
            if a == "--":
                break
            if a in ("--recursive", "--dereference-recursive"):
                return True
            if a.startswith("-") and not a.startswith("--") and _RECURSIVE_SHORT_FLAG.match(a):
                return True
        return False
    return False


def _scan_path_operands(name: str, args: list) -> list:
    """Non-flag operands that name a scan TARGET (as opposed to a pattern or
    a flag value). For `find`, everything before the first primary
    (`-name`, `(`, `!`, ...). For grep-family/`rg`, every non-flag token
    except the one that supplies the search PATTERN — tracked explicitly so
    a pattern given via `-e`/`-f` does not leave the next positional
    (typically the real path) mis-consumed as a phantom pattern."""
    if name == "find":
        paths = []
        for a in args:
            if a.startswith("-") or a.startswith("(") or a.startswith("!"):
                break
            paths.append(a)
        return paths

    is_grep_like = name in _GREP_LIKE or name == "rg"
    operands: list = []
    pattern_consumed = not is_grep_like  # non-grep tools have no pattern slot
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "--":
            continue
        if is_grep_like and a.startswith("--"):
            flag_name = a.split("=", 1)[0]
            if flag_name in _GREP_PATTERN_FLAGS:
                pattern_consumed = True
                if "=" not in a:
                    skip_next = True
            elif flag_name in _GREP_VALUE_FLAGS and "=" not in a:
                skip_next = True
            continue
        if a.startswith("-") and a != "-":
            if is_grep_like and a in _GREP_PATTERN_FLAGS:
                pattern_consumed = True
                skip_next = True
            elif is_grep_like and a in _GREP_VALUE_FLAGS:
                skip_next = True
            continue
        # positional token
        if is_grep_like and not pattern_consumed:
            pattern_consumed = True
            continue
        operands.append(a)
    return operands


def _operand_is_blocked_scan(operand: str, cwd: "str | None") -> bool:
    """True when ``operand`` names a filesystem-wide or system-root scan
    target. Pure string comparison — no filesystem access, matching the rest
    of this module's guards.

    Order matters: tmp exemptions and the session's own cwd are checked
    BEFORE the system-root list, because a repo checkout commonly lives
    under `/Users`/`/home` (both system roots) — without that ordering every
    ordinary repo-scoped scan on such a machine would be denied."""
    if not operand.startswith("/"):
        return False  # relative operands are repo-scoped by construction
    norm = operand.rstrip("/") or "/"
    for prefix in _SCAN_EXEMPT_PREFIXES:
        if norm == prefix or norm.startswith(prefix + "/"):
            return False
    if cwd is not None:
        cwd_norm = str(cwd).rstrip("/") or "/"
        if norm == cwd_norm or norm.startswith(cwd_norm + "/"):
            return False
    if norm in ("/", "/*"):
        return True
    for root in _SYSTEM_ROOTS:
        if norm == root or norm.startswith(root + "/"):
            return True
    return False


def root_scan_denial(cmd: str, cwd: "str | None") -> "str | None":
    """Why this command's filesystem scan must be denied, or `None` to allow
    it. Pure and side-effect-free, mirroring `_venv_install_denial`'s shape.

    Pathless recursive scans (`grep -r pattern`, bare `find`) default to the
    process's own cwd — GNU tools default to `.`, not `/` — so they are
    ALLOWED whenever a session `cwd` is known (the backend always passes the
    session's worktree, i.e. the repo root) and denied only when `cwd` is
    `None`: the one case where the default target genuinely cannot be shown
    to be the repo, per this module's conservative-deny convention."""
    for seg in _CMD_SEP.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            continue
        if not tokens:
            continue
        argv = _strip_wrappers(tokens, _is_scan_exe_name)
        if not argv:
            continue
        name = PurePosixPath(argv[0]).name
        if name not in _SCAN_EXECUTABLES:
            continue
        args = argv[1:]
        if not _is_recursive_scan(name, args):
            continue
        operands = _scan_path_operands(name, args)
        if operands:
            for op in operands:
                if _operand_is_blocked_scan(op, cwd):
                    return _REPO_SCOPE_REASON.format(cmd=seg, root=(cwd or "<repo>"), target=op)
            continue
        if cwd is None:
            return _REPO_SCOPE_REASON.format(
                cmd=seg, root="<repo>", target="the current directory (cwd unknown)")
    return None

# --------------------------------------------------------------------------- #
# Installing into the shared developer venv — defence in depth. A coder
# session's own worktree is disposable, but a Bash command that names the
# PRIMARY checkout's venv explicitly (`/primary/.venv/bin/pip …`,
# `VIRTUAL_ENV=… pip install`, `source /primary/.venv/bin/activate && pip
# install`, `uv pip install --python /primary/.venv/…`, `cd /primary && uv
# sync`, all of those spelled through `sh -c`/`bash -lc`/`xargs`/`timeout`/
# `uv run`) rewrites that venv's editable-install record
# (`_editable_impl_no_human.pth`) to point at the coder's worktree — silently
# breaking the checkout that is running the operator, the same failure
# `doctor.editable_install_problem` only reports AFTER the fact. Installs into
# the session's OWN worktree venv stay allowed — only the venv
# `no_human.__file__` resolves to under the PRIMARY checkout is protected.
# --------------------------------------------------------------------------- #

#: Same venv directory names `testing/runner.py::_venv_bin` probes.
_VENV_DIR_NAMES = (".venv", "venv")


def _primary_checkout() -> "Path | None":
    """The checkout `no_human.__file__` resolves to: this file is
    <checkout>/src/no_human/agent/guard.py, so parents[3] is the checkout
    root. `None` for a non-editable/site-packages/frozen install — the same
    false-positive guard `doctor.editable_install_problem` uses — in which
    case there is nothing to protect."""
    root = Path(__file__).resolve().parents[3]
    if (root / "src" / "no_human" / "__init__.py").is_file():
        return root
    return None


def _protected_venvs(cwd: "str | None") -> list:
    """Venv roots a coder-session install must never write into.

    Always includes the primary checkout's own `.venv`/`venv` (unconditional
    — that is the one this guard exists to protect, whatever the session's
    cwd is). Also includes `sys.prefix` when it is itself a venv, covering a
    venv living outside the checkout that the running process actually uses —
    BUT that candidate is dropped when it is at or under the session's own
    `cwd`, since that is exactly the case where it IS the session's own
    worktree venv, which must stay installable.
    """
    protected = []
    primary = _primary_checkout()
    if primary is not None:
        for name in _VENV_DIR_NAMES:
            d = primary / name
            if d.is_dir():
                protected.append(d.resolve())
    try:
        prefix_cfg = Path(sys.prefix) / "pyvenv.cfg"
        if prefix_cfg.is_file():
            prefix_venv = Path(sys.prefix).resolve()
            if cwd is None:
                protected.append(prefix_venv)
            else:
                cwd_p = Path(cwd).resolve()
                if not (prefix_venv == cwd_p or cwd_p in prefix_venv.parents):
                    protected.append(prefix_venv)
    except OSError:  # pragma: no cover - defensive
        pass
    return protected


#: Verbs that mutate a pip-family environment.
_PIP_VERBS = ("install", "uninstall", "sync")
_PY_EXE_RE = re.compile(r"^python[0-9.]*$")

#: argv[0] names this guard treats as package-manager invocations — passed to
#: `_strip_wrappers` so `env -i pip install …` / `sudo -H <primary>/.venv/`
#: `bin/pip install …` still recover the real command despite a wrapper flag
#: (`-i`, `-H`) that helper does not parse (pip is neither `git` nor a shell
#: runner, the two families it already knows how to re-find).
_PKG_MANAGER_NAMES = frozenset({"pip", "pip3", "uv", "poetry", "pipenv", "conda", "mamba"})


def _is_pkg_manager_name(name: str) -> bool:
    return name in _PKG_MANAGER_NAMES or bool(_PY_EXE_RE.match(name))


def _pkg_install_match(argv: list):
    """The install-command's trailing args, or ``None`` if ``argv`` (argv[0]
    possibly a path) is not a package-install invocation. Deliberately NOT
    matched: `pip list/show/freeze/download`, `uv lock`, `uv run` (handled by
    its own recursion below), `uv venv` — read-only, environment-creation, or
    unrelated to the developer venv's package set."""
    if not argv:
        return None
    name = PurePosixPath(argv[0]).name
    rest = argv[1:]
    if name in ("pip", "pip3"):
        if rest and rest[0] in _PIP_VERBS:
            return rest[1:]
        return None
    if _PY_EXE_RE.match(name):
        if len(rest) >= 3 and rest[0] == "-m" and rest[1] == "pip" and rest[2] in _PIP_VERBS:
            return rest[3:]
        return None
    if name == "uv":
        if rest and rest[0] == "pip":
            if len(rest) >= 2 and rest[1] in _PIP_VERBS:
                return rest[2:]
            return None
        if rest and rest[0] in ("sync", "add", "remove"):
            return rest[1:]
        return None
    if name == "poetry":
        if rest and rest[0] in ("install", "add", "remove", "update"):
            return rest[1:]
        return None
    if name == "pipenv":
        if rest and rest[0] in _PIP_VERBS:
            return rest[1:]
        return None
    if name in ("conda", "mamba"):
        if rest and rest[0] in ("install", "remove", "update"):
            return rest[1:]
        return None
    return None


def _venv_root_from_exe(p: "Path") -> "Path":
    """Venv root given a path that may point at `bin`/`Scripts` or an
    executable inside it; the path unchanged otherwise."""
    if p.name in ("bin", "Scripts"):
        return p.parent
    if p.parent.name in ("bin", "Scripts"):
        return p.parent.parent
    return p


def _resolve_install_path(raw: str, cwd: "str | None") -> "Path":
    p = Path(raw).expanduser()
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    try:
        return p.resolve()
    except OSError:  # pragma: no cover - defensive
        return p


def _flag_value(args: list, names: tuple) -> "str | None":
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


#: Sentinel: the target could not be resolved (shell expansion in a decisive
#: position) — distinct from `None`, which means "nothing named a target and
#: there is no cwd either".
_UNRESOLVED_TARGET = object()


def _install_target(argv0: str, install_args: list, env_map: dict,
                     activated: "str | None", running_cwd: "str | None"):
    """Where this install would land, resolved in priority order (first match
    wins): the invoked executable's own path, an explicit
    `--python`/`--prefix`/`--target`/`--root` flag, uv's own project selector
    (`--project`/`--directory`), a `VIRTUAL_ENV`/`UV_PROJECT_ENVIRONMENT`
    assignment on the command, an earlier `source .../activate` in the same
    command, else the session's current cwd (already folded in any earlier
    `cd` on the same command line). Returns `(path, via_cwd)`, where `via_cwd`
    is True for the project-selector and cwd-fallback cases — the ones where
    uv/pip's own project-root discovery means ANY path at-or-under the primary
    checkout, not just its `.venv`, resolves to the checkout's venv
    (`_target_hits_protected` uses this to tell `cd <primary>/src && uv sync`
    / `uv sync --project <primary>/src` from an unrelated `--target
    <primary>/somewhere`). Returns `(_UNRESOLVED_TARGET, False)` if a decisive
    piece is shell expansion (`$`/backtick), or `(None, True)` if nothing
    resolves it and there is no cwd to fall back on."""
    if "/" in argv0 or "\\" in argv0:
        if _UNRESOLVABLE.search(argv0):
            return _UNRESOLVED_TARGET, False
        return _venv_root_from_exe(_resolve_install_path(argv0, running_cwd)), False
    for names in (("--python", "-p"), ("--prefix",), ("--target",), ("--root",)):
        v = _flag_value(install_args, names)
        if v is not None:
            if _UNRESOLVABLE.search(v):
                return _UNRESOLVED_TARGET, False
            return _venv_root_from_exe(_resolve_install_path(v, running_cwd)), False
    proj = _flag_value(install_args, ("--project", "--directory"))
    if proj is not None:
        if _UNRESOLVABLE.search(proj):
            return _UNRESOLVED_TARGET, False
        return _resolve_install_path(proj, running_cwd), True
    for key in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        v = env_map.get(key)
        if v is not None:
            if _UNRESOLVABLE.search(v):
                return _UNRESOLVED_TARGET, False
            return _resolve_install_path(v, running_cwd), False
    if activated is not None:
        if _UNRESOLVABLE.search(activated):
            return _UNRESOLVED_TARGET, False
        return _resolve_install_path(activated, running_cwd), False
    if running_cwd is None:
        return None, True
    return _resolve_install_path(running_cwd, None), True


def _target_hits_protected(target: "Path", protected: list, primary: "Path | None"):
    """The protected venv ``target`` lands in/on, or `None`. Covers: target IS
    a protected venv, target is INSIDE one, and target is a directory that
    CONTAINS one (target dir == primary checkout → its `.venv` is what an
    install lands in). ``primary`` is only passed for a via_cwd target (see
    `_install_target`): any path at-or-under the primary checkout root also
    hits its venv, because that is what uv/pip's own upward project-root
    discovery does from an arbitrary cwd or `--project`/`--directory` value —
    not because that subdirectory IS the venv."""
    for p in protected:
        if target == p or p in target.parents or target in p.parents:
            return p
    if primary is not None and (target == primary or primary in target.parents):
        for p in protected:
            if p.parent == primary:
                return p
        return primary
    return None


def _venv_install_denial_message(venv: "Path", cwd: "str | None") -> str:
    if cwd is not None:
        alternative = (
            f"Install into THIS session's own venv instead: run the command "
            f"from {cwd} (its `.venv` is already first on PATH, so "
            f"`uv pip install -e .` / `pip install …` land there), or target "
            f"it explicitly with `uv pip install --python {cwd}/.venv/bin/python …`."
        )
    else:
        alternative = (
            "Re-run with the session worktree as cwd; installs into the "
            "worktree's own `.venv` are allowed."
        )
    return (
        f"installing into the shared developer venv at {venv} is blocked: it "
        "is the venv `import no_human` resolves to for the operator's primary "
        "checkout, and an install there rewrites its editable-install record "
        "(`_editable_impl_no_human.pth`) to point at your worktree — silently "
        f"breaking the checkout that is running you. {alternative}"
    )


def _check_install_argv(argv: list, env_map: dict, activated: "str | None",
                         running_cwd: "str | None", protected: list,
                         primary: "Path | None", cwd: "str | None") -> "str | None":
    """Denial reason for a single already-tokenised argv, or `None`."""
    install_args = _pkg_install_match(argv)
    if install_args is None:
        return None
    # `--system` targets the system interpreter, not the developer venv — out
    # of this guard's remit. `--dry-run` changes nothing.
    if "--system" in install_args or "--dry-run" in install_args:
        return None
    target, via_cwd = _install_target(argv[0], install_args, env_map, activated, running_cwd)
    if target is _UNRESOLVED_TARGET or target is None:
        return _venv_install_denial_message(protected[0], cwd)
    hit = _target_hits_protected(target, protected, primary if via_cwd else None)
    if hit is not None:
        return _venv_install_denial_message(hit, cwd)
    return None


#: Punctuation shlex splits into its own tokens (even without surrounding
#: whitespace) OUTSIDE quotes — the shell operator set (`;&|`), redirects
#: (`<>`), grouping (`(){}`), and a literal newline, added here (and removed
#: from `whitespace` below) so a bare newline still ends a segment, exactly
#: like `_CMD_SEP`, instead of silently gluing the next line's command onto
#: this one's argv. `(){}` MUST be here too: without them in the punctuation
#: set, a brace/paren with no surrounding space (`{cd`) glues onto the next
#: word instead of tokenising separately (review round 4, 2026-08-11).
_INSTALL_PUNCTUATION = "(){};<>|&\n"

#: Boundary CHARACTERS (not a literal-token set), derived from the same
#: punctuation set `_segment_tokens` splits on — NOT a narrower hand-picked
#: subset. shlex's `punctuation_chars` mode groups a maximal RUN of
#: punctuation into a single token, so a blank line, `;\n`, `&\n`, `||\n`,
#: a redirect glued to a separator (`;>`), or a grouping char glued to
#: another (`;(`, `)&`) each arrive as ONE token. Checking that token against
#: a literal set of known operators (`{"&&", "||", ";", ...}`), or against a
#: boundary-char set that omits some of `_INSTALL_PUNCTUATION` (redirects,
#: parens, braces), both fall through to `current.append(tok)` for the
#: tokens they don't recognise — gluing two commands into one segment whose
#: argv[0] is the first command, so `_pkg_install_match` never sees the
#: install hiding in `(...)`/`{...}`/behind a redirect (review rounds
#: 2026-08-11 and this ticket). The fix: a token is a boundary when it
#: consists ENTIRELY of characters from `_INSTALL_PUNCTUATION` — the exact
#: set `_segment_tokens` already uses to decide what counts as punctuation —
#: regardless of run length or which characters repeat. A token that mixes
#: punctuation with any non-punctuation character (letters, digits, a space
#: from a quoted payload) is never a boundary, so quoted arguments — even
#: ones containing parens — stay intact as real argv.
_INSTALL_BOUNDARY_CHARS = frozenset(_INSTALL_PUNCTUATION)


def _segment_tokens(text: str) -> list:
    """Tokenise ``text`` into shell command segments (each a list of already
    unquoted tokens), splitting on any run made ENTIRELY of characters from
    `_INSTALL_PUNCTUATION` (`;&|` operators, `<>` redirects, `(){}` grouping,
    newline) — but ONLY when those characters sit OUTSIDE a quoted string.
    `_CMD_SEP.split(text)`
    (used elsewhere in this module) splits the RAW text first, so a separator
    inside a quoted payload — `sh -c "cd /p && uv sync"` — shreds that payload
    into two half-tokens before anything has looked at it. Using shlex's
    `punctuation_chars` mode tokenises the WHOLE string in one pass: a quoted
    substring stays one token regardless of what punctuation it contains,
    while the same punctuation outside quotes still splits it, spaced or not
    (`cmd1&&cmd2` splits exactly like `cmd1 && cmd2`). Falls back to the
    legacy raw-split-then-per-segment approach only for text a real shell
    would itself reject (unbalanced quoting) — malformed input, not a bypass.

    `commenters` is disabled: shlex's default `#` commenter reads through
    end-of-line via its own `readline()`, which SWALLOWS the trailing `\n` —
    so `echo x #\n<venv>/bin/pip install foo` never sees a separator between
    the two commands and they glue into one segment. `shlex.split` disables
    commenters by default for the same reason; this hand-built lexer must too.
    """
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=_INSTALL_PUNCTUATION)
        lex.whitespace = lex.whitespace.replace("\n", "")
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        segments = []
        for raw_seg in _CMD_SEP.split(text):
            raw_seg = raw_seg.strip()
            if not raw_seg:
                continue
            try:
                segments.append(shlex.split(raw_seg))
            except ValueError:
                segments.append(raw_seg.split())
        return segments
    segments = []
    current = []
    for tok in tokens:
        if tok and set(tok) <= _INSTALL_BOUNDARY_CHARS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


#: Bash reserved words that can sit in argv[0] position of a segment after
#: `_segment_tokens` splits on `;`/`\n`/`&`/`|` — `if true; then <install>;
#: fi` and `while false; do <install>; done` split into segments
#: `['then', <install...>]` / `['do', <install...>]`, so an install hiding in
#: a compound-command BODY reads as argv[0]='then'/'do', which is neither a
#: package manager nor a `_WRAPPERS` entry, so `_pkg_install_match` never
#: evaluates it (review rounds 2026-08-11, this ticket — repro'd with both
#: `if/then/fi` and `while/do/done`). These are bash's OWN reserved words
#: used in command position — a finite set fixed by the shell grammar, not
#: an enumeration of examples the next repro can dodge — so stripping ALL of
#: them (not just `then`/`do`) closes the class. `{`/`}`/`(`/`)` need no
#: entry here: they consist entirely of `_INSTALL_PUNCTUATION` characters, so
#: `_segment_tokens` already treats them as segment BOUNDARIES and they never
#: reach a segment as a token.
_SHELL_KEYWORDS = frozenset({
    "if", "then", "elif", "else", "fi",
    "do", "done", "while", "until", "for", "in",
    "case", "esac", "select", "function", "time", "!",
})


def _strip_shell_keywords(tokens: list[str]) -> list[str]:
    """Drop leading shell reserved words, repeatedly, so what remains is the
    compound-command BODY's own argv[0] — `['then', 'do', pip, install, foo]`
    reduces to `[pip, install, foo]`, the thing analysis actually needs to
    see. Only ever strips from the HEAD: a reserved word appearing later in
    the segment (`echo then pip`) is an ordinary argument, not a keyword, and
    is left untouched — `_strip_wrappers`, called on the result, mirrors the
    same head-only discipline for `env`/`sudo`/etc.
    """
    i = 0
    while i < len(tokens) and tokens[i] in _SHELL_KEYWORDS:
        i += 1
    return tokens[i:]


def _looks_quoted(tok: str) -> bool:
    # A multi-word token can only come from something that WAS quoted —
    # shlex/whitespace-splitting never produces a bare space inside a token
    # otherwise. This is the signal that recursion should re-tokenise it as
    # its own command line (`sh -c "..."`, `bash -lc '...'`, `eval "..."`).
    return any(c.isspace() for c in tok)


def _scan_for_install_denial(text: str, cwd: "str | None", running_cwd: "str | None",
                              activated: "str | None", protected: list,
                              primary: "Path | None", depth: int):
    """Scan ``text`` for a package-install invocation that lands in a
    protected venv. Threads `cd`/`source .../activate` state between segments
    on the SAME command line, the way a real shell would. Recurses up to two
    levels into nested shell runners (`sh -c "..."`, `xargs pip install ...`,
    `timeout 300 pip install ...`) — mirrors `_git_push_invocations`'s
    recursion into quoted/continuation argv, so those wrappers cannot launder
    an install past this guard. Returns `(reason_or_None, running_cwd,
    activated)` — the trailing two are this scan's updated state; a nested
    recursion's own updates do not leak back into the caller, matching how a
    subshell's `cd` never affects its parent."""
    for tokens in _segment_tokens(text):
        if not tokens:
            continue

        tokens = _strip_shell_keywords(tokens)
        if not tokens:
            continue

        # Env assignments visible on THIS segment (`VAR=val cmd`, `env VAR=val
        # cmd`) — captured before wrapper-stripping discards them.
        env_map = {}
        i = 0
        while i < len(tokens):
            if tokens[i] == "env":
                i += 1
                continue
            m = re.fullmatch(r"([A-Za-z_]\w*)=(.*)", tokens[i])
            if not m:
                break
            env_map[m.group(1)] = m.group(2)
            i += 1

        argv = _strip_wrappers(tokens, _is_pkg_manager_name)
        if not argv:
            continue

        if argv[0] == "cd" and len(argv) >= 2:
            if _UNRESOLVABLE.search(argv[1]):
                running_cwd = None
            else:
                running_cwd = str(_resolve_install_path(argv[1], running_cwd))
            continue
        if argv[0] in ("source", ".") and len(argv) >= 2:
            m2 = re.match(r"(.+)/(?:bin|Scripts)/activate(?:\.\w+)?$", argv[1])
            if m2:
                activated = m2.group(1)
            continue

        reason = _check_install_argv(argv, env_map, activated, running_cwd, protected,
                                      primary, cwd)
        if reason:
            return reason, running_cwd, activated

        if depth >= 2:
            continue
        name = PurePosixPath(argv[0]).name
        rest = argv[1:]
        if name == "uv" and rest and rest[0] == "run" and len(rest) >= 2:
            reason = _check_install_argv(rest[1:], {}, activated, running_cwd,
                                          protected, primary, cwd)
            if reason:
                return reason, running_cwd, activated
            continue
        if name in _SHELL_RUNNERS:
            # `xargs <venv>/bin/pip install`, `nice -n 10 pip install …`,
            # `timeout 300 pip install …` — the command is the rest of THIS
            # argv, already tokenised, but a leading duration/PID/flag means
            # the install verb is not `rest[0]`. Rather than parse each
            # wrapper's own option grammar, try every suffix — a handful of
            # tokens at most.
            for k in range(len(rest)):
                reason = _check_install_argv(rest[k:], {}, activated, running_cwd,
                                              protected, primary, cwd)
                if reason:
                    return reason, running_cwd, activated
            # `sh -c "pip install …"`, `bash -lc '…'` — the real command is
            # one quoted token; re-tokenise it as its own command line.
            for tok in rest:
                if _looks_quoted(tok):
                    sub_reason, _, _ = _scan_for_install_denial(
                        tok, cwd, running_cwd, activated, protected, primary, depth + 1
                    )
                    if sub_reason:
                        return sub_reason, running_cwd, activated
    return None, running_cwd, activated


def _venv_install_denial(cmd: str, cwd: "str | None") -> "str | None":
    """Why this command's package install must be denied, or `None` to allow
    it. `[]` from `_protected_venvs` (a packaged/non-editable install,
    nothing to protect) makes this a no-op — never a false denial."""
    protected = _protected_venvs(cwd)
    if not protected:
        return None
    primary = _primary_checkout()
    reason, _, _ = _scan_for_install_denial(cmd, cwd, cwd, None, protected, primary, 0)
    return reason


# Merging the PR — the one action that is always a human's (§3.2). `git merge`
# is NOT this: a PR is merged through the forge, and that is what must be denied.
_FORGE_MERGE = re.compile(
    r"\b(?:gh\s+pr\s+merge"           # gh pr merge 7004 --squash
    r"|glab\s+mr\s+merge"             # glab mr merge 12
    r"|gh\s+api\b[^|;&]*?/(?:pulls|merge_requests)/\d+/merge"  # the REST call
    r"|glab\s+api\b[^|;&]*?/merge_requests/\d+/merge"
    # GraphQL merges a PR in one mutation and never touches the REST path
    # above. Found 2026-08-22 by the sweep that found the `nh approve` hole:
    # `gh api graphql -f query="mutation{mergePullRequest(input:{...}){...}}"
    # was ALLOW while every REST spelling was DENY. Matched on the mutation
    # name, which IS the act.
    # enablePullRequestAutoMerge lands it as soon as checks pass, which
    # the project's standing rules forbid in as many words: there is no
    # auto-merge anywhere, and "as soon as checks pass" is auto-merge.
    # Missed by the first sweep, found by review 2026-08-22.
    r"|mergePullRequest\b|enablePullRequestAutoMerge\b)"
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
# `nh` and `no-human` are the SAME entry point: pyproject declares both
# console scripts against no_human.cli.commands:main, and `no-human` is the
# name README and quickstart teach (`uv tool install no-human`). Review
# 2026-08-22 found `no-human approve <id>` ALLOW while `nh approve <id>` was
# DENY — the documented spelling of the act was the unguarded one. Every
# rule below that names the CLI matches both.
# ===========================================================================
# ENDING THE HUMAN GATE — approving, merging, landing.
#
# THIS RULE IS ARGV-SHAPED, NOT LEXICAL, AND THAT IS THE WHOLE POINT.
# Four rounds of lexical patching preceded it and every round was wrong in a
# direction nobody predicted. The record, because the pull back toward "just
# add an alternation" is strong:
#
#   round 1  `nh approve` denied. Missed the API routes entirely.
#   round 2  routes added. Denied `cat src/no_human/vcs/approve_merge.py` —
#            a read-only reviewer could not read the file it reviews.
#   round 3  `no-human approve` was still ALLOW — the SECOND console script for
#            the same entry point, and the name README and quickstart teach.
#            A quoted task id and a `..` segment reached the route.
#   round 4  `nh --repo . approve` was ALLOW (the rules required the binary and
#            the verb to be adjacent). The fix for that introduced a REGRESSION:
#            a `git` token on an earlier LINE reached forward and ate a later
#            `python -m no_human.cli.commands approve` — including from the bare
#            substring `git` inside a path like `~/git/`.
#
# Each round the pattern grew and the next miss was somewhere else: a flag
# (`python -u -c`), a redirection (`nh 2>/dev/null approve`), an unexpanded
# variable (`nh $FLAGS approve`), a percent-encoded byte (`appro%76e`).
# Those are not spelling variants a longer regex reaches. They are what a SHELL
# resolves and a regex cannot, so this asks the question the shell answers:
# for each command in the line, what is argv[0] and what are its arguments?
#
# The same shape already exists twice in this file — `_git_invocations` and the
# package-install guard both work on argv — so this reuses their primitives
# (`_CMD_SEP`, `_strip_wrappers`, `_SHELL_RUNNERS`) rather than adding a third
# way to split a command line.
#
# WHAT IT COSTS. It cannot see a command assembled at runtime, and it cannot
# see a script WRITTEN and then run (`Write /tmp/x.py` then `python /tmp/x.py`)
# — the two tool calls are separate and only the second reaches Bash, where the
# payload is a path. Those are stated in docs/security.md rather than papered
# over. A lexical rule could not see them either.

#: Both console scripts. pyproject declares `nh` AND `no-human` against
#: no_human.cli.commands:main, and `no-human` is the name the install docs use.
#: THE LEXICAL LAYER, KEPT. Deleting it and relying on argv alone was a
#: REGRESSION, and the file said so in advance: "The argv analysis below is
#: additive, never a replacement." Review 2026-08-22 measured it — eight
#: spellings of `nh merge-stack run` that main DENIES went ALLOW on the branch,
#: because shell grouping puts something other than the binary in argv[0]:
#:
#:     (nh merge-stack run --yes)        { nh merge-stack run --yes; }
#:     if true; then nh merge-stack run --yes; fi
#:     for i in 1; do nh merge-stack run --yes; done
#:     cat <(nh merge-stack run --yes)   npx -c 'nh merge-stack run --yes'
#:
#: A commit whose purpose is "the agent never merges" made that weaker. The two
#: layers answer different questions and neither subsumes the other: the regex
#: sees text a tokeniser mis-splits, argv sees structure a regex cannot reach
#: (options between binary and verb, redirections, wrappers, encodings).
#: The regex's prose false positives are the cost this file already argues for
#: on the merge family.
_LEXICAL_MERGE_STACK = re.compile(
    r"(?<![\w.-])(?:nh|no-human)\s+merge-stack\s+run\b")

_LEXICAL_LIVE_SERVER = re.compile(
    r"(?:^|[|;&]\s*|`|\$\(|^\s*|/|\bsudo\s+|\benv\s+[^|;&]*?\s)"
    r"(?:nh|no-human)\s+(?:serve|start|watch|dashboard|bench\s+run)\b")

_APPROVE_BINARIES = frozenset({"nh", "no-human"})

#: Global options on the `nh` group that CONSUME the next token, so the value
#: is not mistaken for the subcommand. Read off the CLI, not guessed.
_NH_VALUE_OPTS = frozenset({"--repo"})

#: Subcommands that end the human gate, and the ones that run a live server
#: against the operator's real config, database and credentials.
_APPROVE_VERBS = frozenset({"approve"})
_LIVE_VERBS = frozenset({"serve", "start", "watch", "dashboard"})
#: `dashboard` is a documented ALIAS that `ctx.invoke(start, ...)`. It was
#: missing here and reachable while `nh start` was denied — review 2026-08-22.
#: A verb list is a list, so tests/test_guard.py asserts this set against the
#: click command table rather than trusting the comment.
_LIVE_VERB_PAIRS = {("bench", "run")}
_MERGE_VERB_PAIRS = {("merge-stack", "run")}

#: argv[0] names that READ. Naming the act is not doing it: a reviewer greps
#: the route in the file that defines it, and an agent writes a commit message
#: about it. This is the "denied titling its own PR" class, and it has now
#: recurred twice, so the exemption is a property of argv[0] rather than of a
#: message-option grammar. The previous attempt — stripping the value of
#: `-m`/`--title`/`--body` — is DELETED: `-m` is also python's module flag, its
#: scoping regex could be reached across a newline by the substring `git` in a
#: path, and it only ever stripped the first such option per segment.
#: Tools that READ and cannot run their arguments as a command. A name list is
#: one entry short in BOTH directions unless it is split this way — review
#: 2026-08-22 found `find -exec`, `awk 'BEGIN{system(...)}'` and `sed .../e`
#: exempted as "text tools" while they execute, and `printf`/`node`/`npm`
#: denied for merely naming the route.
_READ_ONLY_TOOLS = frozenset({
    "grep", "rg", "egrep", "fgrep", "ag", "ack", "cat", "bat", "less", "more",
    "head", "tail", "echo", "printf", "jq", "yq", "diff", "wc", "sort",
    "uniq", "tr", "cut", "ls", "file", "strings", "xxd", "od", "nl", "man",
    "column", "tee",
})

#: `npx -c '<command>'` and `npm exec -c '<command>'` RUN it. These were on
#: the read-only list, which both exempted them from the route check and
#: stopped their payload being recursed into — review 2026-08-22 executed
#: `npx -c 'nh approve <id>'` in bash, zsh and sh.
_NODE_RUNNERS = frozenset({"npm", "npx", "pnpm", "yarn", "bunx"})

#: Tools that read text AND can execute what they read. Not exempt: their
#: sub-argv is analysed like any other command.
#: Tools whose FIRST POSITIONAL (or `-e` value) is a program they run.
#: `awk 'BEGIN{system("nh approve <id>")}'` and `sed 's/x/nh approve <id>/e'`
#: both execute — review 2026-08-22 ran them.
_SCRIPT_TOOLS = frozenset({"awk", "gawk", "mawk", "sed", "perl", "ruby",
                           "node", "deno", "bun", "osascript"})

#: Tools that run something only after an explicit exec flag. `git`, `gh` and
#: `glab` are here rather than in _SCRIPT_TOOLS for one specific reason: their
#: quoted arguments are MESSAGES. `git commit -m "nh approve docs"` must stay
#: allowed — that is the "agent denied titling its own PR" class, which this
#: rule has now had four separate recurrences of.
_ARG_EXEC_TOOLS = frozenset({"find", "git", "gh", "glab"})

_EXEC_CAPABLE_TOOLS = _SCRIPT_TOOLS | _ARG_EXEC_TOOLS

#: Exec-capable tools that still cannot make an HTTP request of their own.
#: `gh` and `glab` are NOT here: `gh api -X POST <url>` posts.
#: ...and they cannot make an HTTP request of their own, so they stay
#: exempt from the route check whatever else they do.
_ROUTE_EXEMPT_TOOLS = frozenset({"find", "git", "npm", "npx",
                                 "pnpm", "yarn", "bunx"})

#: Evidence that a payload MAKES a request rather than mentioning a URL.
#: `node -e "console.log('/api/tasks/x/approve')"` prints; `node -e
#: 'fetch(".../approve",{method:"POST"})'` posts. Naming the act is not doing
#: it — the class this rule has now had five recurrences of — so a script
#: tool's payload needs both the route and this.
_REQUESTS = re.compile(
    r"\bfetch\s*\(|(?:\.|->)post\s*\(|(?:\.|->)request\s*\("
    r"|(?:\.|->)put\s*\(|urlopen\s*\(|\bHTTP::Tiny\b|\bopen-uri\b"
    r"|URI\.open\b|\bGET\s+/api|\bPOST\s+/api"
    r"|\bhttp\.client\b|\bNet::HTTP\b|\bLWP\b|\baxios\b|\bhttpx\b"
    r"|\brequests\.|\bcurl\b|\bwget\b|\bXMLHttpRequest\b|\bsocket\b"
    r"|Invoke-(?:RestMethod|WebRequest)")

#: Runners whose FIRST argument is an operand, not the command. `timeout 600
#: pytest ...` and `flock /tmp/l nh ...` — reading the next token as the
#: command returned `600`, so `timeout 600 pytest -k approve --rootdir=$PWD`
#: was refused as undecidable. Sixth recurrence of the over-denial class.
_RUNNERS_WITH_OPERAND = frozenset({"timeout", "flock"})

#: Runner flags that consume the following token.
_FLAGS_WITH_VALUE = frozenset({"-n", "-o", "-u", "-c", "-I", "-i", "-P", "-s"})

#: Test runners and build drivers. Exempt from the undecidable-input refusal:
#: `uv run pytest -k approve --rootdir=$PWD` is running the tests for the code
#: this rule protects, and refusing it was the FIFTH recurrence of the
#: "denied for naming the act" class (review 2026-08-22).
_TEST_RUNNERS = frozenset({
    "pytest", "tox", "nox", "unittest", "make", "cargo", "go", "jest",
    "vitest", "mocha", "gradle", "mvn", "ctest", "bazel",
})

#: Runners that carry the real command as the REST of their own argv rather
#: than as a quoted string. `_git_invocations` already handles this exact case
#: 400 lines below ("`xargs git restore` / `timeout 30 git restore .`"); the
#: first draft of this rule copied only the quoted-payload branch, so `timeout
#: 5 nh approve <id>`, `nice`, `stdbuf`, `script`, `flock`, `watch` and `xargs`
#: were all ALLOW. Measured against a real shell, not inferred.
#: Flags after which the REST of the argv is a command to run. `find -exec`,
#: `awk -e`, `node -e`, `perl -e`. Review 2026-08-22 reached `curl` through
#: `find . -exec curl -X POST .../approve \\;`.
_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-c", "-e", "--eval", "--exec"})

#: A redirection onto a numbered file descriptor other than stderr-to-stdout.
#: `printf '...' >&3` writes to whatever fd 3 was opened on — in the evasion
#: review 2026-08-22 built, a socket. So a read-only tool doing this is not
#: reading, and the exemption does not apply to that segment.
_FD_WRITE = re.compile(r">&(?!1\b)\d")

#: Programs that speak to a socket and take their payload on STDIN. `printf
#: 'POST /api/tasks/<id>/approve HTTP/1.1...' | nc 127.0.0.1 8420` puts the
#: route in the PIPE, not in the network tool's argv. Same shape as the python
#: pipe case, different tool.
_SOCKET_TOOLS = frozenset({"nc", "netcat", "ncat", "telnet", "socat"})

_TRAILING_ARGV_RUNNERS = frozenset({
    "xargs", "timeout", "nice", "stdbuf", "script", "flock", "watch", "ionice",
    "chrt", "setsid", "unbuffer",
})

_PY_INTERPRETER = re.compile(
    r"^(?:python(?:\d+(?:\.\d+)?)?|ipython\d?|pypy\d?|micropython)$")

#: The code shapes that land a PR in-process. Searched in a python invocation's
#: OWN payload (its arguments and any heredoc body), never across the line, so
#: `python -m pytest tests/... && grep -n "land_task(" src/...` is untouched.
_IN_PROCESS_CODE = re.compile(
    # The import list is `[\w,\s()]*?` and NOT `(?:[\w,\s()]|\bas\b)*?`:
    # `as` is two \w characters AND the second alternative, which is 2^n
    # ambiguity. `python -c "from no_human.cli.commands import n0 as a0,
    # n1 as a1, ..."` cost 878 ms at 20 aliases and 5.7 s adversarially,
    # inside a PreToolUse hook — an exponential this rule INTRODUCED while
    # removing a quadratic, found by review 2026-08-22. `[\w,\s()]` already
    # covers `as`, so the alternative bought nothing and cost that.
    # `main` and `cli` only from the CLI module. A bare alternation on those
    # names denied `from no_human.core.store import Store, main` and
    # `from no_human.agent.guard import evaluate as cli` — review 2026-08-22.
    # ...and it must be CALLED. `from no_human.cli.commands import cli;
    # print(sorted(cli.commands))` is introspection, not an invocation —
    # review 2026-08-22 flagged the bare alternation as a false denial.
    r"from\s+no_human\.cli[\w.]*\s+import\s+[\w,\s()]*?"
    r"\b(?:main|cli|approve)\b[\s\S]{0,200}?\b(?:main|cli|approve)\s*\("
    r"|from\s+no_human[\w.]*\s+import\s+[\w,\s()]*?"
    r"\b(?:approve_merge|land_task)\b"
    # the dynamic spellings of the same import
    r"|import_module\s*\(\s*['\"]no_human\."
    r"|__import__\s*\(\s*['\"]no_human\."
    # shelling the CLI back out from inside python
    r"|(?:os\.system|subprocess\.\w+|os\.exec\w*|os\.popen)\s*\([^)]*"
    r"['\"]?\b(?:nh|no-human)\b[^)]*approve"
    # (A second alternative matching any os.system/subprocess call containing
    # the WORD approve was removed: it denied
    # `subprocess.run(["pytest", "-k", "approve"])` and
    # `subprocess.run(["pytest", "tests/test_approve_merge.py"])` — running the
    # tests for the code this rule protects. The alternative above already
    # covers the case that matters, because a shell-out that lands a PR has to
    # name the binary.)
    r"|import\s+no_human\.vcs\.approve_merge\b"
    r"|\bland_task\s*\("
    r"|\bapprove_merge\.\w+\s*\("
    r"|\bcli\s*\(\s*\[[^\]]*['\"]?approve"
    r"|\bapprove\.callback\s*\("
    r"|run_module\s*\(\s*['\"]no_human\.cli"
    r"|run_path\s*\([^)]*approve_merge"
)

#: A path on OUR local API that ends the gate. `shipped` and `finish-review`
#: do not merge, but each is a human asserting the gate is satisfied — the same
#: forgery as `approve --landed`. Matched against a percent-DECODED token,
#: because curl transmits `%76` verbatim and the server unquotes before routing.
_GATE_PATH = re.compile(
    r"/api/tasks/[^/\s]+/(?:approve(?:-landed)?|shipped|finish-review)"
    r"(?:\b|/|$)", re.IGNORECASE)

#: Any `/api/tasks/...` path, however many segments — the input to
#: normalisation, not a verdict.
_TASKS_PATH = re.compile(r"/api/tasks/[^\s\"']*", re.IGNORECASE)


def _gate_route(tok: str) -> bool:
    """True if the token targets one of the four gate-ending routes, AFTER
    percent-decoding and dot-segment normalisation.

    The previous shape bounded the middle to 120 characters, and the comment
    justifying the bound named the mechanism that defeats it: curl normalises
    `..` away BEFORE sending, so the argument can be arbitrarily long and still
    route. Review 2026-08-22 proved it against a live listener — a 161-character
    padded path arrived as `/api/tasks/abc/approve` while the guard allowed it.
    Normalising is both correct and linear, so the bound was answering the wrong
    question."""
    text = _decoded(tok)
    for m in _TASKS_PATH.finditer(text):
        if _GATE_PATH.search(posixpath.normpath(m.group(0))):
            return True
    return bool(_GATE_PATH.search(text))


#: `$(which nh) approve <id>` needs no rule of its own any more: the
#: undecidable-input branch below denies it, because the command position
#: contains `$` and the segment names the act. A dedicated
#: `_WHICH_SUBST` pattern lived here until a mutation showed it made no
#: difference to any test — redundant, not untested, so it went.
#: Quoted regions and heredoc bodies are MASKED before the command is split.
#: This is the plumbing that a first draft of the argv rule got wrong: a `-c`
#: payload legitimately contains `;` and newlines, and `_CMD_SEP` splits on
#: both, so `python -c "import sys; from no_human... import main; main()"` was
#: torn into three "segments" and the interpreter never saw its own code.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"", re.S)
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1\n(.*?)\n\s*\2\b", re.S)
_MASK = "\x00m{}\x00"
_REDIR_AMP = re.compile(r"\d*>&\d*|&>>?")
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


#: `nh \<newline> approve <id>` is ONE command to every shell — bash, sh and
#: zsh all ran it — but `_CMD_SEP` splits on the newline, so the verb landed in
#: a segment of its own. Joined before anything else reads the line. Mid-token
#: too: `nh appro\<newline>ve <id>` also executes.
_LINE_CONTINUATION = re.compile(r"\\\n")


def _join_continuations(cmd: str) -> str:
    return _LINE_CONTINUATION.sub("", cmd)

#: ANSI-C quoting. `$'\x61pprove'` IS `approve` to bash and zsh, and the
#: escapes are deterministic — so this is not undecidable input, it is input
#: with an encoding, and decoding beats refusing. Review 2026-08-22 walked
#: `$'\x61pprove'` and `$'\x6e\x68' $'\x61pprove'` past the undecidable
#: branch, which keys on the literal word and neither of those spells it.
_ANSI_C_QUOTE = re.compile(r"\$'((?:[^'\\]|\\.)*)'")


def _effective_name(argv: list[str]) -> str:
    """The name of the command that will actually run, after a runner prefix.

    `xargs grep -l approve $EXTRA`, `nice pytest -k approve --color=$C` and
    `uv run pytest ... --rootdir=$PWD` all have a runner in argv[0] and the
    real command behind it. Reading only argv[0] made the undecidable-input
    refusal fire on four routine commands — review 2026-08-22, the fifth
    recurrence of "denied for naming the act"."""
    skip_operand = False
    for tok in argv:
        name = PurePosixPath(tok).name
        if skip_operand:
            skip_operand = False
            continue
        if name in _RUNNERS_WITH_OPERAND:
            skip_operand = True
            continue
        if (name in _SHELL_RUNNERS or name in _TRAILING_ARGV_RUNNERS
                or name in {"uv", "uvx", "poetry", "pdm", "hatch", "rye",
                            "pipx", "env", "sudo", "run", "tool"}):
            continue
        if tok.startswith("-"):
            skip_operand = name in _FLAGS_WITH_VALUE
            continue
        return name
    return PurePosixPath(argv[0]).name if argv else ""


def _decode_ansi_c_body(body: str) -> str:
    try:
        return body.encode("latin-1", "backslashreplace").decode(
            "unicode_escape")
    except Exception:  # noqa: BLE001 — a bad escape stays as written
        return body


def _strip_one_quote_pair(tok: str) -> str:
    """One matching OUTER pair, not every quote character at both ends.

    `str.strip("\"'")` removes every trailing character in the set, so
    `'sh -c "nh approve x"'` became `sh -c "nh approve x` — the inner closing
    quote eaten along with the outer one. `shlex` then raised, the fallback
    split on spaces, and a two-level `sh -c` walked straight through. Review
    2026-08-22 executed it. A defect of this architecture, not an inherited
    one."""
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def _mask_payloads(cmd: str) -> tuple[str, dict[str, str]]:
    """Command with every quoted region and heredoc body replaced by an opaque
    token, plus the table to read them back. Keeps shell separators inside a
    payload from being read as shell separators."""
    table: dict[str, str] = {}

    def take(text: str) -> str:
        key = _MASK.format(len(table))
        table[key] = text
        return key

    # `2>&1` and `&>file` contain `&`, which the shared _CMD_SEP treats as a
    # command separator — `nh >/tmp/l 2>&1 approve <id>` was torn in half and
    # the verb landed in a segment of its own. Masked here rather than by
    # changing _CMD_SEP, which other rules in this file depend on.
    # ANSI-C quoting is decoded INTO A MASK, before any other masking, and
    # never back into the command text. Decoding it in place was a defect this
    # rule introduced while fixing the round before it: `$'\x27'` decodes to a
    # bare apostrophe, so `echo $'\x27' ; nh approve <id> ; echo $'\x27'`
    # became `echo ' ; nh approve <id> ; echo '` — one quoted region, argv[0]
    # `echo`, allowed. Executed in bash, zsh and sh by review 2026-08-22.
    # Masking it keeps the decoded VALUE visible to the rules while keeping its
    # characters out of the shell-syntax layer.
    masked = _ANSI_C_QUOTE.sub(lambda m: take(_decode_ansi_c_body(m.group(1))),
                               cmd)
    masked = _REDIR_AMP.sub(lambda m: take(m.group(0)), masked)
    masked = _HEREDOC.sub(lambda m: "<<" + m.group(2) + " " + take(m.group(3)), masked)
    masked = _QUOTED.sub(lambda m: take(m.group(0)), masked)
    return masked, table


#: Does this segment name the act at all? Only used to SCOPE the
#: undecidable-input denial, so a command we cannot resolve and that says
#: nothing about approving stays allowed.
#: A script-tool payload that shells a command back out. `awk
#: 'BEGIN{system("nh approve <id>")}'` — the inner command is not shell syntax
#: the tokeniser reaches, so it is matched as what it is: an exec call naming
#: the binary and the verb.
_SCRIPT_EXEC = re.compile(
    r"\b(?:system|exec|popen|spawn|backticks|qx|do\s+shell\s+script)\s*[(\[]?[^)\]]*"
    r"(?:nh|no-human)\s+(?:approve|merge-stack|serve|dashboard)", re.IGNORECASE)

_GATE_MENTION = re.compile(
    r"\bapprove\b|\bapprove-landed\b|\bshipped\b|\bfinish-review\b"
    r"|\bmerge-stack\b|\bland_task\b|\bapprove_merge\b|/api/tasks\b"
    r"|\bserve\b|\bdashboard\b", re.IGNORECASE)

#: A redirection operator and its target, wherever it is glued on.
_REDIR_SUFFIX = re.compile(r"\d*(?:>>|>|<)&?\S*$")

#: Grouping and process-substitution punctuation, replaced by a space before
#: tokenising so the body's own argv[0] is what gets read.
_GROUPING = re.compile(r"(?:^|(?<=\s))[({]|[)}](?=\s|$)|<\(|\)")

_MASK_KEY = re.compile(r"\x00m\d+\x00")


def _unmask(tok: str, table: dict[str, str]) -> str:
    """One pass, not one pass PER TABLE ENTRY. The loop this replaces was
    O(tokens x table) and made the whole guard quadratic: `nh "a" x16000` cost
    14.6 SECONDS inside a PreToolUse hook, 192 million str.replace calls,
    against 34 ms without the rule. Found by review 2026-08-22, which correctly
    noted the previous commit message claimed the remaining superlinearity was
    pre-existing. It was not; this function was."""
    if not table:
        return tok
    return _MASK_KEY.sub(lambda m: table.get(m.group(0), m.group(0)), tok)


def _dequote(tok: str) -> str:
    """Quote characters removed ANYWHERE, not just at the ends. `appro''ve` is
    `approve` to a shell — review 2026-08-22 spliced the verb that way and the
    rule read it as an unknown subcommand. `.strip()` cannot see the middle."""
    return tok.replace("'", "").replace('"', "")


def _is_approve_verb(word: str) -> bool:
    """Any subcommand whose name STARTS with `approve`, not only the one that
    exists today. Deliberate: there is exactly one now (checked against the
    click command table), and a future `nh approve-all` should be denied by
    default and let a human notice, rather than shipping unguarded until
    somebody re-audits. This file argues that polarity for the whole merge
    family — a false denial costs one message with a stated alternative, a miss
    lands a PR."""
    return word == "approve" or word.startswith("approve-")


def _nh_subcommand(argv: list[str],
                   table: dict[str, str] | None = None) -> list[str]:
    """The positional words of an `nh`/`no-human` invocation: options, their
    values and redirections removed. `nh --repo . approve <id>` and
    `nh 2>/dev/null approve <id>` both give ['approve', '<id>']."""
    out: list[str] = []
    skip = False
    for tok in argv[1:]:
        if skip:
            skip = False
            continue
        # A masked token is a quoted string or a redirection operator. Read it
        # back before judging it: leaving the opaque placeholder in place made
        # `nh >/tmp/l 2>&1 approve <id>` read the MASK as the subcommand and
        # never see `approve`. Measured.
        if table:
            tok = _dequote(_unmask(tok, table))
        # A redirection GLUED to the verb is still the verb: `nh approve>&2
        # <id>` runs `approve`, and review 2026-08-22 executed it. The previous
        # shape dropped any token containing `>` or `<`, which threw the verb
        # away with the operator. Split the operator off instead, and drop the
        # token only if nothing is left in front of it.
        tok = _REDIR_SUFFIX.sub("", tok)
        if not tok:
            continue
        if tok.startswith("-"):
            if tok in _NH_VALUE_OPTS:
                skip = True
            continue
        out.append(tok)
    return out


def _decoded(tok: str) -> str:
    try:
        return unquote(tok)
    except Exception:  # noqa: BLE001 — a malformed escape is not a crash
        return tok


def _peel_runners(argv: list[str]) -> list[str]:
    """`uv run X`, `uvx X`, `poetry run X`, `env X`, `/usr/bin/env X` peeled to
    X. `_strip_wrappers` handles the bare-name wrappers; this also handles the
    ones reached by an absolute path and the per-tool `run` subcommand."""
    for _ in range(4):
        if not argv:
            return argv
        name = PurePosixPath(argv[0]).name
        if name == "env":
            argv = argv[1:]
            while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
                argv = argv[1:]
            continue
        if name in {"uv", "poetry", "pdm", "hatch", "rye", "pipx"} and len(argv) > 1:
            argv = argv[2:] if argv[1] in {"run", "tool"} else argv[1:]
            continue
        if name == "uvx":
            argv = argv[1:]
            continue
        return argv
    return argv


#: A URL assembled from pieces — `b = ".../api/tasks/"` then `b + tid +
#: "/approve"`. No single token carries the whole route, so the route pattern
#: cannot see it; the two halves in one interpreter payload can.
_ROUTE_HALVES = (re.compile(r"/api/tasks\b"),
                 re.compile(r"/(?:approve(?:-landed)?|shipped|finish-review)\b"))


def _approve_denial(cmd: str, _depth: int = 0) -> str | None:
    """One reason for every way an agent session can end the human gate.

    Reads argv, not the line. See the block comment above for why, and for the
    four lexical rounds that preceded it. Recurses into shell runners up to two
    levels, bounded — a guard must not become a parser with unbounded work."""
    cmd = _join_continuations(cmd)
    masked, table = _mask_payloads(cmd)

    # Unmasked ONCE, not once per segment. Doing it in the loop was quadratic
    # — the same class this rule had already removed from `_unmask` — and cost
    # 3.4 s against a 138 ms base on a realistic 800-line script (review
    # 2026-08-22).
    gate_mentioned = bool(_GATE_MENTION.search(_unmask(masked, table)))
    parts = _CMD_SEP.split(masked)
    seps = [""] + _CMD_SEP.findall(masked)
    prev_raw = ""
    for sep, raw in zip(seps, parts):
        seg = raw.strip()
        if not seg:
            continue
        # Shell GROUPING and reserved words put something other than the
        # binary in argv[0]: `(nh approve <id>)` gives `['(nh', ...]`, and
        # `if true; then nh approve <id>; fi` gives `['then', 'nh', ...]`.
        # `_strip_shell_keywords` already existed in this file for exactly
        # this and the first draft of this rule never called it. Review
        # 2026-08-22 executed `(nh approve X)` in bash, zsh AND sh.
        seg_body = _GROUPING.sub(" ", seg)
        try:
            tokens = shlex.split(seg_body)
        except ValueError:
            tokens = seg_body.split()
        tokens = _strip_shell_keywords(tokens)
        argv = _peel_runners(_strip_wrappers(
            tokens,
            is_extra_target=lambda n: (
                n in _APPROVE_BINARIES or bool(_PY_INTERPRETER.match(n))
                or n in {"uv", "uvx", "poetry", "pipx", "pdm", "hatch", "rye"}
            ),
        ))
        if not argv:
            prev_raw = seg
            continue
        # A quoted binary name (`"nh" approve <id>`) is masked like any other
        # quoted region, so read argv[0] back before taking its basename.
        # Dequoted ANYWHERE, like the verb: `n"h" approve <id>` and
        # `n''h approve <id>` are `nh` to a shell. End-stripping caught the
        # outer form only — `_dequote` existed for `appro''ve` and was applied
        # to the verb but not to the binary. Review 2026-08-22 executed both.
        argv = [_dequote(_unmask(argv[0], table))] + argv[1:]
        name = PurePosixPath(argv[0]).name
        # `nh\ approve` — an escaped space makes shlex produce ONE token. In a
        # real shell that names a binary "nh approve", which does not exist, so
        # this is not a live evasion; it is matched anyway because doing so is
        # free and the alternative is a rule whose behaviour depends on whether
        # a lookalike binary happens to be installed.
        if " " in name:
            head = name.split()[0]
            if head in _APPROVE_BINARIES:
                argv = name.split() + argv[1:]
                name = head

        # 0. UNDECIDABLE INPUT FAILS CLOSED. `shlex` resolves quoting and
        #    backslashes and nothing else: not `$'...'`, not parameter
        #    expansion, not an alias. Review 2026-08-22 walked through with
        #    `nh $'approve' <id>`, `$'nh' approve <id>`, `B=nh; $B approve
        #    <id>` and `$(echo $(echo nh)) approve <id>` — every one runs in a
        #    real shell. A tokeniser that cannot resolve them must not answer
        #    "allowed"; this file already takes that polarity for `git push`,
        #    where `_UNRESOLVABLE` refuses an argv it cannot resolve.
        #
        #    Scoped to segments that MENTION the gate, because denying every
        #    `$VENV/bin/pytest` would be a far worse rule than the hole. So:
        #    "I cannot tell what this runs, and it names the act" -> deny.
        eff = _effective_name(argv)
        if (_depth < 2 and eff not in _READ_ONLY_TOOLS
                and eff not in _TEST_RUNNERS):
            # EVERY token, not just the command and verb positions: the
            # unresolvable one can be the URL — `U=/api/tasks/abc/approve;
            # curl -X POST http://127.0.0.1:8420$U`. Read-only tools are
            # exempt, so `grep -rn "nh approve" $REPO/docs/` stays allowed:
            # they cannot call the route whatever the variable holds.
            unresolved = [t for t in argv if _UNRESOLVABLE.search(t)]
            # The whole command, not just this segment: `U=/api/tasks/abc/
            # approve; curl -X POST ...$U` puts the mention in the assignment
            # and the unresolvable token in the call. Measured.
            if unresolved and gate_mentioned:
                return _UNDECIDABLE_REASON

        # 0a. a command SUBSTITUTION anywhere in the segment really runs, and
        #     it runs before the outer command does. `git commit -m "$(nh
        #     approve <id>)"` lands the PR and then commits its output. The
        #     outer argv[0] is irrelevant — even a text tool's argument runs.
        # Over the SEGMENT TEXT, not per shlex token: a bare `$(nh approve
        # <id>)` is already split into `['$(nh', 'approve', 'abc)']`, so no
        # single token ever matched and only the QUOTED form was caught —
        # backwards. `echo $(nh approve <id>)` executed while allowed (review
        # 2026-08-22). argv[0] is irrelevant here for real: the substitution
        # runs BEFORE the outer command does, so a read-only tool's argument
        # runs too.
        if _depth < 2:
            for inner_text in (_unmask(seg, table),):
                for m in _SUBSTITUTION.finditer(inner_text):
                    reason = _approve_denial(m.group(1) or m.group(2) or "",
                                             _depth + 1)
                    if reason:
                        return reason

        # 0b. a runner carrying the real command. TWO shapes, and the first
        #     draft implemented only one: a QUOTED payload (`sh -c "nh
        #     approve"`), and the rest of THIS argv (`timeout 5 nh approve`,
        #     `xargs nh approve`). `_git_invocations` handles both; this now
        #     does too.
        # A node runner only RUNS a payload behind `-c` (`npx -c '<cmd>'`,
        # `npm exec -c '<cmd>'`). Recursing into every quoted argument denied
        # `npm test -- --grep "/api/tasks/:id/approve"`, which is a test
        # filter — the seventh recurrence of "denied for naming the act",
        # caught here rather than by a review for once.
        runs_quoted = (name in _SHELL_RUNNERS or name in _SCRIPT_TOOLS
                       or (name in _NODE_RUNNERS and "-c" in argv))
        runs_trailing = (name in _SHELL_RUNNERS
                         or name in _TRAILING_ARGV_RUNNERS
                         or name in _ARG_EXEC_TOOLS)
        if (runs_quoted or runs_trailing) and _depth < 2:
            for j, tok in enumerate(argv[1:], start=1):
                inner = _unmask(tok, table)
                stripped = _strip_one_quote_pair(inner)
                after_exec_flag = argv[j - 1] in _EXEC_FLAGS
                if stripped and stripped != tok and (runs_quoted
                                                     or after_exec_flag):
                    if name in _SCRIPT_TOOLS and _SCRIPT_EXEC.search(stripped):
                        return _APPROVE_REASON
                    reason = _approve_denial(stripped, _depth + 1)
                    if reason:
                        return reason
                elif (PurePosixPath(inner).name.lower() in _APPROVE_BINARIES
                      or _PY_INTERPRETER.match(PurePosixPath(inner).name)
                      or after_exec_flag) and runs_trailing:
                    rest = [_unmask(t, table) for t in argv[j:]
                            if t not in {"\\;", ";", "+"}]
                    reason = _approve_denial(" ".join(rest), _depth + 1)
                    if reason:
                        return reason

        # 1. the product's own CLI, under either console script
        # Case-folded: APFS is case-insensitive by default, and review
        # 2026-08-22 ran `NH approve <id>` and `Nh approve <id>` on this
        # machine. On a case-sensitive filesystem the name would not
        # resolve, so folding can only deny something that cannot run.
        if name.lower() in _APPROVE_BINARIES:
            words = _nh_subcommand(argv, table)
            if words and _is_approve_verb(words[0]):
                return _APPROVE_REASON
            if tuple(words[:2]) in _MERGE_VERB_PAIRS:
                return _MERGE_STACK_REASON
            if words and (words[0] in _LIVE_VERBS
                          or tuple(words[:2]) in _LIVE_VERB_PAIRS):
                return _LIVE_SERVER_REASON

        # 2. a python interpreter. Its OWN payload — arguments, heredoc body,
        #    and whatever was piped INTO it — never the rest of the line, so
        #    `python -m pytest ... && grep -n "land_task(" ...` is untouched.
        # A script tool's payload is analysed for the ROUTE the same way a
        # python payload is — `node -e 'fetch(".../approve",{method:"POST"})'`
        # reached a live listener while the guard allowed it, because only the
        # python branch looked.
        if name in _SCRIPT_TOOLS:
            spayload = _unmask(" ".join(argv[1:]), table)
            if _REQUESTS.search(spayload) and (
                    _gate_route(spayload)
                    or all(h.search(spayload) for h in _ROUTE_HALVES)):
                return _APPROVE_REASON

        if _PY_INTERPRETER.match(name):
            payload = _unmask(" ".join(argv[1:]), table)
            if sep == "|":
                payload += " " + _unmask(prev_raw, table)
            if _IN_PROCESS_CODE.search(payload):
                return _APPROVE_REASON
            if all(half.search(payload) for half in _ROUTE_HALVES):
                return _APPROVE_REASON
            if _gate_route(payload):
                return _APPROVE_REASON
            # `python -m no_human.cli.commands approve <id>` — the click entry
            # point without the console script. The module IS the CLI, so the
            # verbs are read the same way as for the binary.
            for i, tok in enumerate(argv[1:], start=1):
                if tok == "-m" and i + 1 < len(argv) \
                        and argv[i + 1].startswith("no_human"):
                    words = _nh_subcommand(argv[i + 1:], table)
                    if words and _is_approve_verb(words[0]):
                        return _APPROVE_REASON
                    if tuple(words[:2]) in _MERGE_VERB_PAIRS:
                        return _MERGE_STACK_REASON
                    if words and (words[0] in _LIVE_VERBS
                                  or tuple(words[:2]) in _LIVE_VERB_PAIRS):
                        return _LIVE_SERVER_REASON

        # 3. our own local API, CALLED rather than named. Skipped for tools
        #    whose job is to read text: naming the route is not calling it.
        # Exempting the tools that CAN EXECUTE from the route check was the
        # opposite of the stated design, and review 2026-08-22 proved it
        # against a live listener: `node -e 'fetch(".../approve",{method:
        # "POST"})'` and `gh api -X POST .../approve` both reached the server
        # while the guard allowed them. Only tools that cannot make a request
        # are exempt now. `git` stays exempt — it cannot POST to an arbitrary
        # URL, and `git log -S".../shipped"` must keep working — while `gh`
        # and `glab`, which have `api`, do not.
        # A script tool's payload is analysed by its own branch above, which
        # requires evidence of a REQUEST as well as the route — so this rule
        # must not also fire on the payload text, or `node -e
        # "console.log('/api/tasks/x/approve')"` is denied for printing.
        #
        # `gh`/`glab` are exempt EXCEPT for their `api` subcommand: `gh api -X
        # POST <url>` posts (proven against a live listener), while
        # `gh pr create --title "fix /api/tasks/{id}/shipped 409"` is a title.
        exempt = (name in _READ_ONLY_TOOLS or name in _ROUTE_EXEMPT_TOOLS
                  or name in _SCRIPT_TOOLS
                  or (name in {"gh", "glab"}
                      and _nh_subcommand(argv, table)[:1] != ["api"]))
        # against the UNMASKED text: _REDIR_AMP masks `>&3` before this runs.
        if exempt and _FD_WRITE.search(_unmask(seg, table)):
            exempt = False
        if not exempt:
            for tok in argv[1:]:
                if _gate_route(_unmask(tok, table)):
                    return _APPROVE_REASON
            if (name in _SOCKET_TOOLS and sep == "|"
                    and _gate_route(_unmask(prev_raw, table))):
                return _APPROVE_REASON
        prev_raw = seg
    return None


_APPROVE_REASON = (
    "approving a task ends the human gate — `nh approve` (and `no-human "
    "approve`, the same entry point) squash-lands the PR under the operator "
    "identity and pushes to the default branch, and POST "
    "/api/tasks/<id>/approve, /approve-landed, /shipped and /finish-review "
    "are the same act over HTTP. The agent never merges, in any session mode, "
    "so it never approves. Open or update your PR and stop; a human approves "
    "it."
)

_UNDECIDABLE_REASON = (
    "this command cannot be resolved well enough to allow it, and it names an "
    "action that ends the human gate. A variable, a `$'...'` quote or a nested "
    "substitution in the command or verb position means the guard cannot tell "
    "what will actually run, and it fails closed rather than guessing. Spell "
    "the command plainly, or — if you were reaching for `nh approve` — stop: "
    "the agent never approves, a human does."
)

_MERGE_STACK_REASON = (
    "`nh merge-stack run` merges PRs — it drives `gh pr merge` for every ready "
    "PR in the stack — and the agent never merges, in any session mode. It is "
    "the operator's command. Open or update your PR and stop; a human runs the "
    "merge stack."
)

_LIVE_SERVER_REASON = (
    "launching a live no_human server/runner (`nh serve`/`start`/`watch`/"
    "`bench run`, under either console script) is blocked in agent sessions — "
    "it runs against the operator's real ~/.no_human config, database, and "
    "credentials regardless of checkout. Test CLI behavior through the test "
    "suite (CliRunner), never a live process."
)


# Any git/forge command that mutates history or a remote. A read-only session
# (planner, aggregator, reviewer) explores and reports; it never writes. Dropping
# the blanket `git merge` ban would otherwise have let a reviewer commit.
# A GLOBAL OPTION BEFORE THE SUBCOMMAND DEFEATS AN ADJACENCY REGEX.
# `gh -R owner/repo pr merge 7 --squash` merges the PR and was ALLOW in EVERY
# session mode; `git -C . commit -am x` and `git -C . push` were ALLOW in a
# read-only session. Both patterns below require the verb to sit immediately
# after the binary, and `-R`/`-C`/`-c`/`--work-tree` slide in front of it.
#
# This file already knew the shape twice over. `_git_subcommand` exists a few
# hundred lines down precisely to skip git's own options, and
# tests/test_guard.py already asserts `git -C /repo stash` and
# `git -c user.name=x stash pop` are denied by the worktree-safe family. The
# merge and read-only families never got the same treatment. Found 2026-08-22
# by a reviewer checking a README sentence, not by a sweep of the guard.
#
# The regexes below are KEPT and the argv checks are added ALONGSIDE them —
# additive, never a replacement. That is not a style preference: deleting a
# lexical rule in favour of an argv one is exactly how eight spellings of
# `nh merge-stack run` regressed earlier today.

#: gh/glab options that sit before the subcommand and take a value.
_FORGE_GLOBAL_OPT_WITH_ARG = frozenset({
    "-R", "--repo", "--hostname", "-H", "--host",
})

#: git subcommands a read-only session must not run.
_GIT_WRITE_SUBCOMMANDS = frozenset({
    "commit", "push", "merge", "rebase", "cherry-pick", "revert", "am",
    "apply", "tag", "reset", "restore", "stash", "branch", "checkout",
    "switch",
})

#: (noun, verb) pairs on gh/glab that write, and the subset that MERGES.
_FORGE_WRITE_PAIRS = {
    ("pr", "create"), ("pr", "merge"), ("pr", "close"), ("pr", "edit"),
    ("pr", "ready"), ("pr", "review"),
    ("mr", "create"), ("mr", "merge"), ("mr", "close"), ("mr", "update"),
}
_FORGE_MERGE_PAIRS = {("pr", "merge"), ("mr", "merge")}


def _forge_subcommand(argv: list[str]) -> tuple[str, str]:
    """(noun, verb) for a `gh`/`glab` argv, skipping the tool's own global
    options and their values. `gh -R o/r pr merge 7` -> ("pr", "merge")."""
    words: list[str] = []
    i = 1
    while i < len(argv) and len(words) < 2:
        tok = argv[i]
        if tok.startswith("-"):
            i += 2 if tok in _FORGE_GLOBAL_OPT_WITH_ARG else 1
            continue
        words.append(tok)
        i += 1
    while len(words) < 2:
        words.append("")
    return words[0], words[1]


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


def _strip_wrappers(tokens: list[str], is_extra_target=None) -> list[str]:
    """argv with leading `VAR=value` assignments and `_WRAPPERS` words removed.

    A wrapper may carry its own flags and flag VALUES (`env -i`, `sudo -u me`),
    and their option grammars differ per tool, so no attempt is made to parse
    them: when what follows a wrapper still starts with a flag, the argv is
    taken from the first token that names `git` or a shell runner — the only
    argv[0] values any caller of this function acted on until ``is_extra_target``
    was added. Skipping the unparsed middle can only widen what is analysed,
    never allow more.

    ``is_extra_target`` is an optional ``str -> bool`` predicate widening the
    recovery scan beyond `git`/shell-runners for a caller with its own set of
    interesting argv[0] names (the package-install guard uses it for
    `pip`/`uv`/`poetry`/... — otherwise `env -i pip install …` recovers to
    nothing, since pip is neither git nor a shell runner).
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
            if (name == "git" or name in _SHELL_RUNNERS
                    or (is_extra_target is not None and is_extra_target(name))):
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


def _forge_invocations(cmd: str) -> list[list[str]]:
    """Every `gh`/`glab` argv in ``cmd``, found the way `_git_invocations`
    finds git: split on shell separators, strip `VAR=value` and wrappers, and
    read `basename(argv[0])`. A global option before the subcommand no longer
    hides the verb, because the verb is read from argv rather than matched
    next to the binary."""
    found: list[list[str]] = []
    for seg in _CMD_SEP.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        argv = _strip_wrappers(
            tokens, is_extra_target=lambda n: n in {"gh", "glab"})
        if argv and PurePosixPath(argv[0]).name in {"gh", "glab"}:
            found.append(argv)
    return found


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
    env: Mapping[str, str] | None = None,
) -> GuardDecision:
    """Return allow/deny for a single proposed tool call.

    ``cwd`` is the session's worktree — where the backend will actually run
    the command — so file-existence questions (is `git checkout notes` a
    branch switch or a wipe?) are answered about the right directory. Without
    it the guard answers those questions conservatively (deny).

    ``env`` is the environment the command would run in (``PATH``,
    ``VIRTUAL_ENV``, ...); it defaults to ``os.environ`` so both backends
    keep working unchanged, and exists as a parameter so tests can inject one
    without leaking into the process's real environment.
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
    if readonly and tool_name in SPAWN_TOOLS:
        return GuardDecision(False, _NO_SPAWN_REASON.format(tool=tool_name))

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
        if readonly:
            # argv-shaped, alongside the regexes below: `git -C . commit -am x`
            # and `git -C . push` were ALLOW in a read-only session, and
            # `_git_subcommand` — which skips exactly these options — has been
            # in this file the whole time.
            for _seg, gargv in _git_invocations(cmd):
                sub, _rest = _git_subcommand(gargv)
                if sub in _GIT_WRITE_SUBCOMMANDS:
                    return GuardDecision(
                        False,
                        f"read-only session: git/forge write blocked: {cmd}. "
                        "Read the repo and report; you do not change it.",
                    )
            for fargv in _forge_invocations(cmd):
                if _forge_subcommand(fargv) in _FORGE_WRITE_PAIRS:
                    return GuardDecision(
                        False,
                        f"read-only session: git/forge write blocked: {cmd}. "
                        "Read the repo and report; you do not change it.",
                    )
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
        # `_approve_denial` below covers the product's own CLI, the
        # in-process paths and the local API — all argv-shaped. This one stays
        # lexical: it matches forge spellings whose grammar we do not own.
        # argv-shaped, alongside the regex: `gh -R owner/repo pr merge 7`
        # merged the PR in every session mode, because `_FORGE_MERGE` wants
        # the verb next to the binary.
        for fargv in _forge_invocations(cmd):
            if _forge_subcommand(fargv) in _FORGE_MERGE_PAIRS:
                return GuardDecision(
                    False,
                    "merging a pull/merge request is blocked — the agent never "
                    "merges. Open the PR, push your fixes to its branch, and "
                    "stop. A human merges it (`nh approve`).",
                )
        if _FORGE_MERGE.search(cmd):
            return GuardDecision(
                False,
                "merging a pull/merge request is blocked — the agent never "
                "merges. Open the PR, push your fixes to its branch, and stop. "
                "A human merges it (`nh approve`).",
            )
        # BOTH layers, argv first (its messages are more specific), then the
        # lexical one. Additive: nothing main denies may become allowed here.
        approve_reason = _approve_denial(cmd)
        if approve_reason:
            return GuardDecision(False, approve_reason)
        lex = _join_continuations(cmd)
        if _LEXICAL_MERGE_STACK.search(lex):
            return GuardDecision(False, _MERGE_STACK_REASON)
        if _LEXICAL_LIVE_SERVER.search(lex):
            return GuardDecision(False, _LIVE_SERVER_REASON)
        # Applies to EVERY session mode, coder included — a whole-filesystem
        # sweep is never the right probe, and gating it on `readonly` would
        # leave the expensive coder path unprotected.
        scan_reason = root_scan_denial(cmd, cwd)
        if scan_reason:
            return GuardDecision(False, scan_reason)
        # Applies to EVERY session mode, like `_LIVE_SERVER` above — this is
        # about the operator's real primary checkout, not the repo's content.
        venv_reason = _venv_install_denial(cmd, cwd)
        if venv_reason:
            return GuardDecision(False, venv_reason)
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
        # Structural (resolved-executable) venv-install guard — task
        # 16a798c1's three review verdicts each defeated a lexical/regex
        # rule here via shell segmentation, not a missing pattern; this is
        # additive and resolves canonical paths instead. Runs last among the
        # Bash checks so a command that is ALSO a merge/push/destructive-git
        # violation keeps reporting that more specific reason. See
        # `venv_install_guard`'s module docstring for the full spec.
        venv_reason = venv_install_guard.denial_reason(cmd, cwd=cwd, env=env)
        if venv_reason:
            return GuardDecision(False, venv_reason)

    return GuardDecision(True)
