"""Tests for BenchTask specs + the no-leak builder (north-star A2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from no_human.eval.bench_task import (
    BenchTask,
    build_bench_tasks,
    find_leaks,
    load_bench_tasks,
)
from no_human.history.extractor import Message, Transcript


def _transcript(cascade_id="cc:abc", request="Fix the login bug in auth.py",
                *, cwd="", branch="main", source="cc:personal",
                extra_user=("also update the docs",)) -> Transcript:
    msgs = [Message(role="user", content=request, step_type="user"),
            Message(role="assistant", content="Done: I changed auth.py line 42 "
                    "to use the session token instead of the cookie value",
                    step_type="assistant")]
    for m in extra_user:
        msgs.append(Message(role="user", content=m, step_type="user"))
    return Transcript(
        cascade_id=cascade_id, title=request[:80], created="2026-07-01",
        messages=msgs, usage={"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 500,
                              "cache_creation_input_tokens": 10},
        started="2026-07-01T10:00:00.000Z", ended="2026-07-01T10:30:00.000Z",
        cwd=cwd, git_branch=branch, source=source, corrections=len(extra_user),
    )


import os


def _commit(repo: Path, msg: str, *, when: str) -> None:
    """Commit with BOTH author and committer dates pinned (rev-list --before
    reads the committer date)."""
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True,
                   capture_output=True, env=env)


def _git_repo(tmp_path: Path) -> Path:
    """A repo whose base commit PREDATES the fixture session (2026-07-01) so
    `rev-list --before=<session start>` has something to resolve."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"],
                 ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    _commit(repo, "base", when="2026-06-01T00:00:00Z")
    return repo


# ------------------------------- schema ----------------------------------- #

def test_yaml_round_trip(tmp_path):
    t = _transcript(cwd=str(_git_repo(tmp_path)))
    (path,) = build_bench_tasks([t], out_dir=tmp_path / "specs")
    loaded = load_bench_tasks(tmp_path / "specs")
    assert len(loaded) == 1
    spec = loaded[0]
    assert spec.request == "Fix the login bug in auth.py"
    assert spec.original["corrections"] == 1
    assert spec.original["wall_clock_s"] == 1800.0
    assert spec.original["tokens"]["cache_read_input_tokens"] == 500
    assert spec.runnable is True
    assert spec.repo["pin"]  # resolved to a real sha or HEAD
    assert path == spec.path


def test_subset_filter(tmp_path):
    d = tmp_path / "specs"
    d.mkdir()
    for i, subset in enumerate(["core", "full"]):
        spec = BenchTask(id=f"ns-{i}", title="t", request="r", subset=subset)
        (d / f"ns-{i}.yaml").write_text(yaml.safe_dump(spec.to_dict()))
    assert [t.id for t in load_bench_tasks(d, subset="core")] == ["ns-0"]
    assert len(load_bench_tasks(d)) == 2


# ------------------------------ no-cheating -------------------------------- #

def test_only_first_user_message_reaches_the_spec(tmp_path):
    """Structural no-leak: corrections and assistant text must never appear
    anywhere in the written YAML."""
    t = _transcript(cwd=str(_git_repo(tmp_path)),
                    extra_user=("the fix is to use the session token",))
    (path,) = build_bench_tasks([t], out_dir=tmp_path / "specs")
    raw = path.read_text()
    assert "Fix the login bug" in raw
    assert "session token" not in raw          # correction content
    assert "auth.py line 42" not in raw        # assistant content


def test_find_leaks_flags_curated_solution_text():
    spec = BenchTask(
        id="ns-1", title="t", request="Fix the login bug",
        acceptance_criteria=["I changed auth.py line 42 to use the session "
                             "token instead of the cookie value"])
    assistant = ("Done: I changed auth.py line 42 to use the session token "
                 "instead of the cookie value")
    assert find_leaks(spec, assistant) == ["acceptance_criteria"]
    # Independent criteria are clean.
    clean = BenchTask(id="ns-2", title="t", request="r",
                      acceptance_criteria=["login succeeds with a valid session"])
    assert find_leaks(clean, assistant) == []


def test_default_build_dir_is_gitignored():
    """The raw corpus is verbatim operator conversation content — the
    builder's DEFAULT target must be the gitignored generated/ dir, and the
    repo's .gitignore must actually cover it."""
    from no_human.eval.bench_task import GENERATED_DIR, NORTHSTAR_DIR

    assert GENERATED_DIR.parent == NORTHSTAR_DIR
    assert GENERATED_DIR.name == "generated"
    repo_root = NORTHSTAR_DIR.parents[1]
    gitignore = (repo_root / ".gitignore").read_text()
    assert "eval/northstar_tasks/generated/" in gitignore

    import inspect
    from no_human.eval.bench_task import build_bench_tasks
    sig = inspect.signature(build_bench_tasks)
    assert sig.parameters["out_dir"].default == GENERATED_DIR


# ------------------------------- dedupe ------------------------------------ #

def test_title_derives_from_request_never_transcript_title(tmp_path):
    """Review finding: transcript titles are auto-summaries of the WHOLE
    conversation and can encode the solution — an unaudited leak channel into
    the coder. The spec title must be user-authored (from the request)."""
    t = _transcript(cwd=str(_git_repo(tmp_path)))
    t.title = "Refactor auth to use session tokens instead of cookies"
    build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.title.startswith("Fix the login bug")
    assert "session tokens" not in spec.title


def test_resumed_sessions_dedupe_by_first_request(tmp_path):
    repo = str(_git_repo(tmp_path))
    a = _transcript(cascade_id="cc:a", cwd=repo)
    b = _transcript(cascade_id="cc:b", cwd=repo)   # same first request
    c = _transcript(cascade_id="cc:c", request="Different task", cwd=repo)
    written = build_bench_tasks([a, b, c], out_dir=tmp_path / "specs")
    assert len(written) == 2


# ---------------------------- non-runnable --------------------------------- #

def test_missing_repo_marked_not_runnable(tmp_path):
    t = _transcript(cwd="/nope/definitely/missing")
    (path,) = build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.runnable is False
    assert "repo missing" in spec.skip_reason


def test_non_git_cwd_marked_not_runnable(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    t = _transcript(cwd=str(plain))
    build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.runnable is False
    assert "not a git repo" in spec.skip_reason


def test_cli_bench_build_writes_specs(tmp_path, monkeypatch):
    """`nh bench build` is offline: parses history files, writes YAMLs, no LLM."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli
    from no_human.history import extractor

    def _no_ide(**_kw):
        raise extractor.IDENotRunningError("no IDE")
    monkeypatch.setattr("no_human.history.extractor.extract_transcripts", _no_ide)

    t = _transcript(cwd=str(_git_repo(tmp_path)))
    monkeypatch.setattr(
        "no_human.history.claude_code.extract_claude_code_transcripts",
        lambda **kw: [t])

    out = tmp_path / "specs"
    result = CliRunner().invoke(
        cli, ["bench", "build", "--days", "5", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert len(list(out.glob("ns-*.yaml"))) == 1


def test_cli_bench_run_exits_1_without_specs(tmp_path):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no specs found" in result.output


def test_pin_resolves_to_commit_before_session(tmp_path):
    repo = _git_repo(tmp_path)
    first_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                               capture_output=True, text=True).stdout.strip()
    # A later commit that postdates the session must NOT be the pin.
    (repo / "later.txt").write_text("y")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    _commit(repo, "later", when="2026-07-05T00:00:00Z")
    t = _transcript(cwd=str(repo))
    build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.repo["pin"] == first_sha
