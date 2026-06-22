"""GitHub Actions backend — interface seam.

Conforms to ``CIBackend`` so the orchestrator can target it via config without
code changes. The trigger/poll wiring (``gh workflow run`` + ``gh run view
--json``) is not implemented yet; it raises a clear, actionable error instead of
silently passing — an un-run pipeline must never read as green.
"""

from __future__ import annotations

from .base import CIBackend, CIResult


class GitHubActionsCI(CIBackend):
    name = "github_actions"

    def __init__(
        self,
        repo: str,
        workflow: str = "",
        ref_prefix: str = "",
        timeout_minutes: int = 60,
        max_infra_retries: int = 2,
        poll_interval: int = 30,
        variables: dict[str, str] | None = None,
        result_parser: str = "pytest",
    ):
        self.repo = repo
        self.workflow = workflow
        self.timeout_minutes = timeout_minutes
        self.max_infra_retries = max_infra_retries
        self.poll_interval = poll_interval
        self.variables = variables or {}
        self.result_parser = result_parser

    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        raise NotImplementedError(
            "GitHub Actions CI backend is a seam, not yet wired. Implement via "
            "`gh workflow run <workflow> --ref <branch>` then poll "
            "`gh run list/view --json conclusion,status`. Until then, configure "
            "ci.backend=gitlab or run tests locally."
        )
