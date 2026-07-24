"""`nh bench run --quick` — stratified iteration tier.

A full core run takes hours, which throttles measurement-driven iteration.
The quick tier picks ONE representative per coverage cell
(project × runnable × expect_escalation × size-bucket), deterministically, so
quick runs are comparable to each other and still exercise the corpus's whole
variety. It is an ITERATION signal only: selection is fixed (fastest original
wall-clock in the cell, id tie-break), and a quick card can never publish as
the baseline — the existing corpus-coverage machinery refuses it.
"""
from __future__ import annotations

from no_human.eval.bench_task import BenchTask, quick_cell, select_quick_subset


def _spec(id, project="p", runnable=True, esc=False, wall=100.0):
    return BenchTask(
        id=id, title=id, request="r", subset="core", runnable=runnable,
        expect_escalation=esc,
        repo={"path": f"/repos/{project}", "pin": "", "branch": ""},
        original={"wall_clock_s": wall},
        spec_repo_path=f"/repos/{project}",
    )


def test_selects_one_per_cell_and_covers_every_cell():
    specs = [
        _spec("ns-a1", project="alpha", wall=500),
        _spec("ns-a2", project="alpha", wall=100),          # same cell as a1
        _spec("ns-b1", project="beta", wall=100),
        _spec("ns-c1", project="alpha", esc=True, wall=100),
        _spec("ns-d1", project="alpha", runnable=False, wall=0),
        _spec("ns-e1", project="alpha", wall=7200),         # L bucket
    ]
    picked = select_quick_subset(specs)
    assert {quick_cell(s) for s in picked} == {quick_cell(s) for s in specs}
    assert len(picked) == len({quick_cell(s) for s in specs})


def test_picks_fastest_original_wall_clock_with_id_tiebreak():
    specs = [
        _spec("ns-slow", wall=400),   # same S bucket (<600s) — same cell
        _spec("ns-fast", wall=50),
        _spec("ns-tie-b", wall=50.0),
    ]
    picked = select_quick_subset(specs)
    assert len(picked) == 1
    # 50s beats 400s; between the two 50s specs the lexically-smaller id wins.
    assert picked[0].id == "ns-fast"


def test_selection_is_deterministic_and_order_independent():
    specs = [
        _spec("ns-a", project="alpha", wall=10),
        _spec("ns-b", project="beta", wall=20),
        _spec("ns-c", project="alpha", esc=True, wall=30),
    ]
    a = [s.id for s in select_quick_subset(specs)]
    b = [s.id for s in select_quick_subset(list(reversed(specs)))]
    assert a == b


def test_cli_quick_runs_only_the_selected_subset(tmp_path, monkeypatch):
    import yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    # Two cells: 3 specs in alpha/S (one representative) + 1 in beta/S.
    for i, (proj, wall) in enumerate(
            [("alpha", 100), ("alpha", 50), ("alpha", 200), ("beta", 100)]):
        s = BenchTask(id=f"ns-q{i}", title=f"t{i}", request="r", subset="core",
                      runnable=True,
                      repo={"path": f"/definitely/not/here/{proj}",
                            "pin": "", "branch": ""},
                      original={"wall_clock_s": wall})
        (d / f"ns-q{i}.yaml").write_text(yaml.safe_dump(s.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"

        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]

    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR",
                        tmp_path / "results")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NORTH_STAR_BENCH.md")
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH",
                        tmp_path / "absent_map.yaml")

    ran: list[str] = []

    class _Probe:
        def __init__(self, *a, **kw): ...

        async def run_one(self, spec, *, workdir):
            ran.append(spec.id)
            return BenchScore(
                task_id=spec.id, title=spec.title, outcome_status="skipped",
                goal_satisfied=None, escalated_honestly=False, mergeable=None,
                nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0, subset=spec.subset)

    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _Probe)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--quick"])
    assert result.exit_code == 0, result.output
    # alpha/S representative = fastest (ns-q1, 50s) + beta/S's only spec.
    assert sorted(ran) == ["ns-q1", "ns-q3"], ran
    assert "quick tier" in result.output
    assert "iteration signal" in result.output


def test_cli_quick_and_full_are_mutually_exclusive(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    result = CliRunner().invoke(cli, ["bench", "run", "--quick", "--full"])
    assert result.exit_code != 0
    assert "--quick" in result.output and "--full" in result.output
