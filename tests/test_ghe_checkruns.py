"""Tests for ci.ghe_checkruns — the GHE check-runs reader (IMPROVEMENT_PLAN D).

Tests the pure conversion logic (check_runs_to_result) with fixture data
so no ``gh`` CLI or network access is needed.
"""

import pytest

from no_human.ci.ghe_checkruns import check_runs_to_result
from no_human.ci.base import PipelineStatus


def _run(name, status="completed", conclusion="success"):
    """Minimal check-run fixture."""
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "html_url": f"https://code.example.com/check/{name}",
    }


def test_all_success():
    runs = [_run("lint"), _run("unit-tests"), _run("integration")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.SUCCESS
    assert result.passed
    assert len(result.jobs) == 3


def test_one_failure():
    runs = [_run("lint"), _run("unit-tests", conclusion="failure"), _run("integration")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.FAILED
    assert not result.passed
    assert any(j.failure_reason == "failure" for j in result.jobs)


def test_in_progress():
    runs = [_run("lint"), _run("unit-tests", status="in_progress")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.RUNNING
    assert not result.status.is_terminal


def test_queued():
    runs = [_run("lint", status="queued")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.RUNNING


def test_empty_runs():
    result = check_runs_to_result([], ref="abc123")
    assert result.status == PipelineStatus.UNKNOWN


def test_cancelled():
    runs = [_run("lint", conclusion="cancelled")]
    result = check_runs_to_result(runs, ref="abc123")
    # Cancelled is a non-success outcome; the aggregate pipeline status is FAILED.
    assert result.status == PipelineStatus.FAILED
    assert not result.passed


def test_neutral_is_success():
    runs = [_run("advisory-check", conclusion="neutral")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.SUCCESS


def test_mixed_success_and_neutral():
    runs = [_run("lint"), _run("advisory", conclusion="neutral")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.SUCCESS


def test_timed_out_is_failure():
    runs = [_run("slow-test", conclusion="timed_out")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.status == PipelineStatus.FAILED


def test_jobs_have_web_urls():
    runs = [_run("lint")]
    result = check_runs_to_result(runs, ref="abc123")
    assert result.jobs[0].web_url == "https://code.example.com/check/lint"


def test_ci_from_config_ghe_checkruns():
    from no_human.ci import ci_from_config
    cfg = {
        "ci": {
            "enabled": True,
            "backend": "ghe_checkruns",
            "repo": "org/my-repo",
            "hostname": "code.example.com",
        }
    }
    runner = ci_from_config(cfg)
    assert runner is not None
    assert runner.name == "ghe_checkruns"
    assert runner.repo == "org/my-repo"
    assert runner.hostname == "code.example.com"


def test_ci_from_config_ghe_no_repo():
    from no_human.ci import ci_from_config
    cfg = {"ci": {"enabled": True, "backend": "ghe_checkruns"}}
    assert ci_from_config(cfg) is None
