"""Cancel must STOP the work, not just flip the DB status — a cancelled task
left orphaned SDK + pytest processes churning on its worktree."""

import subprocess

from no_human.api.app import _kill_task_processes


async def test_refuses_too_broad_patterns():
    # Never `pkill -f` on a short/empty pattern — that could kill unrelated procs.
    assert await _kill_task_processes("") == 0
    assert await _kill_task_processes("abc") == 0


async def test_kills_by_the_full_task_id(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    tid = "25ff5eae53f14d1b8d239ddfbf52ddd6"
    assert await _kill_task_processes(tid) == 1
    assert seen["argv"] == ["pkill", "-9", "-f", tid]


async def test_no_match_is_still_success(monkeypatch):
    def fake_run(argv, **kw):
        class R:
            returncode = 1  # pkill: no processes matched
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert await _kill_task_processes("25ff5eae53f14d1b8d239ddfbf52ddd6") == 1
