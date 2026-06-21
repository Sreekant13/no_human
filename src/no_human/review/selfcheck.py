"""Advisory self-check (PLAN.md Layer 1) — a cheap pre-filter, NOT the gate.

The implementing session runs the verifiable checklist on its own diff to catch
obvious misses before spending review tokens. Its verdict is advisory only: a
model over-rates its own work (self-preference bias), so this can never gate.
The real gate is the independent fresh-context reviewer (Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChecklistItem:
    label: str
    passed: bool
    evidence: str = ""


@dataclass
class SelfCheckResult:
    items: list[ChecklistItem] = field(default_factory=list)
    advisory_pass: bool = False  # advisory only — never a gate

    def add(self, label: str, passed: bool, evidence: str = "") -> None:
        self.items.append(ChecklistItem(label, passed, evidence))

    def finalize(self) -> "SelfCheckResult":
        self.advisory_pass = all(i.passed for i in self.items)
        return self


def build_prompt(task_title: str, acceptance_criteria: list[str]) -> str:
    """Prompt fragment the implementer uses to self-check before finalizing."""
    criteria = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none stated)"
    return (
        "Before you finish, run this verifiable self-check on your OWN diff. "
        "For each item, cite evidence (file:line, or the command + its output). "
        "This is an advisory pre-filter, not the gate — be honest, do not inflate.\n"
        f"Task: {task_title}\n"
        f"Acceptance criteria:\n{criteria}\n"
        "Checklist:\n"
        "  [ ] Each acceptance criterion implemented (cite file:line)\n"
        "  [ ] Tests added/updated and run (paste output)\n"
        "  [ ] No test weakened or deleted (show test-file diff)\n"
        "  [ ] Edge cases / error paths handled (cite lines)\n"
        "  [ ] Existing suite still passes (paste run)\n"
    )
