"""CI backends: GitLab CI trigger + wait + result parsing.

Usage — wire from config:

    ci_runner = ci_from_config(config.data)
    if ci_runner:
        result = await ci_runner.trigger(branch)
"""

from __future__ import annotations

from typing import Any

from .base import CIResult, JobResult, PipelineStatus
from .gitlab import GitLabCI
from .parser import parse_results


def ci_from_config(config: dict[str, Any]) -> "GitLabCI | None":
    """Build a CI runner from the config dict, or None if CI is disabled."""
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
    return None


__all__ = [
    "CIResult", "JobResult", "PipelineStatus",
    "GitLabCI",
    "ci_from_config",
    "parse_results",
]
