"""CI backends: pluggable trigger + wait + result parsing.

Selecting the backend is NOT done here and not by callers — it belongs to
``Orchestrator._resolve_ci_runner``, which ranks an explicit injection, the
project profile's ``ci`` block and the global ``ci:`` config block, in that
order, and raises an advisory when a configured source cannot be built.

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

from .base import CIBackend, CIResult, HumanGatedCI, JobResult, PipelineStatus
from .circleci import CircleCICI
from .ghe_checkruns import GHECheckRunsCI
from .github_actions import GitHubActionsCI
from .gitlab import GitLabCI
from .jenkins import JenkinsCI
from .parser import parse_results


def ci_from_layer(layer_ci: dict[str, Any]) -> CIBackend | None:
    """Build a CI backend from a TestLayer's ``ci`` dict.

    Reuses ``ci_from_config`` by wrapping the layer dict as
    ``{"ci": {"enabled": True, ...}}``.  Returns ``None`` if *layer_ci*
    is empty or missing ``backend``/``project``.
    """
    if not layer_ci or not (layer_ci.get("backend") or layer_ci.get("project")):
        return None
    return ci_from_config({"ci": {"enabled": True, **layer_ci}})


def ci_from_config(config: dict[str, Any]) -> CIBackend | None:
    """Build a CI backend from the config dict, or None if CI is disabled."""
    ci_conf = config.get("ci") or {}
    if not ci_conf.get("enabled"):
        return None
    backend = ci_conf.get("backend", "gitlab")

    if backend == "gitlab":
        project = ci_conf.get("project", "")
        if not project:
            return None
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
        repo = ci_conf.get("repo") or ci_conf.get("project", "")
        if not repo:
            return None
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
        job = ci_conf.get("job") or ci_conf.get("project", "")
        if not job:
            return None
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
        repo = ci_conf.get("repo") or ci_conf.get("project", "")
        if not repo:
            return None
        return GHECheckRunsCI(
            repo=repo,
            hostname=ci_conf.get("hostname", ""),
            timeout_minutes=int(ci_conf.get("timeout_minutes", 60)),
            poll_interval=int(ci_conf.get("poll_interval", 30)),
        )

    if backend == "circleci":
        # Project-slug "<vcs>/<org>/<repo>" (e.g. "gh/acme/svc"). Watch-first;
        # trigger mode is opt-in via ci.mode. Token: CIRCLECI_TOKEN in .env.
        slug = ci_conf.get("project", "")
        if not slug:
            return None
        return CircleCICI(
            project_slug=slug,
            mode=ci_conf.get("mode", "watch"),
            timeout_minutes=int(ci_conf.get("timeout_minutes", 60)),
            max_infra_retries=int(ci_conf.get("max_infra_retries", 2)),
            poll_interval=int(ci_conf.get("poll_interval", 30)),
            result_parser=ci_conf.get("result_parser", "pytest"),
        )

    raise ValueError(f"unknown ci.backend: {backend!r}")


__all__ = [
    "CIBackend", "CIResult", "HumanGatedCI", "JobResult", "PipelineStatus",
    "GitLabCI", "GitHubActionsCI", "JenkinsCI", "GHECheckRunsCI", "CircleCICI",
    "ci_from_config", "ci_from_layer",
    "parse_results",
]
