"""P6: the target repo's own agent-instruction files are surfaced to the agent
(via the compact-instructions section that is actually read at runtime)."""

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


async def test_discovers_and_orders_instruction_files(store, tmp_path):
    repo = tmp_path / "repo"; (repo / ".github").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("Use tabs, not spaces.")
    (repo / "AGENTS.md").write_text("Run `make check` before committing.")
    (repo / ".github" / "copilot-instructions.md").write_text("Prefer composition.")  # term-ok: real file convention
    orch = _orch(store, tmp_path)

    section = orch._repo_instruction_section(repo)
    assert section is not None
    assert "AUTHORITATIVE" in section
    assert "Use tabs, not spaces." in section
    assert "Run `make check` before committing." in section
    assert "Prefer composition." in section
    # CLAUDE.md has highest precedence → appears before AGENTS.md.
    assert section.index("CLAUDE.md") < section.index("AGENTS.md")


async def test_no_instruction_files_returns_none(store, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    orch = _orch(store, tmp_path)
    assert orch._repo_instruction_section(repo) is None


async def test_large_file_is_truncated(store, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "AGENTS.md").write_text("x" * (orch_max := 3000 + 500))
    orch = _orch(store, tmp_path)
    section = orch._repo_instruction_section(repo)
    assert "… (truncated)" in section
    assert len(section) < orch_max + 500  # capped, not the full 3500


async def test_included_in_compact_instructions(store, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "AGENTS.md").write_text("PROJECT_MARKER_CONVENTION")
    orch = _orch(store, tmp_path)
    from no_human.core.task import Task
    t = Task.new("do a thing", repo_path=str(repo))
    orch._materialize_compact_instructions(repo, t)
    content = (repo / ".claude" / "instructions.md").read_text()
    assert "PROJECT_MARKER_CONVENTION" in content
    # Repo conventions come before the generic standing rules.
    assert content.index("PROJECT_MARKER_CONVENTION") < content.index("Standing rules")
