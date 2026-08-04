"""Controlled tag vocabulary for stored lessons (B3).

Before this module, a lesson's tags were whatever happened to reach the write:
the utility model's TAGS line verbatim on the distilled path, words pulled out
of finding labels or the correction gist on the degraded path. Free tags rot —
three lessons about the same venv mistake arrive tagged ``venv``,
``interpreter`` and ``python3``, nothing groups them, ``nh learnings`` cannot
be filtered by topic, and the set of tag values grows monotonically with no
one reviewing a single addition. So the vocabulary is a REVIEWED CONSTANT:
every tag a proposal path may store is listed here, and a raw tag that maps to
nothing is dropped rather than stored.

HOW TO EXTEND IT — this is the review step, do not skip it:
  * Add a new CANONICAL tag only when a real lesson had no honest home in the
    existing set; name it as the lowercase stem a future task's text would
    actually contain (``triggers.py`` matches tags as substrings of task text,
    so ``depend`` deliberately covers "dependency" and "dependencies" where
    "dependencies" would cover neither).
  * Add an ALIAS to an existing tag freely — that is the cheap, common case.
    An alias may belong to exactly ONE canonical tag; the import-time check
    below refuses a duplicate, so a collision is a test failure, not a silent
    re-mapping of stored data.
  * Never remove or rename a canonical tag: stored rows reference tags by
    value, and a removed tag would orphan every row that carries it.

WHAT THIS DOES NOT GOVERN. ``propose_from_outcome``'s tags (``success``,
``blocker``, the task source, the blocker category) are machine-derived from
closed enums — controlled by construction, and useful to filter on verbatim.
The vocabulary exists to stop FREE text (an LLM's TAGS line, words mined from
labels) from becoming stored data; it does not re-map values that were never
free.

TRIGGERS STILL FIRE ON THE SPECIFIC WORD. Storing ``environment`` instead of
``venv`` would lose the trigger match on a future task that says "venv" — so
``learning/triggers.py`` expands a canonical tag to this module's alias set
when matching. The stored value is controlled; the trigger surface is the
alias family minus ``NON_TRIGGER_ALIASES`` (words too generic to match task
text by substring), and provenance tags contribute no trigger surface at all.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "TAG_VOCABULARY",
    "PROVENANCE_TAGS",
    "NON_TRIGGER_ALIASES",
    "sanitize_tags",
    "trigger_terms",
]

# Canonical tag -> alias terms. Aliases are the words a raw tag (or a future
# task's text) may use for the same topic; they are matched EXACTLY against a
# raw tag in `sanitize_tags`, and as substrings of task text in `triggers.py`.
# Canonical names are stems on purpose — see the module docstring.
TAG_VOCABULARY: dict[str, frozenset[str]] = {
    "test": frozenset({
        "tests", "testing", "pytest", "unittest", "assertion", "assertions",
        "fixture", "fixtures", "coverage", "flaky", "suite", "e2e",
    }),
    "build": frozenset({
        "builds", "compile", "compilation", "packaging", "wheel", "bundler",
        "makefile",
    }),
    "pipeline": frozenset({
        "ci", "cd", "workflow", "workflows", "actions", "jenkins", "runner",
        "gitlab-ci",
    }),
    "git": frozenset({
        "branch", "branches", "commit", "commits", "merge", "rebase", "push",
        "checkout", "worktree", "gitignore",
    }),
    "depend": frozenset({
        "dependency", "dependencies", "requirements", "install",
        "installation", "package", "packages", "version", "versions",
    }),
    "environment": frozenset({
        "venv", "virtualenv", "interpreter", "python", "python3", "shell",
        "path", "env",
    }),
    "lint": frozenset({
        "linting", "linter", "format", "formatting", "style", "ruff",
        "flake8", "typing", "typecheck", "mypy",
    }),
    "security": frozenset({
        "secret", "secrets", "credential", "credentials", "token", "tokens",
        "auth", "authentication", "permission", "permissions", "password",
    }),
    "database": frozenset({
        "sql", "sqlite", "schema", "migration", "migrations", "query",
        "queries",
    }),
    "api": frozenset({
        "endpoint", "endpoints", "http", "request", "requests", "response",
        "rest", "json",
    }),
    "cli": frozenset({
        "command", "commands", "flag", "flags", "option", "options",
        "terminal", "console",
    }),
    "config": frozenset({
        "configuration", "settings", "yaml", "toml", "dotenv",
    }),
    "docs": frozenset({
        "documentation", "readme", "changelog", "docstring", "comment",
        "comments",
    }),
    "error": frozenset({
        "errors", "exception", "exceptions", "traceback", "crash", "failure",
        "failures", "failing", "failed", "retry", "retries", "timeout",
        "timeouts",
    }),
    "performance": frozenset({
        "slow", "latency", "memory", "cache", "caching", "speed",
    }),
    "concurrency": frozenset({
        "async", "await", "asyncio", "race", "lock", "locking", "thread",
        "threads", "deadlock",
    }),
    "escape": frozenset({
        "escaping", "interpolation", "injection", "quoting", "sanitize",
        "sanitization",
    }),
    "container": frozenset({
        "containers", "docker", "dockerfile", "image", "images", "registry",
        "digest", "kubernetes",
    }),
    "release": frozenset({
        "deploy", "deployment", "publish", "publishing",
    }),
}

# The two queue-provenance tags (`learning/queue.py`'s ORIGIN_* values ride in
# tags so `nh learnings` can be filtered by producer). Part of the vocabulary —
# a sanitized tag list may carry them — but they alias nothing: they name where
# a lesson came from, not what it is about. For the same reason they contribute
# NO trigger terms (`trigger_terms` returns the empty set for them): every
# review-origin lesson carries `review`, and task text saying "address the
# review comments" is routine, so letting the tag match would make the entire
# review-origin shelf near-unconditional injection. A lesson tagged ONLY with
# provenance therefore never auto-injects — it has no topical trigger
# condition; it stays reachable through `nh learnings`.
PROVENANCE_TAGS: frozenset[str] = frozenset({"review", "supervisor"})

# Aliases that stay valid for `sanitize_tags` (raw tags match EXACTLY, so an
# LLM tag "env" unambiguously means environment) but are removed from the
# TRIGGER surface (task text matches by SUBSTRING, and these words ride in
# unrelated tasks constantly — "path", "env", "json", "request" would make
# their canonical tags fire on half the queue).
NON_TRIGGER_ALIASES: frozenset[str] = frozenset({
    "env", "path", "json", "request", "requests",
})


def _alias_index() -> dict[str, str]:
    """alias -> canonical, refusing ambiguity at import time. An alias that
    appears under two canonical tags would silently re-map half the lessons
    that use it; better a loud failure the moment the constant is edited."""
    index: dict[str, str] = {}
    for tag, aliases in TAG_VOCABULARY.items():
        for alias in {tag, *aliases}:
            claimed = index.get(alias)
            if claimed is not None and claimed != tag:
                raise ValueError(
                    f"tag vocabulary is ambiguous: {alias!r} maps to both "
                    f"{claimed!r} and {tag!r}")
            index[alias] = tag
    for tag in PROVENANCE_TAGS:
        claimed = index.get(tag)
        if claimed is not None and claimed != tag:
            raise ValueError(
                f"tag vocabulary is ambiguous: provenance tag {tag!r} is an "
                f"alias of {claimed!r}")
        index[tag] = tag
    return index


_ALIAS_TO_TAG: dict[str, str] = _alias_index()


def sanitize_tags(raw: Iterable[str]) -> list[str]:
    """Raw tags (an LLM's TAGS line, words mined from labels or a gist) →
    canonical vocabulary tags, order-preserving and de-duplicated.

    A raw tag that maps to nothing is DROPPED — that is the point, not a loss:
    an unmappable tag is either noise or the signal that the vocabulary needs
    a reviewed extension (see the module docstring for how)."""
    out: list[str] = []
    for tag in raw or []:
        canonical = _ALIAS_TO_TAG.get(str(tag).strip().lower())
        if canonical is not None and canonical not in out:
            out.append(canonical)
    return out


def trigger_terms(tag: str) -> frozenset[str]:
    """Every term a stored tag should trigger on: the tag itself plus, for a
    canonical vocabulary tag, its alias family minus `NON_TRIGGER_ALIASES`.
    A PROVENANCE tag contributes nothing — it names a producer, not a topic
    (see `PROVENANCE_TAGS`). A tag from outside the vocabulary (pre-B3 rows,
    outcome-path enum tags) triggers on its own value exactly as it always
    did."""
    low = (tag or "").strip().lower()
    if low in PROVENANCE_TAGS:
        return frozenset()
    aliases = TAG_VOCABULARY.get(low)
    if aliases is None:
        return frozenset({low})
    return frozenset({low, *aliases}) - NON_TRIGGER_ALIASES
