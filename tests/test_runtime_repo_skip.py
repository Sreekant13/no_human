"""An unresolvable repo is a SKIP at run time, not a capability failure.

`runnable`/`skip_reason` are decided at spec GENERATION time. Nothing re-checked
them at run time, so a spec whose repo path no longer resolved still entered
`run_one`, reached `git clone`, and was booked as `crashed` with
`goal_satisfied=False` — a broken INSTRUMENT scored as the agent failing.

The opposite mistake is worse and is tested for explicitly: skipping too eagerly
turns real capability failures into skips, and skipped specs leave the success
denominator entirely, so over-skipping INFLATES the headline.
"""

from __future__ import annotations

import subprocess

import pytest

from no_human.eval.bench_task import BenchTask
from no_human.eval.northstar import NorthStarRunner


def _runner() -> NorthStarRunner:
    # _skipped and the guard need no collaborators.
    return NorthStarRunner.__new__(NorthStarRunner)


def _committed_repo(path) -> "object":
    """A repo with one commit — `_setup_sandbox` resets to HEAD, which fails on
    an empty repo for reasons unrelated to this guard."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=a@b",
                    "-c", "user.name=a", "commit", "-qm", "c"], check=True)
    return path


def _spec(path: str, task_id: str = "ns-1") -> BenchTask:
    return BenchTask(id=task_id, title="t", request="r", subset="core",
                     runnable=True,
                     repo={"path": path, "pin": "", "branch": ""})


@pytest.mark.asyncio
async def test_a_repo_that_does_not_exist_is_skipped_not_crashed(tmp_path):
    score = await _runner().run_one(_spec(str(tmp_path / "gone")),
                                    workdir=tmp_path / "wd")
    assert score.outcome_status == "skipped"
    assert score.goal_satisfied is None      # NOT False — it did not fail
    assert "repo missing at run time" in score.notes


@pytest.mark.asyncio
async def test_a_relative_path_is_skipped_rather_than_resolved_to_our_own_cwd(
        tmp_path):
    """`.`, `./` and `""` all resolve to the RUNNER's checkout, which has a
    .git — so the probe would pass and the bench would sandbox-copy no_human
    itself as the spec's subject."""
    for bad in (".", "./", "relative/path"):
        score = await _runner().run_one(_spec(bad), workdir=tmp_path / "wd")
        assert score.outcome_status == "skipped", bad
        assert "not absolute" in score.notes, bad


@pytest.mark.asyncio
async def test_an_empty_path_is_skipped(tmp_path):
    score = await _runner().run_one(_spec(""), workdir=tmp_path / "wd")
    assert score.outcome_status == "skipped"
    assert "no repo.path" in score.notes


@pytest.mark.asyncio
async def test_a_git_worktree_whose_dotgit_is_a_FILE_is_not_treated_as_missing(
        tmp_path):
    """A worktree and a submodule carry `.git` as a FILE. Probing with
    `.is_dir()` would skip perfectly good repos — the false-skip direction."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    (main / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main), "-c", "user.email=a@b",
                    "-c", "user.name=a", "commit", "-qm", "c"], check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q",
                    str(wt), "-b", "probe"], check=True)
    assert (wt / ".git").is_file(), "fixture must have .git as a FILE"

    # The guard must NOT skip it. It will fail later for unrelated reasons
    # (no backend wired), so assert only that it got past the guard.
    with pytest.raises(Exception) as exc:
        await _runner().run_one(_spec(str(wt)), workdir=tmp_path / "wd")
    assert "repo missing at run time" not in str(exc.value)


@pytest.mark.asyncio
async def test_the_guard_does_not_swallow_a_real_capability_failure(tmp_path):
    """THE failure mode that is worse than the bug being fixed.

    A spec whose repo IS available must never be skipped by this guard —
    skipped specs leave the success denominator, so over-skipping inflates the
    headline. Proven by getting PAST the guard on a resolvable repo.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert (repo / ".git").is_dir()

    with pytest.raises(Exception) as exc:
        await _runner().run_one(_spec(str(repo)), workdir=tmp_path / "wd")
    # It failed for a REAL reason, not because the guard skipped it.
    for phrase in ("repo missing at run time", "not absolute", "no repo.path"):
        assert phrase not in str(exc.value)


@pytest.mark.asyncio
async def test_a_whitespace_padded_path_does_not_crash_downstream(tmp_path):
    """The guard validates a stripped path; `_setup_sandbox` re-derives
    `Path(spec.repo["path"])` itself. Validating one string while the consumer
    clones another meant a padded path passed the guard and then died in
    `git clone` — this guard's own code producing the crash it exists to
    prevent. Either it is skipped, or the validated path is what gets used;
    what must NOT happen is reaching a clone with the raw value.
    """
    repo = _committed_repo(tmp_path / "repo")
    spec = _spec(f"  {repo}  ")

    with pytest.raises(Exception) as exc:
        await _runner().run_one(spec, workdir=tmp_path / "wd")

    # It got past the guard AND past the clone: the failure is the un-inited
    # runner, not a CalledProcessError from cloning a padded path.
    assert "CalledProcessError" not in type(exc.value).__name__, exc.value
    # The validated path replaced the padded one, so the consumer cannot use it.
    assert spec.repo["path"] == str(repo)


@pytest.mark.asyncio
async def test_a_skip_note_carries_the_SPEC_path_not_the_local_one(tmp_path):
    """The skip reason names the repo path — which after the repo-map
    translation is the operator's REAL local checkout, and this note is
    rendered into the tracked report.

    The consequence is sharper than a leak: the report writer refuses any
    rendered artifact containing a home path, with no --force override. So a
    single run-time skip would kill an otherwise-clean run at the final write,
    and run-time skips are exactly what this change makes normal.
    """
    spec = _spec(str(tmp_path / "real" / "private-checkout"))
    spec.spec_repo_path = "/spec/neutral/service-a"

    score = await _runner().run_one(spec, workdir=tmp_path / "wd")

    assert score.outcome_status == "skipped"
    assert "/spec/neutral/service-a" in score.notes, score.notes
    assert str(tmp_path) not in score.notes, score.notes
    assert "private-checkout" not in score.notes, score.notes


@pytest.mark.asyncio
async def test_the_preflight_and_the_runtime_guard_agree(tmp_path):
    """A clean pre-flight followed by a run-time skip is the exact surprise
    the pre-flight exists to prevent. They disagreed in BOTH directions: a
    padded path was reported as broken and then ran, a dot-relative path was
    reported clean and then skipped."""
    from no_human.eval.bench_task import check_repo_map

    repo = _committed_repo(tmp_path / "repo")

    padded = _spec(f"  {repo}  ", task_id="ns-pad")
    # "." specifically: it IS a directory, so `is_dir()` alone does NOT
    # flag it and the mutant that drops the is_absolute branch survives
    # the whole suite. It also resolves to the RUNNER's own checkout,
    # which has a .git — exactly the "clean pre-flight, then skipped at
    # run time" shape this test is named for.
    relative = _spec(".", task_id="ns-rel")

    flagged = {p.split(":")[0] for p in check_repo_map([padded, relative])}
    assert "ns-pad" not in flagged, "padded path RUNS, so must not be flagged"
    assert "ns-rel" in flagged, "relative path SKIPS, so must be flagged"

    # And that is what actually happens at run time.
    rel_score = await _runner().run_one(relative, workdir=tmp_path / "wd")
    assert rel_score.outcome_status == "skipped"


def test_the_report_headline_does_not_call_a_run_time_skip_NON_RUNNABLE():
    """"non-runnable" was true when every skip came from spec.runnable=False,
    decided at generation time. The dominant skip is now "repo missing at run
    time" — the instrument breaking, not a spec that was never runnable — so a
    reader of the tracked report would conclude the corpus holds N unrunnable
    specs rather than that N checkouts vanished.

    Reverting the label passed the entire suite, so the relabel had no
    defender at all.
    """
    from no_human.eval.northstar import BenchScore
    from no_human.eval.northstar_card import NorthStarCard, render_northstar_md

    def _s(tid, status, sat):
        return BenchScore(
            task_id=tid, title=tid, outcome_status=status, goal_satisfied=sat,
            escalated_honestly=False, mergeable=None, nh_tokens=1000,
            nh_cache_tokens=0, nh_cache_creation_tokens=0, nh_turns=1,
            nh_wall_clock_s=1.0, orig_tokens=10_000, orig_cache_tokens=0,
            orig_cache_creation_tokens=0, orig_wall_clock_s=1.0,
            orig_corrections=0)

    scores = [_s(f"ok-{i}", "done", True) for i in range(8)]
    scores.append(_s("gone-1", "skipped", None))
    md = render_northstar_md(NorthStarCard(scores=scores, label="t"))

    assert "skipped (not measured): 1" in md, md[:600]
    assert "non-runnable" not in md, "a run-time skip is not a non-runnable spec"
