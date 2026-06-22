"""Jenkins backend — human-gated seam.

In the agreed topology (see ROADMAP Phase 4) a human builds images on Jenkins
(build.example.com) before the integration pipeline can run. no_human does not
reproduce that infra; it models the step as a park-with-wake blocker. So this
backend's ``trigger`` raises ``HumanGatedCI`` — the orchestrator parks the task
with a wake hint rather than mocking, skipping, or faking the build.
"""

from __future__ import annotations

from .base import CIBackend, CIResult, HumanGatedCI


class JenkinsCI(CIBackend):
    name = "jenkins"

    def __init__(
        self,
        job: str,
        base_url: str = "https://build.example.com",
        wake_hint: str = "",
    ):
        self.job = job
        self.base_url = base_url.rstrip("/")
        self.wake_hint = wake_hint or (
            f"Build the image on Jenkins job {job} ({self.base_url}), then reply "
            "to unblock so integration CI can run."
        )

    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        raise HumanGatedCI(
            f"Jenkins job {self.job} must be built by a human before CI can run.",
            wake_hint=self.wake_hint,
        )
