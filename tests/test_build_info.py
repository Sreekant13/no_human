"""The loaded-code snapshot, and the attempt row that carries it.

Motivating incident: task ecfe1789 escalated on a tamper-guard false positive
3h18m after the commit fixing that exact false positive had merged. The guard
on main was right; the process was stale. Nothing on the record could tell the
two apart, so the failure was attributed to the ticket.
"""

import subprocess

import pytest

from no_human.core.build_info import LoadedCode, _detect, staleness_note
from no_human.core.db import Store
from no_human.core.task import Task


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real git checkout with a package dir inside it."""
    root = tmp_path / "repo"
    (root / "src" / "no_human").mkdir(parents=True)
    (root / "src" / "no_human" / "__init__.py").write_text("x = 1\n")
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


# --- provenance ------------------------------------------------------------


def test_clean_checkout_reports_its_sha(repo):
    info = _detect(repo / "src" / "no_human")
    assert info.sha == _git(repo, "rev-parse", "HEAD")
    assert info.dirty is False
    assert info.descriptor == f"git:{info.sha}"


def test_dirty_package_tree_is_marked_dirty(repo):
    """A sha alone would claim fidelity the working tree does not have."""
    (repo / "src" / "no_human" / "__init__.py").write_text("x = 2\n")
    info = _detect(repo / "src" / "no_human")
    assert info.dirty is True
    assert info.descriptor.endswith("+dirty")


def test_edits_outside_the_package_do_not_mark_it_dirty(repo):
    """Repo-wide dirt is not loaded-code dirt — otherwise the flag is noise
    in exactly the actively-developed repo it exists to describe."""
    (repo / "README.md").write_text("unrelated\n")
    assert _detect(repo / "src" / "no_human").dirty is False


def test_no_checkout_reports_unknown_not_a_borrowed_sha(tmp_path, monkeypatch):
    """Installed from a wheel there is no sha. Say so."""
    monkeypatch.setattr("no_human.core.build_info._dist_version", lambda: None)
    outside = tmp_path / "site-packages" / "no_human"
    outside.mkdir(parents=True)
    info = _detect(outside)
    assert info.sha is None
    assert info.descriptor == "unknown"


def test_no_checkout_falls_back_to_the_distribution_version(tmp_path, monkeypatch):
    monkeypatch.setattr("no_human.core.build_info._dist_version", lambda: "1.2.3")
    outside = tmp_path / "site-packages2" / "no_human"
    outside.mkdir(parents=True)
    info = _detect(outside)
    assert info.sha is None
    assert info.descriptor == "dist:1.2.3"
    # `dist:` must never be mistakable for a commit.
    assert not info.descriptor.startswith("git:")


def test_dirty_is_tri_state_not_collapsed_to_clean():
    """None means unknowable. Reporting it as False would present a wheel as
    a verified-clean checkout."""
    assert LoadedCode(dist_version="1.0").dirty is None


# --- the advisory staleness signal ----------------------------------------


def test_staleness_note_fires_when_loaded_sha_is_behind_head(repo):
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("newer\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    note = staleness_note(LoadedCode(sha=old, dirty=False), package_root=repo)
    assert note is not None and old[:8] in note


def test_staleness_note_silent_when_current(repo):
    head = _git(repo, "rev-parse", "HEAD")
    assert staleness_note(LoadedCode(sha=head, dirty=False), package_root=repo) is None


def test_staleness_note_silent_when_provenance_unknown(repo):
    assert staleness_note(LoadedCode(dist_version="1.0"), package_root=repo) is None


def test_staleness_note_silent_for_a_diverged_sha(repo):
    """A detached review checkout is not stale — it is elsewhere. A signal
    that fires on every worktree is one nobody reads."""
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("s\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-")
    assert _git(repo, "rev-parse", "HEAD") == head
    assert staleness_note(LoadedCode(sha=side, dirty=False), package_root=repo) is None


# --- the record ------------------------------------------------------------


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


async def test_attempt_records_the_loaded_code_version(store):
    """The whole point: every attempt carries the sha of the code that
    produced its verdict, so an escalation can be re-judged afterwards."""
    from no_human.core.build_info import loaded_code

    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)

    rows = await store.list_attempts(t.id)
    assert [r["id"] for r in rows] == [attempt_id]
    assert rows[0]["loaded_code_version"] == loaded_code().descriptor
    assert rows[0]["loaded_code_version"]  # never blank — "unknown" at worst


async def test_recorded_version_survives_later_updates(store):
    """update_attempt rewrites the mutable surface; the provenance stamp is
    written at creation and must not be lost by a later write."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)
    before = (await store.list_attempts(t.id))[0]["loaded_code_version"]
    await store.update_attempt(attempt_id, status="failed",
                               failure_reason="boom")
    assert (await store.list_attempts(t.id))[0]["loaded_code_version"] == before
