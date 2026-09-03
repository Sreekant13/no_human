"""P1 brain hygiene: the memories-table quarantine + write-time provenance
gate (`learning/provenance.py`, `core/db.py`, `context/sessions.py`,
`api/app.py`, `cli/commands.py`).

Planted employer-context rows are built from `MEMORY_NEEDLE_HEX`'s own
hex-decoded fragments (`no_human.eval._vendor_terms_private`) — never a
spelled literal — so this file cannot become the leak it exists to guard
against. One vendor-class row uses a PUBLIC competitor product name (already
spelled freely elsewhere in this suite, e.g. tests/test_bench_publish.py,
per `eval/vendor_terms.py`'s own docstring) rather than any employer term.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human.core.db import Store
from no_human.core.task import Task

_vtp = pytest.importorskip(
    "no_human.eval._vendor_terms_private",
    reason="private supplement not distributed in this checkout",
)
MEMORY_NEEDLE_HEX = _vtp.MEMORY_NEEDLE_HEX

from no_human.learning.provenance import (  # noqa: E402
    InventoryError,
    NEEDLE_CLASS_PHRASES,
    NEEDLE_CLASS_PROJECT,
    NEEDLE_CLASS_VENDOR,
    quarantine_reason,
    scan_memories,
)
import no_human.learning.provenance as provenance  # noqa: E402


def _decoded(hexes: list[str]) -> list[str]:
    return [bytes.fromhex(h).decode() for h in hexes]


_PHRASES = _decoded(MEMORY_NEEDLE_HEX)
# The compound store/app-name fragment — the one the word-boundary test below
# targets (see MEMORY_NEEDLE_HEX / test_deidentify_p1_repo_names.py for its
# hex-encoded shape; never spelled here). Located by content rather than
# position, so a reorder of MEMORY_NEEDLE_HEX does not silently break this
# test.
_STORE_APP_FRAGMENT = next(f for f in _PHRASES if "store" in f.lower())


def _bare_form(fragment: str) -> str:
    """A concrete string the fragment's regex matches: its `[..]` character
    class collapsed to one literal separator character."""
    return re.sub(r"\[.*?\]", "-", fragment)


def _make_runner(db_path, monkeypatch):
    """Mirrors tests/test_rules_skills.py's `_make_runner`."""
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
    return CliRunner()


# --------------------------------------------------------------------------- #
# Inventory: per-class + union counts                                         #
# --------------------------------------------------------------------------- #

async def test_scan_reports_counts_per_class_and_union(tmp_path):
    bare = _bare_form(_STORE_APP_FRAGMENT)
    allowlist = ["/allowed/repo"]
    async with Store(tmp_path / "t.db") as store:
        vendor_id = await store.add_memory(
            mem_type="rule", title="windsurf integration notes",  # term-ok: public competitor term
            content="x", confirmed=True, source="board")
        phrase_id = await store.add_memory(
            mem_type="rule", title=f"{bare} REST client", content="x",
            confirmed=True, source="board")
        project_id = await store.add_memory(
            mem_type="rule", title="repo note", content="x",
            project="/other/repo", confirmed=True, source="board")
        both_id = await store.add_memory(
            mem_type="rule", title=f"windsurf + {bare} notes",  # term-ok: public competitor term
            content="x", confirmed=True, source="board")

        inv = await scan_memories(store, allowlist=allowlist)

    assert inv.per_class[NEEDLE_CLASS_VENDOR] == 2      # vendor_id + both_id
    assert inv.per_class[NEEDLE_CLASS_PHRASES] == 2     # phrase_id + both_id
    assert inv.per_class[NEEDLE_CLASS_PROJECT] == 1     # project_id
    assert inv.union_total == 4                          # each row once
    assert set(inv.union_ids) == {vendor_id, phrase_id, project_id, both_id}


def test_scan_reports_counts_by_class_index_and_needle_index(tmp_path, monkeypatch):
    """The blocking Criterion 1 gap: a PR body can only ever quote counts BY
    CLASS INDEX, never a class label — so `Inventory` must expose that shape
    directly, and `nh memories scan --json` must carry it.

    SYNC on purpose — see test_quarantined_rows_absent_from_cli_output:
    CliRunner.invoke() calls asyncio.run() internally, which cannot nest
    inside an async-def test's own event loop."""
    import asyncio

    bare = _bare_form(_STORE_APP_FRAGMENT)
    needle_index = _PHRASES.index(_STORE_APP_FRAGMENT) + 1
    allowlist = ["/allowed/repo"]
    db_path = tmp_path / "t.db"

    async def _seed_and_scan():
        async with Store(db_path) as store:
            await store.add_memory(
                mem_type="rule", title="windsurf integration notes",  # term-ok: public competitor term
                content="x", confirmed=True, source="board")
            await store.add_memory(
                mem_type="rule", title=f"{bare} REST client", content="x",
                confirmed=True, source="board")
            await store.add_memory(
                mem_type="rule", title="repo note", content="x",
                project="/other/repo", confirmed=True, source="board")
            return await scan_memories(store, allowlist=allowlist)

    inv = asyncio.run(_seed_and_scan())

    assert inv.per_class_index == {
        1: inv.per_class[NEEDLE_CLASS_VENDOR],
        2: inv.per_class[NEEDLE_CLASS_PHRASES],
        3: inv.per_class[NEEDLE_CLASS_PROJECT],
    }
    assert inv.per_class_index == {1: 1, 2: 1, 3: 1}
    assert inv.per_needle_index[needle_index] == 1
    assert inv.union_total == 3

    runner = _make_runner(db_path, monkeypatch)
    from no_human.cli.commands import cli
    result = runner.invoke(cli, ["memories", "scan", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert set(body["per_class_index"].keys()) == {"1", "2", "3"}
    assert body["per_class_index"]["2"] == 1
    assert body["per_needle_index"][str(needle_index)] == 1


def test_word_boundary_does_not_match_ordinary_english():
    bare = _bare_form(_STORE_APP_FRAGMENT)
    evasion = "re" + bare + "roval"   # the "restore-approval" shape
    assert quarantine_reason(title="ok", content=evasion) is None
    assert quarantine_reason(title="ok", content=bare) == NEEDLE_CLASS_PHRASES


# --------------------------------------------------------------------------- #
# Known-positive control — the empty-table trap                               #
# --------------------------------------------------------------------------- #

async def test_scan_fails_when_it_scans_an_empty_table_while_the_ui_lists_items(tmp_path):
    async def _empty_row_source():
        return []

    async with Store(tmp_path / "t.db") as store:
        await store.add_memory(mem_type="rule", title="a real confirmed rule",
                                content="x", confirmed=True, source="board")
        with pytest.raises(InventoryError):
            await scan_memories(store, row_source=_empty_row_source)


async def test_scan_control_passes_on_the_live_store(tmp_path):
    async with Store(tmp_path / "t.db") as store:
        await store.add_memory(mem_type="rule", title="a real confirmed rule",
                                content="x", confirmed=True, source="board")
        inv = await scan_memories(store)   # default row_source: the live table
    assert inv.total_rows >= 1


async def test_planted_row_shows_red_then_clean_after_removal(tmp_path):
    """The reproducible twin of the live backfill transcript: scan a store of
    only clean rows (0), plant one flagged row BYPASSING the write-time gate
    (raw INSERT with quarantined=0 — mirrors a row written before this
    feature existed, or inserted outside `add_memory`, exactly the live
    procedure), show the scan turns RED, remove the row, show it goes clean
    again."""
    bare = _bare_form(_STORE_APP_FRAGMENT)
    needle_index = _PHRASES.index(_STORE_APP_FRAGMENT) + 1
    async with Store(tmp_path / "t.db") as store:
        await store.add_memory(mem_type="rule", title="clean rule",
                                content="nothing flagged here", confirmed=True,
                                source="board")
        inv_before = await scan_memories(store)
        assert inv_before.union_total == 0

        planted_id = uuid.uuid4().hex
        await store.db.execute(
            """INSERT INTO memories
                 (id, type, title, content, confirmed, source, tags,
                  quarantined)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (planted_id, "rule", f"{bare} REST client", "x", 1, "board",
             json.dumps([]), 0),
        )
        await store.db.commit()

        inv_red = await scan_memories(store)
        assert inv_red.union_total == 1
        assert inv_red.union_ids == [planted_id]
        assert inv_red.per_class[NEEDLE_CLASS_PHRASES] == 1
        assert inv_red.per_class_index[2] == 1
        assert inv_red.per_needle_index[needle_index] == 1

        await store.db.execute(
            "DELETE FROM memories WHERE id = ?", (planted_id,))
        await store.db.commit()

        inv_clean = await scan_memories(store)
        assert inv_clean.union_total == 0


# --------------------------------------------------------------------------- #
# Quarantine excluded from UI listings (list_memories, API, CLI)              #
# --------------------------------------------------------------------------- #

async def test_quarantined_rows_absent_from_list_memories_and_api(tmp_path):
    db_path = tmp_path / "t.db"
    store = await Store(db_path).connect()
    try:
        rule_id = await store.add_memory(mem_type="rule", title="a confirmed rule",
                                          content="x", confirmed=True, source="board")
        skill_id = await store.add_memory(mem_type="skill", title="a confirmed skill",
                                           content="x", confirmed=True, source="board")
        assert await store.set_quarantine(rule_id, True, "manual test flag")
        assert await store.set_quarantine(skill_id, True, "manual test flag")

        # list_memories
        visible_ids = {m["id"] for m in await store.list_memories()}
        assert rule_id not in visible_ids
        assert skill_id not in visible_ids

        # API
        from no_human.api.app import app
        from no_human.config import load_config
        app.state.store = store
        app.state.config = load_config(tmp_path / "config.yaml")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            r = await client.get("/api/rules")
            assert rule_id not in {m["id"] for m in r.json()}
            r = await client.get("/api/skills")
            assert skill_id not in {m["id"] for m in r.json()}
            r = await client.get("/api/learnings?active=true")
            active_ids = {m["id"] for m in r.json()}
            assert rule_id not in active_ids and skill_id not in active_ids
    finally:
        await store.close()


def test_quarantined_rows_absent_from_cli_output(tmp_path, monkeypatch):
    # SYNC on purpose: `nh rules list` / `nh skills list` run their own body
    # via `asyncio.run(...)`, which raises "cannot be called from a running
    # event loop" if invoked from inside an async test (pytest-asyncio's own
    # loop). See tests/test_rules_skills.py's CLI tests for the same shape.
    import asyncio

    db_path = tmp_path / "t.db"

    async def _seed():
        async with Store(db_path) as store:
            rule_id = await store.add_memory(
                mem_type="rule", title="a confirmed rule", content="x",
                confirmed=True, source="board")
            skill_id = await store.add_memory(
                mem_type="skill", title="a confirmed skill", content="x",
                confirmed=True, source="board")
            await store.set_quarantine(rule_id, True, "manual test flag")
            await store.set_quarantine(skill_id, True, "manual test flag")
            return rule_id, skill_id

    rule_id, skill_id = asyncio.run(_seed())

    runner = _make_runner(db_path, monkeypatch)
    from no_human.cli.commands import cli
    out = runner.invoke(cli, ["rules", "list"])
    assert rule_id[:8] not in out.output
    out = runner.invoke(cli, ["skills", "list"])
    assert skill_id[:8] not in out.output


# --------------------------------------------------------------------------- #
# Excluded from rule injection (both routes)                                  #
# --------------------------------------------------------------------------- #

async def test_quarantined_never_injected(tmp_path):
    from no_human.core.orchestrator import Orchestrator
    from no_human.context.sessions import SessionsSource

    async with Store(tmp_path / "t.db") as store:
        mem_id = await store.add_memory(
            mem_type="rule", title="widgetgadget rule",
            content="applies to widgetgadget tasks", confirmed=True, source="board")
        assert await store.set_quarantine(mem_id, True, "manual test flag")

        orch = Orchestrator(store, {}, None, None)
        task = Task.new("Fix widgetgadget rendering")
        all_scoped, triggered = await orch._load_active_memories(task)
        assert mem_id not in {m["id"] for m in all_scoped}
        assert mem_id not in {m["id"] for m in triggered}

        chunks = await SessionsSource(store).gather(task)
        assert not any("widgetgadget rule" in c.title for c in chunks)


# --------------------------------------------------------------------------- #
# Excluded from export paths                                                  #
# --------------------------------------------------------------------------- #

async def test_quarantined_never_written_to_skill_files(tmp_path):
    from no_human.core.orchestrator import Orchestrator

    async with Store(tmp_path / "t.db") as store:
        skill_id = await store.add_memory(
            mem_type="skill", title="widgetgadget-skill",
            content="how to do the widgetgadget thing", confirmed=True, source="board")
        assert await store.set_quarantine(skill_id, True, "manual test flag")

        orch = Orchestrator(store, {}, None, None)
        task = Task.new("Use the widgetgadget skill")
        await orch._load_active_memories(task)

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        orch._materialize_skills(repo_path)

    skill_path = repo_path / ".claude" / "skills" / "widgetgadget-skill" / "SKILL.md"
    assert not skill_path.exists()


# --------------------------------------------------------------------------- #
# Rows preserved                                                              #
# --------------------------------------------------------------------------- #

async def test_quarantine_preserves_the_row(tmp_path):
    async with Store(tmp_path / "t.db") as store:
        mem_id = await store.add_memory(mem_type="rule", title="preserved rule",
                                         content="keep me around", confirmed=True,
                                         source="board")
        before = await store.find_memory(mem_id)
        assert await store.set_quarantine(mem_id, True, "manual test flag")
        all_rows = await store.list_memories(include_quarantined=True)

    assert len(all_rows) == 1
    row = all_rows[0]
    assert row["id"] == mem_id
    assert row["content"] == before["content"]
    assert row["file_path"] == before["file_path"]
    assert row["quarantined"] == 1


# --------------------------------------------------------------------------- #
# Write-time gate                                                             #
# --------------------------------------------------------------------------- #

async def test_ingest_of_planted_employer_memory_is_quarantined(tmp_path):
    async with Store(tmp_path / "t.db") as store:
        mem_id = await store.add_memory(
            mem_type="rule", title="windsurf setup notes",  # term-ok: public competitor term
            content="x", confirmed=True, source="board")
        row = await store.find_memory(mem_id)
    assert row["quarantined"] == 1


async def test_clean_memory_is_not_quarantined(tmp_path):
    async with Store(tmp_path / "t.db") as store:
        mem_id = await store.add_memory(
            mem_type="rule", title="always wrap errors", content="be nice",
            confirmed=True, source="board")
        row = await store.find_memory(mem_id)
    assert row["quarantined"] == 0


async def test_provenance_field_recorded(tmp_path):
    async with Store(tmp_path / "t.db") as store:
        clean_id = await store.add_memory(
            mem_type="rule", title="always wrap errors", content="be nice",
            project="/some/repo", origin="review", confirmed=True, source="board")
        clean_row = await store.find_memory(clean_id)
        prov = json.loads(clean_row["provenance"])
        assert prov["project"] == "/some/repo"
        assert prov["context"] == "review"
        assert prov.get("ingested_at")
        assert prov["quarantine_reason"] is None

        bad_id = await store.add_memory(
            mem_type="rule", title="windsurf note",  # term-ok: public competitor term
            content="x", confirmed=True, source="board")
        bad_row = await store.find_memory(bad_id)
        prov2 = json.loads(bad_row["provenance"])
        assert prov2["quarantine_reason"] == NEEDLE_CLASS_VENDOR


# --------------------------------------------------------------------------- #
# Gate fails closed                                                           #
# --------------------------------------------------------------------------- #

def test_matcher_error_quarantines(monkeypatch):
    def _boom(text):
        raise RuntimeError("matcher exploded")
    monkeypatch.setattr(provenance, "find_banned_terms", _boom)
    reason = provenance.quarantine_reason(title="anything", content="anything")
    assert reason == NEEDLE_CLASS_VENDOR + provenance.MATCHER_ERROR_SUFFIX


async def test_matcher_error_quarantines_at_write_time(tmp_path, monkeypatch):
    def _boom(text):
        raise RuntimeError("matcher exploded")
    monkeypatch.setattr(provenance, "find_banned_terms", _boom)
    async with Store(tmp_path / "t.db") as store:
        mem_id = await store.add_memory(mem_type="rule", title="anything",
                                         content="anything", confirmed=True,
                                         source="board")
        row = await store.find_memory(mem_id)
    assert row["quarantined"] == 1


# --------------------------------------------------------------------------- #
# Project allowlist                                                           #
# --------------------------------------------------------------------------- #

def test_project_allowlist_unset_is_inert(monkeypatch):
    monkeypatch.delenv(provenance.ALLOWLIST_ENV_VAR, raising=False)
    assert provenance.project_allowlist() is None
    assert provenance.quarantine_reason(
        title="x", content="y", project="/anything/at/all") is None


def test_non_allowlisted_project_is_quarantined(monkeypatch):
    monkeypatch.setenv(provenance.ALLOWLIST_ENV_VAR, "/git/allowed/repo")
    allow = provenance.project_allowlist()
    assert allow == ["/git/allowed/repo"]
    assert provenance.quarantine_reason(
        title="x", content="y", project="/git/other/repo", allowlist=allow,
    ) == NEEDLE_CLASS_PROJECT
    assert provenance.quarantine_reason(
        title="x", content="y", project="/git/allowed/repo/sub", allowlist=allow,
    ) is None


def test_allowlist_sibling_prefix_is_not_trusted():
    """Round-3 review advisory 1: a bare `project.startswith(prefix)` admits
    a SIBLING repo whose name happens to share the allowlisted prefix as a
    substring (`/git/allowed/repo-evil` is not under `/git/allowed/repo`).
    The boundary must be exact-match or the next path separator."""
    allowlist = ["/git/allowed/repo"]
    assert provenance.quarantine_reason(
        title="x", content="y", project="/git/allowed/repo-evil",
        allowlist=allowlist,
    ) == NEEDLE_CLASS_PROJECT
    assert provenance.quarantine_reason(
        title="x", content="y", project="/git/allowed/repo/sub",
        allowlist=allowlist,
    ) is None
    assert provenance.quarantine_reason(
        title="x", content="y", project="/git/allowed/repo",
        allowlist=allowlist,
    ) is None


async def test_add_memory_wires_the_env_allowlist_without_an_explicit_arg(
    tmp_path, monkeypatch,
):
    """The gap the reviewer caught: `add_memory` must resolve the allowlist
    itself when no caller passes `project_allowlist` — and no caller in this
    repo does (`db.py`'s own docstring on `add_memory`). Proves the class is
    wired at the real write chokepoint, not just reachable through
    `quarantine_reason`/`project_allowlist` called directly."""
    monkeypatch.setenv(provenance.ALLOWLIST_ENV_VAR, "/git/allowed/repo")
    async with Store(tmp_path / "t.db") as store:
        outside_id = await store.add_memory(
            mem_type="rule", title="always wrap errors", content="be nice",
            project="/git/other/repo", confirmed=True, source="board")
        inside_id = await store.add_memory(
            mem_type="rule", title="always wrap errors", content="be nice",
            project="/git/allowed/repo/sub", confirmed=True, source="board")
        outside_row = await store.find_memory(outside_id)
        inside_row = await store.find_memory(inside_id)
    assert outside_row["quarantined"] == 1
    assert json.loads(outside_row["provenance"])["quarantine_reason"] == (
        NEEDLE_CLASS_PROJECT)
    assert inside_row["quarantined"] == 0


async def test_add_memory_explicit_empty_allowlist_overrides_env(
    tmp_path, monkeypatch,
):
    """An explicit `project_allowlist=[]` (falsy) overrides the env var
    rather than falling back to it — `add_memory`'s docstring says an
    explicit value always wins."""
    monkeypatch.setenv(provenance.ALLOWLIST_ENV_VAR, "/git/allowed/repo")
    async with Store(tmp_path / "t.db") as store:
        mem_id = await store.add_memory(
            mem_type="rule", title="always wrap errors", content="be nice",
            project="/git/other/repo", confirmed=True, source="board",
            project_allowlist=[])
        row = await store.find_memory(mem_id)
    assert row["quarantined"] == 0


# --------------------------------------------------------------------------- #
# Backfill idempotent                                                         #
# --------------------------------------------------------------------------- #

def test_scan_apply_is_idempotent(tmp_path, monkeypatch):
    # SYNC on purpose — see test_quarantined_rows_absent_from_cli_output.
    import asyncio

    db_path = tmp_path / "t.db"

    async def _seed():
        async with Store(db_path) as store:
            # `add_memory` already quarantines this at write time (the gate
            # under test), so — to exercise the BACKFILL path specifically —
            # reset the flag afterward to simulate a row written before this
            # feature existed: quarantine-worthy content, `quarantined = 0`.
            mem_id = await store.add_memory(
                mem_type="rule", title="windsurf note",  # term-ok: public competitor term
                content="x", confirmed=True, source="board")
            await store.set_quarantine(mem_id, False, None)
            await store.add_memory(mem_type="rule", title="clean rule", content="y",
                                    confirmed=True, source="board")

    asyncio.run(_seed())

    runner = _make_runner(db_path, monkeypatch)
    from no_human.cli.commands import cli
    r1 = runner.invoke(cli, ["memories", "scan", "--apply", "--json"])
    assert r1.exit_code == 0, r1.output
    out1 = json.loads(r1.output)
    assert out1["newly_flagged"] == 1

    r2 = runner.invoke(cli, ["memories", "scan", "--apply", "--json"])
    assert r2.exit_code == 0, r2.output
    out2 = json.loads(r2.output)
    assert out2["newly_flagged"] == 0
    assert out2["union_total"] == out1["union_total"]


# --------------------------------------------------------------------------- #
# API counts                                                                   #
# --------------------------------------------------------------------------- #

async def test_quarantine_counts_endpoint(tmp_path):
    store = await Store(tmp_path / "t.db").connect()
    try:
        rid = await store.add_memory(mem_type="rule", title="r", content="x",
                                      confirmed=True, source="board")
        sid = await store.add_memory(mem_type="skill", title="s", content="x",
                                      confirmed=True, source="board")
        await store.set_quarantine(rid, True, "manual test flag")
        await store.set_quarantine(sid, True, "manual test flag")

        from no_human.api.app import app
        from no_human.config import load_config
        app.state.store = store
        app.state.config = load_config(tmp_path / "config.yaml")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            r = await client.get("/api/memories/quarantine")
            assert r.status_code == 200
            body = r.json()
            assert body["rules"] == 1
            assert body["skills"] == 1
            assert body["learnings"] == 2
            assert body["total"] == 2
    finally:
        await store.close()


async def test_quarantine_counts_total_spans_all_types(tmp_path):
    """Round-3 review advisory 2: `total` is the ALL-TYPES quarantined count,
    not `rules + skills` — a quarantined row of a type outside the four
    Rules/Skills panel types (e.g. `lesson`) must still show up in `total`
    and `learnings` while being invisible to both panel subsets, pinning the
    documented `total >= rules + skills` relationship."""
    store = await Store(tmp_path / "t.db").connect()
    try:
        rid = await store.add_memory(mem_type="rule", title="r", content="x",
                                      confirmed=True, source="board")
        other_id = await store.add_memory(mem_type="lesson", title="l",
                                           content="x", confirmed=True,
                                           source="board")
        await store.set_quarantine(rid, True, "manual test flag")
        await store.set_quarantine(other_id, True, "manual test flag")

        from no_human.api.app import app
        from no_human.config import load_config
        app.state.store = store
        app.state.config = load_config(tmp_path / "config.yaml")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            r = await client.get("/api/memories/quarantine")
            assert r.status_code == 200
            body = r.json()
            assert body["rules"] == 1
            assert body["skills"] == 0
            assert body["total"] == 2                   # all-types: rule + lesson
            assert body["learnings"] == body["total"]
            assert body["total"] > body["rules"] + body["skills"]
    finally:
        await store.close()
