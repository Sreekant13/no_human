"""Git ops + local PR-open path against a real bare repo. Never merges."""

import subprocess
from pathlib import Path

import pytest

from no_human import vcs
from no_human.vcs import GitRepo, ProtectedBranch


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_bare_remote(tmp_path):
    """A work repo with one commit on `main`, wired to a local bare remote."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def test_branch_and_commit_under_agent_identity(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                   identity_email="no-human@acme.com")
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    result = repo.commit_all("PROJ-1: add feature")
    assert result.branch == "no-human/abc123"
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"],
                            cwd=repo.path, capture_output=True, text=True).stdout.strip()
    assert author == "no_human <no-human@acme.com>"


def test_refuses_to_create_protected_branch(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    with pytest.raises(ProtectedBranch):
        repo.create_branch("main")


def test_refuses_to_commit_on_protected_branch(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    (repo.path / "x.py").write_text("z = 3\n")
    with pytest.raises(ProtectedBranch):
        repo.commit_all("should not commit on main")


def test_refuses_to_push_protected_branch(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    with pytest.raises(ProtectedBranch):
        repo.push("main")


def test_stage_excludes_ephemeral_artifacts(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    cache = repo.path / "__pycache__"
    cache.mkdir()
    (cache / "feature.cpython-312.pyc").write_bytes(b"\x00\x01")
    (repo.path / "stale.pyc").write_bytes(b"\x00")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "__pycache__" not in files
    assert ".pyc" not in files


def test_stage_excludes_no_human_settings(repo_with_bare_remote):
    """Regression: .no_human/project.yml must never be committed in a PR."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    nh_dir = repo.path / ".no_human"
    nh_dir.mkdir()
    (nh_dir / "project.yml").write_text("ecosystem: node\ntest_cmd: npm test\n")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert ".no_human" not in files


def test_commit_paths_only_stages_listed_files(repo_with_bare_remote):
    """Regression: test side-effects (e.g. alert-state.json) must not be committed."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    # Agent intentionally edits feature.py
    (repo.path / "feature.py").write_text("y = 2\n")
    # Test run creates a side-effect file
    teams = repo.path / "teams" / "test"
    teams.mkdir(parents=True)
    (teams / "alert-state.json").write_text('{"state": "updated"}')
    # commit_paths only stages the agent's file
    result = repo.commit_paths(
        [str(repo.path / "feature.py")], "PROJ-1: add feature"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "alert-state.json" not in files


def test_commit_paths_falls_back_when_no_tracked_paths(repo_with_bare_remote):
    """If agent only used Bash (no Write/Edit tracked), commit_paths falls back to commit_all."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "script_output.txt").write_text("created by bash\n")
    # Pass empty list — should fall back to stage_all
    result = repo.commit_paths([], "PROJ-1: bash work")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "script_output.txt" in files


def test_commit_paths_includes_modified_tracked_files(repo_with_bare_remote):
    """Modified tracked files (e.g. requirements.txt edited via Bash) must be
    committed even when the agent only Edit-tracked a test file."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    # Set up: add a tracked file to the repo first
    (repo.path / "requirements.txt").write_text("flask\n")
    repo.commit_all("initial: add requirements.txt")
    # Now: agent creates a test file via Edit tool (tracked by orchestrator)
    (repo.path / "tests_new.py").write_text("def test_x(): pass\n")
    # And: agent modifies the existing tracked file via Bash (not tracked)
    (repo.path / "requirements.txt").write_text("flask\nredis\n")
    # commit_paths is called with only the test file (Edit-tracked)
    result = repo.commit_paths(
        [str(repo.path / "tests_new.py")], "add tests + update requirements"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "tests_new.py" in files       # explicitly listed (Edit-tracked)
    assert "requirements.txt" in files   # modified tracked file auto-included


def test_commit_paths_includes_untracked_code_files(repo_with_bare_remote):
    """New .py files created via Bash should be auto-staged."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    # Agent edits one file via Edit tool
    (repo.path / "test_thing.py").write_text("assert True\n")
    # Agent creates a new source file via Bash (untracked)
    src = repo.path / "src"
    src.mkdir()
    (src / "limiter.py").write_text("class Limiter: pass\n")
    result = repo.commit_paths(
        [str(repo.path / "test_thing.py")], "add tests + limiter"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "test_thing.py" in files
    assert "src/limiter.py" in files  # untracked .py auto-staged


def test_commit_paths_excludes_untracked_json_side_effects(repo_with_bare_remote):
    """Untracked .json files (test side-effects) must still be excluded."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    # Test side-effect: a .json state file
    data = repo.path / "data"
    data.mkdir()
    (data / "state.json").write_text('{"updated": true}')
    result = repo.commit_paths(
        [str(repo.path / "feature.py")], "PROJ-1: add feature"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "state.json" not in files  # untracked .json excluded


def test_commit_paths_excludes_venv_py_files(repo_with_bare_remote):
    """Untracked .py files inside .venv*/ dirs must not be committed."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    # .venv312 with .py files (common in real repos)
    venv = repo.path / ".venv312" / "lib" / "site-packages" / "redis"
    venv.mkdir(parents=True)
    (venv / "__init__.py").write_text("")
    (venv / "client.py").write_text("class Redis: pass\n")
    result = repo.commit_paths(
        [str(repo.path / "feature.py")], "PROJ-1: add feature"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert ".venv312" not in files   # venv excluded by _EPHEMERAL


def test_commit_paths_includes_new_source_when_only_an_edit_is_tracked(repo_with_bare_remote):
    """The PR #2 bug: the hook tracked only the EDITED file (App.jsx); the coder
    also created a new .js helper + a .mjs test. `git add -- <paths>
    :(exclude,glob)…` silently dropped the untracked ones, so App.jsx shipped
    importing a module that wasn't committed — a broken PR. All coder-created
    source (incl. .mjs) must land; ephemeral venv files must not."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/pr2", base="main")
    (repo.path / "App.jsx").write_text("import {b} from './helper.js'; export const a=b;\n")
    (repo.path / "helper.js").write_text("export const b=3;\n")          # new .js
    (repo.path / "helper.test.mjs").write_text("import {b} from './helper.js';\n")  # new .mjs
    venv = repo.path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "junk.py").write_text("x=1\n")                               # ephemeral
    # Simulate the hook having tracked ONLY the edited App.jsx.
    repo.commit_paths([str(repo.path / "App.jsx")], "add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "App.jsx" in files
    assert "helper.js" in files          # the .js that was dropped in PR #2
    assert "helper.test.mjs" in files    # the .mjs (ext was missing from _CODE_EXTS)
    assert ".venv" not in files          # ephemeral still excluded


def test_uncommitted_source_files_flags_an_incomplete_commit(repo_with_bare_remote):
    """The committed-state guard: a coder-created source file left OUT of the
    commit must be reported (it would ship a broken PR); non-code/ephemeral
    files are ignored; a complete commit reports nothing."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/guard", base="main")
    (repo.path / "app.js").write_text("import './helper.js';\n")
    (repo.path / "helper.js").write_text("export const b=1;\n")   # source, must land
    (repo.path / "notes.txt").write_text("scratch\n")             # non-code, ignored
    (repo.path / ".venv").mkdir()
    (repo.path / ".venv" / "x.py").write_text("y=1\n")            # ephemeral, ignored
    # Simulate the PR #2 bug: commit only app.js, leaving helper.js behind.
    subprocess.run(["git", "add", "app.js"], cwd=repo.path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "partial"], cwd=repo.path, check=True, capture_output=True)
    leftover = repo.uncommitted_source_files()
    assert "helper.js" in leftover                    # the broken-PR file
    assert "notes.txt" not in leftover                # non-code data ignored
    assert not any(".venv" in f for f in leftover)    # ephemeral ignored
    # After committing everything, the guard is clean.
    repo.commit_all("rest")
    assert repo.uncommitted_source_files() == []


def test_is_github_remote_detects_github_and_ghe():
    from no_human.vcs import github
    assert github.is_github_remote("https://github.com/org/repo.git")
    assert github.is_github_remote("git@github.com:org/repo.git")
    # GHE host only when configured.
    ghe = "https://code.example.com/dev/repo.git"
    assert not github.is_github_remote(ghe)
    assert github.is_github_remote(ghe, ["code.example.com"])
    # unrelated host stays false
    assert not github.is_github_remote("https://gitlab.com/org/repo.git", ["code.example.com"])


# --------------------------------------------------------------------------- #
# PR labels: a repo can gate CI on a label of its own at PR-open, so          #
# they must be applied when the PR is created, not after.                     #
# --------------------------------------------------------------------------- #


def test_label_args_normalizes():
    from no_human.vcs._labels import label_args
    assert label_args(None) == []
    assert label_args([]) == []
    assert label_args(["needs-review"]) == ["--label", "needs-review"]
    # order preserved, blanks dropped, duplicates collapsed
    assert label_args(["needs-review", "  ", "", "triage", "needs-review"]) == [
        "--label", "needs-review", "--label", "triage",
    ]
    # a label with spaces stays one argv element
    assert label_args(["DO NOT MERGE"]) == ["--label", "DO NOT MERGE"]
    # surrounding whitespace is stripped, so config typos can't emit --label ''
    assert label_args([" needs-review "]) == ["--label", "needs-review"]


def _capture_argv(monkeypatch, module, returncode=0, stdout="https://pr/1"):
    """Record the argv the forge CLI would be invoked with."""
    seen = {}

    class _Proc:
        pass

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        proc = _Proc()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = ""
        return proc

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return seen


def test_github_open_pr_passes_labels_at_creation(monkeypatch, tmp_path):
    from no_human.vcs import github
    seen = _capture_argv(monkeypatch, github)
    url = github.open_pr(tmp_path, "br", "PROJ-1: t", "body", base="dev",
                         labels=["needs-review"])
    assert url == "https://pr/1"
    argv = seen["argv"]
    assert argv[:3] == ["gh", "pr", "create"]
    # label rides on the create call itself — never a follow-up `gh pr edit`
    assert "--label" in argv and argv[argv.index("--label") + 1] == "needs-review"
    assert "--draft" in argv  # never merges


def test_github_open_pr_without_labels_omits_flag(monkeypatch, tmp_path):
    from no_human.vcs import github
    seen = _capture_argv(monkeypatch, github)
    github.open_pr(tmp_path, "br", "t", "body", base="dev")
    assert "--label" not in seen["argv"]


def test_gitlab_open_mr_passes_labels(monkeypatch, tmp_path):
    from no_human.vcs import gitlab
    seen = _capture_argv(monkeypatch, gitlab)
    gitlab.open_mr(tmp_path, "br", "t", "body", base="dev", labels=["needs-review", "triage"])
    argv = seen["argv"]
    assert argv[:3] == ["glab", "mr", "create"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--label"] == ["needs-review", "triage"]
    assert "--no-merge" in argv  # never merges


def _sequence_run(monkeypatch, module, results):
    """Queue (returncode, stdout, stderr) tuples; record every argv seen."""
    calls = []
    queue = list(results)

    class _Proc:
        pass

    def fake_run(argv, **kwargs):
        calls.append(argv)
        rc, out, err = queue.pop(0)
        proc = _Proc()
        proc.returncode, proc.stdout, proc.stderr = rc, out, err
        return proc

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return calls


def test_is_label_error_is_narrow():
    from no_human.vcs._labels import is_label_error
    assert is_label_error("could not add label: 'needs-review' not found")
    assert is_label_error("label 'needs-review' does not exist")
    assert not is_label_error("authentication required")  # unrelated failure
    assert not is_label_error("a merge conflict on the branch")  # 'label' absent


def test_github_open_pr_retries_without_labels_when_label_missing(monkeypatch, tmp_path):
    """A label applied to a repo that never defined it must not
    strand the task — the PR opens unlabelled on retry (the ca23ce68 E2E blocker)."""
    from no_human.vcs import github
    calls = _sequence_run(monkeypatch, github, [
        (1, "", "could not add label: 'needs-review' not found"),   # 1st: with labels
        (0, "https://pr/9", ""),                            # 2nd: without labels
    ])
    url = github.open_pr(tmp_path, "br", "t", "body", base="dev", labels=["needs-review"])
    assert url == "https://pr/9"
    assert "--label" in calls[0]           # first attempt carried the label
    assert "--label" not in calls[1]       # retry dropped it
    assert len(calls) == 2


def test_github_open_pr_non_label_failure_is_not_masked(monkeypatch, tmp_path):
    from no_human.vcs import github
    _sequence_run(monkeypatch, github, [(1, "", "authentication required")])
    with pytest.raises(RuntimeError, match="authentication required"):
        github.open_pr(tmp_path, "br", "t", "body", base="dev", labels=["needs-review"])


def test_gitlab_open_mr_retries_without_labels_when_label_missing(monkeypatch, tmp_path):
    from no_human.vcs import gitlab
    calls = _sequence_run(monkeypatch, gitlab, [
        (1, "", "label 'needs-review' does not exist"),
        (0, "https://mr/3", ""),
    ])
    url = gitlab.open_mr(tmp_path, "br", "t", "body", base="dev", labels=["needs-review"])
    assert url == "https://mr/3"
    assert "--label" not in calls[1]
    assert len(calls) == 2


def test_open_pr_local_path(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    repo.commit_all("PROJ-1: add feature")
    pr = vcs.open_pr(repo, "no-human/abc123", "PROJ-1: add feature", "body", base="main")
    assert pr.kind == "local"
    assert "no-human/abc123" in pr.url
    # branch made it to the bare remote
    branches = subprocess.run(["git", "branch", "--list"], cwd=repo.path,
                              capture_output=True, text=True).stdout
    assert "no-human/abc123" in branches


# --------------------------------------------------------------------------- #
# Worktree isolation (Phase 7 foundation)                                      #
# --------------------------------------------------------------------------- #

def test_worktree_is_isolated_working_tree(repo_with_bare_remote, tmp_path):
    """Two worktrees off the same repo have independent files/branches; an edit
    in one is invisible in the other and on the main tree."""
    repo = GitRepo(repo_with_bare_remote)
    wt_a = repo.add_worktree(tmp_path / "wt-a", base="main", branch="no-human/a")
    wt_b = repo.add_worktree(tmp_path / "wt-b", base="main", branch="no-human/b")

    (wt_a.path / "a_only.py").write_text("a = 1\n")
    wt_a.commit_all("PROJ-1: a change")

    assert (wt_a.path / "a_only.py").exists()
    assert not (wt_b.path / "a_only.py").exists()        # isolated from sibling
    assert not (repo.path / "a_only.py").exists()         # and from the main tree
    assert wt_a.current_branch() == "no-human/a"
    assert wt_b.current_branch() == "no-human/b"
    # Both share the object store but have distinct HEADs.
    assert wt_a.head_sha() != wt_b.head_sha()


def test_worktree_refuses_protected_branch(repo_with_bare_remote, tmp_path):
    repo = GitRepo(repo_with_bare_remote)
    with pytest.raises(ProtectedBranch):
        repo.add_worktree(tmp_path / "wt-main", base="main", branch="main")


def test_worktree_context_manager_cleans_up(repo_with_bare_remote, tmp_path):
    repo = GitRepo(repo_with_bare_remote)
    wt_path = tmp_path / "wt-scoped"
    with repo.worktree(wt_path, base="main", branch="no-human/scoped") as wt:
        (wt.path / "f.py").write_text("z = 3\n")
        wt.commit_all("PROJ-9: scoped")
        assert wt_path.exists()
        assert str(wt_path.resolve()) in [w for w in repo.list_worktrees()]
    # Removed on exit; main repo no longer lists it.
    assert str(wt_path.resolve()) not in [w for w in repo.list_worktrees()]


# --------------------------------------------------------------------------- #
# Commit message sanitization (I2)                                             #
# --------------------------------------------------------------------------- #


def test_commit_strips_claude_attribution(repo_with_bare_remote):
    """Co-authored-by: Claude trailer must be silently removed."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    msg = "PROJ-1: add feature\n\nCo-authored-by: Claude <noreply@anthropic.com>"
    result = repo.commit_all(msg)
    log = subprocess.run(["git", "log", "-1", "--format=%B"],
                         cwd=repo.path, capture_output=True, text=True).stdout.strip()
    assert "Claude" not in log
    assert "anthropic" not in log
    assert "PROJ-1: add feature" in log


def test_commit_strips_openai_attribution(repo_with_bare_remote):  # term-ok: must match a real third-party trailer
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    msg = "PROJ-1: fix\n\nCo-authored-by: OpenAI <noreply@openai.com>"  # term-ok: real third-party trailer text
    result = repo.commit_all(msg)
    log = subprocess.run(["git", "log", "-1", "--format=%B"],
                         cwd=repo.path, capture_output=True, text=True).stdout.strip()
    assert "OpenAI" not in log  # term-ok: real third-party trailer text


def test_commit_preserves_legitimate_coauthor(repo_with_bare_remote):
    """A real human co-author must NOT be stripped."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    msg = "PROJ-1: fix\n\nCo-authored-by: Jane Doe <jane@example.com>"
    result = repo.commit_all(msg)
    log = subprocess.run(["git", "log", "-1", "--format=%B"],
                         cwd=repo.path, capture_output=True, text=True).stdout.strip()
    assert "Jane Doe" in log


# --------------------------------------------------------------------------- #
# Expanded ephemeral exclusions (I6)                                           #
# --------------------------------------------------------------------------- #


def test_stage_excludes_windsurf_devin_claude_dirs(repo_with_bare_remote):
    """IDE/agent config directories must never be committed."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    for dirname in (".windsurf", ".devin", ".claude"):  # term-ok: real on-disk IDE config dirs
        d = repo.path / dirname
        d.mkdir()
        (d / "settings.json").write_text("{}")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert ".windsurf" not in files  # term-ok: real on-disk IDE config dir
    assert ".devin" not in files  # term-ok: real on-disk IDE config dir
    assert ".claude" not in files


def test_stage_excludes_handover_md(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    (repo.path / "HANDOVER.md").write_text("# handover notes\n")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "HANDOVER.md" not in files


def test_stage_excludes_plan_md(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/plan123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    (repo.path / "PLAN.md").write_text("## FILES TO CHANGE\n- feature.py\n")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "PLAN.md" not in files


def test_open_pr_refuses_an_unrecognized_forge_before_pushing(repo_with_bare_remote):
    """An https remote we can't classify must never receive a push — the old
    behavior pushed first and reported a fake local-pr:// marker as success."""
    import pytest
    from no_human.vcs import open_pr

    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                   identity_email="no-human@acme.com")
    _git(repo_with_bare_remote, "remote", "set-url", "origin",
         "https://forge.example.com/team/repo.git")
    repo.create_branch("nh-test")
    with pytest.raises(RuntimeError, match="not recognized"):
        open_pr(repo, "nh-test", "t", "b", base="main")


def test_open_pr_still_pushes_to_a_local_bare_remote(repo_with_bare_remote, tmp_path):
    from no_human.vcs import open_pr

    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                   identity_email="no-human@acme.com")
    repo.create_branch("nh-local")
    pr = open_pr(repo, "nh-local", "t", "b", base="main")
    assert pr.kind == "local" and pr.url.startswith("local-pr://")


def test_worktree_attach_existing_branch_for_resume(repo_with_bare_remote, tmp_path):
    """create=False attaches an existing branch — the resume-a-parked-task path."""
    repo = GitRepo(repo_with_bare_remote)
    wt1 = repo.add_worktree(tmp_path / "wt1", base="main", branch="no-human/resume")
    (wt1.path / "wip.py").write_text("w = 1\n")
    wt1.commit_all("PROJ-7: wip")
    sha = wt1.head_sha()
    repo.remove_worktree(tmp_path / "wt1")
    # Re-attach the same branch in a fresh worktree (no -b): work is preserved.
    wt2 = repo.add_worktree(tmp_path / "wt2", base="main",
                            branch="no-human/resume", create=False)
    assert wt2.head_sha() == sha
    assert (wt2.path / "wip.py").exists()


# --- C3: default-branch auto-detection (origin/HEAD) --- #

def test_default_branch_via_remote_show_fallback(repo_with_bare_remote):
    """A plain `git init` + `remote add` + `push` (this fixture, and what
    no_human's own clone path does) never populates refs/remotes/origin/HEAD —
    only `git clone` does that automatically. Must fall back to
    `git remote show origin` and still resolve correctly."""
    repo = GitRepo(repo_with_bare_remote)
    assert repo.default_branch() == "main"


def test_default_branch_via_symbolic_ref_when_set(repo_with_bare_remote):
    """When origin/HEAD IS set locally (e.g. after a real `git clone`), use it
    directly without the slower `remote show` round-trip."""
    repo = GitRepo(repo_with_bare_remote)
    subprocess.run(["git", "remote", "set-head", "origin", "main"],
                   cwd=repo.path, check=True, capture_output=True)
    assert repo.default_branch() == "main"


def test_default_branch_empty_when_remote_show_reports_unknown(tmp_path):
    """git prints 'HEAD branch: (unknown)' when the remote's HEAD points at a
    ref the local repo has no knowledge of. Must be treated as undetermined,
    not returned literally as a fake branch name."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "master")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "master")  # bare's "main" ref never created
    repo = GitRepo(work)
    assert repo.default_branch() == ""


def test_default_branch_empty_when_no_origin(tmp_path):
    """No remote at all (e.g. a fresh local-only repo) → best-effort empty,
    never raises."""
    work = tmp_path / "solo"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    repo = GitRepo(work)
    assert repo.default_branch() == ""


def test_coder_touched_non_code_files_flagged_as_leftovers(repo_with_bare_remote):
    """B2 #7: a coder-CREATED Dockerfile/yaml left out of the commit shipped a
    broken PR past every gate (the favicon/commit_paths class, non-code shape).
    A leftover the coder itself touched is flagged regardless of extension;
    untouched non-code leftovers (test side-effects) stay ignored."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/b27", base="main")
    (Path(repo.path) / "Dockerfile").write_text("FROM python:3.12\n")
    (Path(repo.path) / "alert-state.json").write_text("{}")  # side-effect

    plain = repo.uncommitted_source_files()
    assert "Dockerfile" not in plain and "alert-state.json" not in plain

    flagged = repo.uncommitted_source_files(
        coder_touched={"Dockerfile"})
    assert "Dockerfile" in flagged
    assert "alert-state.json" not in flagged
