"""Every transition back into a CLAIMABLE status must have a stated disposition.

🔴 THIS TEST EXISTS BECAUSE TEN CONSECUTIVE REVIEW ROUNDS DREW THE BOUNDARY IN
THE WRONG PLACE. Each round scoped the invariant to *writers of `resume_from`* —
"three writers", then "four", then "eight", then "six that carry a checkpoint and
two that do not". Every one of those counts was wrong, and the misses were always
the same shape: a path that re-enters the loop while writing NOTHING inherits the
previous actor's verdict just as surely as one that writes the wrong thing.

The zero-diff honesty gate (`_is_own_partial`) reads `resume_from.sha` together
with `resume_from.by` and credits work already ahead of base only when a HUMAN
gated it. So the invariant is not about who writes that key. It is:

    for EVERY transition that puts a task back into a claimable status,
    `resume_from` must describe THAT re-entry — or be absent.

which is a property of `set_status(..., PENDING | IMPLEMENTING)`, not of
`merge_context({"resume_from": ...})`.

So this test enumerates the call sites from the SOURCE, by AST, and fails when
one appears that nobody has classified. A new re-entry path cannot be added
without writing down which of the four dispositions it takes. Counts in prose
cannot fail; this one can.

AST, not grep, deliberately: `nh unblock` passes a VARIABLE target
(`set_status(t, target, ...)`), so any text search for `TaskStatus.IMPLEMENTING`
misses it — and `nh unblock` is exactly where the ninth defect lived.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "no_human"

# disposition -> why it is safe.
STAMPS = "stamps provenance for the actor performing this re-entry"
CLEARS = "clears resume_from: a fresh run must not inherit a checkpoint"
INHERITS = "deliberately inherits — executes a decision another actor already made"
INTERNAL = "within-run transition, not a re-entry from a parked/terminal state"

REGISTRY: dict[tuple[str, str], str] = {
    # --- human re-entries: stamp `by: "human"` via resume_provenance ---
    ("api/app.py", "send_back"): STAMPS,
    ("api/app.py", "resume_task"): STAMPS,
    ("api/app.py", "reply_task"): STAMPS,
    ("cli/commands.py", "task_resume._go"): STAMPS,
    ("cli/commands.py", "reply._go"): STAMPS,
    ("cli/commands.py", "reject._go"): STAMPS,
    # `unblock` passes a VARIABLE target — invisible to any text search.
    ("cli/commands.py", "unblock._go"): STAMPS,
    # --- machine re-entries ---
    ("blockers/wake.py", "WakeWatcher._resume"): STAMPS,   # by: "wake"
    # --- fresh runs: clear, so nothing is inherited ---
    ("api/app.py", "retry_task"): CLEARS,
    ("cli/commands.py", "task_retry._go"): CLEARS,
    ("core/lead_agent.py", "LeadAgent.check_completion"): CLEARS,
    ("core/lead_agent.py", "LeadAgent._unblock_ready"): CLEARS,
    # --- deliberate inheritance, argued in the code ---
    # Requeues an attempt that was already in flight when the process died. No
    # new decision was made, and stamping would OVERWRITE the provenance of the
    # actor who gated the run — failing their resume as fabrication.
    ("core/scheduler.py", "Scheduler._recover_orphans"): INHERITS,
    # --- internal, within a run the loop already entered ---
    ("core/orchestrator.py", "Orchestrator._run_attempt"): INTERNAL,
}

CLAIMABLE = {"PENDING", "IMPLEMENTING"}


def _reentry_sites() -> set[tuple[str, str]]:
    """Every `set_status` call whose target is claimable, or is a variable."""
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        stack: list[str] = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            def visit_Call(self, node):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "set_status":
                    target = node.args[1] if len(node.args) > 1 else None
                    text = ast.unparse(target) if target is not None else ""
                    claimable = any(text.endswith(f".{s}") for s in CLAIMABLE)
                    # A non-literal target (a variable) is treated as claimable:
                    # it MIGHT be, and that is exactly the case that hid a bug.
                    opaque = bool(target) and not isinstance(target, ast.Attribute)
                    if claimable or opaque:
                        found.add((str(path.relative_to(SRC)), ".".join(stack)))
                self.generic_visit(node)

        V().visit(tree)
    return found


def test_every_reentry_into_the_loop_has_a_stated_disposition():
    sites = _reentry_sites()

    # Positive control: the instrument must actually find things. If this ever
    # trips, the AST walk broke and the test below would pass vacuously.
    assert len(sites) >= 10, (
        f"the AST walk found only {len(sites)} re-entry sites — the instrument "
        "is broken, so its 'no unregistered paths' result means nothing")

    unregistered = sorted(sites - set(REGISTRY))
    assert not unregistered, (
        "a path re-enters the loop with no stated disposition:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in unregistered)
        + "\n\nThe zero-diff honesty gate reads `resume_from.sha` WITH "
          "`resume_from.by`. Decide which this path is and add it to REGISTRY:\n"
          "  STAMPS   — a human or machine DECIDED to re-enter; write provenance\n"
          "             via `blockers.resume_provenance(checkpoint, by)`\n"
          "  CLEARS   — a FRESH run; write {'resume_from': None} so nothing is\n"
          "             inherited (branches from base)\n"
          "  INHERITS — it merely EXECUTES a decision another actor made; say so\n"
          "             in the code, because silence here has cost ten rounds\n"
          "  INTERNAL — a transition inside a run the loop already entered")


def test_the_registry_has_no_entries_for_paths_that_no_longer_exist():
    """A registry that outlives its code rots into a lie — the same failure as
    the prose counts this test replaces."""
    stale = sorted(set(REGISTRY) - _reentry_sites())
    assert not stale, (
        "REGISTRY lists paths that no longer re-enter the loop; remove them:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in stale))
