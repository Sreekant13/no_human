"""Repository auto-discovery — scan the conventional clone roots so onboarding
and the composer can offer a list instead of demanding a typed path.

Every test builds a fake HOME on tmp_path and points the discovery at it, so
nothing here reads the operator's real machine.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from no_human import repo_discovery
from no_human.repo_discovery import (
    CONVENTIONAL_ROOTS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_RESULTS,
    discover_repos,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def _real_repo(path: Path, *, dirty: bool = False, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    (path / "README.md").write_text("hello\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    if dirty:
        (path / "README.md").write_text("edited\n")
    return path


def _fake_repo(path: Path) -> Path:
    """A .git directory with a HEAD but no objects — enough for the cheap scan,
    and what most of these tests need (they assert on shape, not git state).
    HEAD is real so the dirty probe still runs, and the timing test therefore
    still pays the subprocess cost it is measuring."""
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def _by_name(result) -> dict[str, dict]:
    return {r["name"]: r for r in result["repos"]}


# --------------------------------------------------------------------------- #
# Roots                                                                        #
# --------------------------------------------------------------------------- #

def test_conventional_roots_cover_the_standard_clone_locations():
    assert CONVENTIONAL_ROOTS == (
        "Projects", "Code", "Development", "Dev", "repos", "git", "workspace", "src",
    )


def test_discovers_across_every_conventional_root(tmp_path):
    for i, root in enumerate(CONVENTIONAL_ROOTS):
        _fake_repo(tmp_path / root / f"proj-{i}")
    res = discover_repos(home=tmp_path)
    names = set(_by_name(res))
    assert names == {f"proj-{i}" for i in range(len(CONVENTIONAL_ROOTS))}
    # The roots that actually existed are reported, so the UI can say where it looked.
    assert set(res["roots_scanned"]) == {str(tmp_path / r) for r in CONVENTIONAL_ROOTS}
    assert res["roots_missing"] == []


def test_missing_roots_are_reported_not_errors(tmp_path):
    (tmp_path / "git").mkdir()
    res = discover_repos(home=tmp_path)
    assert res["roots_scanned"] == [str(tmp_path / "git")]
    assert len(res["roots_missing"]) == len(CONVENTIONAL_ROOTS) - 1


def test_operator_configured_extra_roots_are_scanned(tmp_path):
    _fake_repo(tmp_path / "work" / "acme-api")
    res = discover_repos(home=tmp_path, extra_roots=["~/work"])
    assert "acme-api" in _by_name(res)


def test_extra_roots_outside_home_are_refused(tmp_path):
    outside = tmp_path.parent / "outside-home"
    _fake_repo(outside / "secret-repo")
    home = tmp_path / "home"
    home.mkdir()
    res = discover_repos(home=home, extra_roots=[str(outside)])
    assert res["repos"] == []
    assert str(outside) in res["roots_refused"]


def test_a_symlink_escaping_home_is_not_followed(tmp_path):
    home = tmp_path / "home"
    (home / "git").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    _fake_repo(outside / "secret-repo")
    (home / "git" / "link").symlink_to(outside)
    res = discover_repos(home=home)
    assert "secret-repo" not in _by_name(res)


# --------------------------------------------------------------------------- #
# What comes back per repo                                                     #
# --------------------------------------------------------------------------- #

def test_reports_path_name_git_branch_and_dirty(tmp_path):
    root = tmp_path / "git"
    clean = _real_repo(root / "clean-svc", branch="main")
    dirty = _real_repo(root / "dirty-svc", dirty=True, branch="feat/x")

    res = discover_repos(home=tmp_path)
    got = _by_name(res)

    assert got["clean-svc"]["path"] == str(clean)
    assert got["clean-svc"]["is_git"] is True
    assert got["clean-svc"]["branch"] == "main"
    assert got["clean-svc"]["dirty"] is False

    assert got["dirty-svc"]["path"] == str(dirty)
    assert got["dirty-svc"]["branch"] == "feat/x"
    assert got["dirty-svc"]["dirty"] is True


def test_untracked_file_counts_as_dirty(tmp_path):
    repo = _real_repo(tmp_path / "git" / "svc")
    (repo / "scratch.txt").write_text("wip\n")
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty"] is True
    assert got["dirty_scan"] == "complete"


def test_a_clean_repo_reports_a_complete_scan(tmp_path):
    _real_repo(tmp_path / "git" / "svc")
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty"] is False
    assert got["dirty_scan"] == "complete"


def test_a_tracked_edit_is_reported_without_waiting_on_the_untracked_scan(tmp_path, monkeypatch):
    """The untracked scan is the expensive half. A repo whose TRACKED files are
    already modified is dirty regardless, so that scan must not run at all."""
    repo = _real_repo(tmp_path / "git" / "svc", dirty=True)
    calls: list[tuple[str, ...]] = []
    real = repo_discovery._git_status

    def spy(path, untracked, timeout):
        calls.append(untracked)
        return real(path, untracked, timeout)

    monkeypatch.setattr(repo_discovery, "_git_status", spy)
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty"] is True
    assert calls == ["no"], "the untracked scan must be skipped once tracked edits are found"


def test_a_slow_untracked_scan_degrades_to_a_partial_answer_not_a_hang(tmp_path, monkeypatch):
    """A huge working tree can take seconds to scan for untracked files. The
    picker must still come back: report what the cheap probe proved and mark
    the answer partial rather than blocking the list on it."""
    _real_repo(tmp_path / "git" / "svc")
    real = repo_discovery._git_status

    def slow_untracked(path, untracked, timeout):
        if untracked == "no":
            return real(path, untracked, timeout)
        return None  # what a timeout looks like to the caller

    monkeypatch.setattr(repo_discovery, "_git_status", slow_untracked)
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty"] is False
    assert got["dirty_scan"] == "partial"


def test_the_untracked_scan_has_a_tighter_timeout_than_the_tracked_one(tmp_path):
    assert repo_discovery.UNTRACKED_TIMEOUT_S < repo_discovery.GIT_TIMEOUT_S


def test_detached_head_reports_the_short_sha_not_a_branch(tmp_path):
    repo = _real_repo(tmp_path / "git" / "svc")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-q", "--detach", sha)
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["branch"] == sha[:8]
    assert got["detached"] is True


def test_non_git_project_directory_is_offered_with_is_git_false(tmp_path):
    proj = tmp_path / "Projects" / "sketch"
    proj.mkdir(parents=True)
    (proj / "package.json").write_text("{}")
    got = _by_name(discover_repos(home=tmp_path))["sketch"]
    assert got["is_git"] is False
    assert got["branch"] == ""
    assert got["dirty"] is False


def test_a_plain_directory_with_no_repo_and_no_manifest_is_not_offered(tmp_path):
    (tmp_path / "git" / "just-notes").mkdir(parents=True)
    assert discover_repos(home=tmp_path)["repos"] == []


def test_ecosystem_is_carried_through_for_the_existing_repo_list_ui(tmp_path):
    node = _fake_repo(tmp_path / "git" / "web-app")
    (node / "package.json").write_text("{}")
    py = _fake_repo(tmp_path / "git" / "svc")
    (py / "pyproject.toml").write_text("[project]\n")
    got = _by_name(discover_repos(home=tmp_path))
    assert got["web-app"]["ecosystem"] == "node"
    assert got["svc"]["ecosystem"] == "python"


# --------------------------------------------------------------------------- #
# Bounds: depth, exclusions, caps                                              #
# --------------------------------------------------------------------------- #

def test_default_depth_is_three_and_reaches_host_owner_repo_layouts(tmp_path):
    # The layout this machine actually uses: ~/git/<host>/<branchdir>/<repo>.
    assert DEFAULT_MAX_DEPTH == 3
    _fake_repo(tmp_path / "git" / "example-host" / "master" / "deep-svc")
    assert "deep-svc" in _by_name(discover_repos(home=tmp_path))


def test_repos_below_the_depth_limit_are_not_returned(tmp_path):
    _fake_repo(tmp_path / "git" / "a" / "b" / "c" / "too-deep")
    assert discover_repos(home=tmp_path)["repos"] == []


def test_a_repo_is_a_leaf_nested_repos_are_not_descended_into(tmp_path):
    outer = _fake_repo(tmp_path / "git" / "outer")
    _fake_repo(outer / "inner")
    got = _by_name(discover_repos(home=tmp_path))
    assert set(got) == {"outer"}


@pytest.mark.parametrize("junk", ["node_modules", ".venv", "vendor", ".hidden", "venv"])
def test_excluded_directories_are_never_descended_into(tmp_path, junk):
    _fake_repo(tmp_path / "git" / junk / "buried")
    assert discover_repos(home=tmp_path)["repos"] == []


def test_results_are_capped_and_the_cap_is_announced_not_silent(tmp_path):
    root = tmp_path / "git"
    for i in range(12):
        _fake_repo(root / f"r{i:02d}")
    res = discover_repos(home=tmp_path, max_results=5)
    assert len(res["repos"]) == 5
    assert res["capped"] is True
    assert res["limit"] == 5
    assert res["note"], "a capped result must carry a human-readable note"
    assert "5" in res["note"]


def test_an_uncapped_result_says_so(tmp_path):
    _fake_repo(tmp_path / "git" / "only")
    res = discover_repos(home=tmp_path)
    assert res["capped"] is False
    assert res["note"] == ""
    assert res["limit"] == DEFAULT_MAX_RESULTS


def test_results_are_sorted_by_name_for_a_stable_list(tmp_path):
    for n in ("zeta", "alpha", "Mid"):
        _fake_repo(tmp_path / "git" / n)
    names = [r["name"] for r in discover_repos(home=tmp_path)["repos"]]
    assert names == ["alpha", "Mid", "zeta"]


def test_elapsed_ms_is_reported_so_a_slow_scan_is_visible(tmp_path):
    _fake_repo(tmp_path / "git" / "only")
    res = discover_repos(home=tmp_path)
    assert isinstance(res["elapsed_ms"], int)
    assert res["elapsed_ms"] >= 0


def test_a_wide_tree_stays_fast_enough_for_a_ui(tmp_path):
    """60 repos across three roots must scan well inside a UI budget."""
    for root in ("git", "Projects", "Code"):
        for i in range(20):
            _fake_repo(tmp_path / root / f"r{i:02d}")
    t0 = time.perf_counter()
    res = discover_repos(home=tmp_path)
    wall = time.perf_counter() - t0
    assert len(res["repos"]) == 60
    assert wall < 3.0, f"discovery took {wall:.2f}s for 60 repos"


def test_the_untracked_pass_stops_at_a_shared_budget(tmp_path, monkeypatch):
    """Twelve slow repositories must not cost twelve timeouts in a row.

    Per-probe timeouts alone let total wall time grow with the number of large
    checkouts, which is exactly the machine where discovery matters most. The
    untracked pass gets ONE budget for the whole scan; repos it does not reach
    come back partial instead of extending the wait.
    """
    for i in range(12):
        _real_repo(tmp_path / "git" / f"r{i:02d}")

    seen: list[str] = []

    def slow(path, untracked, timeout):
        seen.append(untracked)
        if untracked == "no":
            return ""          # tracked files clean
        time.sleep(0.2)
        return None            # the expensive pass never answers

    monkeypatch.setattr(repo_discovery, "_git_status", slow)
    monkeypatch.setattr(repo_discovery, "DIRTY_BUDGET_S", 0.05)

    res = discover_repos(home=tmp_path)
    assert seen.count("no") == 12, "every repo still gets the cheap probe"
    assert seen.count("normal") < 12, "the budget must cut the expensive pass short"
    assert all(r["dirty_scan"] == "partial" for r in res["repos"])


def test_the_budget_does_not_bite_when_the_scan_is_fast(tmp_path):
    for i in range(4):
        _real_repo(tmp_path / "git" / f"r{i}")
    res = discover_repos(home=tmp_path)
    assert [r["dirty_scan"] for r in res["repos"]] == ["complete"] * 4


def test_no_row_leaks_an_internal_scan_state(tmp_path):
    _real_repo(tmp_path / "git" / "svc")
    (tmp_path / "Projects" / "sketch").mkdir(parents=True)
    (tmp_path / "Projects" / "sketch" / "go.mod").write_text("module x\n")
    for r in discover_repos(home=tmp_path)["repos"]:
        assert r["dirty_scan"] in {"complete", "partial", "unavailable", "not-a-repo"}


def test_one_budget_covers_both_passes_not_just_the_expensive_one(tmp_path, monkeypatch):
    """The cheap probe is not free either: on a very large checkout `git status
    --untracked-files=no` still refreshes the index and was measured at 1.6s on
    this machine. Budgeting only the untracked pass leaves total wall time
    unbounded, so ONE deadline covers both.
    """
    for i in range(4):
        _real_repo(tmp_path / "git" / f"r{i}")
    calls: list[str] = []

    def spy(path, untracked, timeout):
        calls.append(untracked)
        return ""

    monkeypatch.setattr(repo_discovery, "_git_status", spy)
    monkeypatch.setattr(repo_discovery, "DIRTY_BUDGET_S", -1.0)  # already spent

    res = discover_repos(home=tmp_path)
    assert calls == [], "no git probe may run once the budget is gone"
    assert {r["dirty_scan"] for r in res["repos"]} == {"unavailable"}


def test_an_unreached_repo_reports_unavailable_rather_than_clean(tmp_path, monkeypatch):
    _real_repo(tmp_path / "git" / "svc")
    monkeypatch.setattr(repo_discovery, "DIRTY_BUDGET_S", -1.0)
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty_scan"] == "unavailable"
    assert got["dirty"] is False   # false, but the row says the check never ran
    # The cheap metadata still comes back - it costs no subprocess at all.
    assert got["branch"] == "main"
    assert got["is_git"] is True


def test_the_budget_bounds_the_whole_scan_not_each_repo(tmp_path, monkeypatch):
    for i in range(16):
        _real_repo(tmp_path / "git" / f"r{i:02d}")

    def slow(path, untracked, timeout):
        time.sleep(0.3)
        return ""

    monkeypatch.setattr(repo_discovery, "_git_status", slow)
    monkeypatch.setattr(repo_discovery, "DIRTY_BUDGET_S", 0.3)
    monkeypatch.setattr(repo_discovery, "GIT_TIMEOUT_S", 1.0)
    t0 = time.perf_counter()
    res = discover_repos(home=tmp_path)
    wall = time.perf_counter() - t0
    assert len(res["repos"]) == 16
    # 16 serial 0.3s probes would be 4.8s; the budget plus one in-flight probe
    # is the ceiling regardless of how many repos there are.
    assert wall < 2.0, f"scan took {wall:.2f}s"
