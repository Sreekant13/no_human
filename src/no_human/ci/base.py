"""CI data types shared across backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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
