"""Tests for the "rebase round cannot resolve a generated-artefact conflict"
bugfix: `WakeWatcher._check_pr_conflict` must enumerate conflicting paths,
name them in the `pr_conflict` event, and resolve mechanically (no coder
round) when every conflicting path is a derived artefact. Only
`RELEASE_MANIFEST.txt` qualifies (`dc.DERIVED_ARTEFACTS`) -- it is fully
rebuilt from the tree by `export_guard.py approve`. `EXPORT_CLASSIFICATION.txt`
sits right next to it in the export gate but is deliberately NOT derived:
its per-rule win-COUNTS are hand-maintained and no command re-tallies them,
so a conflict touching it -- alone, or mixed with the manifest -- must still
open a coder round.

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

import subprocess
import sys
from pathlib import Path

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import derived_conflict as dc


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
    verdict: str
    pattern: str
    count: int | None = None


@dataclass
class Classification:
    shipped: list = field(default_factory=list)
    dropped: list = field(default_factory=list)


def parse_classification(text: str) -> list:
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("ship", "drop"):
            continue
        verdict = parts[0]
        if len(parts) >= 3 and parts[1].isdigit():
            rules.append(Rule(verdict=verdict, pattern=parts[2], count=int(parts[1])))
        else:
            rules.append(Rule(verdict=verdict, pattern=parts[1]))
    return rules


def classify(rules, paths):
    shipped, dropped = [], []
    for path in paths:
        verdict = "drop"
        for rule in rules:
            if fnmatch.fnmatch(path, rule.pattern):
                verdict = rule.verdict
        (shipped if verdict == "ship" else dropped).append(path)
    return Classification(shipped=sorted(shipped), dropped=sorted(dropped))


def check_counts(rules, paths) -> list:
    """Re-tally each COUNTED rule's win-count against `paths` (mirrors "last
    matching rule wins" from `classify()`) and report every rule whose
    declared count doesn't match the tree. This never rewrites a count --
    there is no regenerator, by design (see module docstring)."""
    winner_idx: dict = {}
    for path in paths:
        idx = None
        for i, rule in enumerate(rules):
            if fnmatch.fnmatch(path, rule.pattern):
                idx = i
        if idx is not None:
            winner_idx[path] = idx
    problems = []
    for i, rule in enumerate(rules):
        if rule.count is None:
            continue
        actual = sum(1 for wi in winner_idx.values() if wi == i)
        if actual != rule.count:
            problems.append(
                f"count drift: {rule.verdict} {rule.pattern} declares "
                f"{rule.count}, tree has {actual}"
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
    cls = _classification(root)
    bad = [p for p in args.paths if p not in cls.shipped]
    if bad:
        sys.stdout.write(
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
        "ship src/**\nship   1  src/base*.py\ndrop tests/**\n", encoding="utf-8"
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


def _approve(work: Path, paths: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/export_guard.py", "approve", *paths],
        cwd=str(work), capture_output=True, text=True,
    )


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


async def test_an_unenumerable_conflict_says_so_and_still_opens_a_round(store):
    """repo_path unresolvable (as in the pre-existing fake-repo tests):
    conflicting_paths() can't run, conflict_desc says so, behaviour is the
    unchanged coder-round fallthrough."""
    events = []
    t = await _approval_task(store, "/tmp/does-not-exist")
    w = _watcher(store, events=events)
    result = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26", "DIRTY", branch="feature")

    assert result == "resumed"
    text = next(txt for k, txt in events if k == "pr_conflict")
    assert "could not enumerate" in text


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

    def spying_resolver(repo_path, branch, base_tip_sha, remote="origin"):
        resolver_calls.append((repo_path, branch, base_tip_sha))
        return dc.resolve_derived_conflict(repo_path, branch, base_tip_sha, remote=remote)

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

    def failing_resolver(repo_path, branch, base_tip_sha, remote="origin"):
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


async def test_two_concurrent_branches_bump_the_same_classification_count_and_verify_catches_the_drift(
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
    verify` must catch that drift and refuse -- the resolver must not report
    this as resolved.

    Mutation-testing note (not asserted here, run by hand): with
    `builder.check_counts()` deleted from `_cmd_verify` above, this test
    goes from FAILING (proving the check matters) to PASSING for the wrong
    reason -- i.e. it was vacuous without the count re-tally.
    """
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt_a = tmp_path / "wt_branch_a"
    _worktree(work, wt_a, "branch-a")
    (wt_a / "src" / "base_two.py").write_text("base two\n", encoding="utf-8")
    _git(wt_a, "add", "src/base_two.py")
    (wt_a / "EXPORT_CLASSIFICATION.txt").write_text(
        "ship src/**\nship   2  src/base*.py\ndrop tests/**\n", encoding="utf-8"
    )
    _git(wt_a, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt_a, "commit", "-qm", "add base_two.py, bump counted rule 1 -> 2")
    _approve(wt_a, ["src/base_two.py"])
    _git(wt_a, "add", "RELEASE_MANIFEST.txt")
    _git(wt_a, "commit", "-qm", "pin base_two.py")
    _push_branch(work, wt_a, "branch-a")

    (work / "src" / "base_three.py").write_text("base three\n", encoding="utf-8")
    _git(work, "add", "src/base_three.py")
    (work / "EXPORT_CLASSIFICATION.txt").write_text(
        "ship src/**\nship   2  src/base*.py\ndrop tests/**\n", encoding="utf-8"
    )
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

    assert result != "resolved_pr_conflict"
    kinds = [k for k, _ in events]
    assert "pr_conflict_resolved" not in kinds

    stored = await store.get_task(t.id)
    assert stored.status == TaskStatus.ESCALATED
    assert "count drift" in (stored.blocker.get("evidence") or "")


async def test_an_export_classification_conflict_alone_opens_a_coder_round(store, tmp_path, monkeypatch):
    """A conflict confined to EXPORT_CLASSIFICATION.txt is not mechanically
    resolvable: its counts are hand-maintained and no command rebuilds them,
    so even though it sits in the export gate next to RELEASE_MANIFEST.txt,
    a coder round opens exactly as for a source-file conflict."""
    _use_stub_export_guard(monkeypatch)
    work = _repo(tmp_path)

    wt = tmp_path / "wt_feature"
    _worktree(work, wt, "feature")
    (wt / "EXPORT_CLASSIFICATION.txt").write_text(
        "ship src/**\nship   5  src/base*.py\ndrop tests/**\n", encoding="utf-8"
    )
    _git(wt, "commit", "-qam", "feature reclassifies the counted rule to 5")
    _push_branch(work, wt, "feature")

    (work / "EXPORT_CLASSIFICATION.txt").write_text(
        "ship src/**\nship   7  src/base*.py\ndrop tests/**\n", encoding="utf-8"
    )
    _git(work, "commit", "-qam", "main reclassifies the counted rule to 7")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"EXPORT_CLASSIFICATION.txt"}
    assert not dc.all_derived(paths)

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
    (wt / "EXPORT_CLASSIFICATION.txt").write_text(
        "ship src/**\nship   5  src/base*.py\ndrop tests/**\n", encoding="utf-8"
    )
    _git(wt, "add", "EXPORT_CLASSIFICATION.txt")
    _git(wt, "commit", "-qm", "feature adds on_feature.py, reclassifies to 5")
    _approve(wt, ["src/on_feature.py"])
    _git(wt, "add", "RELEASE_MANIFEST.txt")
    _git(wt, "commit", "-qm", "pin on_feature.py")
    _push_branch(work, wt, "feature")

    (work / "src" / "on_main.py").write_text("on main\n", encoding="utf-8")
    _git(work, "add", "src/on_main.py")
    (work / "EXPORT_CLASSIFICATION.txt").write_text(
        "ship src/**\nship   7  src/base*.py\ndrop tests/**\n", encoding="utf-8"
    )
    _git(work, "add", "EXPORT_CLASSIFICATION.txt")
    _git(work, "commit", "-qm", "main adds on_main.py, reclassifies to 7")
    _approve(work, ["src/on_main.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin on_main.py")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    paths = await dc.conflicting_paths(str(work), "main", "feature")
    assert paths == {"RELEASE_MANIFEST.txt", "EXPORT_CLASSIFICATION.txt"}
    assert not dc.all_derived(paths)

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
