"""`nh approve` merges the PR: local squash as the operator identity
(operator directive 2026-08-12, refile of 74bf7dec).

Drives REAL git against REAL temp repos — an origin bare repo plus a clone
that stands in for a task's `repo_path` — with a STUB `scripts/export_guard.py`
and a minimal double of `scripts/build_public_export.py`'s classification
grammar (the real files require the source repo's private term inventory —
`build_public_export.py` loads `src/no_human/eval/vendor_terms.py` at IMPORT
TIME, and `export_guard.py`'s `_terms_or_die` refuses outright without it —
neither of which this fixture repo carries). The double reimplements exactly
`parse_classification`/`classify`/`Rule` (same glob-to-regex translation,
same last-match-wins semantics); the REAL grammar is exercised against the
REAL file by tests/test_export_manifest.py, so this is a documented, narrow
double of a heavy dependency, not a second copy of a gate. A `gh` stub on
PATH records every argv it was called with.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs.approve_merge import land_task
from no_human.vcs.git import GitRepo, ProtectedBranch
from no_human.vcs.pr_watcher import default_branch_shipped

pytestmark = pytest.mark.usefixtures("isolated_env_file")

# --------------------------------------------------------------------------- #
# git / fixture plumbing                                                      #
# --------------------------------------------------------------------------- #


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )


_CLASSIFICATION = """\
ship 2 src/*.py
ship 2 scripts/*.py
ship 1 EXPORT_CLASSIFICATION.txt
ship 1 RELEASE_MANIFEST.txt
drop 1 README.md
drop 1 tests/*.py
"""

# A minimal double of scripts/build_public_export.py's classification
# grammar — see the module docstring for why the real file cannot be used
# unmodified here. `parse_classification`/`classify`/`Rule` are a verbatim
# port of the real ones (same glob-to-regex translation, same last-match-wins
# semantics); everything else (the term scanner, the build, the manifest
# writer) is intentionally absent — nothing here calls it.
_MINI_BUILD_PUBLIC_EXPORT = '''\
import re
from dataclasses import dataclass, field

CLASSIFICATION_NAME = "EXPORT_CLASSIFICATION.txt"
RELEASE_MANIFEST_NAME = "RELEASE_MANIFEST.txt"


class ExportError(RuntimeError):
    pass


def _entry_to_regex(entry):
    is_dir = entry.endswith("/")
    body = entry.rstrip("/")
    out = []
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif body.startswith("**", i):
            out.append(r".*")
            i += 2
        elif body[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif body[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    return re.compile("".join(out) + (r"/.*" if is_dir else "") + r"\\Z")


@dataclass
class Rule:
    verb: str
    declared: int
    pattern: str
    lineno: int

    def matcher(self):
        return _entry_to_regex(self.pattern)


def parse_classification(text):
    rules = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) != 3 or parts[0] not in ("ship", "drop") or not parts[1].isdigit():
            raise ExportError(f"{CLASSIFICATION_NAME}:{lineno}: cannot parse {line!r}")
        rules.append(Rule(parts[0], int(parts[1]), parts[2], lineno))
    return rules


@dataclass
class Classification:
    wins: dict
    shipped: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    unclassified: list = field(default_factory=list)


def classify(rules, paths):
    matchers = [(r, r.matcher()) for r in rules]
    out = Classification(wins={r.lineno: 0 for r in rules})
    for path in paths:
        winner = None
        for rule, matcher in matchers:
            if matcher.match(path):
                winner = rule
        if winner is None:
            out.unclassified.append(path)
            continue
        out.wins[winner.lineno] += 1
        (out.shipped if winner.verb == "ship" else out.dropped).append(path)
    return out
'''

# A stub of scripts/export_guard.py: same CLI surface (approve <paths...>,
# verify) and the same exit-code contract (0/1/2 for approve, 0/1 for
# verify), over the mini classification grammar above — but with no term
# scan (the real `_terms_or_die` refuses without the source repo's private
# term inventory). `REFUSE_SCAN` / `REFUSE_BEFORE_WRITE` / `FORCE_VERIFY_FAIL`
# marker files (committed by a test's branch) drive the failure paths
# deterministically.
_EXPORT_GUARD_STUB = '''\
"""Test stub of scripts/export_guard.py — see tests/test_approve_merge.py."""
import argparse
import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MANIFEST = "RELEASE_MANIFEST.txt"


def _builder():
    spec = importlib.util.spec_from_file_location(
        "_stub_build_public_export", HERE / "scripts" / "build_public_export.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _pins(root):
    path = root / MANIFEST
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        out[rel] = digest
    return out


def _write_pins(root, pins):
    rows = "".join(f"{d}  {p}\\n" for p, d in sorted(pins.items()))
    (root / MANIFEST).write_text("# RELEASE_MANIFEST.txt\\n" + rows, encoding="utf-8")


def _shipped(root):
    b = _builder()
    rules = b.parse_classification(
        (root / b.CLASSIFICATION_NAME).read_text(encoding="utf-8"))
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                         capture_output=True, text=True).stdout
    tracked = [p for p in out.split("\\0") if p]
    cls = b.classify(rules, tracked)
    return set(cls.shipped)


def cmd_approve(args, root):
    if (root / "REFUSE_SCAN").exists():
        print("approve: REFUSED (stub) - 1 scan hit(s)", file=sys.stderr)
        return 1
    if (root / "REFUSE_BEFORE_WRITE").exists():
        print("approve: REFUSED (stub) - the advisory scan cannot run", file=sys.stderr)
        return 2
    shipped = _shipped(root)
    pins = _pins(root)
    targets = list(dict.fromkeys(args.paths)) if args.paths else \\
        sorted(r for r in shipped if r != MANIFEST)
    bad = [t for t in targets if t not in shipped and t != MANIFEST]
    if bad:
        print("approve: REFUSED (stub) - not ship-classified:\\n  "
              + "\\n  ".join(bad), file=sys.stderr)
        return 2
    for rel in targets:
        if rel == MANIFEST:
            continue
        pins[rel] = _sha(root / rel)
        print(f"approved  {pins[rel][:12]}  {rel} (stub)")
    _write_pins(root, pins)
    print(f"{MANIFEST}: {len(pins)} pinned file(s) total")
    return 0


def cmd_verify(args, root):
    if (root / "FORCE_VERIFY_FAIL").exists():
        print("verify: FAILED (stub forced)", file=sys.stderr)
        return 1
    pins = _pins(root)
    if not pins:
        print("verify: FAILED (stub) - no manifest", file=sys.stderr)
        return 1
    shipped = _shipped(root)
    for rel in shipped:
        if rel == MANIFEST:
            continue
        if rel not in pins:
            print(f"verify: FAILED (stub) - {rel} unpinned", file=sys.stderr)
            return 1
        if pins[rel] != _sha(root / rel):
            print(f"verify: FAILED (stub) - {rel} hash mismatch", file=sys.stderr)
            return 1
    print(f"verify: OK (stub) - {len(shipped)} shipped file(s)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_a = sub.add_parser("approve")
    ap_a.add_argument("paths", nargs="*")
    ap_a.add_argument("--all", action="store_true")
    ap_a.add_argument("--prune", action="store_true")
    ap_a.add_argument("--acknowledge", action="store_true")
    sub.add_parser("verify")
    args = ap.parse_args(argv)
    root = Path.cwd()
    if args.cmd == "approve":
        return cmd_approve(args, root)
    return cmd_verify(args, root)


if __name__ == "__main__":
    sys.exit(main())
'''

# A `gh` stub: records every argv (one JSON array per line) to
# $GH_STUB_LOG, and answers `pr view --json state` / `pr close` from a
# one-line state file at $GH_STUB_STATE_FILE ("OPEN" by default).
_GH_STUB = '''\
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
log = Path(os.environ["GH_STUB_LOG"])
with log.open("a") as f:
    f.write(json.dumps(argv) + "\\n")

state_file = Path(os.environ["GH_STUB_STATE_FILE"])

if argv[:2] == ["pr", "view"]:
    state = state_file.read_text().strip() if state_file.exists() else "OPEN"
    print(json.dumps({"state": state}))
    sys.exit(0)
if argv[:2] == ["pr", "close"]:
    state_file.write_text("CLOSED")
    sys.exit(0)
sys.exit(0)
'''


class LandEnv:
    def __init__(self, tmp_path, origin, clone, config, gh_log, gh_state):
        self.tmp_path = tmp_path
        self.origin = origin
        self.clone = clone
        self.config = config
        self.gh_log = gh_log
        self.gh_state = gh_state
        self.pr_url = "https://github.com/acme/widget/pull/42"

    def tip_sha(self) -> str:
        return _git(self.origin, "rev-parse", "main").stdout.strip()

    def remote_main_sha(self) -> str:
        return _git(self.origin, "rev-parse", "main").stdout.strip()

    def cut_branch(self, name: str, *, extra_files: dict[str, str] | None = None,
                   corrupt_manifest: bool = True) -> tuple[str, str]:
        """Branch `name` off the CURRENT origin/main tip, adding
        `src/feature.py` (a new ship-classified file) and bumping the
        classification's win-count for it — the shape a real coder attempt
        produces. `corrupt_manifest` wipes RELEASE_MANIFEST.txt on the
        branch (proving land re-derives it from the tip, never carries the
        branch's own copy forward)."""
        _git(self.clone, "fetch", "-q", "origin", "main")
        _git(self.clone, "checkout", "-q", "-B", name, "origin/main")
        (self.clone / "src" / "feature.py").write_text("def feature():\n    return 3\n")
        cls_path = self.clone / "EXPORT_CLASSIFICATION.txt"
        cls_path.write_text(cls_path.read_text().replace(
            "ship 2 src/*.py", "ship 3 src/*.py"))
        if corrupt_manifest:
            (self.clone / "RELEASE_MANIFEST.txt").write_text("# RELEASE_MANIFEST.txt\n")
        for rel, content in (extra_files or {}).items():
            p = self.clone / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", f"feature: add feature.py ({name})")
        _git(self.clone, "push", "-q", "-u", "origin", name)
        head_sha = _git(self.clone, "rev-parse", "HEAD").stdout.strip()
        return name, head_sha

    def advance_origin(self, note: str) -> str:
        """Push a trivial commit onto origin/main from a THIRD clone —
        simulates a concurrent human/other-task push landing on main."""
        race = self.tmp_path / f"race-{note}"
        _git(self.tmp_path, "clone", "-q", str(self.origin), str(race))
        (race / "RACE.md").write_text(note)
        _git(race, "add", "RACE.md")
        _git(race, "commit", "-qm", f"race: {note}")
        _git(race, "push", "-q", "origin", "HEAD:main")
        return _git(race, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def land_env(tmp_path, monkeypatch) -> LandEnv:
    # Hermetic under `-n 4`: none of `land_task`'s many `git`/`gh` subprocess
    # calls pass an explicit `env=`, so they inherit whatever this process's
    # ambient environment is. `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`/
    # `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` are process-
    # global and other test modules in this repo (test_guard.py,
    # test_repro_gate.py, test_repo_discovery.py,
    # test_doctor_editable_install.py) mutate them; a real `~/.gitconfig`
    # with a credential helper or an interactive prompt would also reach
    # every `git`/`gh` call below. Scrub and redirect so this fixture's git
    # never depends on the worker's ambient state or on test load order.
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    global_gitconfig = tmp_path / "gitconfig"
    # `land_task`'s squash step (`git merge --squash`) needs SOME resolvable
    # identity for its internal virtual-merge machinery even though the
    # squash itself makes no commit — the real commit later overrides this
    # via `-c user.name=/-c user.email=` (operator identity, unaffected).
    # Deleting the GIT_AUTHOR_*/GIT_COMMITTER_* env vars above and pointing
    # HOME at an empty tmp_path leaves git with no identity to resolve at
    # all, so this redirected global config supplies an innocuous one.
    global_gitconfig.write_text(
        "[user]\n\tname = Hermetic Test\n\temail = hermetic-test@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))

    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "src").mkdir()
    (seed / "scripts").mkdir()
    (seed / "tests").mkdir()
    (seed / "README.md").write_text("hello\n")
    (seed / "src" / "app.py").write_text("def app():\n    return 1\n")
    (seed / "src" / "lib.py").write_text("def lib():\n    return 2\n")
    (seed / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n")
    (seed / "scripts" / "build_public_export.py").write_text(_MINI_BUILD_PUBLIC_EXPORT)
    (seed / "scripts" / "export_guard.py").write_text(_EXPORT_GUARD_STUB)
    (seed / "EXPORT_CLASSIFICATION.txt").write_text(_CLASSIFICATION)
    # `git ls-files` (what the guard classifies from) only sees the INDEX, so
    # everything must be staged before `approve` runs.
    _git(seed, "add", "-A")
    subprocess.run(
        [sys.executable, "scripts/export_guard.py", "approve", "--all"],
        cwd=seed, check=True, capture_output=True, text=True,
    )
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))

    # `gh` stub on PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_path = bin_dir / "gh"
    gh_path.write_text(_GH_STUB)
    gh_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    gh_log = tmp_path / "gh_log.jsonl"
    gh_log.write_text("")
    monkeypatch.setenv("GH_STUB_LOG", str(gh_log))
    gh_state = tmp_path / "gh_state.txt"
    gh_state.write_text("OPEN")
    monkeypatch.setenv("GH_STUB_STATE_FILE", str(gh_state))

    config = {
        "git": {
            "agent_identity_name": "no_human",
            "agent_identity_email": "no-human@acme.com",
            "never_push_to": ["main", "master", "release/*"],
            "approve_identity": {
                "name": "eyalgolan",
                "email": "5146175+eyalgolan@users.noreply.github.com",
            },
        },
        "approve_merge": {"enabled": True, "test_timeout_seconds": 120},
    }
    return LandEnv(tmp_path, origin, clone, config, gh_log, gh_state)


# --------------------------------------------------------------------------- #
# land_task — unit level                                                      #
# --------------------------------------------------------------------------- #

def test_lands_on_remote_tracking_tip_not_stale_local_branch(land_env):
    """Regression pin, deterministic: `land_task` resolves the land base from
    the REMOTE-TRACKING ref (`origin/main`), never a same-named LOCAL branch
    — exactly the shape a task's `repo_path` clone carries from clone time
    (a checked-out `main` that a plain `git fetch` never fast-forwards). The
    harness fetches here, not `land_task` — so this pins the actual
    base-selection behavior with zero dependence on `land_task`'s own
    best-effort `repo.fetch()` succeeding under load."""
    branch, head_sha = land_env.cut_branch("no-human/t-tip-remote")
    new_tip = land_env.advance_origin("advance-before-land")
    _git(land_env.clone, "fetch", "-q", "origin", "main")
    local_main = _git(land_env.clone, "rev-parse", "main").stdout.strip()
    assert local_main != new_tip, (
        "premise: the clone's local `main` must still be stale after a "
        "plain fetch, or this test proves nothing")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature",
        review_evidence="review PASS on abc123 after 1 round(s)",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    parent = _git(land_env.origin, "rev-parse", f"{result.landed_sha}^1").stdout.strip()
    assert parent == new_tip


def test_lands_on_a_tip_pushed_after_the_branch(land_env):
    """Keeps the ORIGINAL test's shape — no harness pre-fetch, so
    `land_task`'s own internal `repo.fetch()` call (step 2, at the very
    start) stays covered. This is the test that flaked under `-n 4` in
    train28 (as `test_lands_on_current_tip_not_stale_base`): `GitRepo.fetch`
    is best-effort and silently swallows `TimeoutExpired`/`OSError`
    (vcs/git.py:419-425 — out of scope to make fatal, that is a documented
    product behavior), so under fd/process pressure the fetch can silently
    no-op, leaving the clone's `origin/main` stale — `land_task` then
    correctly (by its own contract) bases the land on the pre-advance tip.
    The original test read that as "wrong parent", indistinguishable from an
    actual base-selection bug.

    This version checks the PREMISE first and loudly, using the
    `_before_push` test seam (already used by
    `test_aborts_when_tip_moved_during_land`) to sample `origin/main`
    immediately after step 2's fetch but before step 7's pre-push
    RE-fetch/re-check — the `git push` at the end of a successful land
    opportunistically advances the local `origin/main` tracking ref to the
    just-landed sha, so sampling it AFTER `land_task` returns (rather than
    via this seam) would always show forward movement and prove nothing
    about step 2's fetch specifically."""
    branch, head_sha = land_env.cut_branch("no-human/t-tip-ownfetch")
    new_tip = land_env.advance_origin("advance-before-land")
    sampled: dict[str, str] = {}

    def _sample_pre_push():
        sampled["origin_main"] = _git(
            land_env.clone, "rev-parse", "origin/main").stdout.strip()

    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature",
        review_evidence="review PASS on abc123 after 1 round(s)",
        config=land_env.config, _before_push=_sample_pre_push,
    )
    assert result.ok, result.stderr
    observed = sampled.get("origin_main")
    if observed != new_tip:
        diag = _git(land_env.clone, "fetch", "origin", "main", check=False)
        pytest.fail(
            "premise failed: land_task's own step-2 repo.fetch() did not "
            f"observe the advanced tip (origin/main was {observed} "
            f"immediately before the pre-push re-check, expected {new_tip}); "
            f"a diagnostic re-fetch here returned rc={diag.returncode} "
            f"stderr={diag.stderr.strip()!r} — see GitRepo.fetch's "
            "best-effort TimeoutExpired/OSError swallow (vcs/git.py)")
    parent = _git(land_env.origin, "rev-parse", f"{result.landed_sha}^1").stdout.strip()
    assert parent == new_tip


def test_squash_produces_single_commit(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-squash")
    tip = land_env.tip_sha()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    count = _git(land_env.origin, "rev-list", "--count",
                 f"{tip}..{result.landed_sha}").stdout.strip()
    assert count == "1"


def test_manifest_reset_classification_preserved(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-manifest")
    branch_classification = (land_env.clone / "EXPORT_CLASSIFICATION.txt").read_text()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    sha = result.landed_sha
    names = _git(land_env.origin, "show", "--name-only", "--format=", sha).stdout.split()
    assert "RELEASE_MANIFEST.txt" in names
    assert "src/feature.py" in names
    manifest = _git(land_env.origin, "show", f"{sha}:RELEASE_MANIFEST.txt").stdout
    assert "src/feature.py" in manifest
    classification = _git(land_env.origin, "show", f"{sha}:EXPORT_CLASSIFICATION.txt").stdout
    assert classification == branch_classification


def test_commit_uses_operator_identity(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-ident")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    out = _git(land_env.origin, "show", "-s", "--format=%an%x09%ae",
               result.landed_sha).stdout.strip()
    name, _, email = out.partition("\t")
    assert name == "eyalgolan"
    assert email == "5146175+eyalgolan@users.noreply.github.com"


def test_commit_message_shape(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-msg")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="task-deadbeef", task_title="Add the feature everyone wants",
        review_evidence="review PASS on abc123 after 2 round(s)",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    msg = _git(land_env.origin, "show", "-s", "--format=%B", result.landed_sha).stdout
    assert "Add the feature everyone wants" in msg
    assert "task-deadbeef" in msg
    assert "review PASS on abc123 after 2 round(s)" in msg


def test_commit_message_scrubs_flagged_vendor_term_from_title(land_env):
    """A task title (or review-evidence line) carrying a flagged vendor term
    — task titles have carried one before, the PR #334/#339 incident — must
    never land verbatim in a commit message pushed to the forge's default
    branch. Both go through the same outbound scrub `nh bench publish` uses
    before free text reaches a tracked, published artifact."""
    from no_human.eval.vendor_terms import BANNED_TERMS, find_banned_terms

    term = BANNED_TERMS[0]
    branch, head_sha = land_env.cut_branch("no-human/t-vendor-term")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="task-deadbeef", task_title=f"Fix the {term} integration bug",
        review_evidence=f"review PASS, compared favorably to {term}",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    msg = _git(land_env.origin, "show", "-s", "--format=%B", result.landed_sha).stdout
    assert not find_banned_terms(msg), msg
    assert term not in msg.lower()
    assert "<redacted>" in msg


def test_commit_message_leaves_ordinary_title_and_evidence_unchanged(land_env):
    """Control: text carrying no flagged term must pass through the scrub
    byte-for-byte, so the fix does not corrupt an ordinary commit message."""
    branch, head_sha = land_env.cut_branch("no-human/t-vendor-term-control")
    title = "Add the feature everyone wants"
    evidence = "review PASS on abc123 after 2 round(s)"
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="task-deadbeef", task_title=title, review_evidence=evidence,
        config=land_env.config,
    )
    assert result.ok, result.stderr
    msg = _git(land_env.origin, "show", "-s", "--format=%B", result.landed_sha).stdout
    assert title in msg
    assert evidence in msg


def test_aborts_when_export_guard_verify_fails(land_env):
    branch, head_sha = land_env.cut_branch(
        "no-human/t-verifyfail", extra_files={"FORCE_VERIFY_FAIL": "x"})
    before = land_env.remote_main_sha()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok
    assert result.step == "verify"
    assert result.stderr
    assert land_env.remote_main_sha() == before


def test_change_scoped_tests_run_against_worktree(land_env, tmp_path, monkeypatch):
    marker = tmp_path / "test_marker.txt"
    monkeypatch.setenv("TEST_MARKER_FILE", str(marker))
    marker_test = (
        "import os, pathlib\n"
        "def test_records_env():\n"
        "    p = pathlib.Path(os.environ['TEST_MARKER_FILE'])\n"
        "    p.write_text(os.getcwd() + '\\n' + os.environ.get('PYTHONPATH', ''))\n"
    )
    branch, head_sha = land_env.cut_branch(
        "no-human/t-scopedtests", extra_files={"tests/test_feature.py": marker_test})
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    assert marker.exists(), "the change-scoped test never ran"
    recorded_cwd, _, recorded_pythonpath = marker.read_text().partition("\n")
    assert recorded_cwd != str(land_env.clone), \
        "must run in the temp WORKTREE, not the task's own repo checkout"
    assert recorded_pythonpath.endswith(f"{os.sep}src")


def test_push_advances_remote_ref(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-push")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    assert land_env.remote_main_sha() == result.landed_sha


def test_aborts_when_tip_moved_during_land(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-race")
    before = land_env.remote_main_sha()
    raced: dict[str, str] = {}

    def _race():
        raced["sha"] = land_env.advance_origin("mid-land-race")

    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config, _before_push=_race,
    )
    assert not result.ok
    assert result.step == "push"
    assert raced.get("sha")
    assert land_env.remote_main_sha() == raced["sha"]
    assert land_env.remote_main_sha() != before


def test_closes_pr_without_comment(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-close")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    argvs = [json.loads(l) for l in land_env.gh_log.read_text().splitlines() if l.strip()]
    close_calls = [a for a in argvs if a[:2] == ["pr", "close"]]
    assert close_calls, f"no `pr close` call recorded: {argvs}"
    for a in argvs:
        assert "--comment" not in a
        assert a[:2] != ["pr", "comment"]


def test_already_closed_pr_is_not_a_failure(land_env):
    land_env.gh_state.write_text("MERGED")
    branch, head_sha = land_env.cut_branch("no-human/t-alreadyclosed")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    argvs = [json.loads(l) for l in land_env.gh_log.read_text().splitlines() if l.strip()]
    assert not any(a[:2] == ["pr", "close"] for a in argvs)


def test_failure_removes_temp_worktree_and_keeps_awaiting_approval(land_env):
    branch, head_sha = land_env.cut_branch(
        "no-human/t-abort", extra_files={"REFUSE_BEFORE_WRITE": "x"})
    repo = GitRepo(land_env.clone, never_push_to=["main", "master", "release/*"])
    before_worktrees = repo.list_worktrees()
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert not result.ok
    assert result.step == "manifest"
    assert result.stderr
    after_worktrees = repo.list_worktrees()
    assert after_worktrees == before_worktrees
    assert len(after_worktrees) == 1


def test_disabled_config_records_approval_only(land_env):
    branch, head_sha = land_env.cut_branch("no-human/t-disabled")
    before = land_env.remote_main_sha()
    cfg = json.loads(json.dumps(land_env.config))
    cfg["approve_merge"]["enabled"] = False
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=cfg,
    )
    assert result.ok
    assert result.skipped
    assert land_env.remote_main_sha() == before


def test_never_push_to_still_blocks_agent_pushes(land_env):
    repo = GitRepo(land_env.clone, never_push_to=["main", "master", "release/*"])
    with pytest.raises(ProtectedBranch):
        repo.push("main")


# --------------------------------------------------------------------------- #
# landed-completion — validates (never rebuilds) blockers/shipped.py's       #
# containment probe: this is the coupling that completes a closed-PR task.  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_landed_squash_reads_as_shipped_to_the_containment_probe(land_env):
    """`blockers/shipped.py::complete_if_content_landed` calls
    `pr_watcher.default_branch_shipped`, which asks — by TREE CONTENT, not
    commit ancestry — whether a branch still has anything left to contribute
    against the tip. `land_task` produces exactly one squash commit whose
    tree differs from the branch's only in `RELEASE_MANIFEST.txt`
    (`_GENERATED_LEDGERS`), so the probe must read the branch as shipped
    once `land_task` has landed it — this is the mechanism a closed-PR task
    (b53b9e13, 424f14a2) relies on to actually complete."""
    branch, head_sha = land_env.cut_branch("no-human/t-shipped-probe")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr
    shipped = await default_branch_shipped(str(land_env.origin), branch, base="main")
    assert shipped, (
        "default_branch_shipped must read the approve-merge squash as "
        "containing the branch's content (ledger-only diff excluded) — "
        "this is the coupling that completes a closed-PR task")


# --------------------------------------------------------------------------- #
# CLI: `nh approve`                                                           #
# --------------------------------------------------------------------------- #

def _make_cli_runner(db_path, config_data, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"

        def __init__(self, data, db_path):
            self.data = data
            self._db_path = db_path

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

        @property
        def db_path(self):
            return self._db_path

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg(config_data, db_path))
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(cmd_mod, "_running_pool_width", lambda _cfg: None)
    return CliRunner()


def _seed_land_task(db_path, status, *, repo_path, branch=None, pr_url=None,
                    review_history=None, title="Fix the thing",
                    task_id=None) -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path=repo_path)
            if task_id is not None:
                t.id = task_id
            t.acceptance_criteria = ["Should work"]
            await s.create_task(t)
            ctx = {}
            if pr_url:
                ctx["pr_watch"] = pr_url
            if branch:
                ctx["pr_branch"] = branch
            if review_history is not None:
                ctx["review_history"] = review_history
            if ctx:
                await s.merge_context(t.id, ctx)
            if status is not TaskStatus.PENDING:
                await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def _fetch_task_and_events(db_path, task_id):
    async def _go():
        async with Store(db_path) as s:
            t = await s.find_task(task_id)
            events = await s.list_events(task_id)
            return t, events
    return asyncio.run(_go())


def test_refuses_when_not_awaiting_approval(land_env, tmp_path, monkeypatch):
    branch, head_sha = land_env.cut_branch("no-human/t-notawaiting")
    before = land_env.remote_main_sha()
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.IMPLEMENTING, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
    )
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code != 0
    assert land_env.remote_main_sha() == before


def test_refuses_when_no_review_pass_for_head_sha(land_env, tmp_path, monkeypatch):
    branch, head_sha = land_env.cut_branch("no-human/t-noreview")
    before = land_env.remote_main_sha()
    db = tmp_path / "t.db"
    off_branch_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": off_branch_sha, "passed": True}],
    )
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code != 0
    assert "preconditions" in result.output.lower(), result.output
    assert land_env.remote_main_sha() == before


def test_cli_approve_marks_done_with_landed_sha(land_env, tmp_path, monkeypatch):
    branch, head_sha = land_env.cut_branch("no-human/t-clidone")
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
    )
    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])
    assert result.exit_code == 0, result.output

    t, events = _fetch_task_and_events(db, task_id)
    assert t.status is TaskStatus.DONE
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert len(merged) == 1
    sha = merged[0]["sha"]
    assert sha
    assert sha[:12] in result.output


# --------------------------------------------------------------------------- #
# API: POST /api/tasks/{id}/approve                                           #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def api_store_client(tmp_path):
    from no_human.config import load_config

    # `app.state` is the module-global FastAPI singleton, shared with ~every
    # other test module that drives the API — save/restore instead of
    # clobbering, so nothing here leaks into a worker-mate under `-n 4`.
    had_store = hasattr(app.state, "store")
    prior_store = getattr(app.state, "store", None)
    had_config = hasattr(app.state, "config")
    prior_config = getattr(app.state, "config", None)

    store = await Store(tmp_path / "api.db").connect()
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, store
    finally:
        await store.close()
        if had_store:
            app.state.store = prior_store
        else:
            del app.state.store
        if had_config:
            app.state.config = prior_config
        else:
            del app.state.config


@pytest.mark.asyncio
async def test_api_approve_marks_done_with_landed_sha(land_env, api_store_client):
    client, store = api_store_client
    branch, head_sha = land_env.cut_branch("no-human/t-apidone")

    t = Task.new("Fix the thing", repo_path=str(land_env.clone))
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.merge_context(t.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch,
        "review_history": [{"sha": head_sha, "passed": True}],
    })
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["landed_sha"]

    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert len(merged) == 1
    assert merged[0]["sha"] == data["landed_sha"]


@pytest.mark.asyncio
async def test_api_approve_surfaces_land_failure(land_env, api_store_client):
    """A genuine `land_task` failure (export_guard verify fails here) must
    come back as an HTTPException the caller can see — not a silent 200
    wearing the record-only "merge it yourself" message, which would read
    a real failure as success. The task stays awaiting_approval either way
    (plan §8's clean-abort contract, mirrored by the CLI's own exit(1))."""
    client, store = api_store_client
    branch, head_sha = land_env.cut_branch(
        "no-human/t-apiverifyfail", extra_files={"FORCE_VERIFY_FAIL": "x"})
    before = land_env.remote_main_sha()

    t = Task.new("Fix the thing", repo_path=str(land_env.clone))
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.merge_context(t.id, {
        "pr_watch": land_env.pr_url, "pr_branch": branch,
        "review_history": [{"sha": head_sha, "passed": True}],
    })
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["step"] == "verify"
    assert detail["stderr"]

    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL
    events = await store.list_events(t.id)
    assert not [e for e in events if e.get("kind") == "human_merged"]
    assert land_env.remote_main_sha() == before
