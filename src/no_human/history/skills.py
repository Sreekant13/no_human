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
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("no_human.history")


def _resolve_user_skills() -> Path:
    """User-level Claude skills dir, honoring CLAUDE_CONFIG_DIR — the env var
    the Claude CLI uses to relocate its config dir. Falls back to
    ~/.claude/skills when the var is unset, empty/whitespace, unparseable, or
    does not point at an existing directory, so a bad value never silently
    yields zero skills."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if raw:
        try:
            cfg = Path(raw).expanduser()
            if cfg.is_dir():
                return cfg / "skills"
        except (ValueError, RuntimeError, OSError):
            pass
    return Path.home() / ".claude" / "skills"


# Resolved once at import: CLAUDE_CONFIG_DIR is a process-launch env var, fixed
# for the life of the run, so import-time resolution is correct and keeps every
# existing consumer and monkeypatch (which read/patch this module constant)
# working unchanged.
USER_SKILLS = _resolve_user_skills()


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
# similarity): those need whole identifiers, this needs marker-shaped whole-name
# matching.
# SCRUM-17 precision fix: the original sub-token/description overlap matcher
# matched on generic single words ("fields"/"design"/"ui"), so any ticket
# mentioning a common English word pulled in the operator's entire personal
# and workplace-internal skill roster (the whole design suite, plus every
# team-prefixed skill) into every coder session — a cost and de-identification
# leak. A skill now matches ONLY on a high-signal
# signal: its whole hyphenated name appears as one cohesive
# token in the task text/repo name, or its name equals the task repo's
# basename. Bare single-word skill names never match free text — they need an
# exact repo-name match or DB confirmation to be delivered.


def _cohesive_tokens(text: str) -> set[str]:
    """Identifier-shaped tokens: words plus hyphen/underscore compounds,
    lowercased. 'use the ui-styling skill' -> {'use','the','ui-styling','skill'}."""
    return set(re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text.lower()))


def skill_matches_task(skill: SkillInfo, task_text: str, *, repo_name: str = "") -> bool:
    """True only on a high-signal match:
      • the skill's exact name equals the task repo's basename, OR
      • the whole hyphenated skill name (a marker-shaped compound like
        'ui-styling', 'broker-topic-fields') appears as ONE cohesive token in
        the task text/repo name.
    Bare single-word names (design, brand, ui, slides) never match free text —
    generic English words were the leak vector. They deliver only via exact
    repo name or DB confirmation."""
    name = skill.name.strip().lower()
    if not name:
        return False
    if name == repo_name.strip().lower():
        return True
    if ("-" in name or "_" in name) and name in _cohesive_tokens(f"{task_text} {repo_name}"):
        return True
    return False


def relevant_skill_names(
    disk_skills: list[SkillInfo],
    db_skill_titles: set[str],
    task_text: str,
    *,
    repo_name: str = "",
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
            and not skill_matches_task(s, task_text, repo_name=repo_name)
        ):
            log.debug("skill %r filtered (no relevance to task)", s.name)
            continue
        names.append(s.name)
    return names
