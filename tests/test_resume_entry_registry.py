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
INHERITS_ELSE_STAMPS = (
    "inherits a HUMAN's gated branch point; otherwise stamps the machine's own "
    "provenance with the sha of the attempt that died")

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
    # Requeues an attempt that was already in flight when the process died.
    # Stamping over a HUMAN's gated sha would fail their resume as fabrication,
    # so theirs is inherited untouched. Everything else is re-stamped, and both
    # halves of that are load-bearing. Inheriting NOTHING inherits nothing: the
    # requeue re-enters at attempt_n == 1, where `handoff.wip_sha` is ignored,
    # so the dead attempt's committed work was redone from base (11 attempts
    # burned over three restarts, 2026-08-10). And inheriting the sweep's OWN
    # previous stamp is what made restarts 2 and 3 of that same incident resume
    # onto restart 1's sha. The stamp is `by: "orphan_recovery"` — machine
    # provenance, so the zero-diff gate stays armed — over the sha of the
    # attempt that actually died (the one still `in_progress`), never one a
    # CLEARS path above deliberately removed.
    ("core/scheduler.py", "Scheduler._recover_orphans"): INHERITS_ELSE_STAMPS,
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


def _drops_checkpoint(v: ast.AST) -> bool:
    """Does this `resume_from` value carry NO sha?

    Both spellings, because the orphan sweep cannot tell them apart: a literal
    ``None`` (RFC 7396 — deletes the key) and ``resume_provenance(None, ...)``
    (writes ``sha: None``) both leave a context that reads exactly like a task
    which never had a checkpoint. A `resume_from` built from a VARIABLE
    checkpoint may well carry a sha and is deliberately not matched.
    """
    if isinstance(v, ast.Constant) and v.value is None:
        return True
    return (isinstance(v, ast.Call)
            and getattr(v.func, "id", getattr(v.func, "attr", "")) == "resume_provenance"
            and bool(v.args)
            and isinstance(v.args[0], ast.Constant)
            and v.args[0].value is None)


def _drop_sites_and_closers() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(functions that drop the checkpoint, functions that retire the rows).

    Two spellings are matched, because both are live in `src/`: the key inside a
    dict literal (`merge_context(t.id, {"resume_from": None})`) and an assignment
    to a subscript of a patch built up first (`patch["resume_from"] = None`, as at
    `api/app.py:2637` and `blockers/wake.py:474`). Missing the second is the exact
    shape of miss this file exists to stop — the twin of one that got fixed.

    NOT matched, and deliberately so rather than silently: the keyword spelling
    `dict(resume_from=None)`, `patch.setdefault("resume_from", None)`, a subscript
    `AugAssign`, and any patch assembled through a variable the walk would have to
    follow. Matching those needs dataflow analysis, not an AST shape; a new writer
    in one of those spellings would be invisible here.
    """
    drops: set[tuple[str, str]] = set()
    closes: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        rel = str(path.relative_to(SRC))
        stack: list[str] = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            def visit_Dict(self, node):
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant) and k.value == "resume_from"
                            and _drops_checkpoint(v)):
                        drops.add((rel, ".".join(stack)))
                self.generic_visit(node)
            def visit_Assign(self, node):
                # `patch["resume_from"] = None` — no ast.Dict to see.
                for tgt in getattr(node, "targets", None) or [node.target]:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value == "resume_from"
                            and _drops_checkpoint(node.value)):
                        drops.add((rel, ".".join(stack)))
                self.generic_visit(node)
            visit_AnnAssign = visit_Assign
            def visit_Call(self, node):
                if getattr(node.func, "attr", "") == "close_open_attempts":
                    closes.add((rel, ".".join(stack)))
                self.generic_visit(node)

        V().visit(tree)
    return drops, closes


def test_every_path_that_drops_the_checkpoint_also_retires_the_attempt_rows():
    """🔴 CLEARING THE CONTEXT IS ONLY HALF OF A CLEAR.

    `Scheduler._recover_orphans` re-derives a checkpoint from the attempt row
    still `in_progress`, and a crashed worker leaves exactly that row behind.
    So every one of these paths promised "a fresh run from base" and the first
    orphan sweep afterwards put the dropped checkpoint straight back — from a
    run that had already failed. The cure is `Store.close_open_attempts` at the
    clear, because the sweep has nothing left to read: a context with no
    checkpoint looks identical whether one was deliberately dropped or never
    existed.

    Enumerated from the source rather than listed in prose, and by AST rather
    than grep, for the reason the rest of this file exists: the count of these
    sites has been written down wrong at least three times, and the ones that
    got missed were always the twin of one that got fixed (`nh task retry`
    behind `POST /retry`, `nh reject` behind Send back).
    """
    drops, closes = _drop_sites_and_closers()
    # Positive control — a broken walk must not pass vacuously.
    assert len(drops) >= 6, (
        f"the AST walk found only {len(drops)} checkpoint-dropping writers "
        f"({sorted(drops)}) — the instrument is broken")

    missing = sorted(drops - closes)
    assert not missing, (
        "these paths drop the resume checkpoint without retiring the attempt "
        "rows the orphan sweep re-derives it from, so the next crash "
        "resurrects it:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in missing)
        + "\n\nAdd `await store.close_open_attempts(<task_id>)` BEFORE the "
          "context write (ordered so a crash between the two leaves the rows "
          "closed rather than the checkpoint cleared with the rows open).")


def test_the_registry_has_no_entries_for_paths_that_no_longer_exist():
    """A registry that outlives its code rots into a lie — the same failure as
    the prose counts this test replaces."""
    stale = sorted(set(REGISTRY) - _reentry_sites())
    assert not stale, (
        "REGISTRY lists paths that no longer re-enter the loop; remove them:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in stale))
