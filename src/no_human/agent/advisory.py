"""The toolless seam for single-turn advisory calls.

Refile of db605dd0 (38.5M tok/week measured): the utility and supervisor
tiers' single-turn, prompt-in/text-out calls (intake evaluation, context
distillation, the supervisor course-corrector) were built with the same
`ClaudeBackend()` construction as the coder, which ships the full built-in
tool schema on every call even though none of these calls ever uses a tool.

`advisory_backend()` is the one call-construction seam for that: it returns a
`ClaudeBackend` with `tools=[]` (no tool schemas serialized) and a minimal
role-specific `system_prompt` (not the coding harness's). Every other
`ClaudeBackend` construction site — coder, reviewer, planner, `grill_spec`
(it explores a real repo) — is untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .claude_backend import ClaudeBackend

#: Minimal per-role prompts. Each names the role, its one job, and that it
#: has no tools — the model must never attempt Read/Bash/Grep on a session
#: that was not given any.
ADVISORY_ROLE_PROMPTS: dict[str, str] = {
    "intake": (
        "You are the intake evaluator for an autonomous coding agent. "
        "Answer only in the format the prompt requests. "
        "You have no tools; do not attempt to read files or run commands."
    ),
    "distill": (
        "You are a context distiller for an autonomous coding agent. "
        "Summarize only the text given to you in the prompt. "
        "You have no tools; do not attempt to read files or run commands."
    ),
    "supervisor": (
        "You are the supervisor course-corrector for an autonomous coding "
        "agent. Judge only the progress described in the prompt. "
        "You have no tools; do not attempt to read files or run commands."
    ),
}


def advisory_backend(model: str, *, role: str) -> ClaudeBackend:
    """A `ClaudeBackend` for one single-turn advisory call.

    `role` must be one of `ADVISORY_ROLE_PROMPTS` — fails closed (raises)
    rather than silently returning a fully-harnessed backend for a typo'd
    or new role.
    """
    if role not in ADVISORY_ROLE_PROMPTS:
        raise ValueError(
            f"unknown advisory role {role!r}; expected one of "
            f"{sorted(ADVISORY_ROLE_PROMPTS)}"
        )
    # Lazy, call-time import — same seam every other advisory call site uses
    # (`intake/evaluator.py`, `review/reviewer.py`, `api/app.py`) and the one
    # `tests/conftest.py::_hermetic_sdk` patches first: a module-top import
    # here would bind a private, unpatchable copy of the name and leave every
    # advisory call live under the hermetic test fixture (the exact bug
    # `conftest.py` documents fixing once already, for the orchestrator's own
    # alias).
    from .claude_backend import ClaudeBackend
    return ClaudeBackend(
        model=model, readonly=True, tools=[],
        system_prompt=ADVISORY_ROLE_PROMPTS[role],
    )
