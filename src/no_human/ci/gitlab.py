"""GitLab CI backend: trigger via `glab ci run`, poll via `glab api`.

CLAUDE.md facts encoded here:
  - `glab ci run` with `--variables "KEY:VALUE"` syntax (not KEY=VALUE).
  - Always check job statuses via `glab api`, not just pipeline status.
  - On infra failures: auto-retry after 2 minutes, max_infra_retries times.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import urllib.parse
from typing import Any

from .base import CIResult, JobResult, PipelineStatus

# Job failure_reason values that indicate infra problems, not real test failures.
_INFRA_REASONS = frozenset({
    "runner_system_failure",
    "stuck_or_timeout_failure",
    "scheduler_failure",
    "unmet_prerequisites",
    "data_integrity_failure",
    "runner_unsupported",
})

_PIPELINE_URL_RE = re.compile(r"https://[^\s]*/pipelines?/(\d+)")
_PIPELINE_ID_RE = re.compile(r"[Pp]ipeline[:\s#]+(\d+)")
_INFRA_BACKOFF_SECONDS = 120  # 2 minutes (CLAUDE.md)


def _parse_trigger_output(text: str) -> tuple[str, str]:
    """Extract (pipeline_id, pipeline_url) from glab ci run stdout+stderr."""
    m = _PIPELINE_URL_RE.search(text)
    if m:
        return m.group(1), m.group(0)
    m = _PIPELINE_ID_RE.search(text)
    if m:
        return m.group(1), ""
    return "", ""


class GitLabCI:
    """Async CI runner for GitLab — injectable in the orchestrator."""

    def __init__(
        self,
        project: str,
        hostname: str = "gitlab.acme.net",
        timeout_minutes: int = 60,
        max_infra_retries: int = 2,
        poll_interval: int = 30,
        variables: dict[str, str] | None = None,
        result_parser: str = "pytest",
        _run_cmd: Any = None,  # override for testing
    ):
        self.project = project
        self.hostname = hostname
        self.timeout_minutes = timeout_minutes
        self.max_infra_retries = max_infra_retries
        self.poll_interval = poll_interval
        self.variables = variables or {}
        self.result_parser = result_parser
        self._run_cmd = _run_cmd or _subprocess_run

    async def trigger(
        self,
        branch: str,
        extra_variables: dict[str, str] | None = None,
    ) -> CIResult:
        """Trigger CI and wait. Retry only on infra failures (max_infra_retries)."""
        merged_vars = {**self.variables, **(extra_variables or {})}
        for attempt in range(self.max_infra_retries + 1):
            result = await asyncio.to_thread(
                self._trigger_and_wait, branch, merged_vars
            )
            if not result.infra_failure:
                return result
            if attempt < self.max_infra_retries:
                await asyncio.sleep(_INFRA_BACKOFF_SECONDS)
        return result  # still infra failure after retries

    def _trigger_and_wait(
        self, branch: str, variables: dict[str, str]
    ) -> CIResult:
        """Blocking: trigger + poll until terminal or timeout."""
        pipeline_id, pipeline_url = self._trigger(branch, variables)
        if not pipeline_id:
            return CIResult(
                pipeline_id="",
                pipeline_url="",
                status=PipelineStatus.FAILED,
                infra_failure=True,
                parsed_output="trigger produced no pipeline ID — treating as infra failure",
            )
        return self._wait_for_pipeline(pipeline_id, pipeline_url)

    def _trigger(self, branch: str, variables: dict[str, str]) -> tuple[str, str]:
        cmd = [
            "glab", "ci", "run",
            "--repo", self.project,
            "--branch", branch,
        ]
        for k, v in variables.items():
            cmd += ["--variables", f"{k}:{v}"]
        out = self._run_cmd(cmd)
        return _parse_trigger_output(out)

    def _wait_for_pipeline(
        self, pipeline_id: str, pipeline_url: str
    ) -> CIResult:
        import time
        encoded = urllib.parse.quote(self.project, safe="")
        deadline = time.monotonic() + self.timeout_minutes * 60

        while time.monotonic() < deadline:
            raw_status = self._get_pipeline_status(pipeline_id, encoded)
            if raw_status is None:
                # API call failed — treat as infra
                return CIResult(
                    pipeline_id=pipeline_id,
                    pipeline_url=pipeline_url,
                    status=PipelineStatus.FAILED,
                    infra_failure=True,
                    parsed_output="pipeline status API call failed",
                )
            try:
                status = PipelineStatus(raw_status)
            except ValueError:
                status = PipelineStatus.UNKNOWN

            if status.is_terminal:
                jobs = self._get_jobs(pipeline_id, encoded)
                infra = status == PipelineStatus.FAILED and _is_infra_failure(jobs)
                return CIResult(
                    pipeline_id=pipeline_id,
                    pipeline_url=pipeline_url,
                    status=status,
                    jobs=jobs,
                    infra_failure=infra,
                )
            time.sleep(self.poll_interval)

        return CIResult(
            pipeline_id=pipeline_id,
            pipeline_url=pipeline_url,
            status=PipelineStatus.FAILED,
            infra_failure=True,
            parsed_output=f"timed out after {self.timeout_minutes}m",
        )

    def _glab_api(self, path: str) -> Any:
        cmd = ["glab", "api", "--hostname", self.hostname, path]
        text = self._run_cmd(cmd)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _get_pipeline_status(
        self, pipeline_id: str, encoded_project: str
    ) -> str | None:
        data = self._glab_api(
            f"projects/{encoded_project}/pipelines/{pipeline_id}"
        )
        if isinstance(data, dict):
            return data.get("status")
        return None

    def _get_jobs(
        self, pipeline_id: str, encoded_project: str
    ) -> list[JobResult]:
        data = self._glab_api(
            f"projects/{encoded_project}/pipelines/{pipeline_id}/jobs"
        )
        if not isinstance(data, list):
            return []
        return [
            JobResult(
                name=j.get("name", ""),
                status=j.get("status", ""),
                failure_reason=j.get("failure_reason") or None,
                web_url=j.get("web_url", ""),
            )
            for j in data
        ]


def _is_infra_failure(jobs: list[JobResult]) -> bool:
    """True iff all failed jobs have an infra-class failure_reason."""
    failed = [j for j in jobs if j.status == "failed"]
    if not failed:
        return False
    return all(j.failure_reason in _INFRA_REASONS for j in failed)


def _subprocess_run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return (proc.stdout or "") + (proc.stderr or "") if proc.returncode == 0 else ""
