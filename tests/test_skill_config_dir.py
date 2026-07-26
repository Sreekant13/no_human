"""CLAUDE_CONFIG_DIR must relocate the user-skills root used by skill discovery.

Skill discovery previously hardcoded ~/.claude/skills, ignoring CLAUDE_CONFIG_DIR
— the env var the Claude CLI itself uses to relocate its config dir. On any
machine running a non-default config dir, this meant the WRONG user skill
toolbox was read (or the right one silently ignored). These tests cover the
resolver in isolation from the existing tests/test_context_diet.py and
tests/test_rules_skills.py suites, which patch `sk.USER_SKILLS` directly and
must keep passing unchanged as the regression guard for consumer behavior.
"""

from __future__ import annotations

from pathlib import Path

import no_human.history.skills as sk
from no_human.history.skills import SkillInfo, relevant_skill_names


def _write_skill(root: Path, name: str, description: str = "d") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody\n",
        encoding="utf-8",
    )


def test_user_skills_root_honors_config_dir(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    skills_root = cfg_dir / "skills"
    _write_skill(skills_root, "relocated-skill")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))

    assert sk._resolve_user_skills() == skills_root

    monkeypatch.setattr(sk, "USER_SKILLS", sk._resolve_user_skills())
    found = sk.discover_skills()
    assert "relocated-skill" in [s.name for s in found]


def test_user_skills_root_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert sk._resolve_user_skills() == Path.home() / ".claude" / "skills"


def test_config_dir_missing_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    assert sk._resolve_user_skills() == Path.home() / ".claude" / "skills"


def test_config_dir_blank_falls_back(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
    assert sk._resolve_user_skills() == Path.home() / ".claude" / "skills"


def test_config_dir_file_not_dir_falls_back(tmp_path, monkeypatch):
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("nope", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(bogus))
    assert sk._resolve_user_skills() == Path.home() / ".claude" / "skills"


def test_relevance_filter_unchanged_under_config_dir(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    skills_root = cfg_dir / "skills"
    skills_root.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(sk, "USER_SKILLS", sk._resolve_user_skills())

    user_skill_src = skills_root / "metrics-core-troubleshoot"
    project_skill_src = tmp_path / "repo" / ".claude" / "skills" / "project-helper"

    disk_skills = [
        SkillInfo(name="metrics-core-troubleshoot", description="d", source=str(user_skill_src)),
        SkillInfo(name="project-helper", description="d", source=str(project_skill_src)),
    ]

    names = relevant_skill_names(disk_skills, set(), "Add a formatBytes helper")

    assert "metrics-core-troubleshoot" not in names
    assert "project-helper" in names
