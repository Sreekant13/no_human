"""`.no_human.yml` — the repo's own no_human hints, carried IN the repo (C3-G2).

A repo can ship a small config so no_human works well on it without the operator
re-teaching it every time (the "agents-as-repo-config" pattern). The file is
**untrusted input**: it is written by whoever wrote the repo, not by the operator
who owns this no_human install. So the contract is deliberately narrow:

    repo config may only ADD hints or TIGHTEN safety — never the reverse.

Concretely, exactly three keys are read (:data:`_WHITELIST`); everything else in
the file is ignored:

``test_commands``       change-scoped routing rules (``glob`` → ``command`` [+ ``cwd``]).
                        NOT the default gate: ``test_cmd`` stays operator-owned
                        (``nh onboard`` proves it, a human confirms it). Rules that
                        match *everything* are rejected — that is a gate override,
                        not routing — and the onboarded profile's own rules always win.
``playbook_hints``      short advisory lines rendered into the coder prompt.
``forbidden_paths_extra``  paths the guard must also refuse. Append-only: a repo
                        can add a forbidden path, never remove one.

Nothing here can touch ``never_push_to``, models, auth, or any other gate.
Malformed, oversized, hostile, or absent input all resolve to ``{}``:
:func:`load_repo_config` never raises.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

REPO_CONFIG_RELPATH = ".no_human.yml"

# Untrusted file: read at most this many bytes, and refuse the whole thing if the
# file is bigger (a huge config is either a mistake or an attempt to bury a rule).
MAX_BYTES = 16 * 1024
MAX_HINTS = 10
MAX_HINT_CHARS = 200
MAX_FORBIDDEN_EXTRA = 20

_WHITELIST = frozenset({"test_commands", "playbook_hints", "forbidden_paths_extra"})

_GLOB_META = "*?["


def _is_scoped_glob(glob: str) -> bool:
    """A repo routing rule may only scope tests to a NAMED subdirectory: the
    segment before the first ``/`` must be a wildcard-free literal.

    Change-scoped routing fires only when EVERY changed file matches a rule, so
    a rule that can match arbitrary paths would replace the operator's proven
    gate for arbitrary changes — the one power this untrusted file must not have.
    A literal-spelling denylist is not enough: ``fnmatch`` lets ``*`` span ``/``,
    so ``?*``, ``*.py``, ``src*`` and ``[!x]*`` all match every path. Requiring a
    literal leading directory (``web/**``, ``src/api/**``) makes a rule structurally
    incapable of matching outside the directory the repo names, which is exactly
    what change-scoped routing means."""
    head, sep, _ = glob.partition("/")
    if not sep or not head or head in (".", ".."):
        return False
    return not any(c in head for c in _GLOB_META)


def load_repo_config(repo_path: str | Path) -> dict[str, Any]:
    """Read ``<repo>/.no_human.yml`` and return its whitelisted, validated keys.

    Absent / unreadable / oversized / malformed / non-mapping ⇒ ``{}``. Keys that
    survive validation empty (e.g. every rule was rejected) are dropped, so callers
    can treat a present key as "the repo said something usable".
    """
    path = Path(repo_path).expanduser() / REPO_CONFIG_RELPATH
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return {}
        raw = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — untrusted input never breaks a run
        return {}
    if not isinstance(raw, dict):
        return {}

    cfg: dict[str, Any] = {}
    rules = _clean_rules(raw.get("test_commands"))
    if rules:
        cfg["test_commands"] = rules
    hints = _clean_hints(raw.get("playbook_hints"))
    if hints:
        cfg["playbook_hints"] = hints
    extras = _clean_paths(raw.get("forbidden_paths_extra"))
    if extras:
        cfg["forbidden_paths_extra"] = extras
    return {k: v for k, v in cfg.items() if k in _WHITELIST}


def apply_repo_config(profile, cfg: dict[str, Any]):
    """Merge the repo's routing hints into ``profile``, returning a NEW profile.

    The onboarded profile always wins: repo rules apply only where it declares
    none. The store's object is never mutated. ``None`` profile / empty cfg ⇒ the
    input is returned unchanged.
    """
    if profile is None or not cfg:
        return profile
    rules = cfg.get("test_commands") or []
    if not rules or list(getattr(profile, "test_commands", None) or []):
        return profile
    return replace(profile, test_commands=rules)


# --- validation ---------------------------------------------------------- #

def _clean_rules(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        glob, command = item.get("glob"), item.get("command")
        if not isinstance(glob, str) or not isinstance(command, str):
            continue
        glob, command = glob.strip(), command.strip()
        if not command or not _is_scoped_glob(glob):
            continue
        rule = {"glob": glob, "command": command}
        cwd = item.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                continue
            cwd = cwd.strip()
            # `repo_root / cwd` must stay inside the repo (orchestrator
            # _resolve_test_target joins it verbatim).
            if not cwd or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
                continue
            rule["cwd"] = cwd
        out.append(rule)
    return out


def _clean_hints(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, str):
            continue
        hint = " ".join(item.split())[:MAX_HINT_CHARS].strip()
        if hint:
            out.append(hint)
    return out[:MAX_HINTS]


def _clean_paths(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = [p.strip() for p in raw if isinstance(p, str) and p.strip()]
    return out[:MAX_FORBIDDEN_EXTRA]
