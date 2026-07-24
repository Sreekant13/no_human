"""B6: run_shadow must abort a hung coder turn with an honest 'timed_out'
result + event instead of wedging forever (there was no wall-clock watchdog —
the backend bounds only max_turns, not wall time)."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from no_human.config import DEFAULT_CONFIG
from no_human.core.orchestrator import Orchestrator
from no_human.eval.harness import run_shadow


def _tiny_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"],
                 ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_run_shadow_times_out_instead_of_wedging(tmp_path, monkeypatch):
    repo = _tiny_repo(tmp_path)

    async def _hang(self, task):  # a hung backend turn — never returns in time
        await asyncio.sleep(30)

    monkeypatch.setattr(Orchestrator, "run_task", _hang)

    events: list[dict] = []
    cfg = {"bounds": {**DEFAULT_CONFIG["bounds"], "shadow_timeout_s": 0.3}}
    result = await run_shadow(
        cfg, repo_path=str(repo), task_title="hang test",
        backend=object(), reviewer=None, on_event=lambda e: events.append(e),
    )

    assert result.outcome_status == "timed_out", result
    assert result.pushed is False
    assert any(e.get("kind") == "shadow_timeout" for e in events), events


@pytest.mark.asyncio
async def test_run_shadow_returns_normally_when_the_task_finishes_in_time(tmp_path, monkeypatch):
    """The watchdog must NOT fire for a normal fast run — no false timeout."""
    repo = _tiny_repo(tmp_path)

    class _Outcome:
        class status:
            value = "completed"

    async def _quick(self, task):
        return _Outcome()

    monkeypatch.setattr(Orchestrator, "run_task", _quick)

    events: list[dict] = []
    cfg = {"bounds": {**DEFAULT_CONFIG["bounds"], "shadow_timeout_s": 30}}
    result = await run_shadow(
        cfg, repo_path=str(repo), task_title="quick", backend=object(),
        reviewer=None, on_event=lambda e: events.append(e),
    )
    assert result.outcome_status == "completed", result
    assert not any(e.get("kind") == "shadow_timeout" for e in events), events
