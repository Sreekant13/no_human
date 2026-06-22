"""CI data types + the pluggable backend interface.

The orchestrator depends only on ``CIBackend.trigger`` — never on ``glab`` or any
provider CLI directly. ``ci_from_config`` selects the concrete backend from the
project profile / config, so adding GitHub Actions or Jenkins is a new subclass,
not a change to the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HumanGatedCI(Exception):
    """Raised by a backend whose pipeline must be started by a human (e.g. a
    Jenkins image build). The orchestrator parks the task with a wake condition
    rather than faking or skipping the step."""

    def __init__(self, message: str, wake_hint: str = ""):
        super().__init__(message)
        self.wake_hint = wake_hint


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"
    MANUAL = "manual"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in (
            PipelineStatus.SUCCESS,
            PipelineStatus.FAILED,
            PipelineStatus.CANCELED,
            PipelineStatus.SKIPPED,
        )

    @property
    def is_success(self) -> bool:
        return self == PipelineStatus.SUCCESS


@dataclass
class JobResult:
    name: str
    status: str
    failure_reason: str | None = None
    web_url: str = ""


@dataclass
class CIResult:
    pipeline_id: str
    pipeline_url: str
    status: PipelineStatus
    jobs: list[JobResult] = field(default_factory=list)
    infra_failure: bool = False
    parsed_output: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PipelineStatus.SUCCESS

    @property
    def failed(self) -> bool:
        return self.status in (PipelineStatus.FAILED, PipelineStatus.CANCELED)

    @property
    def summary(self) -> str:
        if self.infra_failure:
            verdict = "INFRA-FAIL"
        elif self.passed:
            verdict = "PASS"
        else:
            verdict = "FAIL"
        pid = self.pipeline_id or "?"
        return f"[{verdict}] pipeline #{pid}: {self.status.value}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "pipeline_url": self.pipeline_url,
            "status": self.status.value,
            "infra_failure": self.infra_failure,
            "passed": self.passed,
            "jobs": [{"name": j.name, "status": j.status,
                      "failure_reason": j.failure_reason} for j in self.jobs],
        }


class CIBackend(ABC):
    """Provider-agnostic CI contract the orchestrator drives.

    Implementations: ``GitLabCI`` (complete), ``GitHubActionsCI`` and
    ``JenkinsCI`` (seams). All must retry only on infra failures, never on real
    test failures, and surface a parsed ``CIResult``.
    """

    name: str = "ci"
    # Backends that don't auto-retry infra (seams, human-gated) inherit 0 so the
    # orchestrator can read it unconditionally without crashing.
    max_infra_retries: int = 0

    @abstractmethod
    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        """Trigger CI for ``branch``, wait for a terminal status, return the
        parsed result. Raise ``HumanGatedCI`` if a human must start the run."""
        raise NotImplementedError
