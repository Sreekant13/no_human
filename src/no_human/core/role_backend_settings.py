"""Per-role backend/model picker: the ONE code path the GET/PUT models API
uses to read and write ``llm.role_backends`` — an EXPLICIT operator choice
that overrides ``CLAUDE_PINNED_ROLES``'s default pin for a role, per
constraint amendment §6d (operator, commit ``413d76f0d``): the Claude role
pins are a DEFAULT set, not absolute — an explicit per-role Settings choice
overrides the pin for that role. Today wired for ``"reviewer"`` only;
planner/supervisor/utility/intake are future entries through this same seam.

Mirrors ``core/backend_settings.py``'s split (a pure sync module, no FastAPI
import, run under ``asyncio.to_thread`` by the caller) and deliberately
reuses two answers that already exist rather than re-deriving either:

* "can this install actually run backend X, right now" —
  ``core.backend_settings.describe_backend``, the SAME availability check the
  coder-backend picker uses (which itself calls
  ``core.runtime.assert_task_backend_usable``, the exact preflight
  ``core.runtime.build_orchestrator`` runs before the first coder turn).
  This module never reimplements that check.
* "which Claude ids may a role pick" — ``core.model_catalog.options_for``,
  the same catalog the existing per-agent model picker uses, so a
  ``role_backends`` Claude choice can never name an unpriced id ``options_for``
  itself would refuse.

The write itself happens in ``config.set_role_backend`` — THE single writer
for ``llm.role_backends`` (constraint: no task/agent/env-var/config-file
drift may set this key; ``config._reject_invalid_role_backends`` rejects
anything that reaches ``load_config`` without having gone through that
writer). This module is the validation + availability-refusal layer in
front of it, exactly as ``backend_settings.apply_backend_change`` sits in
front of ``config.set_worker_backend``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import config as _config
from ..agent.backend import SUPPORTED_BACKENDS
from ..blockers.taxonomy import human_event
from .backend_settings import describe_backend
from .model_catalog import _is_claude_id, options_for

__all__ = [
    "ROLE_BACKEND_ROLES",
    "RoleBackendError",
    "CONFIG_AUDIT_TASK_ID",
    "effective_role_backend",
    "validate_role_backend_entries",
    "apply_role_backend_change",
    "role_backend_change_event",
]

#: The roles that may appear as a key in ``llm.role_backends`` — the whitelist
#: this module (and ``config._reject_invalid_role_backends``, kept in sync)
#: enforces. §6d wires the reviewer only; planner/supervisor/utility/intake
#: are future entries through this same role-generic seam, added here as a
#: deliberate edit, never smuggled in by a stray config value.
ROLE_BACKEND_ROLES = ("reviewer",)

#: The default backend/model a role falls back to with no explicit
#: ``role_backends`` entry — always Claude, per ``CLAUDE_PINNED_ROLES``'s
#: DEFAULT pin. Keyed by the SAME ``llm.<role>_model`` config key the
#: existing per-agent picker reads, so this never re-derives a second
#: opinion of "what does the reviewer run today".
_DEFAULT_MODEL_KEY_BY_ROLE = {"reviewer": "review_model"}


class RoleBackendError(ValueError):
    """A ``role_backends`` write was refused. The message is always safe to
    show verbatim to an operator: never a stack trace, and any value it
    quotes is exactly what the operator just picked in Settings."""


#: The same sentinel task id ``model_settings.py``/``backend_settings.py``
#: use for their own audit events — reused rather than a second constant, so
#: every kind of config-settings write shows up under one synthetic id in
#: the activity feed / ``nh logs`` (``task_events`` has no real task row for
#: any of them).
CONFIG_AUDIT_TASK_ID = "__config__"


def effective_role_backend(cfg_data: dict[str, Any] | None, role: str) -> dict[str, Any]:
    """The backend/model *role* actually runs on right now, and whether that
    is the §6d default or an explicit Settings choice.

    The ONE resolver both the GET payload (``model_settings.models_payload``)
    and disclosure (task-detail models line, PR body) read — never re-derived
    a second way. Returns ``{"backend", "model", "is_default"}``:

    * an explicit, valid ``llm.role_backends[role]`` entry if one is present
      (config-load already rejects a malformed one — see
      ``config._reject_invalid_role_backends`` — so any entry reaching this
      function is well-shaped by construction; this still fails safe to the
      default rather than raising, since a resolver has no good way to refuse
      an already-loaded config);
    * otherwise ``{"backend": "claude", "model": llm.review_model or
      "claude-opus-4-8", "is_default": True}`` — reading the exact same
      ``llm.review_model`` key ``review.reviewer.AdversarialReviewer.
      from_config`` reads, so this can never drift from what actually gets
      constructed.
    """
    llm = (cfg_data or {}).get("llm") or {}
    role_backends = llm.get("role_backends") or {}
    entry = role_backends.get(role) if isinstance(role_backends, dict) else None
    if isinstance(entry, dict) and entry.get("backend") and entry.get("model"):
        return {
            "backend": str(entry["backend"]),
            "model": str(entry["model"]),
            "is_default": False,
        }

    default_key = _DEFAULT_MODEL_KEY_BY_ROLE.get(role, "review_model")
    default_model = llm.get(default_key) or "claude-opus-4-8"
    return {"backend": "claude", "model": default_model, "is_default": True}


def validate_role_backend_entries(
    entries: Any,
    *,
    on_disk_cfg: dict[str, Any],
) -> dict[str, dict[str, str] | None]:
    """Validate *entries* — a ``{role: {"backend": ..., "model": ...} |
    None}`` mapping, ``None``/``{}`` clearing a role back to the default —
    against *on_disk_cfg*, writing nothing.

    Raises :class:`RoleBackendError` on: a non-dict *entries*; a role outside
    :data:`ROLE_BACKEND_ROLES`; a non-dict entry value (other than ``None``);
    a missing/non-string ``backend`` or ``model``; a backend outside
    ``SUPPORTED_BACKENDS``; a ``backend: "claude"`` model id not offered by
    ``model_catalog.options_for(role)`` — checked whenever the backend is
    Claude, regardless of whether the model string even looks Claude-shaped,
    so a hand-edited or forged entry can't smuggle an out-of-catalog id past
    this check by claiming a non-``claude-``-shaped id under the Claude
    backend (B7); or — via the SAME :func:`core.backend_settings.
    describe_backend` the coder-backend picker uses, never a second opinion —
    a backend this install cannot currently run, with the server's own reason
    verbatim.

    Returns a normalized ``{role: {"backend": ..., "model": ...} | None}``
    mapping (``None`` for a role being cleared to the default) — the caller
    (:func:`apply_role_backend_change`) is the only thing that may act on it.
    """
    if not isinstance(entries, dict):
        raise RoleBackendError(
            'expected a JSON object of {role: {"backend": ..., "model": ...} '
            "| null}"
        )

    normalized: dict[str, dict[str, str] | None] = {}

    for role, value in entries.items():
        if role not in ROLE_BACKEND_ROLES:
            raise RoleBackendError(
                f"{role!r} is not a role that accepts a backend choice; must "
                f"be one of {sorted(ROLE_BACKEND_ROLES)!r}"
            )

        if value is None or value == {}:
            normalized[role] = None
            continue

        if not isinstance(value, dict):
            raise RoleBackendError(
                f"llm.role_backends.{role} must be a JSON object of "
                '{"backend": ..., "model": ...}, or null to clear it'
            )
        backend = value.get("backend")
        model = value.get("model")
        if not isinstance(backend, str) or not backend.strip():
            raise RoleBackendError(f"{role}.backend is required and must be a non-blank string")
        backend = backend.strip().lower()
        if not isinstance(model, str) or not model.strip():
            raise RoleBackendError(f"{role}.model is required and must be a non-blank string")
        model = model.strip()

        if backend not in SUPPORTED_BACKENDS:
            raise RoleBackendError(
                f"{backend!r} is not a supported backend; must be one of "
                f"{sorted(SUPPORTED_BACKENDS)!r}"
            )

        # B7: a `backend: "claude"` claim is checked against the catalog
        # unconditionally — not just when the model string happens to look
        # Claude-shaped (`_is_claude_id`) — so a well-shaped-but-foreign id
        # (e.g. `gpt-5-codex`) can't ride in under the Claude backend.
        if backend == "claude" or _is_claude_id(model):
            offered = {opt.id for opt in options_for(role)}
            if model not in offered:
                raise RoleBackendError(
                    f"{model!r} is not an offered model for the {role} role; "
                    f"must be one of {sorted(offered)!r}"
                )

        availability = describe_backend(backend, on_disk_cfg)
        if not availability["available"]:
            raise RoleBackendError(availability["reason"])

        normalized[role] = {"backend": backend, "model": model}

    return normalized


def apply_role_backend_change(
    entries: Any,
    *,
    running_cfg_data: dict[str, Any],
    config_path: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    """Validate (via :func:`validate_role_backend_entries`) and, for every
    role whose resolved value actually changes, write *entries* into
    ``llm.role_backends``.

    All validation happens before this function writes anything — see
    :func:`validate_role_backend_entries` for the full list of refusals.

    Returns ``(changes, effective)``: *changes* is
    ``{role: {"old": {...} | None, "new": {...} | None}}`` for every role
    whose resolved value actually changed (empty when the request was a
    no-op repeat — an idempotent PUT writes nothing and emits nothing, same
    convention as ``model_settings.apply_model_changes`` /
    ``backend_settings.apply_backend_change``); *effective* is
    ``{role: effective_role_backend(...)}`` for every role touched, reflecting
    the write.
    """
    on_disk_cfg = _config.load_config(config_path).data
    normalized = validate_role_backend_entries(entries, on_disk_cfg=on_disk_cfg)

    changes: dict[str, dict[str, str]] = {}
    effective: dict[str, dict[str, Any]] = {}

    for role, new in normalized.items():
        before = effective_role_backend(on_disk_cfg, role)
        old = None if before["is_default"] else {
            "backend": before["backend"],
            "model": before["model"],
        }

        if new == old:
            effective[role] = before
            continue

        _config.set_role_backend(
            role,
            new["backend"] if new else None,
            new["model"] if new else None,
            config_path=config_path,
        )
        on_disk_cfg = _config.load_config(config_path).data
        changes[role] = {"old": old, "new": new}
        effective[role] = effective_role_backend(on_disk_cfg, role)

    return changes, effective


def role_backend_change_event(changes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The ``source=human`` task_event for a ``role_backends`` write,
    persisted against :data:`CONFIG_AUDIT_TASK_ID` via ``Store.save_events``.
    Built from the same ``human_event`` helper every other human-originated
    event uses (``blockers/taxonomy.py``), so this reads the same shape as
    any other human action in the activity feed."""
    return {
        **human_event("config_role_backend_set", prior_status=""),
        "ts": time.time(),
        "changes": changes,
    }
