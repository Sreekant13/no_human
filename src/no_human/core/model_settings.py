"""Model picker part 2 of 3: the ONE code path the GET/PUT API and the
``nh config models`` CLI both call. Building this as a pure module (no
FastAPI/click import) is deliberate — see ``core/model_catalog.py``'s
module docstring for the reasoning; that module answers "is this id
allowed", this one answers "what does the running system look like now, and
what happens when an operator asks to change it".

Everything here is synchronous and does blocking file I/O
(``config.set_model_ids`` writes with plain ``pathlib``/``os.replace``); an
async caller (the FastAPI handler) is expected to run it under
``asyncio.to_thread``, exactly like ``save_integration_config`` already does
for its own write.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import config as _config
from ..blockers.taxonomy import human_event
from .model_catalog import (
    CODER_BACKEND_REASON,
    REVIEWER_COST_NOTE,
    ROLES,
    _is_claude_id,
    defaults,
    options_for,
    validate,
)

__all__ = [
    "ALLOWED_KEYS",
    "ROLE_BY_KEY",
    "ModelSettingsError",
    "CONFIG_AUDIT_TASK_ID",
    "current_models",
    "models_payload",
    "apply_model_changes",
    "model_change_event",
]

#: The five ``llm.*`` config keys a PUT/CLI write may ever touch — the exact
#: allow-list, derived from ``model_catalog.ROLES`` rather than re-typed, so
#: a sixth role can never appear here without also appearing there.
ALLOWED_KEYS = frozenset(ROLES.values())

#: The reverse of ``ROLES``: config key -> role name, for turning a submitted
#: key back into the role ``model_catalog.validate`` wants.
ROLE_BY_KEY = {config_key: role for role, config_key in ROLES.items()}


class ModelSettingsError(ValueError):
    """A model-settings write was refused. The message is always safe to
    show verbatim to an operator: never a stack trace, and any value it
    quotes is exactly what the operator just typed into a model-id field."""


#: ``task_events`` has no task row for a config change — there is no task.
#: ``task_id`` carries no foreign-key constraint (see migrations), so a
#: fixed sentinel id lets ``nh logs``/the activity feed find every model
#: change in one place without inventing a task that never existed.
CONFIG_AUDIT_TASK_ID = "__config__"


def current_models(cfg_data: dict[str, Any] | None) -> dict[str, str]:
    """The five ``llm.*`` values resolved from *cfg_data*, keyed by config
    key. Falls back to the shipped default for any key absent from
    *cfg_data* — so a key missing from a raw/partial dict compares equal to
    its default rather than to ``None``, the same resolution ``load_config``
    itself performs via its deep-merge with ``DEFAULT_CONFIG``.
    """
    llm = (cfg_data or {}).get("llm") or {}
    fallback = defaults()
    return {key: llm.get(key) or fallback[key] for key in ROLES.values()}


def _option_dict(opt: Any) -> dict[str, Any]:
    return {
        "id": opt.id,
        "price_class": {
            "label": opt.price_class.label,
            "input_rate": opt.price_class.input_rate,
            "output_rate": opt.price_class.output_rate,
        },
        "is_default": opt.is_default,
        "note": opt.note,
        "requires_backend": opt.requires_backend,
        # The exact sentence a PUT of this id would be refused with —
        # options_for() leaves the coder role's `note` blank, so without this
        # a requires_backend option would otherwise reach the browser with no
        # explanatory text at all. Empty string (never null) for an option a
        # PUT would accept.
        "disabled_reason": (
            CODER_BACKEND_REASON.format(model_id=opt.id) if opt.requires_backend else ""
        ),
    }


def models_payload(
    running_cfg_data: dict[str, Any], config_path: Path
) -> dict[str, Any]:
    """The GET /api/models (and ``nh config models``) payload.

    Options and defaults come ONLY from ``model_catalog`` (never guessed or
    re-derived here); ``current`` comes from *running_cfg_data* — the config
    object the caller already has bound (the running server's ``app.state.
    config.data`` for the API, or a freshly loaded one for the CLI, which has
    no running process to ask).

    ``restart_required`` is a true file-vs-process comparison: the five
    values resolved from a FRESH read of *config_path* are compared against
    the five values resolved from *running_cfg_data*. They differ exactly
    when a write has landed on disk that the running process has not picked
    up — the same shape of check ``/api/auth/status``'s
    ``_auth_status_payload`` already performs for the auth profile.
    """
    running = current_models(running_cfg_data)
    on_disk = current_models(_config.load_config(config_path).data)

    roles = [
        {
            "role": role,
            "key": key,
            "current": running[key],
            "default": defaults()[key],
            "options": [_option_dict(opt) for opt in options_for(role)],
            # The reviewer-only cost/quality note (REVIEWER_COST_NOTE); every
            # other role carries "" — the A/B evidence only exists for this
            # role's tier decision (see model_catalog.py / config.py).
            "cost_note": REVIEWER_COST_NOTE if role == "reviewer" else "",
        }
        for role, key in ROLES.items()
    ]
    return {"roles": roles, "restart_required": on_disk != running}


def apply_model_changes(
    body: Any,
    *,
    running_cfg_data: dict[str, Any],
    config_path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Validate and (if anything actually changed) write *body* — a
    ``{config_key: model_id}`` mapping — to *config_path*.

    Returns ``(payload, changes)``: *payload* is the refreshed
    ``models_payload`` (reflecting the write, if one happened); *changes* is
    ``{key: {"old": ..., "new": ...}}`` for every key whose value actually
    changed (empty when the request was a no-op repeat — an idempotent PUT
    writes nothing and emits nothing).

    Raises :class:`ModelSettingsError` on: a non-dict body, a non-string
    value, an unrecognised key, any ``model_catalog.validate`` refusal
    (vendor pin, unpriced id, reviewer==coder collision in either
    direction), or — checked strictly AFTER ``validate`` returns ``None`` —
    a non-Claude id for the coder role, which no backend reads from
    ``llm.primary_model``.
    """
    if not isinstance(body, dict):
        raise ModelSettingsError("expected a JSON object of {config_key: model_id}")

    stripped: dict[str, str] = {}
    for key, value in body.items():
        if not isinstance(value, str):
            raise ModelSettingsError(f"{key!r} value must be a string, not {value!r}")
        stripped[key] = value.strip()

    unknown = set(stripped) - ALLOWED_KEYS
    if unknown:
        raise ModelSettingsError(
            f"unrecognised config key(s) {sorted(unknown)!r}; must be a "
            f"subset of {sorted(ALLOWED_KEYS)!r}"
        )

    on_disk = current_models(_config.load_config(config_path).data)
    # The full resolved five AFTER this write: on-disk values overlaid with
    # every submitted value — so a multi-key PUT (e.g. coder and reviewer
    # swapping models in one request) validates against where each key is
    # HEADED, not where it stood before the request.
    resolved_after = {**on_disk, **stripped}

    for key, value in stripped.items():
        role = ROLE_BY_KEY[key]
        reason = validate(role, value, current=resolved_after)
        if reason is not None:
            raise ModelSettingsError(reason)
        if role == "coder" and not _is_claude_id(value):
            raise ModelSettingsError(CODER_BACKEND_REASON.format(model_id=value))

    changes = {
        key: {"old": on_disk[key], "new": value}
        for key, value in stripped.items()
        if value != on_disk[key]
    }

    if not changes:
        return models_payload(running_cfg_data, config_path), {}

    _config.set_model_ids({key: c["new"] for key, c in changes.items()}, config_path)
    return models_payload(running_cfg_data, config_path), changes


def model_change_event(changes: dict[str, dict[str, str]]) -> dict[str, Any]:
    """The ``source=human`` task_event for a model-settings write, persisted
    against :data:`CONFIG_AUDIT_TASK_ID` via ``Store.save_events``. Built
    from the same ``human_event`` helper every other human-originated event
    uses (``blockers/taxonomy.py``), so a config change reads the same shape
    as any other human action in the activity feed.
    """
    return {
        **human_event("config_models_set", prior_status=""),
        "ts": time.time(),
        "changes": changes,
    }
