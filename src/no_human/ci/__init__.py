"""CI backends: pluggable trigger + wait + result parsing.

Selecting the backend is NOT done here and not by callers — it belongs to
``Orchestrator._resolve_ci_runner``, which ranks an explicit injection, the
project profile's ``ci`` block and the global ``ci:`` config block, in that
order, emits an advisory when a configured source cannot be built, and
escalates the run when NO configured source can be built (an ungated PR must
never be the answer to "my CI config has a typo").

Do not copy ``ci_from_config(config.data)`` into new code. This docstring
taught exactly that call for a long time while nothing in the product made it,
which is plausibly why the global ``ci:`` block ended up documented in two more
places and read by none of them: users configured a gate they never got.

Both entry points require a truthy ``ci.enabled``, which is why callers wrap
the dict rather than passing a raw profile/layer block:

    backend = ci_from_layer(layer.ci)                      # TestLayer ci block
    backend = ci_from_config({"ci": {**conf, "enabled": True}})

The orchestrator depends only on the ``CIBackend`` interface, never on a
provider CLI. Adding a provider = a new ``CIBackend`` subclass + a branch here.
"""

from __future__ import annotations

from typing import Any

from .base import (
    CIBackend, CIMisconfigured, CIResult, HumanGatedCI, JobResult, PipelineStatus,
)
from .circleci import CircleCICI
from .ghe_checkruns import GHECheckRunsCI
from .github_actions import GitHubActionsCI
from .gitlab import GitLabCI
from .jenkins import JenkinsCI
from .parser import parse_results


# Keys that name WHICH pipeline to drive. A ci block carrying none of them is
# a detection hint, not a request for a gate (see `ci_sources` docstring).
_CI_TARGET_KEYS: tuple[str, ...] = ("project", "repo", "job")


def ci_sources(config: dict[str, Any], prof: Any | None) -> list[tuple[str, dict[str, Any]]]:
    """List ``(origin, conf)`` CI sources naming a pipeline target, in
    precedence order: the project profile's ``ci`` block first (one repo,
    human-confirmed via ``nh onboard``), then the global ``ci:`` block (the
    install-wide fallback).

    Shared by ``Orchestrator._resolve_ci_runner`` (which owns emitting
    advisories + picking ``self.ci_runner``) and
    ``resolve_ci_backend_for_repo`` (used by the wake watcher's default
    ``ci_green``/``ci_terminal`` checkers) so both resolve a repo's CI to the
    SAME backend. A profile-only config must not read differently at wake
    time than it did when the orchestrator gated the run on it (audit finding
    A7, 2026-08-11): the prior wake-side implementation read only the global
    block, so a task human-gated on a profile-configured backend could never
    self-wake — ``ci_green`` was permanently unreachable for the common case.

    A profile ``ci`` block with none of ``_CI_TARGET_KEYS`` set is a detection
    hint (``nh onboard`` writes a bare ``{"backend": "gitlab"}`` on seeing a
    ``.gitlab-ci.yml``), not a claim, and is not returned as a source.
    """
    sources: list[tuple[str, dict[str, Any]]] = []
    prof_ci = dict(getattr(prof, "ci", None) or {})
    if any(str(prof_ci.get(k) or "").strip() for k in _CI_TARGET_KEYS):
        sources.append(("project profile", prof_ci))
    global_ci = dict((config or {}).get("ci") or {})
    if global_ci.get("enabled"):
        sources.append(("global config", global_ci))
    return sources


def resolve_ci_backend_for_repo(
    config: dict[str, Any], repo_path: str | None,
) -> CIBackend | None:
    """Rebuild the CI backend a repo's human-gated park was raised from —
    used at WAKE time, when only ``repo_path`` (not a live ``Orchestrator``)
    is available. Loads the on-disk ``ProjectProfile`` for *repo_path* (if
    any) and applies the exact same precedence as
    ``Orchestrator._resolve_ci_runner`` via ``ci_sources``, so a task parked
    on a profile-configured backend resolves the SAME backend here.

    Returns ``None`` when no source is configured or buildable for this
    repo — the caller (a wake checker) must then read the wake condition as
    NOT satisfied, never crash and never guess a backend.
    """
    prof = None
    if repo_path:
        try:
            from ..profile import ProjectProfile
            prof = ProjectProfile.load(repo_path)
        except Exception:  # noqa: BLE001 — a bad/missing profile is not a source
            prof = None
    for _origin, conf in ci_sources(config, prof):
        try:
            built = ci_from_config({"ci": {**conf, "enabled": True}})
        except Exception:  # noqa: BLE001 — unusable source, try the next one
            built = None
        if built is not None:
            return built
    return None


def ci_from_layer(layer_ci: dict[str, Any]) -> CIBackend | None:
    """Build a CI backend from a TestLayer's ``ci`` dict.

    Reuses ``ci_from_config`` by wrapping the layer dict as
    ``{"ci": {"enabled": True, ...}}``.  Returns ``None`` if *layer_ci*
    is empty — the layer asked for nothing.  A layer that DOES name a backend
    but no pipeline target raises ``CIMisconfigured`` from ``ci_from_config``;
    ``plan_runner`` turns that into a failed layer result naming the key,
    which is the same distinction this module makes everywhere else.
    """
    if not layer_ci or not (layer_ci.get("backend") or layer_ci.get("project")):
        return None
    return ci_from_config({"ci": {"enabled": True, **layer_ci}})


def _target(ci_conf: dict[str, Any], backend: str, *keys: str) -> str:
    """The first non-blank pipeline target among *keys*, or raise.

    Blank-but-present is the case that mattered: ``project: ""`` (and
    ``project: "   "``, which used to build a backend pointed at whitespace
    and fail later as an unrelated-looking remote error).
    """
    for key in keys:
        value = str(ci_conf.get(key) or "").strip()
        if value:
            return value
    names = " / ".join(f"ci.{k}" for k in keys)
    raise CIMisconfigured(
        f"ci.enabled is true and ci.backend is {backend!r}, but no pipeline "
        f"target is set: {names} is empty. This config asks for a CI gate that "
        f"cannot run — set ci.{keys[0]}, or set ci.enabled: false.",
        backend=backend, missing=keys,
    )


def ci_from_config(config: dict[str, Any]) -> CIBackend | None:
    """Build a CI backend from the config dict.

    Returns ``None`` for EXACTLY ONE reason: CI is switched off. Every other
    way to fail raises ``CIMisconfigured`` (see its docstring) — a caller can
    therefore read ``None`` as "the operator declined CI" without a second
    check, and cannot silently read a broken block as the same thing.
    """
    ci_conf = config.get("ci") or {}
    if not ci_conf.get("enabled"):
        return None
    backend = ci_conf.get("backend", "gitlab")

    if backend == "gitlab":
        project = _target(ci_conf, backend, "project")
        return GitLabCI(
            project=project,
            hostname=ci_conf.get("hostname", "gitlab.acme.net"),
            timeout_minutes=int(ci_conf.get("timeout_minutes", 60)),
            max_infra_retries=int(ci_conf.get("max_infra_retries", 2)),
            poll_interval=int(ci_conf.get("poll_interval", 30)),
            variables=ci_conf.get("variables") or {},
            result_parser=ci_conf.get("result_parser", "pytest"),
        )

    if backend == "github_actions":
        repo = _target(ci_conf, backend, "repo", "project")
        return GitHubActionsCI(
            repo=repo,
            workflow=ci_conf.get("workflow", ""),
            timeout_minutes=int(ci_conf.get("timeout_minutes", 60)),
            max_infra_retries=int(ci_conf.get("max_infra_retries", 2)),
            poll_interval=int(ci_conf.get("poll_interval", 30)),
            variables=ci_conf.get("variables") or {},
            result_parser=ci_conf.get("result_parser", "pytest"),
        )

    if backend == "jenkins":
        job = _target(ci_conf, backend, "job", "project")
        return JenkinsCI(
            job=job,
            base_url=ci_conf.get("base_url", "https://build.example.com"),
            mode=ci_conf.get("mode", "watch"),
            timeout_minutes=int(ci_conf.get("timeout_minutes", 60)),
            max_infra_retries=int(ci_conf.get("max_infra_retries", 2)),
            poll_interval=int(ci_conf.get("poll_interval", 30)),
            result_parser=ci_conf.get("result_parser", "surefire"),
            wake_hint=ci_conf.get("wake_hint", ""),
            auth=ci_conf.get("auth", "token"),
            crumb_path=ci_conf.get("crumb_path", "crumbIssuer/api/json"),
            storage_state_path=ci_conf.get("storage_state_path"),
            cookie_auto_refresh=bool(ci_conf.get("cookie_auto_refresh", True)),
        )

    if backend == "ghe_checkruns":
        repo = _target(ci_conf, backend, "repo", "project")
        return GHECheckRunsCI(
            repo=repo,
            hostname=ci_conf.get("hostname", ""),
            timeout_minutes=int(ci_conf.get("timeout_minutes", 60)),
            poll_interval=int(ci_conf.get("poll_interval", 30)),
        )

    if backend == "circleci":
        # Project-slug "<vcs>/<org>/<repo>" (e.g. "gh/acme/svc"). Watch-first;
        # trigger mode is opt-in via ci.mode. Token: CIRCLECI_TOKEN in .env.
        slug = _target(ci_conf, backend, "project")
        return CircleCICI(
            project_slug=slug,
            mode=ci_conf.get("mode", "watch"),
            timeout_minutes=int(ci_conf.get("timeout_minutes", 60)),
            max_infra_retries=int(ci_conf.get("max_infra_retries", 2)),
            poll_interval=int(ci_conf.get("poll_interval", 30)),
            result_parser=ci_conf.get("result_parser", "pytest"),
        )

    raise CIMisconfigured(
        f"unknown ci.backend: {backend!r} — no backend can be built, so a "
        f"config with ci.enabled: true has no gate. Supported: gitlab, "
        f"github_actions, jenkins, ghe_checkruns, circleci.",
        backend=str(backend),
    )


__all__ = [
    "CIBackend", "CIResult", "HumanGatedCI", "JobResult", "PipelineStatus",
    "GitLabCI", "GitHubActionsCI", "JenkinsCI", "GHECheckRunsCI", "CircleCICI",
    "CIMisconfigured",
    "ci_from_config", "ci_from_layer",
    "ci_sources", "resolve_ci_backend_for_repo",
    "parse_results",
]
