"""CI backends: pluggable trigger + wait + result parsing.

Usage — wire from config / project profile:

    ci_runner = ci_from_config(config.data)
    if ci_runner:
        result = await ci_runner.trigger(branch)

The orchestrator depends only on the ``CIBackend`` interface, never on a
provider CLI. Adding a provider = a new ``CIBackend`` subclass + a branch here.
"""

from __future__ import annotations

from typing import Any

from .base import CIBackend, CIResult, HumanGatedCI, JobResult, PipelineStatus
from .ghe_checkruns import GHECheckRunsCI
from .github_actions import GitHubActionsCI
from .gitlab import GitLabCI
from .jenkins import JenkinsCI
from .parser import parse_results


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

    raise ValueError(f"unknown ci.backend: {backend!r}")


__all__ = [
    "CIBackend", "CIResult", "HumanGatedCI", "JobResult", "PipelineStatus",
    "GitLabCI", "GitHubActionsCI", "JenkinsCI", "GHECheckRunsCI",
    "ci_from_config",
    "parse_results",
]
