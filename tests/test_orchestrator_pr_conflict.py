"""Tests for the "rebase round cannot resolve a generated-artefact conflict"
bugfix: `WakeWatcher._check_pr_conflict` must enumerate conflicting paths,
name them in the `pr_conflict` event, and resolve mechanically (no coder
round) when every conflicting path is a derived artefact. Only
`RELEASE_MANIFEST.txt` unconditionally qualifies (`dc.DERIVED_ARTEFACTS`) --
it is fully rebuilt from the tree by `export_guard.py approve`.
`EXPORT_CLASSIFICATION.txt` sits right next to it in the export gate and is
NOT derived in general: its per-rule win-COUNTS are hand-maintained and no
command re-tallies them, so a conflict touching it -- alone, or mixed with
the manifest -- must still open a coder round BY DEFAULT. The one narrow
exception (`dc.classification_count_only`, exercised end to end below and in
`test_derived_conflict_count_only.py`): when every conflicting hunk in it
differs from the other side ONLY in the numeric count digits -- same verb,
same pattern, same everything else -- the conflict is arithmetic, not a hand
decision, and is repaired with the EXISTING
`approve_merge.reconcile_merge_count_drift` (never a second implementation).
Any edit to a pattern, verb, comment, or an added/removed/reordered rule
line still falls through to a coder round exactly as before.

The scratch repo built by `_repo()` below is a real, from-scratch git
repository with its own bare `origin`, self-contained STUB
`scripts/export_guard.py` / `scripts/build_public_export.py` (the real
`export_guard.py` unconditionally scans a private term inventory on every
`approve`, which a throwaway test repo cannot satisfy), a two-line
`RELEASE_MANIFEST.txt` and a minimal `EXPORT_CLASSIFICATION.txt` that
carries one COUNTED glob rule (`ship   1  src/base*.py`). The stub
`export_guard.py verify` re-tallies that counted rule against the tracked
tree and refuses on a mismatch, the same way the real one does -- so a test
that drives two branches into an identically-auto-merged (hence
non-conflicting) count line can actually exhibit the count-drift bug the
mechanical resolver must not paper over. Tests build branches with real git
commits so that `merge_tree_conflicts` sees a genuine conflict, never a
mocked one.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import GitError, GitRepo, commit_with_manifest_repair
from no_human.vcs import approve_merge
from no_human.vcs import derived_conflict as dc
from no_human.vcs.approve_merge import reconcile_commit_count_drift
from tests.test_vcs import _HOOK_CHECK_SRC


# ---------------------------------------------------------------------------
# git plumbing helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{args} failed rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def _git(cwd, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd)


_BUILD_PUBLIC_EXPORT_STUB = '''\
"""Minimal stand-in for scripts/build_public_export.py, used only by the
test fixture's scratch repo. Exposes exactly the surface
`_ship_classified_paths` (src/no_human/vcs/approve_merge.py) and the stub
`export_guard.py` need: classification parsing + classifying, nothing about
term scanning or tree verification.

Rules may optionally carry a hand-maintained win-COUNT (`ship   1  glob`),
mirroring the real EXPORT_CLASSIFICATION.txt format. `check_counts()` is the
stub's only re-tallying logic -- deliberately just a comparison against the
tree, never a rewrite: nothing here regenerates a drifted count, matching
`scripts/export_guard.py`, which has no count-rewriting path either.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

CLASSIFICATION_NAME = "EXPORT_CLASSIFICATION.txt"
RELEASE_MANIFEST_NAME = "RELEASE_MANIFEST.txt"


@dataclass
class Rule:
    # Field names/order mirror the REAL scripts/build_public_export.py Rule
    # exactly (verb, declared, pattern, lineno) -- `approve_merge.
    # reconcile_commit_count_drift` dynamically imports this module (or the
    # real one) and reads those attribute names plus `Classification.wins`,
    # so a test stub with different names would AttributeError instead of
    # exercising the reconciler.
    verb: str
    declared: int | None
    pattern: str
    lineno: int


@dataclass
class Classification:
    shipped: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    unclassified: list = field(default_factory=list)
    wins: dict = field(default_factory=dict)   # rule.lineno -> real count


def parse_classification(text: str) -> list:
    rules = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 2 or parts[0] not in ("ship", "drop"):
            continue
        verb = parts[0]
        if len(parts) >= 3 and parts[1].isdigit():
            rules.append(Rule(verb=verb, declared=int(parts[1]), pattern=parts[2], lineno=lineno))
        else:
            # Uncounted rule (no hand-maintained tally) -- declared is None
            # and never checked/rewritten.
            rules.append(Rule(verb=verb, declared=None, pattern=parts[1], lineno=lineno))
    return rules


def classify(rules, paths):
    out = Classification(wins={rule.lineno: 0 for rule in rules})
    for path in paths:
        winner = None
        for rule in rules:
            if fnmatch.fnmatch(path, rule.pattern):
                winner = rule
        if winner is None:
            out.unclassified.append(path)
            continue
        out.wins[winner.lineno] += 1
        (out.shipped if winner.verb == "ship" else out.dropped).append(path)
    out.shipped.sort()
    out.dropped.sort()
    return out


def check_counts(rules, paths) -> list:
    """Re-tally each COUNTED rule's win-count against `paths` (mirrors "last
    matching rule wins" from `classify()`) and report every rule whose
    declared count doesn't match the tree. This never rewrites a count --
    there is no regenerator, by design (see module docstring)."""
    cls = classify(rules, paths)
    problems = []
    for rule in rules:
        if rule.declared is None:
            continue
        actual = cls.wins.get(rule.lineno, 0)
        if actual != rule.declared:
            problems.append(
                f"EXPORT_CLASSIFICATION.txt:{rule.lineno}: `{rule.verb} {rule.declared}  "
                f"{rule.pattern}` actually wins {actual} file(s)."
            )
    return problems
'''


_EXPORT_GUARD_STUB = '''\
"""Minimal stand-in for scripts/export_guard.py, used only by the test
fixture's scratch repo. Implements just `approve <paths...>` and `verify`
against the stub build_public_export.py -- no term scanning, no
`_script_repo_root()` git lookup (this always runs with an explicit cwd).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import build_public_export as builder

MANIFEST_HEADER = "# generated pins -- do not hand edit\\n"


def _tracked(root: Path) -> list[str]:
    import subprocess
    out = subprocess.run(["git", "ls-files"], cwd=str(root), capture_output=True, text=True).stdout
    return [p for p in out.splitlines() if p]


def _rules(root: Path):
    text = (root / builder.CLASSIFICATION_NAME).read_text(encoding="utf-8")
    return builder.parse_classification(text)


def _classification(root: Path):
    return builder.classify(_rules(root), _tracked(root))


def _write_pins(root: Path, pins: dict) -> None:
    lines = [MANIFEST_HEADER]
    for path in sorted(pins):
        lines.append(f"{pins[path]}  {path}\\n")
    (root / builder.RELEASE_MANIFEST_NAME).write_text("".join(lines), encoding="utf-8")


def _read_pins(root: Path) -> dict:
    manifest = root / builder.RELEASE_MANIFEST_NAME
    if not manifest.exists():
        return {}
    pins = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, _, path = line.partition("  ")
        if path:
            pins[path] = digest
    return pins


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cmd_approve(args) -> int:
    root = args.root
    # Same order and same phrasing as the real guard (scripts/export_guard.py
    # `_cmd_approve` -> build_public_export.classification_errors): a count
    # that drifted refuses BEFORE any pin is written, rc 2.
    drift = builder.check_counts(_rules(root), _tracked(root))
    if drift:
        # Real export_guard.py writes refusals to STDERR (manifest_repair.py's
        # reactive path reads `proc.stderr` to find the count-drift message).
        sys.stderr.write("approve: REFUSED -- fix EXPORT_CLASSIFICATION.txt first -- "
                         "approvals on top of a wrong classification pin the wrong review:\\n"
                         + "\\n".join(f"  {d}" for d in drift) + "\\n")
        return 2
    cls = _classification(root)
    bad = [p for p in args.paths if p not in cls.shipped]
    if bad:
        sys.stderr.write(
            "approve: REFUSED -- not ship-classified (classify each in "
            f"{builder.CLASSIFICATION_NAME} first):\\n"
            + "\\n".join(f"  {p}" for p in bad) + "\\n"
        )
        return 2
    pins = _read_pins(root)
    for p in args.paths:
        pins[p] = _sha256(root / p)
    _write_pins(root, pins)
    sys.stdout.write(f"approve: pinned {len(args.paths)} path(s)\\n")
    return 0


def _cmd_verify(args) -> int:
    root = args.root
    rules = _rules(root)
    tracked = _tracked(root)
    cls = builder.classify(rules, tracked)
    pins = _read_pins(root)
    problems = []
    shipped = [p for p in cls.shipped if p != builder.RELEASE_MANIFEST_NAME]
    for p in shipped:
        if p not in pins:
            problems.append(f"missing pin: {p}")
        elif pins[p] != _sha256(root / p):
            problems.append(f"stale pin: {p}")
    for p in pins:
        if p not in shipped:
            problems.append(f"pinned but not shipped: {p}")
    problems.extend(builder.check_counts(rules, tracked))
    if problems:
        sys.stdout.write("verify: FAILED\\n" + "\\n".join(f"  {p}" for p in problems) + "\\n")
        return 1
    sys.stdout.write(f"verify: OK -- {len(shipped)} shipped == {len(pins)} pins\\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_approve = sub.add_parser("approve")
    p_approve.add_argument("paths", nargs="+")
    sub.add_parser("verify")
    args = parser.parse_args()
    args.root = args.root.resolve()
    if args.cmd == "approve":
        return _cmd_approve(args)
    return _cmd_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _repo(tmp_path: Path) -> Path:
    """Build a from-scratch repo (+ bare origin, pushed) with the stub export
    gate wired, one ship-classified source file, and a pinned manifest.
    Returns the working-tree path. `main` is pushed with `-u` so
    `@{upstream}` resolves (required by `pr_watcher._base_tips`).
    """
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _run(["git", "init", "-q", "--bare", str(origin)], cwd=tmp_path)
    _run(["git", "init", "-q", "-b", "main", str(work)], cwd=tmp_path)
    _git(work, "config", "user.email", "a@example.com")
    _git(work, "config", "user.name", "a")

    scripts = work / "scripts"
    scripts.mkdir()
    (scripts / "build_public_export.py").write_text(_BUILD_PUBLIC_EXPORT_STUB, encoding="utf-8")
    (scripts / "export_guard.py").write_text(_EXPORT_GUARD_STUB, encoding="utf-8")
    (work / "EXPORT_CLASSIFICATION.txt").write_text(
        # `src/base*.py` is a COUNTED rule (last-match-wins over the general
        # `ship src/**`, so it "wins" the tally for files it also matches) --
        # this is the rule the count-drift tests bump. Files added by other
        # tests (on_feature.py, feat_a.py, ...) don't start with "base", so
        # they never touch this rule's declared count.
        # Models the real repo: the classification file itself is DROPPED (never
        # pinned), and the tests rule carries a COUNT so a drift in a drop rule
        # is a real thing `verify` can catch.
        "ship src/**\nship   1  src/base*.py\ndrop   1  tests/**\ndrop   1  EXPORT_CLASSIFICATION.txt\n", encoding="utf-8"
    )
    (work / "RELEASE_MANIFEST.txt").write_text("# generated pins -- do not hand edit\n", encoding="utf-8")
    src = work / "src"
    src.mkdir()
    (src / "base.py").write_text("base\n", encoding="utf-8")
    tests_dir = work / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("test x\n", encoding="utf-8")

    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _approve(work, ["src/base.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base.py")

    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


def _approve(work: Path, paths: list[str], *, expect_ok: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        [sys.executable, "scripts/export_guard.py", "approve", *paths],
        cwd=str(work), capture_output=True, text=True,
    )
    if expect_ok:
        # The stub guard can refuse (count drift, not ship-classified); a
        # fixture that silently fails to pin surfaces two git commands later
        # as "nothing to commit" — name it here instead.
        assert r.returncode == 0, f"approve refused in a fixture:\n{r.stdout}{r.stderr}"
    return r


def _verify(work: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/export_guard.py", "verify"],
        cwd=str(work), capture_output=True, text=True,
    )


def _worktree(work: Path, dest: Path, branch: str, base: str = "main") -> None:
    _git(work, "worktree", "add", "-q", "-b", branch, str(dest), base)


def _push_branch(work: Path, wt: Path, branch: str) -> None:
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    _git(work, "push", "-q", "origin", f"{sha}:refs/heads/{branch}")
    # also make the local branch ref (created by `worktree add -b`) match,
    # and set its upstream so future @{upstream} lookups on it work too.
    _git(work, "branch", "-q", "--set-upstream-to", f"origin/{branch}", branch)


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def _approval_task(store, repo_path: str, *, branch="feature", base="main"):
    t = Task.new("conflict", repo_path=repo_path)
    t.context = {
        "pr_watch": "https://code.example.com/dev/x/pull/26",
        "pr_branch": branch,
        "base_branch": base,
    }
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable="CONFLICTING", merge_state="DIRTY", events=None,
             derived_resolver=None):
    async def pr_mergeable(url):
        return {"mergeable": mergeable, "mergeStateStatus": merge_state}
    return WakeWatcher(
        store, {},
        pr_mergeable=pr_mergeable,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
        derived_resolver=derived_resolver,
    )


def _use_stub_export_guard(monkeypatch):
    """Point derived_conflict._export_guard_argv() at the real interpreter +
    the stub script, instead of `uv run python ...`."""
    monkeypatch.setattr(dc, "_export_guard_argv", lambda: [sys.executable, "scripts/export_guard.py"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_the_pr_conflict_event_names_the_conflicting_paths(store, tmp_path, monkeypatch):
    """A source-only conflict (negative control shape) still names the path
    in the pr_conflict event before opening a coder round."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature edits base.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main also edits base.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "src/base.py" in text


async def test_an_unenumerable_conflict_escalates_instead_of_opening_a_round(store):
    """repo_path unresolvable (as in the pre-existing fake-repo tests):
    conflicting_paths() returns None without raising, on both the first
    attempt and the post-fetch retry -- an unknown, so it escalates instead
    of opening a coder round on an unresolved question (bugfix: this used to
    fall through to `_resume` with a bare "could not enumerate" event)."""
    events = []
    t = await _approval_task(store, "/tmp/does-not-exist")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "escalated_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict" not in kinds
    text = next(txt for k, txt in events if k == "escalated_pr_conflict")
    assert "could not enumerate" not in text
    assert "no coder round opened" in text

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert stored.blocker["category"] == "NOVEL_UNKNOWN"
    assert stored.blocker["evidence"]
    assert not (stored.context or {}).get("send_back_feedback")


async def test_a_manifest_only_conflict_is_resolved_without_a_coder_session(store, tmp_path, monkeypatch):
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    # feature: adds a new ship-classified file, pins it into the manifest.
    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "src/on_feature.py")
    _git(wt_f, "commit", "-qm", "add on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    # main: independently adds a different ship-classified file, pins it too
    # -- both edits append to the same short manifest, so they collide on
    # the exact same insertion point (verified empirically: two independent
    # end-of-file appends to a short file are a genuine git merge conflict,
    # not an auto-merge).
    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: confirm the conflict is real and confined to the manifest.
    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt"}

    events = []
    t = await _approval_task(store, str(work))
    resolver_calls = []

    def spying_resolver(repo_path, branch, base_tip_sha, remote="origin",
                         eligible=dc.DERIVED_ARTEFACTS):
        resolver_calls.append((repo_path, branch, base_tip_sha))
        return dc.resolve_derived_conflict(repo_path, branch, base_tip_sha,
                                            remote=remote, eligible=eligible)

    w = _watcher(store, events=events, derived_resolver=spying_resolver)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resolved_pr_conflict"
    assert len(resolver_calls) == 1
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds
    assert "resumed" not in kinds  # no coder round
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "RELEASE_MANIFEST.txt" in text

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL  # unchanged by mechanical resolution

    # the pushed feature branch's manifest now pins both files and verifies clean.
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "feature")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr
    pins_text = (wt_check / "RELEASE_MANIFEST.txt").read_text(encoding="utf-8")
    assert "src/on_feature.py" in pins_text
    assert "src/on_main.py" in pins_text


async def test_a_raising_enumeration_recovers_after_a_ref_fetch_and_resolves_mechanically(
        store, tmp_path, monkeypatch):
    """conflicting_paths() raises once (simulating a stale/missing ref) then
    succeeds once `fetch_conflict_refs` has run -- the mechanical resolver is
    still reached and no coder round is opened."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "src/on_feature.py")
    _git(wt_f, "commit", "-qm", "add on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: confirm the conflict is real and confined to the manifest.
    assert await dc.conflicting_paths(str(work), "main", "feature") == {"RELEASE_MANIFEST.txt"}

    real_conflicting_paths = dc.conflicting_paths
    real_fetch = dc.fetch_conflict_refs
    calls = {"n": 0}
    fetch_calls = []

    async def flaky(repo_path, base_tip, branch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad object main")
        return await real_conflicting_paths(repo_path, base_tip, branch)

    async def spying_fetch(repo_path, base, branch):
        fetch_calls.append((repo_path, base, branch))
        return await real_fetch(repo_path, base, branch)

    monkeypatch.setattr(dc, "conflicting_paths", flaky)
    monkeypatch.setattr(dc, "fetch_conflict_refs", spying_fetch)

    events = []
    t = await _approval_task(store, str(work))
    resolver_calls = []

    def spying_resolver(repo_path, branch, base_tip_sha, remote="origin",
                         eligible=dc.DERIVED_ARTEFACTS):
        resolver_calls.append((repo_path, branch, base_tip_sha))
        return dc.resolve_derived_conflict(repo_path, branch, base_tip_sha,
                                            remote=remote, eligible=eligible)

    w = _watcher(store, events=events, derived_resolver=spying_resolver)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert fetch_calls == [(str(work), "main", "feature")]
    assert calls["n"] == 2
    assert result == "resolved_pr_conflict"
    assert len(resolver_calls) == 1
    kinds = [k for k, _ in events]
    assert "pr_conflict" not in kinds  # no coder round
    assert "pr_conflict_resolved" in kinds

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL  # unchanged, not IMPLEMENTING
    assert stored.context.get("pr_conflict_enumerate_error")
    assert "bad object main" in stored.context["pr_conflict_enumerate_error"]


async def test_an_always_raising_enumeration_escalates_instead_of_opening_a_coder_round(
        store, tmp_path, monkeypatch):
    """conflicting_paths() raises on both the first attempt and the retry
    after a failed fetch -- the repro for the bugfix: escalate NOVEL_UNKNOWN
    with the exception text, never open a coder round. Uses a real repo (not
    the unresolvable-path shape of the None-return test above) so this
    exercises the actual raise-handling branch, distinct from a plain None
    return."""
    work = _repo(tmp_path)

    async def always_raises(repo_path, base_tip, branch):
        raise RuntimeError("fatal: bad object refs/heads/main")

    async def fetch_always_fails(repo_path, base, branch):
        return False

    monkeypatch.setattr(dc, "conflicting_paths", always_raises)
    monkeypatch.setattr(dc, "fetch_conflict_refs", fetch_always_fails)

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "escalated_pr_conflict"

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert stored.blocker["category"] == "NOVEL_UNKNOWN"
    assert "bad object refs/heads/main" in stored.blocker["evidence"]
    assert stored.context.get("pr_conflict_enumerate_error")
    assert "bad object refs/heads/main" in stored.context["pr_conflict_enumerate_error"]

    kinds = [k for k, _ in events]
    assert "pr_conflict" not in kinds  # no coder round
    assert not (stored.context or {}).get("send_back_feedback")

    text = next(txt for k, txt in events if k == "escalated_pr_conflict")
    assert "bad object refs/heads/main" in text
    assert "could not enumerate" not in text

    persisted = await store.list_events(t.id)
    escalate_events = [e for e in persisted if e.get("kind") == "escalated_pr_conflict"]
    assert escalate_events
    assert "bad object refs/heads/main" in escalate_events[0].get("error", "")


async def test_a_successful_enumeration_never_fetches_or_escalates(store, tmp_path, monkeypatch):
    """A source-only conflict that enumerates cleanly on the first try must
    never call `fetch_conflict_refs` and must never escalate -- the
    fetch/retry machinery is purely a failure-recovery path."""
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature edits base.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main also edits base.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    fetch_calls = []

    async def spy_fetch(repo_path, base, branch):
        fetch_calls.append((repo_path, base, branch))
        return True

    monkeypatch.setattr(dc, "fetch_conflict_refs", spy_fetch)

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert fetch_calls == []
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds
    assert "escalated_pr_conflict" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "src/base.py" in text

    persisted = await store.list_events(t.id)
    pr_conflict_events = [e for e in persisted if e.get("kind") == "pr_conflict"]
    assert pr_conflict_events
    assert "error" not in pr_conflict_events[0]


def test_derived_artefacts_is_exact_repo_root_paths():
    """RELEASE_MANIFEST.txt only: EXPORT_CLASSIFICATION.txt's win-counts are
    hand-maintained and no command re-tallies them, so it is not eligible
    for mechanical (`--ours`) conflict resolution."""
    assert dc.DERIVED_ARTEFACTS == frozenset({"RELEASE_MANIFEST.txt"})


async def test_a_mixed_ship_and_drop_change_pins_only_the_ship_path(store, tmp_path, monkeypatch):
    """A drop-classified file's changed content must not block mechanical
    resolution: export_guard refuses to approve it (not ship-classified),
    that refusal is handled gracefully (committed unpinned), and the
    ship-classified file in the same branch still gets pinned."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    (wt_f / "tests" / "test_x.py").write_text("test x changed\n", encoding="utf-8")
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "add ship file + change drop file")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt"}

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resolved_pr_conflict"
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "unpinned" in text
    assert "tests/test_x.py" in text

    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "feature")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr
    pins_text = (wt_check / "RELEASE_MANIFEST.txt").read_text(encoding="utf-8")
    assert "src/on_feature.py" in pins_text
    assert "tests/test_x.py" not in pins_text
    # the drop-classified file's own change still landed on the branch, just unpinned.
    assert (wt_check / "tests" / "test_x.py").read_text(encoding="utf-8") == "test x changed\n"


async def test_a_source_conflict_still_opens_a_coder_round_exactly_as_today(store, tmp_path, monkeypatch):
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature edits base.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main also edits base.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"src/base.py"}

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []  # mechanical path never entered
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds
    assert "pr_conflict_resolved" not in kinds
    assert "escalated_pr_conflict" not in kinds
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


async def test_a_derived_shaped_conflict_with_an_unresolvable_base_tip_escalates_not_a_coder_round(
        store, tmp_path, monkeypatch):
    """Review finding on PR #568: after `mechanically_resolvable` replaced
    `all_derived`, a conflict confined to the derived/classification files
    whose base tip could NOT be resolved (the ref vanished between
    enumeration's own resolve and the watcher's) left `eligible=None` and
    fell through to a PAID coder round. Main escalated it ("could not resolve
    the base tip to a commit"). A coder cannot fix these files; the honest
    outcome is the escalation. Fails only wake.py's resolve call — the
    enumeration's own call (inside `conflicting_paths`) still succeeds."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "RELEASE_MANIFEST.txt").write_text("pin feature\n", encoding="utf-8")
    _git(wt, "commit", "-qam", "feature re-pins")
    _push_branch(work, wt, "feature")
    (work / "RELEASE_MANIFEST.txt").write_text("pin main\n", encoding="utf-8")
    _git(work, "commit", "-qam", "main re-pins")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    real_resolve = dc.resolve_base_tip
    calls = {"n": 0}

    async def vanishing_ref(repo_path, base_branch):
        # First caller is `conflicting_paths` (enumeration) -> real answer;
        # the watcher's own call is the one that comes back empty.
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_resolve(repo_path, base_branch)
        return None

    monkeypatch.setattr(dc, "resolve_base_tip", vanishing_ref)
    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt"}
    calls["n"] = 0  # the watcher's enumeration is call #1 again

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(
        t, "https://code.example.com/dev/x/pull/27", "DIRTY", branch="feature")

    assert result == "escalated_pr_conflict", result
    assert resolver_calls == []             # nothing to resolve against
    kinds = [k for k, _ in events]
    assert "escalated_pr_conflict" in kinds
    assert "resumed" not in kinds           # never a coder round
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert "could not resolve the base tip" in (stored.blocker or {}).get("evidence", "")


async def test_a_mixed_derived_and_source_conflict_opens_a_coder_round(store, tmp_path, monkeypatch):
    """Not every conflicting path is derived: unchanged behaviour, even
    though RELEASE_MANIFEST.txt is one of the conflicting paths too."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "base.py").write_text("base\nfeature change\n", encoding="utf-8")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "feature edits base.py and adds on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "base.py").write_text("base\nmain change\n", encoding="utf-8")
    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main edits base.py and adds on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"src/base.py", "RELEASE_MANIFEST.txt"}

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "src/base.py" in text and "RELEASE_MANIFEST.txt" in text


async def test_a_failing_verify_escalates_and_pushes_nothing(store, tmp_path, monkeypatch):
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt_f, "add", "src/on_feature.py")
    _git(wt_f, "commit", "-qm", "add on_feature.py")
    _approve(wt_f, ["src/on_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _git(work, "commit", "-qm", "add on_main.py")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    before_sha = _git(work, "ls-remote", "origin", "refs/heads/feature").stdout.strip()

    def failing_resolver(repo_path, branch, base_tip_sha, remote="origin",
                          eligible=dc.DERIVED_ARTEFACTS):
        return dc.DerivedResolution(ok=False, step="verify", detail="synthetic failure for the test")

    events = []
    t = await _approval_task(store, str(work))
    w = _watcher(store, events=events, derived_resolver=failing_resolver)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "escalated_pr_conflict"
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert stored.blocker["category"] == "NOVEL_UNKNOWN"
    assert "verify" in stored.blocker["question"]

    after_sha = _git(work, "ls-remote", "origin", "refs/heads/feature").stdout.strip()
    assert after_sha == before_sha  # nothing pushed

    kinds = [k for k, _ in events]
    assert "escalated_pr_conflict" in kinds
    assert "pr_conflict_resolved" not in kinds
    assert "resumed" not in kinds


async def test_two_concurrent_branches_collide_on_the_manifest_and_resolve_mechanically(store, tmp_path, monkeypatch):
    """End-to-end: two independently-built branches append distinct
    ship-classified files, both regenerate the manifest, and merging one
    into the other's base produces a REAL git conflict confined to
    RELEASE_MANIFEST.txt -- resolved mechanically, no coder session."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "feat_a.py").write_text("feature a\n", encoding="utf-8")
    _git(wt_a, "add", "src/feat_a.py")
    _git(wt_a, "commit", "-qm", "add feat_a.py")
    _approve(wt_a, ["src/feat_a.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin feat_a.py")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "feat_b.py").write_text("feature b\n", encoding="utf-8")
    _git(work, "add", "src/feat_b.py")
    _git(work, "commit", "-qm", "add feat_b.py")
    _approve(work, ["src/feat_b.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin feat_b.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    merged_conflicts = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert merged_conflicts == {"RELEASE_MANIFEST.txt"}

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    no_coder_session = []
    w._resume = lambda task: no_coder_session.append(task) or pytest.fail("coder round opened")  # type: ignore[assignment]

    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    assert no_coder_session == []


async def test_two_concurrent_count_bumps_are_reconciled_by_merge_arithmetic(
    store, tmp_path, monkeypatch,
):
    """The Issue-1 regression scenario: two branches each add a file
    matching the SAME counted glob rule and each independently (and, alone,
    correctly) bump that rule's declared count by one. Because both edits
    are the identical text change ("1" -> "2"), git auto-merges
    EXPORT_CLASSIFICATION.txt cleanly -- it is NOT a conflicting path -- so
    the only conflict is RELEASE_MANIFEST.txt, `all_derived()` is True, and
    mechanical resolution is attempted. But the merged tree actually holds
    base.py + base_two.py + base_three.py == 3 files matching
    `src/base*.py`, not the 2 the auto-merged count declares. `export_guard
    `export_guard verify` used to catch that drift and refuse, and this test
    pinned "the resolver must NOT report this as resolved" -- the task then
    escalated to a human, who did the arithmetic by hand (2026-08-20, task
    c309a6a3 / PR #511, a review-PASSED delivery). That was the defect, not
    the doctrine: two reviewed counts meeting is arithmetic, not a hand
    decision -- base (2) + (branch (2) - merge-base (1)) == real (3) -- so the
    resolver now rewrites the number under exactly that equality and refuses
    anything else (negative control below). The stub guard refuses `approve`
    on a drifted count with the real guard's phrasing, so this exercises the
    real refuse -> reconcile -> re-approve path, not a shortcut.
    """
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    _git(work, "add", "src/base_three.py")
    _bump_count(work, "src/base*.py", 2)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "add base_three.py, bump counted rule 1 -> 2")
    _approve(work, ["src/base_three.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base_three.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: the identical count bump auto-merges -- EXPORT_CLASSIFICATION.txt
    # is not a conflicting path, only the manifest is, and it IS eligible.
    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {"RELEASE_MANIFEST.txt"}
    assert dc.all_derived(paths)

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    # The human gate must be able to SEE that a hand-maintained file was
    # edited, and by what arithmetic.
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "EXPORT_CLASSIFICATION.txt count reconciled" in text and "2 -> 3" in text, text
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    assert "ship   3  src/base*.py" in (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr


async def test_a_count_only_classification_conflict_is_resolved_without_a_coder_session(
    store, tmp_path, monkeypatch,
):
    """The bugfix scenario: EXPORT_CLASSIFICATION.txt is ITSELF a conflicting
    path (unlike the identical-bump case above, which auto-merges cleanly) --
    branch-a bumps the counted rule 1 -> 2 (for the one file it adds), main
    bumps the SAME rule 1 -> 3 (for the two files it adds). The two edits are
    different text on the same line, so git conflicts on it for real. But the
    only difference between the two conflicting hunks is the digit -- same
    verb, same pattern -- so this is arithmetic, not a hand decision, and
    `mechanically_resolvable` must say so and the resolver must repair it with
    the SAME `reconcile_merge_count_drift` arithmetic as the clean-auto-merge
    case, never a second implementation."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    (work / "src" / "base_four.py").write_text("base four\n", encoding="utf-8")
    _git(work, "add", "src/base_three.py", "src/base_four.py")
    _bump_count(work, "src/base*.py", 3)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "add base_three.py + base_four.py, bump counted rule 1 -> 3")
    _approve(work, ["src/base_three.py", "src/base_four.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin base_three.py + base_four.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    # sanity: this time the count bumps genuinely conflict (different digits
    # on the same line) -- EXPORT_CLASSIFICATION.txt IS a conflicting path,
    # `all_derived()` is False (it never considers this file), but the
    # count-only shape check says the conflict is still mechanical.
    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert dc.CLASSIFICATION_NAME in paths and paths <= (dc.DERIVED_ARTEFACTS | {dc.CLASSIFICATION_NAME})
    assert not dc.all_derived(paths)
    base_tip = await dc.resolve_base_tip(str(work), "main")
    eligible = await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a")
    assert eligible == paths

    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" in kinds and "resumed" not in kinds
    text = next(txt for k, txt in events if k == "pr_conflict_resolved")
    assert "EXPORT_CLASSIFICATION.txt count reconciled" in text, text
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.AWAITING_APPROVAL
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    assert "ship   4  src/base*.py" in (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr


async def test_a_count_conflict_that_also_flips_a_verb_opens_a_coder_round(store, tmp_path, monkeypatch):
    """Same shape as the count-only conflict above, EXCEPT one side also
    flips the rule's verdict (ship -> drop) instead of only changing the
    digit. That is a hand decision, not arithmetic, so this must still open
    a coder round -- the shape check must be exact-except-count, not merely
    'touches the same line'."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    cls = work / "EXPORT_CLASSIFICATION.txt"
    cls.write_text(
        cls.read_text(encoding="utf-8").replace("ship   1  src/base*.py", "drop   1  src/base*.py"),
        encoding="utf-8",
    )
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "main reclassifies the counted rule to drop")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {"EXPORT_CLASSIFICATION.txt"}
    assert not dc.all_derived(paths)
    base_tip = await dc.resolve_base_tip(str(work), "main")
    assert await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a") is None

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result == "resumed"
    assert resolver_calls == []
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


async def test_a_count_only_conflict_whose_arithmetic_fails_escalates(store, tmp_path, monkeypatch):
    """A count-only conflict (same verb, same pattern, only the digit
    differs) whose two declared counts don't satisfy the merge-base
    arithmetic must escalate honestly, never guess and push. Main bumps to 5
    instead of the correct 3 for the two files it adds -- a hand mistake, not
    a derivable number."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    # Hand-pin instead of `_approve()` here: main's own declared count (5) is
    # wrong for main's own tree (3 files: base.py + the two new ones), so the
    # stub `approve` would correctly refuse it as internally inconsistent --
    # that is a DIFFERENT bug than the one under test. This mirrors
    # `test_a_count_drift_that_is_not_merge_arithmetic_still_refuses` above:
    # a hand mistake that only shows up once merged with branch-a.
    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    (work / "src" / "base_four.py").write_text("base four\n", encoding="utf-8")
    _git(work, "add", "src/base_three.py", "src/base_four.py")
    _bump_count(work, "src/base*.py", 5)
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "add base_three.py + base_four.py, bump counted rule 1 -> 5 (wrong)")
    pins = work / "RELEASE_MANIFEST.txt"
    pins.write_text(
        pins.read_text(encoding="utf-8")
        + "0" * 64 + "  src/base_four.py\n"
        + "0" * 64 + "  src/base_three.py\n",
        encoding="utf-8",
    )
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "hand pin base_three.py + base_four.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    base_tip = await dc.resolve_base_tip(str(work), "main")
    eligible = await dc.mechanically_resolvable(str(work), paths, base_tip, "branch-a")
    assert eligible == paths

    before = _git(work, "rev-parse", "origin/branch-a").stdout.strip()
    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")

    assert result != "resolved_pr_conflict"
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    evidence = (stored.blocker.get("evidence") or "") + (stored.blocker.get("question") or "")
    assert "not merge arithmetic" in evidence or "not a mechanical merge" in evidence, stored.blocker
    assert _git(work, "rev-parse", "origin/branch-a").stdout.strip() == before, "arithmetic failed but something was pushed"


def test_the_count_repair_reuses_reconcile_merge_count_drift(tmp_path):
    """The fix must never grow a second arithmetic implementation --
    `derived_conflict` imports and calls the EXISTING
    `approve_merge.reconcile_merge_count_drift`, proven here by identity, not
    by behaviour (behaviour is covered by the end-to-end tests above)."""
    from no_human.vcs import approve_merge

    assert dc.reconcile_merge_count_drift is approve_merge.reconcile_merge_count_drift


async def test_an_export_classification_conflict_alone_opens_a_coder_round(store, tmp_path, monkeypatch):
    """A conflict confined to EXPORT_CLASSIFICATION.txt is not mechanically
    resolvable: its counts are hand-maintained and no command rebuilds them,
    so even though it sits in the export gate next to RELEASE_MANIFEST.txt,
    a coder round opens exactly as for a source-file conflict."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    _cls = wt / "EXPORT_CLASSIFICATION.txt"
    _cls.write_text(_cls.read_text(encoding="utf-8").replace("drop   1  tests/**", "drop docs/**\ndrop   1  tests/**"), encoding="utf-8")
    _git(wt, "commit", "-qam", "feature reclassifies the counted rule to 5")
    _push_branch(work, wt, "feature")

    _cls = work / "EXPORT_CLASSIFICATION.txt"
    _cls.write_text(_cls.read_text(encoding="utf-8").replace("drop   1  tests/**", "drop dist/**\ndrop   1  tests/**"), encoding="utf-8")
    _git(work, "commit", "-qam", "main reclassifies the counted rule to 7")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"EXPORT_CLASSIFICATION.txt"}
    assert not dc.all_derived(paths)
    base_tip = await dc.resolve_base_tip(str(work), "main")
    assert await dc.mechanically_resolvable(str(work), paths, base_tip, "feature") is None

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []
    kinds = [k for k, _ in events]
    assert "pr_conflict" in kinds
    assert "pr_conflict_resolved" not in kinds
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


async def test_an_export_classification_conflict_mixed_with_manifest_opens_a_coder_round(store, tmp_path, monkeypatch):
    """Not every conflicting path is derived even when RELEASE_MANIFEST.txt
    is one of them: a mixed EXPORT_CLASSIFICATION.txt + manifest conflict
    still opens a coder round, matching the mixed source+manifest case."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "src" / "on_feature.py").write_text("on feature\n", encoding="utf-8")
    _git(wt, "add", "src/on_feature.py")
    _cls = wt / "EXPORT_CLASSIFICATION.txt"
    _cls.write_text(_cls.read_text(encoding="utf-8").replace("drop   1  tests/**", "drop docs/**\ndrop   1  tests/**"), encoding="utf-8")
    _git(wt, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt, "commit", "-qm", "feature adds on_feature.py, adds a drop rule for docs/ (counts stay correct)")
    _approve(wt, ["src/on_feature.py"])
    _git(wt, "add", "RELEASE_MANIFEST.txt")
    _git(wt, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    _cls = work / "EXPORT_CLASSIFICATION.txt"
    _cls.write_text(_cls.read_text(encoding="utf-8").replace("drop   1  tests/**", "drop dist/**\ndrop   1  tests/**"), encoding="utf-8")
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "main adds on_main.py, adds a drop rule for dist/ (counts stay correct)")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt", "EXPORT_CLASSIFICATION.txt"}
    assert not dc.all_derived(paths)
    base_tip = await dc.resolve_base_tip(str(work), "main")
    assert await dc.mechanically_resolvable(str(work), paths, base_tip, "feature") is None

    events = []
    resolver_calls = []
    t = await _approval_task(store, str(work))
    w = _watcher(
        store, events=events,
        derived_resolver=lambda *a, **k: resolver_calls.append((a, k)),
    )
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    assert resolver_calls == []
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "RELEASE_MANIFEST.txt" in text and "EXPORT_CLASSIFICATION.txt" in text
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.IMPLEMENTING


# --------------------------------------------------------------------------- #
# A merge of two reviewed COUNT bumps is arithmetic, not a hand decision      #
# --------------------------------------------------------------------------- #


def _bump_count(root: Path, pattern: str, new: int) -> None:
    text = (root / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    import re as _re
    text = _re.sub(rf"^(ship\s+)\d+(\s+{_re.escape(pattern)})$", rf"\g<1>{new}\2", text, flags=_re.M)
    (root / "EXPORT_CLASSIFICATION.txt").write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_a_count_drift_that_is_not_merge_arithmetic_still_refuses(store, tmp_path, monkeypatch):
    """Negative control: main added a file WITHOUT bumping the count (a stale
    count on one side is a hand problem, not merge arithmetic) — the resolver
    must refuse at 'regenerate' and show the arithmetic, never guess."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "base_feature.py").write_text("f\n", encoding="utf-8")
    _bump_count(wt_f, "src/base*.py", 2)
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "add base_feature.py (count 1->2)")
    _approve(wt_f, ["src/base_feature.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin base_feature.py")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "base_main.py").write_text("m\n", encoding="utf-8")   # count left at 1
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "add base_main.py (count NOT bumped)")
    pins = (work / "RELEASE_MANIFEST.txt")
    pins.write_text(pins.read_text(encoding="utf-8") + "0" * 64 + "  src/base_main.py\n", encoding="utf-8")
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "hand pin (conflicts with feature's manifest)")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    base_tip = _git(work, "rev-parse", "origin/main").stdout.strip()
    res = dc.resolve_derived_conflict(str(work), "feature", base_tip, remote="origin")
    assert not res.ok and res.step == "regenerate"
    assert "not a mechanical merge" in res.detail and "real 3" in res.detail, res.detail


# --------------------------------------------------------------------------- #
# The reconcile's refusal guards are not decorative                           #
# --------------------------------------------------------------------------- #


def _three_way_repo(tmp_path):
    """A repo whose HEAD is its own base/branch/merge-base (the guards under
    test fire before any arithmetic), with the stub guard wired."""
    from no_human.vcs.approve_merge import reconcile_merge_count_drift
    work = _repo(tmp_path)
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    return work, sha, reconcile_merge_count_drift


def test_reconcile_refuses_when_the_refusal_names_no_drift(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    ok, note = reconcile(work, sha, sha, "approve: REFUSED -- not ship-classified")
    assert not ok and "no count drift" in note


def test_reconcile_refuses_a_rule_absent_on_a_side(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    ok, note = reconcile(work, sha, sha,
                         "EXPORT_CLASSIFICATION.txt:9: `ship 1  nope/*.py` actually wins 2 file(s).")
    assert not ok and "not present on every side" in note


def test_reconcile_refuses_a_duplicated_rule(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    cls = work / "EXPORT_CLASSIFICATION.txt"
    cls.write_text(cls.read_text(encoding="utf-8") + "ship   0  src/base*.py\n", encoding="utf-8")
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "duplicate rule")
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    ok, note = reconcile(work, sha, sha,
                         "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).")
    assert not ok and "more than once" in note


def test_reconcile_refuses_when_the_named_line_is_not_in_the_file(tmp_path):
    """The refusal says the rule declares 7; the merged file declares 1 —
    nothing to rewrite, and guessing is exactly what is forbidden."""
    work, sha, reconcile = _three_way_repo(tmp_path)
    # base==branch==merge-base ⇒ expected == declared(1) ⇒ real must be 1 to
    # pass the arithmetic; 1 != 7 on the line ⇒ 0 hits.
    ok, note = reconcile(work, sha, sha,
                         "EXPORT_CLASSIFICATION.txt:2: `ship 7  src/base*.py` actually wins 1 file(s).")
    assert not ok and "matched 0 line(s)" in note
    assert "ship   1  src/base*.py" in (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")


def test_reconcile_refuses_without_a_merge_base(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    ok, note = reconcile(work, sha, "0" * 40,
                         "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).")
    assert not ok and "no merge base" in note


# --------------------------------------------------------------------------- #
# COMMIT-time reconciliation: an attempt's own diff moved a declared count.   #
# `reconcile_commit_count_drift` shares its refusal parser, rewrite-only-the- #
# number writer and "refuse with arithmetic" reporting with the merge-time    #
# `reconcile_merge_count_drift` above -- only the arithmetic differs (base    #
# diff adds/removes vs. base/branch/merge-base declared counts).              #
# --------------------------------------------------------------------------- #


def _commit_time_repo(tmp_path: Path) -> Path:
    """`_repo` plus a REAL git pre-commit hook wired via `_HOOK_CHECK_SRC`
    (the same stub `tests/test_vcs.py` uses for its realistic manifest gate),
    so committing a pinned-file change without a matching manifest re-pin
    takes the REACTIVE path in `commit_with_manifest_repair`: hook refusal ->
    parse -> re-approve subprocess -> `_try_reconcile_count_drift` ->
    `reconcile_commit_count_drift(repo, "HEAD", ...)` -> retry -> commit."""
    work = _repo(tmp_path)
    hook_py = work / ".git" / "hooks" / "_check_manifest.py"
    hook_py.write_text(_HOOK_CHECK_SRC, encoding="utf-8")
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\nexec python3 "$(dirname "$0")/_check_manifest.py"\n', encoding="utf-8")
    hook.chmod(0o755)
    return work


def test_commit_time_count_drift_reconciles_and_the_attempt_proceeds(tmp_path):
    """AC1 (positive repro): the attempt's diff adds exactly one drop-
    classified `tests/*.py` file and leaves the declared count untouched.
    Committing a pinned-file change triggers the real pre-commit hook's
    refusal (staged content != pin), which forces the reactive path; that
    path must reconcile the incidental `tests/**` count drift instead of
    failing the whole attempt. RED on unfixed code (reconcile wiring removed
    or stub schema mismatched): `commit_with_manifest_repair` raises
    `GitError` with 'actually wins' instead of returning a commit sha."""
    work = _commit_time_repo(tmp_path)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y", never_push_to=[])
    repo.create_branch("no-human/commit-time-drift", base="main")

    # A normal coder edit to the already-pinned file (forces the hook to
    # refuse: staged content no longer matches RELEASE_MANIFEST.txt's pin).
    (work / "src" / "base.py").write_text("base\nchanged\n", encoding="utf-8")
    # The incidental new test file: bumps the REAL drop-tests/** count from
    # 1 to 2 without anyone touching EXPORT_CLASSIFICATION.txt.
    (work / "tests" / "test_y.py").write_text("test y\n", encoding="utf-8")

    repairs = []
    result = commit_with_manifest_repair(
        repo, ["src/base.py", "tests/test_y.py"], "fix: y",
        on_repair=lambda paths, note: repairs.append((paths, note)),
    )
    assert result.sha, result
    assert repairs, "on_repair was never called -- reconciliation did not run"
    assert "count drift reconciled" in repairs[-1][1], repairs[-1]

    cls_text = _git(work, "show", "HEAD:EXPORT_CLASSIFICATION.txt").stdout
    assert "drop   2  tests/**" in cls_text, cls_text
    changed = _git(work, "show", "--name-only", "--format=", "HEAD").stdout
    assert "EXPORT_CLASSIFICATION.txt" in changed, changed


def test_commit_time_unexplained_drift_fails_with_the_reconcilers_arithmetic(tmp_path):
    """Refusal path, end to end: the drift at the named rule is +2 but the
    attempt's own diff explains only one file of it, so the safety net must
    DECLINE -- and the failed attempt must show that it ran and why
    (the reconciler's arithmetic), not only the guard's stale number.
    RED on the first cut, which computed the arithmetic and threw it away."""
    work = _commit_time_repo(tmp_path)
    # Pre-existing drift the attempt did not cause: a second tests/** file
    # already on main with the declared count left at 1.
    (work / "tests" / "test_pre.py").write_text("pre\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "pre-existing drift, not this attempt")
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y", never_push_to=[])
    repo.create_branch("no-human/commit-time-unexplained", base="main")

    (work / "src" / "base.py").write_text("base\nchanged\n", encoding="utf-8")
    (work / "tests" / "test_y.py").write_text("test y\n", encoding="utf-8")
    before = (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")

    with pytest.raises(GitError) as err:
        commit_with_manifest_repair(repo, ["src/base.py", "tests/test_y.py"], "fix: y")
    msg = str(err.value)
    assert "manifest re-approve failed" in msg, msg
    assert "count-drift reconciliation declined" in msg, msg
    assert "not explained by this attempt's own changes" in msg, msg
    # Nothing was rewritten: the number is a hand decision here.
    assert (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8") == before


def test_reconcile_commit_count_drift_explains_an_added_file(tmp_path):
    """AC (unit): a single added file the attempt's own diff introduced
    fully explains a ship rule's 1 -> 2 drift."""
    work = _repo(tmp_path)
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    (work / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(work, "add", "src/base_two.py")
    _git(work, "commit", "-qm", "add base_two.py, count not bumped")

    ok, note = reconcile_commit_count_drift(
        work, base_sha,
        "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).",
    )
    assert ok and "1 -> 2" in note, note
    assert "ship   2  src/base*.py" in (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")


def test_reconcile_commit_count_drift_explains_a_removed_file(tmp_path):
    """AC (negative-shaped positive): a removed file reconciles to N-1."""
    work = _repo(tmp_path)
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "rm", "-q", "tests/test_x.py")
    _git(work, "commit", "-qm", "remove tests/test_x.py, count not bumped")

    ok, note = reconcile_commit_count_drift(
        work, base_sha,
        "EXPORT_CLASSIFICATION.txt:3: `drop 1  tests/**` actually wins 0 file(s).",
    )
    assert ok and "1 -> 0" in note, note
    assert "drop   0  tests/**" in (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")


def test_reconcile_commit_count_drift_refuses_unexplained_drift(tmp_path):
    """AC (negative): drift is +2 at the named rule, but the attempt's own
    diff (base_sha..worktree) only added ONE matching file -- one file of
    drift predates the attempt and is not this reconciler's to explain.
    Refuses exactly as today, with the unexplained amount named; no rewrite."""
    work = _repo(tmp_path)
    # Pre-existing drift the attempt did not cause.
    (work / "tests" / "test_pre.py").write_text("pre\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "pre-existing drift, not this attempt")
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    (work / "tests" / "test_new.py").write_text("new\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "attempt adds one more drop file, count not bumped")

    before = (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    ok, note = reconcile_commit_count_drift(
        work, base_sha,
        "EXPORT_CLASSIFICATION.txt:3: `drop 1  tests/**` actually wins 3 file(s).",
    )
    assert not ok and "not explained by this attempt's own changes" in note, note
    assert (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8") == before


def test_reconcile_commit_count_drift_ignores_a_non_matching_added_file(tmp_path):
    """AC (negative): a drop-classified added file must not explain a SHIP
    rule's drift -- the winner lookup (`cls.wins`), not mere presence in the
    diff, decides which rule a path counts against."""
    work = _repo(tmp_path)
    (work / "src" / "base_extra.py").write_text("extra\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "pre-existing ship drift, not this attempt")
    base_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    (work / "tests" / "test_new2.py").write_text("new2\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "attempt adds only a drop-classified file")

    before = (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    ok, note = reconcile_commit_count_drift(
        work, base_sha,
        "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).",
    )
    assert not ok and "not explained by this attempt's own changes" in note, note
    assert (work / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8") == before


def test_reconcile_commit_count_drift_reuses_the_merge_reconcilers_shared_code():
    """The parser, number-writer and refusal reporting are the SAME code as
    `reconcile_merge_count_drift` -- imported, not re-implemented -- and the
    only win-count source is `cls.wins` (no second glob matcher)."""
    src = inspect.getsource(approve_merge)
    assert src.count("def _rewrite_declared_count(") == 1
    assert src.count("def _write_classification_lines(") == 1
    assert src.count("COUNT_DRIFT_RE = ") == 1

    fn_src = inspect.getsource(reconcile_commit_count_drift)
    assert "_rewrite_declared_count(" in fn_src
    assert "_write_classification_lines(" in fn_src
    assert "COUNT_DRIFT_RE" in fn_src
    assert "fnmatch" not in fn_src, "a second glob matcher would defeat cls.wins as the single win-count source"


# --------------------------------------------------------------------------- #
# A drift that never reaches `approve` is still stopped by `verify` (step 7)  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_drift_that_skips_approve_is_caught_by_verify_and_escalates(store, tmp_path, monkeypatch):
    """The branch touches ONLY its manifest (a coder's re-approve comment), so
    `base..branch` names nothing ship-classified, `approve` never runs and the
    reconcile never runs; main meanwhile added tests/test_z.py under the
    COUNTED drop rule without bumping it. Step-7 `verify` is the last gate:
    it must refuse at step 'verify', the task must escalate, and nothing may
    be pushed. RED when `check_counts` is removed from the fixture guard's
    `verify` (the mutant pushes the drifted tree) — the proof that gate is
    live. Also the reconcile's known limit: it rides on `approve`."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    pins = wt_a / "RELEASE_MANIFEST.txt"
    pins.write_text(pins.read_text(encoding="utf-8") + "# re-approved on the branch\n", encoding="utf-8")
    _git(wt_a, "add", "-A")
    _git(wt_a, "commit", "-qm", "manifest-only touch")
    _push_branch(work, wt_a, "branch-a")

    (work / "tests" / "test_z.py").write_text("test z\n", encoding="utf-8")   # drop count left at 1
    pins = work / "RELEASE_MANIFEST.txt"
    pins.write_text(pins.read_text(encoding="utf-8") + "# main re-approved\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "add tests/test_z.py, count NOT bumped")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    before = _git(work, "rev-parse", "origin/branch-a").stdout.strip()

    paths = await dc.conflicting_paths(str(work), "main", "branch-a")
    assert paths == {"RELEASE_MANIFEST.txt"}
    events = []
    t = await _approval_task(store, str(work), branch="branch-a")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="branch-a")
    assert result != "resolved_pr_conflict"
    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    evidence = (stored.blocker.get("evidence") or "") + (stored.blocker.get("question") or "")
    assert "step 'verify'" in evidence and "actually wins" in evidence, stored.blocker
    assert _git(work, "rev-parse", "origin/branch-a").stdout.strip() == before, "verify refused but something was pushed"



def test_reconcile_refuses_when_the_classification_is_absent_on_a_side(tmp_path):
    work, sha, reconcile = _three_way_repo(tmp_path)
    _git(work, "rm", "-q", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "no classification here")
    gone = _git(work, "rev-parse", "HEAD").stdout.strip()
    ok, note = reconcile(work, gone, sha,
                         "EXPORT_CLASSIFICATION.txt:2: `ship 1  src/base*.py` actually wins 2 file(s).")
    assert not ok and "missing on one side" in note


@pytest.mark.asyncio
async def test_reconcile_repins_a_shipped_classification_file(store, tmp_path, monkeypatch):
    """A repo that SHIPS its classification file pins it, and the count rewrite
    stales that pin. The resolver must re-approve it on the retry or step-7
    `verify` refuses the tree — this is the derived_conflict copy of that
    logic (the land fixture covers approve_merge's copy). RED when the
    `_ship_classified_paths(…, [CLASSIFICATION_NAME])` element is removed
    from the retry targets."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)
    # reclassify the classification file as SHIPPED and pin it (a different
    # repo's choice; this repo drops it)
    cls = work / "EXPORT_CLASSIFICATION.txt"
    cls.write_text(cls.read_text(encoding="utf-8").replace(
        "drop   1  EXPORT_CLASSIFICATION.txt", "ship   1  EXPORT_CLASSIFICATION.txt"), encoding="utf-8")
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "this repo ships its classification")
    _approve(work, ["EXPORT_CLASSIFICATION.txt"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin the classification")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _bump_count(wt_a, "src/base*.py", 2)
    _git(wt_a, "add", "-A")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump 1 -> 2")
    _approve(wt_a, ["src/base_two.py", "EXPORT_CLASSIFICATION.txt"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    _bump_count(work, "src/base*.py", 2)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "add base_three.py, bump 1 -> 2")
    _approve(work, ["src/base_three.py", "EXPORT_CLASSIFICATION.txt"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    assert await dc.conflicting_paths(str(work), "main", "branch-a") == {"RELEASE_MANIFEST.txt"}
    base_tip = _git(work, "rev-parse", "origin/main").stdout.strip()
    res = dc.resolve_derived_conflict(str(work), "branch-a", base_tip, remote="origin")
    assert res.ok, f"{res.step}: {res.detail}"
    assert "1 -> 3" not in res.reconciled and "2 -> 3" in res.reconciled
    wt_check = tmp_path / "wt_check"
    _worktree(work, wt_check, "check", "branch-a")
    assert "ship   3  src/base*.py" in (wt_check / "EXPORT_CLASSIFICATION.txt").read_text(encoding="utf-8")
    v = _verify(wt_check)
    assert v.returncode == 0, v.stdout + v.stderr   # the re-pinned classification verifies
