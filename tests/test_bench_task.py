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

def test_windsurf_workspaces_resolve_repo_path(tmp_path):
    """Windsurf/Devin transcripts carry `workspaces` (file:// URIs), not cwd —
    without the fallback the ENTIRE original 89-conversation corpus builds as
    non-runnable (found live: all Windsurf specs skipped 'repo missing')."""
    repo = _git_repo(tmp_path)
    t = _transcript(cwd="", branch="")
    t.workspaces = [f"file://{repo}/"]
    t.source = "windsurf"
    build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.runnable is True
    assert spec.repo["path"] == str(repo)


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


def test_cli_bench_run_wiring_end_to_end(tmp_path, monkeypatch):
    """Exercise bench run PAST the no-specs exit (the live baseline launch
    crashed on a NameError this path would have caught): specs present,
    _bootstrap stubbed, runner stubbed to skip — the command must reach the
    card/report stage and exit 0."""
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    spec = BenchTask(id="ns-wire1", title="t", request="r", subset="core",
                     runnable=False, skip_reason="wiring test")
    (d / "ns-wire1.yaml").write_text(_yaml.safe_dump(spec.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]

    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))

    class _StubRunner:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            return BenchScore(
                task_id=spec.id, title=spec.title, outcome_status="skipped",
                goal_satisfied=None, escalated_honestly=False, mergeable=None,
                nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0,
                subset=spec.subset, notes="stub")
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _StubRunner)
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR",
                        tmp_path / "results")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NORTH_STAR_BENCH.md")

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d)])
    assert result.exit_code == 0, result.output
    assert "success" in result.output


def test_cli_bench_run_survives_a_crashing_task(tmp_path, monkeypatch):
    """A single task's hard crash (SDK CLI dying on quota saturation killed a
    LIVE baseline at 3/10 and lost every partial result) must be recorded as
    crashed and the run must continue to the next spec."""
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    for i in range(2):
        spec = BenchTask(id=f"ns-crash{i}", title="t", request="r",
                         subset="core", runnable=True)
        (d / f"ns-crash{i}.yaml").write_text(_yaml.safe_dump(spec.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]
    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))

    calls = []

    class _CrashThenSkip:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            calls.append(spec.id)
            if len(calls) == 1:
                raise RuntimeError("Stream closed")
            return BenchScore(
                task_id=spec.id, title=spec.title, outcome_status="skipped",
                goal_satisfied=None, escalated_honestly=False, mergeable=None,
                nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0,
                subset=spec.subset, notes="stub")
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _CrashThenSkip)
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", tmp_path / "res")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NS.md")

    result = CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2, "the run must continue past the crash"
    assert "crashed" in result.output
    import json as _json
    saved = _json.loads((tmp_path / "res" / "latest.json").read_text())
    crashed = [x for x in saved["scores"] if x["outcome_status"] == "crashed"]
    assert len(crashed) == 1 and crashed[0]["goal_satisfied"] is False


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


def test_cli_bench_run_checkpoints_and_resumes(tmp_path, monkeypatch):
    """A mid-run death (quota 'Stream closed' killed the expanded run at 3/14)
    must not waste completed specs: each spec checkpoints, and --resume skips
    the already-scored ones."""
    import json as _json
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    for i in range(3):
        spec = BenchTask(id=f"ns-r{i}", title="t", request="r", subset="core",
                         runnable=True)
        (d / f"ns-r{i}.yaml").write_text(_yaml.safe_dump(spec.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]
    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))
    res_dir = tmp_path / "res"
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", tmp_path / "N.md")

    seen = []

    def _score(spec):
        return BenchScore(
            task_id=spec.id, title=spec.title, outcome_status="skipped",
            goal_satisfied=None, escalated_honestly=False, mergeable=None,
            nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0, orig_cache_tokens=0,
            orig_cache_creation_tokens=0, orig_wall_clock_s=0.0,
            orig_corrections=0, subset=spec.subset, notes="stub")

    class _DieOnThird:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            seen.append(spec.id)
            if len(seen) == 3:
                # A hook-thread error escapes `except Exception` and kills the
                # process — the exact case the checkpoint protects (the caught
                # kind completes and cleans up).
                raise KeyboardInterrupt("Stream closed (process killed)")
            return _score(spec)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _DieOnThird)

    # First run: 2 succeed, the 3rd hard-kills the process — the checkpoint
    # must hold the 2 completed specs.
    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])
    ckpt = _json.loads((res_dir / "progress.json").read_text())
    assert len({s["task_id"] for s in ckpt["scores"]}) >= 2

    # Resume: the 2 already-scored specs are skipped.
    seen.clear()

    class _AllSkip:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            seen.append(spec.id)
            return _score(spec)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _AllSkip)
    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d), "--resume"])
    assert "ns-r0" not in seen and "ns-r1" not in seen, "resume re-ran done specs"

    # The checkpointed specs must survive INTO the final card, not just be
    # skipped-and-dropped — latest.json holds all 3, and the checkpoint is gone.
    final = _json.loads((res_dir / "latest.json").read_text())
    assert {s["task_id"] for s in final["scores"]} == {"ns-r0", "ns-r1", "ns-r2"}
    assert not (res_dir / "progress.json").exists(), "clean run left a checkpoint"

    # A checkpoint from a DIFFERENT spec set must not bleed foreign specs into
    # latest.json (the gate baseline) — resume filters to this run's ids.
    (res_dir / "progress.json").write_text(_json.dumps({
        "created_at": "x", "label": "stale",
        "scores": [_score(BenchTask(id="ns-foreign", title="t", request="r",
                                    subset="core", runnable=True)).as_dict()],
    }))
    seen.clear()
    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d), "--resume"])
    final2 = _json.loads((res_dir / "latest.json").read_text())
    assert "ns-foreign" not in {s["task_id"] for s in final2["scores"]}, \
        "resume leaked a foreign spec into the baseline"
