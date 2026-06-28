"""Deterministic per-edit lint feedback (IMPROVEMENT_PLAN B1).

After the agent edits a code file, run the project's linter scoped to *that one
file* and, on failure, inject the errors straight back into the session so the
model fixes them immediately instead of carrying them to end-of-attempt review.

Evidence: immediate per-edit feedback raised SWE-agent resolution ~15%
(arxiv 2405.15793, NeurIPS 2024). This hook is deterministic (no LLM cost),
debounced to the just-edited file, and feature-flagged. It runs *alongside* the
supervisor's every-5 LLM check on PostToolUse — cheap syntactic/style guardrail
vs. expensive semantic-drift check.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from ..testing import runner

# Tools that mutate a file on disk.
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Only lint files a linter can meaningfully check.
_CODE_EXTS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".go", ".rs", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp",
)


class LintFeedbackHook:
    """A PostToolUse hook that lints the just-edited file and feeds back errors."""

    def __init__(
        self,
        *,
        repo_path: str | Path,
        lint_cmd: str | None,
        on_event: Callable[[str, str], None] | None = None,
        timeout: int = 60,
    ):
        self.repo_path = Path(repo_path)
        self.lint_cmd = lint_cmd
        self._on_event = on_event or (lambda kind, text: None)
        self.timeout = timeout

    @staticmethod
    def _path_of(tool_input: dict) -> str | None:
        p = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
        )
        return str(p) if p else None

    def _file_arg(self, raw: str) -> str:
        """Return a repo-relative path when possible (linters prefer that)."""
        try:
            return str(Path(raw).relative_to(self.repo_path))
        except ValueError:
            return raw

    async def hook(
        self, input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        if not self.lint_cmd:
            return {}
        if input_data.get("tool_name", "") not in _EDIT_TOOLS:
            return {}
        raw = self._path_of(input_data.get("tool_input", {}) or {})
        if not raw or not raw.lower().endswith(_CODE_EXTS):
            return {}

        file_arg = self._file_arg(raw)
        result = await asyncio.to_thread(
            runner.run_lint_on_changed,
            self.repo_path,
            self.lint_cmd,
            [file_arg],
            timeout=self.timeout,
        )
        if not result.ran or result.ok:
            return {}

        self._on_event("lint_feedback", f"lint failed on {file_arg}")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"[LINT] Your edit to {file_arg} introduced lint/type errors. "
                    "Fix them now before continuing — do not leave them for review:\n"
                    f"{result.output.strip()[:1500]}"
                ),
            }
        }
