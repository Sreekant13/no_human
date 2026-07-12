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


# C1 seed-context diet: every skill delivered to the coder costs context on
# EVERY turn of the session, so user-level skills are relevance-filtered.
# Deliberately separate from context/base.py keywords() (case-preserving code
# identifiers used as grep keys) and orchestrator._label_tokens (review-label
# similarity): those need whole identifiers, this needs sub-token overlap.
# Tokens split on non-alphanumerics — 'metrics-core-query-service' must match skill
# 'metrics-core-troubleshoot' (whitespace-split matching filtered every metrics-core skill
# from metrics-core tasks). Stopwords keep shared English filler from counting as
# relevance, or the filter would keep everything.
_STOPWORDS = frozenset(
    "the and for with that this from are was has have not you all its can "
    "use run get set new one two how what when where which your our their "
    "into onto over under out off per via any each every some more most "
    "than then them they there here also just only very will would should "
    "could may might must been being does did done make made task tasks "
    "file files code test tests".split()
)


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.split(r"[^a-z0-9]+", text.lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def skill_matches_task(skill: SkillInfo, task_text: str) -> bool:
    """True when the skill's name/description shares a meaningful token with
    the task's title/description/repo path."""
    return bool(_tokens(task_text) & _tokens(f"{skill.name} {skill.description}"))


def relevant_skill_names(
    disk_skills: list[SkillInfo],
    db_skill_titles: set[str],
    task_text: str,
    *,
    filter_user: bool = True,
) -> list[str]:
    """Names of discovered skills to deliver to the coder session.

    DB-confirmed skills are delivered separately and never repeated here.
    Project-level skills (materialized learnings, repo-local skills) are always
    kept; only user-level (~/.claude/skills) ones are relevance-filtered —
    they are the operator's personal toolbox, mostly for OTHER projects.
    """
    names: list[str] = []
    for s in disk_skills:
        if s.name in db_skill_titles:
            continue
        if (
            filter_user
            and Path(s.source).is_relative_to(USER_SKILLS)
            and not skill_matches_task(s, task_text)
        ):
            log.debug("skill %r filtered (no relevance to task)", s.name)
            continue
        names.append(s.name)
    return names
