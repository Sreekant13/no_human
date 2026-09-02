"""The "reject/reply dispatch onto a near-exhausted budget, silently" defect
(`.no_human/PLAN.md`): two real incidents (2026-08-24) — e79db976 rejected at
113% of its lifetime cap (already over, straight to FAILED) and c9f04943
rejected at 90.3% (would have died mid-rework, losing a HIGH-severity
security fix's feedback) — because `nh reject` / `nh reply` and their
board/API twins (`send-back`, `reply`) dispatch a task without ever looking
at remaining lifetime budget. The enforcement gate
(`Orchestrator._check_lifetime_budget` and friends) catches the overrun, but
only AFTER the human's feedback is already spent on an attempt that cannot
finish.

RED on pre-fix code: `no_human.core.budget_floor` did not exist before this
fix — every test below imports it (directly, or indirectly through
`no_human.cli.commands`/`no_human.api.app` calling `check_budget_floor`), so
every one of them fails with `ModuleNotFoundError`/`ImportError` (or, for the
CLI/API tests, an `AttributeError`/silent absence of the `budget_warning`
key) against the pre-fix tree. See the final report for the exact argument
(no `git stash` is used, per the outer harness rule against git commands in
this session).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human.cli.commands import cli
from no_human.config import load_config
from no_human.core.bounds import Bounds
from no_human.core.budget_floor import FLOOR_FRACTION, BudgetFloorWarning, check_budget_floor
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Helpers — mirrors tests/test_cli_commands.py / tests/test_send_back_refused #
# .py conventions: CLI commands call asyncio.run() internally, so any test    #
# that invokes them must be synchronous; each helper opens its own fresh     #
# Store connection inside its own asyncio.run().                             #
# --------------------------------------------------------------------------- #

def _seed_task_sync(
    db_path: Path, status: TaskStatus, *,
    cap_tokens: int = 1000, used_tokens: int = 0, used_attempts: int = 1,
    task_id: str | None = None,
) -> str:
    """Seed a task with a real cost-weighted cap and real attempt spend.

    ``config={"lifetime_tokens": cap_tokens, "budget_unit": "weighted"}`` is
    the exact marker `core.pricing.config_is_weighted` looks for (see
    `tests/test_send_back_refused.py`) — without it the cutover guard in
    `Orchestrator._stored_token_cap` would treat `cap_tokens` as a PRE-CUTOVER
    RAW number and convert it, which would make every number in this file
    wrong by ~5x.

    Spend is booked with `tokens_used` ONLY (no cache_read/cache_creation) so
    `weighted_tokens()` returns exactly `used_tokens` — fresh input tokens
    price at 1.0, so the weighted total equals the raw sum with no rounding,
    keeping every expected figure below an exact integer instead of an
    approximation.
    """
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("Fix the thing", repo_path="/tmp/repo")
            if task_id is not None:
                t.id = task_id
            t.acceptance_criteria = ["it works"]
            t.config = {"lifetime_tokens": cap_tokens, "budget_unit": "weighted"}
            await s.create_task(t)
            await s.set_status(t, status, validate=False)
            if used_tokens:
                per = used_tokens // used_attempts
                for n in range(1, used_attempts + 1):
                    amt = used_tokens - per * (used_attempts - 1) if n == used_attempts else per
                    aid = await s.create_attempt(t.id, n)
                    await s.update_attempt(aid, tokens_used=amt)
            return t.id
    return asyncio.run(_go())


def _get_task_sync(db_path: Path, task_id: str) -> Task:
    async def _go():
        async with Store(db_path) as s:
            return await s.find_task(task_id)
    return asyncio.run(_go())


def _make_cli_runner(db_path: Path, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db_path
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    return CliRunner()


DEFAULT_BOUNDS = Bounds()  # max_attempts=3, lifetime_attempts=9, lifetime_tokens=4_000_000


def _norm(s: str) -> str:
    """Collapse whitespace runs to a single space.

    `Console(stderr=True).print(...)` (rich) hard-wraps long lines at the
    terminal width, inserting newlines mid-sentence — CliRunner's captured
    output therefore contains line breaks the warning text itself doesn't.
    Comparisons must be wrap-insensitive, not byte-exact, or every assertion
    below would be pinned to a specific terminal width.
    """
    return " ".join(s.split())


def _expected_message(task_id: str, *, cap_tokens: int, used_tokens: int,
                       used_attempts: int, cap_attempts: int = DEFAULT_BOUNDS.lifetime_attempts,
                       max_attempts: int = DEFAULT_BOUNDS.max_attempts) -> str:
    """The exact `BudgetFloorWarning.message()` a low-budget seed produces —
    computed the same way `check_budget_floor` does, so a format drift
    between the CLI/API/board surfaces and this expectation, or between the
    two dispatch paths (reject/reply), fails a comparison instead of a
    hand-typed number silently going stale."""
    import math
    remaining_tokens = max(0, cap_tokens - used_tokens)
    remaining_attempts = max(0, cap_attempts - used_attempts)
    floor_tokens = int(cap_tokens * FLOOR_FRACTION)
    raise_tokens = math.ceil(used_tokens * 1.5 / 100_000) * 100_000
    raise_to = {"lifetime_tokens": raise_tokens, "lifetime_attempts": used_attempts + max_attempts}
    return BudgetFloorWarning(
        task_id=task_id, used_attempts=used_attempts, cap_attempts=cap_attempts,
        remaining_attempts=remaining_attempts, used_tokens=used_tokens,
        cap_tokens=cap_tokens, remaining_tokens=remaining_tokens,
        floor_tokens=floor_tokens, raise_to=raise_to,
    ).message()


# --------------------------------------------------------------------------- #
# AC1 — explicit floor: 15% of `lifetime_tokens_cap`                          #
# --------------------------------------------------------------------------- #

async def test_floor_is_fifteen_percent_of_the_cap(tmp_path):
    assert FLOOR_FRACTION == 0.15

    async with Store(tmp_path / "nh.db") as store:
        # Boundary case: remaining EXACTLY at the floor (150 of 1000) — the
        # acceptance criteria's own worked example — must NOT warn (`<`, not
        # `<=`).
        t_at = Task.new("at the floor", repo_path="/tmp/repo")
        t_at.acceptance_criteria = ["it works"]
        t_at.config = {"lifetime_tokens": 1000, "budget_unit": "weighted"}
        await store.create_task(t_at)
        aid = await store.create_attempt(t_at.id, 1)
        await store.update_attempt(aid, tokens_used=850)  # remaining = 150
        warning = await check_budget_floor(store, t_at, bounds=DEFAULT_BOUNDS)
        assert warning is None, warning

        # One weighted token below the floor DOES warn.
        t_below = Task.new("just under the floor", repo_path="/tmp/repo")
        t_below.acceptance_criteria = ["it works"]
        t_below.config = {"lifetime_tokens": 1000, "budget_unit": "weighted"}
        await store.create_task(t_below)
        aid2 = await store.create_attempt(t_below.id, 1)
        await store.update_attempt(aid2, tokens_used=851)  # remaining = 149
        warning2 = await check_budget_floor(store, t_below, bounds=DEFAULT_BOUNDS)
        assert warning2 is not None
        assert warning2.floor_tokens == 150, warning2
        assert warning2.remaining_tokens == 149, warning2


# --------------------------------------------------------------------------- #
# AC2 — `nh reject` on 8% remaining warns with real figures + both options    #
# --------------------------------------------------------------------------- #

def test_reject_below_floor_warns_with_real_figures(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    task_id = _seed_task_sync(
        db, TaskStatus.AWAITING_APPROVAL, cap_tokens=1000, used_tokens=920)
    runner = _make_cli_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs another pass"])

    assert result.exit_code == 0, result.output
    assert "sent back" in result.output.lower()

    expected = _expected_message(task_id, cap_tokens=1000, used_tokens=920, used_attempts=1)
    output = _norm(result.output)
    assert _norm(expected) in output, result.output
    # The two action options, verbatim per PLAN.md.
    assert "refile this ticket smaller" in output
    assert "raise the cap deliberately" in output
    assert "nh task config" in output
    assert "lifetime_tokens=" in output
    assert "lifetime_attempts=" in output
    # Real figures, not placeholders.
    assert "80" in output  # remaining tokens
    assert "8 of 9 attempts left" in output

    # The action itself proceeded exactly as it would with no warning.
    refreshed = _get_task_sync(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    feedback = refreshed.context.get("send_back_feedback", [])
    assert any("another pass" in f["message"] for f in feedback)


# --------------------------------------------------------------------------- #
# AC3 — `nh reply` emits the identical warning format                        #
# --------------------------------------------------------------------------- #

def test_reply_below_floor_warns_identically(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    # Same task_id across a fresh DB so the short-id embedded in the message
    # is identical to the reject test's — a genuine cross-path format check,
    # not just two separately-correct strings.
    fixed_id = "b1a2c3d4" + "0" * 24
    task_id = _seed_task_sync(
        db, TaskStatus.BLOCKED, cap_tokens=1000, used_tokens=920, task_id=fixed_id)
    runner = _make_cli_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "here is my answer", "--no-run"])

    assert result.exit_code == 0, result.output
    assert "resumed" in result.output.lower()

    expected = _expected_message(task_id, cap_tokens=1000, used_tokens=920, used_attempts=1)
    output = _norm(result.output)
    assert _norm(expected) in output, result.output
    assert "refile this ticket smaller" in output
    assert "raise the cap deliberately" in output
    assert "nh task config" in output

    # Byte-identical to the reject path's warning for the same inputs (the
    # `_expected_message` string embeds the same task_id both times).
    reject_db = tmp_path / "nh_reject.db"
    reject_task_id = _seed_task_sync(
        reject_db, TaskStatus.AWAITING_APPROVAL, cap_tokens=1000, used_tokens=920,
        task_id=fixed_id)
    reject_runner = _make_cli_runner(reject_db, monkeypatch)
    reject_result = reject_runner.invoke(
        cli, ["reject", reject_task_id[:8], "--reason", "x"])
    assert _norm(expected) in _norm(reject_result.output)

    refreshed = _get_task_sync(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


# --------------------------------------------------------------------------- #
# AC4 — negative control: >=20% remaining triggers no warning                 #
# --------------------------------------------------------------------------- #

def test_ample_budget_warns_on_neither_path(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    # 40% spent -> 60% remaining, comfortably above the 15% floor and the
    # acceptance criteria's 20% negative-control line.
    task_id = _seed_task_sync(
        db, TaskStatus.AWAITING_APPROVAL, cap_tokens=1000, used_tokens=400)
    runner = _make_cli_runner(db, monkeypatch)

    async def _direct_check():
        async with Store(db) as store:
            t = await store.find_task(task_id)
            return await check_budget_floor(store, t, bounds=DEFAULT_BOUNDS)
    assert asyncio.run(_direct_check()) is None

    reject_result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "minor tweak"])
    assert reject_result.exit_code == 0, reject_result.output
    assert "budget floor" not in reject_result.output.lower()
    assert "refile" not in reject_result.output.lower()

    db2 = tmp_path / "nh2.db"
    task_id2 = _seed_task_sync(
        db2, TaskStatus.BLOCKED, cap_tokens=1000, used_tokens=400)
    runner2 = _make_cli_runner(db2, monkeypatch)
    reply_result = runner2.invoke(cli, ["reply", task_id2[:8], "an answer", "--no-run"])
    assert reply_result.exit_code == 0, reply_result.output
    assert "budget floor" not in reply_result.output.lower()
    assert "refile" not in reply_result.output.lower()


# --------------------------------------------------------------------------- #
# AC5 — board send-back / reply APIs surface the same warning                 #
# --------------------------------------------------------------------------- #

async def test_send_back_and_reply_apis_return_budget_warning(tmp_path):
    from no_human.api.app import app

    async with Store(tmp_path / "nh.db") as store:
        low = Task.new("low budget", repo_path="/tmp/repo")
        low.acceptance_criteria = ["it works"]
        low.config = {"lifetime_tokens": 1000, "budget_unit": "weighted"}
        await store.create_task(low)
        aid = await store.create_attempt(low.id, 1)
        await store.update_attempt(aid, tokens_used=920)
        await store.set_status(low, TaskStatus.AWAITING_APPROVAL, validate=False)

        low_reply = Task.new("low budget reply", repo_path="/tmp/repo")
        low_reply.acceptance_criteria = ["it works"]
        low_reply.config = {"lifetime_tokens": 1000, "budget_unit": "weighted"}
        await store.create_task(low_reply)
        aid2 = await store.create_attempt(low_reply.id, 1)
        await store.update_attempt(aid2, tokens_used=920)
        await store.set_status(low_reply, TaskStatus.BLOCKED, validate=False)

        ample = Task.new("ample budget", repo_path="/tmp/repo")
        ample.acceptance_criteria = ["it works"]
        ample.config = {"lifetime_tokens": 1000, "budget_unit": "weighted"}
        await store.create_task(ample)
        aid3 = await store.create_attempt(ample.id, 1)
        await store.update_attempt(aid3, tokens_used=400)
        await store.set_status(ample, TaskStatus.AWAITING_APPROVAL, validate=False)

        app.state.store = store
        app.state.config = load_config(tmp_path / "config.yaml")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            r_low = await client.post(
                f"/api/tasks/{low.id}/send-back",
                json={"message": "needs another pass"})
            r_low_reply = await client.post(
                f"/api/tasks/{low_reply.id}/reply",
                json={"answer": "here you go"})
            r_ample = await client.post(
                f"/api/tasks/{ample.id}/send-back",
                json={"message": "tiny tweak"})

    assert r_low.status_code == 200, r_low.text
    body_low = r_low.json()
    assert body_low["budget_warning"] is not None, body_low
    assert body_low["budget_warning"]["remaining_tokens"] == 80, body_low
    assert body_low["budget_warning"]["cap_tokens"] == 1000, body_low
    assert "refile this ticket smaller" in body_low["budget_warning"]["message"]

    assert r_low_reply.status_code == 200, r_low_reply.text
    body_low_reply = r_low_reply.json()
    assert body_low_reply["budget_warning"] is not None, body_low_reply
    assert body_low_reply["budget_warning"]["message"] == body_low["budget_warning"]["message"].replace(
        low.id[:8], low_reply.id[:8]
    ), (body_low_reply["budget_warning"]["message"], body_low["budget_warning"]["message"])

    assert r_ample.status_code == 200, r_ample.text
    body_ample = r_ample.json()
    assert body_ample["budget_warning"] is None, body_ample


# --------------------------------------------------------------------------- #
# AC6 — enforcement unchanged: the warning never blocks or alters the action  #
# --------------------------------------------------------------------------- #

def test_warning_never_blocks_the_action(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    task_id = _seed_task_sync(
        db, TaskStatus.AWAITING_APPROVAL, cap_tokens=1000, used_tokens=920)
    runner = _make_cli_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs better tests"])
    assert result.exit_code == 0, result.output

    refreshed = _get_task_sync(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    feedback = refreshed.context.get("send_back_feedback", [])
    assert any("better tests" in f["message"] for f in feedback)
    pending = (refreshed.context or {}).get("pending_send_back") or {}
    assert pending.get("source") == "reject", pending
    # Nothing was auto-raised: the human's own config value is untouched.
    assert refreshed.config.get("lifetime_tokens") == 1000, refreshed.config


# --------------------------------------------------------------------------- #
# AC7 — fail-open: a usage-read failure must never block the dispatch         #
# --------------------------------------------------------------------------- #

def test_warning_is_skipped_when_usage_read_fails(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    task_id = _seed_task_sync(
        db, TaskStatus.AWAITING_APPROVAL, cap_tokens=1000, used_tokens=920)
    runner = _make_cli_runner(db, monkeypatch)

    async def _boom(self, task_id):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(Store, "lifetime_usage_by_class", _boom)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs fixes"])

    assert result.exit_code == 0, result.output
    assert "sent back" in result.output.lower()
    assert "budget floor" not in result.output.lower()

    refreshed = _get_task_sync(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
