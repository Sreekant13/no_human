"""Catalog the user's Claude Code skills as proposed `skill` memories.

Skills live as ``<dir>/<name>/SKILL.md`` with YAML frontmatter (``name`` +
``description``) under ``~/.claude/skills/`` (user) and ``.claude/skills/``
(project). Cataloging them matters for two reasons:

  1. The onboarding "rules review" should surface the skills Legion knows about.
  2. The Supervisor's "I can't / skill-exists" detector (EVOLUTION_PLAN §1.3
     row 1) can only convert "I can't access X" into "use skill Y" if it knows
     skill Y exists — these confirmed `skill` memories are that knowledge.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("no_human.history")

USER_SKILLS = Path.home() / ".claude" / "skills"


@dataclass
class SkillInfo:
    name: str
    description: str
    source: str  # absolute path to the skill dir


def _parse_frontmatter(md: str) -> dict[str, str]:
    """Minimal YAML-frontmatter reader for name/description (no yaml dep needed)."""
    if not md.startswith("---"):
        return {}
    end = md.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in md[3:end].splitlines():
        m = re.match(r"\s*(name|description)\s*:\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def discover_skills(*, extra_roots: list[Path] | None = None) -> list[SkillInfo]:
    """Find SKILL.md files under the user skills dir (+ any extra roots, e.g. a
    project's .claude/skills). De-duplicated by skill name."""
    roots = [USER_SKILLS, *(extra_roots or [])]
    found: dict[str, SkillInfo] = {}
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        for skill_md in Path(root).glob("*/SKILL.md"):
            try:
                fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = fm.get("name") or skill_md.parent.name
            if name in found:
                continue
            found[name] = SkillInfo(
                name=name,
                description=fm.get("description", "")[:400],
                source=str(skill_md.parent),
            )
    skills = sorted(found.values(), key=lambda s: s.name)
    log.info("discovered %d Claude Code skills", len(skills))
    return skills
