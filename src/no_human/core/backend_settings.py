"""Coder-backend picker: the ONE code path the GET/PUT API uses to read and
write ``worker.backend`` — the GLOBAL default coder backend a task falls
back to when it names no ``--backend``/composer override of its own.

Mirrors ``core/model_settings.py``'s split (a pure sync module, no FastAPI
import, run under ``asyncio.to_thread`` by the caller) and deliberately
reuses two answers that already exist rather than re-deriving either:

* "which backend does the coder use" — ``agent.backend.resolve_backend_name``
  (coder-only; every other role is pinned, see ``CLAUDE_PINNED_ROLES``).
* "can this install actually run backend X, right now" —
  ``core.runtime.assert_task_backend_usable``, the exact preflight
  ``core.runtime.build_orchestrator`` runs before the first coder turn. This
  module never reimplements that check; it only calls it and turns its
  raise/no-raise into a structured answer a UI can render.

Per-task overrides (``task.config["backend"]``, set by TaskComposer's picker
or ``nh task add --backend``) are UNCHANGED by anything here — see
``core/runtime.py``'s ``task_backend_override``. Writing through this module
only moves the GLOBAL default a task falls back to when it specifies none.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import config as _config
from ..agent.backend import SUPPORTED_BACKENDS, resolve_backend_name
from ..blockers.taxonomy import human_event
from .runtime import assert_task_backend_usable

__all__ = [
    "BackendSettingsError",
    "CONFIG_AUDIT_TASK_ID",
    "describe_backend",
    "backend_payload",
    "apply_backend_change",
    "backend_change_event",
]


class BackendSettingsError(ValueError):
    """A ``worker.backend`` write was refused. The message is always safe to
    show verbatim to an operator: never a stack trace, and any value it
    quotes is exactly what the operator just picked in the dropdown."""


#: The same sentinel task id ``model_settings.py`` uses for its own audit
#: events — reused rather than a second constant, so both kinds of
#: config-settings write show up under one synthetic id in the activity
#: feed / ``nh logs`` (``task_events`` has no real task row for either).
CONFIG_AUDIT_TASK_ID = "__config__"

#: The one backend whose config fields (``llm.local_model`` /
#: ``llm.local_base_url``) the Settings row can set. Named once here — the GET
#: payload hands it to the frontend so the view-model never hardcodes a backend
#: id, matching how the option list already flows from ``SUPPORTED_BACKENDS``.
_LOCAL_BACKEND = "local"


def describe_backend(name: str, config_data: dict[str, Any] | None) -> dict[str, Any]:
    """Whether *config_data* (this install's config) can run *name* as the
    coder backend right now, and why not if it can't.

    Calls ``core.runtime.assert_task_backend_usable`` — the exact function
    the orchestrator itself runs before the first coder turn — with *name*
    forced, so the answer a UI shows is the SAME preflight ``nh`` and the
    task-creation API already run, never a second opinion invented for this
    picker. ``assert_task_backend_usable`` has no branch for ``"claude"`` (or
    for any name it does not recognise), so those never raise and are always
    reported available — this is what lets a future fourth backend show up
    as available with no change here, the same way ``SUPPORTED_BACKENDS``
    already drives the option list with no frontend change.
    """
    try:
        assert_task_backend_usable(name, config_data)
    except Exception as exc:  # AuthError / BackendUnavailable — both already
        # written to be shown verbatim to an operator; anything else would be
        # a real bug in the check itself, not something this picker should
        # swallow, but there is no narrower common base class to catch here,
        # and a picker degrading to "unavailable: <message>" is still the
        # fail-closed choice, matching "a backend this install cannot run is
        # NOT submittable" for any check failure, expected or not.
        return {"id": name, "available": False, "reason": str(exc)}
    return {"id": name, "available": True, "reason": ""}


def backend_payload(running_cfg_data: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """The GET payload for Settings' coder-backend row: the current global
    default, every option with its LIVE availability + reason, and whether a
    write already on disk is still waiting for a restart to take effect.

    ``restart_required`` is a true file-vs-process comparison, the same
    shape of check ``model_settings.models_payload`` performs for the five
    model ids: ``worker.backend`` is read at the same construction site
    (``core.runtime.build_orchestrator``, bound to the config object the
    server loaded at start), so a write here needs the same restart.
    """
    running_current = resolve_backend_name(running_cfg_data, role="coder")
    on_disk_data = _config.load_config(config_path).data
    on_disk_current = resolve_backend_name(on_disk_data, role="coder")
    # The editable fields prefill with what is ON DISK — what the operator just
    # saved and what the NEXT run will use — not the running process's stale
    # copy (which `current`/`restart_required` above already speak for).
    llm = (on_disk_data.get("llm") or {})
    return {
        "current": running_current,
        "default": "claude",
        "options": [
            describe_backend(name, running_cfg_data) for name in SUPPORTED_BACKENDS
        ],
        "restart_required": on_disk_current != running_current,
        # The local backend's two non-secret config fields, so the Settings row
        # can prefill and edit them. The server owns everything the row needs —
        # the backend id they belong to, each field's config key, label,
        # placeholder and current value — so the frontend view-model hardcodes
        # no backend id or config key of its own. Both values are safe to echo
        # (a model id + a loopback URL; no credential ever reaches config —
        # `config._reject_api_key_in_config`).
        "local_fields": {
            "backend": _LOCAL_BACKEND,
            "fields": [
                {
                    "key": "local_model",
                    "value": str(llm.get("local_model") or ""),
                    "label": "Local model",
                    "placeholder": "the model id the local server exposes",
                },
                {
                    "key": "local_base_url",
                    "value": str(llm.get("local_base_url") or ""),
                    "label": "Local base URL",
                    "placeholder": "http://localhost:8000",
                },
            ],
        },
    }


def apply_backend_change(
    body: Any, *, running_cfg_data: dict[str, Any], config_path: Path
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Validate and (if it actually changes anything) write the new global
    default coder backend.

    Raises :class:`BackendSettingsError` on: a non-dict body, a missing or
    non-string ``"backend"`` value, a name outside ``SUPPORTED_BACKENDS``,
    or — via the SAME :func:`describe_backend` the GET payload uses, never a
    second check — a backend this install cannot currently run. Returns
    ``(payload, changes)``; ``changes`` is ``{}`` for a no-op repeat (an
    idempotent PUT writes nothing and emits nothing, same convention as
    ``model_settings.apply_model_changes``).
    """
    if not isinstance(body, dict):
        raise BackendSettingsError('expected a JSON object of {"backend": <name>}')

    changes: dict[str, dict[str, str]] = {}

    # 1) Optional local-backend field writes FIRST, so that when the same
    #    request also switches to 'local', the availability check below sees the
    #    just-written base_url/model instead of refusing on the stale on-disk
    #    values. Only changed values are written (idempotent, same convention as
    #    the backend switch); a bad URL is refused here with nothing on disk.
    on_disk_cfg = _config.load_config(config_path).data
    local_updates: dict[str, str] = {}
    for key in ("local_model", "local_base_url"):
        if key in body:
            raw = body[key]
            if not isinstance(raw, str):
                raise BackendSettingsError(f'"{key}" must be a string')
            local_updates[key] = raw.strip()
    if local_updates:
        cur_llm = on_disk_cfg.get("llm") or {}
        to_write = {
            k: v for k, v in local_updates.items() if str(cur_llm.get(k) or "") != v
        }
        if to_write:
            try:
                _config.set_local_backend_fields(to_write, config_path)
            except (ValueError, _config.AuthError) as exc:
                raise BackendSettingsError(str(exc)) from exc
            for k, v in to_write.items():
                changes[k] = {"old": str(cur_llm.get(k) or ""), "new": v}
            on_disk_cfg = _config.load_config(config_path).data

    # 2) Optional backend switch. Absent when the request only edits the local
    #    fields (e.g. the coder is already 'local' and the operator retunes it).
    value = body.get("backend")
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise BackendSettingsError('"backend" must be a non-empty string')
        value = value.strip().lower()
        if value not in SUPPORTED_BACKENDS:
            raise BackendSettingsError(
                f"{value!r} is not a supported coder backend; must be one of "
                f"{sorted(SUPPORTED_BACKENDS)!r}"
            )
        on_disk_current = resolve_backend_name(on_disk_cfg, role="coder")
        availability = describe_backend(value, on_disk_cfg)
        if not availability["available"]:
            raise BackendSettingsError(availability["reason"])
        if value != on_disk_current:
            _config.set_worker_backend(value, config_path)
            changes["backend"] = {"old": on_disk_current, "new": value}
    elif not local_updates:
        raise BackendSettingsError(
            'expected a JSON object with "backend" and/or local backend fields'
        )

    return backend_payload(running_cfg_data, config_path), changes


def backend_change_event(changes: dict[str, dict[str, str]]) -> dict[str, Any]:
    """The ``source=human`` task_event for a backend-settings write. Same
    shape as ``model_settings.model_change_event`` — see that function's
    docstring for why ``CONFIG_AUDIT_TASK_ID`` has no real task row."""
    return {
        **human_event("config_backend_set", prior_status=""),
        "ts": time.time(),
        "changes": changes,
    }
