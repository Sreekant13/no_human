"""Structural guard: the review gate has a SINGLE reviewer chokepoint, and it
computes the reviewer's confirmed_rules from the EXCLUSION channel.

Why this exists (D3-M1). `learning.auto_confirm_recurring` lets a recurring
review-origin lesson become an active rule with no human click. That lesson MUST
reach the coder but NEVER the reviewer that produced it, or the gate would
consume a rule derived from its own past verdicts (constraint #3). The behaviour
is enforced by `Orchestrator._format_reviewer_memories`, whose exclusion is
pinned by tests/test_auto_confirm_recurring.py.

But a behavioural test only proves the ONE path it drives. The bypass this guard
closes is a FUTURE reviewer call site that reaches for `_format_active_memories`
(the coder channel) instead — every existing site is correct, and nothing stops
the next one from being wrong. So the reviewer is reached through exactly one
method, `Orchestrator._run_reviewer`, which sets `confirmed_rules` itself; this
guard fails if any `self.reviewer.review(...)` appears anywhere else, or if the
chokepoint stops sourcing its rules from the exclusion channel.

MUTATION PROOF (both directions):
  * add a `self.reviewer.review(..., confirmed_rules=self._format_active_memories())`
    call anywhere outside `_run_reviewer` → `test_reviewer_is_only_reached_
    through_the_single_chokepoint` FAILS (a call outside the chokepoint).
  * make `_run_reviewer` build its rules from `_format_active_memories` →
    `test_the_chokepoint_sources_rules_from_the_exclusion_channel` FAILS.
"""

from __future__ import annotations

import ast
from pathlib import Path

import no_human.core.orchestrator as orch_mod

CHOKEPOINT = "_run_reviewer"


def _orchestrator_tree() -> ast.Module:
    return ast.parse(Path(orch_mod.__file__).read_text(encoding="utf-8"))


def _find_func(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    return None


def _is_self_attr_call(func: ast.expr, attr: str) -> bool:
    """True for a `self.<attr>(...)` call target."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == attr
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _is_reviewer_review(func: ast.expr) -> bool:
    """True for a `self.reviewer.review(...)` call target."""
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "review"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "reviewer"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "self"
    )


def test_the_single_reviewer_chokepoint_exists():
    assert _find_func(_orchestrator_tree(), CHOKEPOINT) is not None, (
        f"Orchestrator.{CHOKEPOINT} — the single reviewer chokepoint — is gone; "
        "every reviewer.review call must route through it")


def test_reviewer_is_only_reached_through_the_single_chokepoint():
    """No `self.reviewer.review(...)` may appear outside `_run_reviewer`. This is
    the wall: a new reviewer site cannot silently feed the gate coder-channel
    rules, because it must go through the chokepoint (which applies the
    exclusion) or trip this guard."""
    tree = _orchestrator_tree()
    choke = _find_func(tree, CHOKEPOINT)
    assert choke is not None
    inside = set(ast.walk(choke))

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_reviewer_review(node.func)
        and node not in inside
    ]
    assert offenders == [], (
        "self.reviewer.review(...) called OUTSIDE the "
        f"Orchestrator.{CHOKEPOINT} chokepoint at line(s) {offenders}. Route it "
        f"through {CHOKEPOINT} so the gate-independence exclusion "
        "(_format_reviewer_memories) cannot be bypassed.")

    # And the chokepoint really does reach the reviewer (guard is not vacuous).
    reached = any(
        isinstance(node, ast.Call) and _is_reviewer_review(node.func)
        for node in inside
    )
    assert reached, f"{CHOKEPOINT} does not call self.reviewer.review — the guard "\
        "would be vacuous"


def test_the_chokepoint_sources_rules_from_the_exclusion_channel():
    """`_run_reviewer` must build confirmed_rules from
    `_format_reviewer_memories` (the exclusion channel) and NEVER from
    `_format_active_memories` (the coder channel)."""
    tree = _orchestrator_tree()
    choke = _find_func(tree, CHOKEPOINT)
    assert choke is not None

    calls_reviewer_channel = any(
        isinstance(node, ast.Call)
        and _is_self_attr_call(node.func, "_format_reviewer_memories")
        for node in ast.walk(choke)
    )
    calls_coder_channel = any(
        isinstance(node, ast.Call)
        and _is_self_attr_call(node.func, "_format_active_memories")
        for node in ast.walk(choke)
    )
    assert calls_reviewer_channel, (
        f"{CHOKEPOINT} must compute the reviewer's confirmed_rules from "
        "self._format_reviewer_memories() (the exclusion channel)")
    assert not calls_coder_channel, (
        f"{CHOKEPOINT} builds reviewer rules from self._format_active_memories() "
        "— the CODER channel, which INCLUDES auto-confirmed review lessons and "
        "would break gate independence")


def test_chokepoint_refuses_a_caller_supplied_confirmed_rules():
    """Behavioural companion to the AST guards: the chokepoint owns
    confirmed_rules and rejects a caller trying to pass it, so the value can
    never come from anywhere but the exclusion channel."""
    import asyncio

    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)

    async def _call():
        return await o._run_reviewer(object(), confirmed_rules="smuggled")

    try:
        asyncio.run(_call())
    except TypeError as exc:
        assert "confirmed_rules" in str(exc)
    else:  # pragma: no cover - the raise above is the whole point
        raise AssertionError(
            "_run_reviewer accepted a caller-supplied confirmed_rules")
