"""A learned rule carrying a banned term must never reach a prompt.

The learning store is written by the product from transcripts of real sessions,
so a rule can arrive carrying a customer's or an employer's name. Once injected
it reaches the coder, the reviewer, and — through `_write_skill_memories` —
`.claude/skills/<name>/SKILL.md` on disk, any of which can land in a commit
message, a PR body or a doc destined for a public repo.

Observed 2026-07-31: a rule whose title named a private project was injected
into a task targeting this repo. That output happened to be clean. This file
exists so the next one does not depend on luck.

No banned term is spelled here. They are read at runtime from the product's own
list, which is exactly what the screen uses — a term hardcoded in this file
would also be a term shipped in this file.
"""

from __future__ import annotations

import pytest

from no_human.eval.vendor_terms import BANNED_TERMS, find_banned_terms


@pytest.fixture
def screen():
    """The screen, bound off the real class rather than reimplemented here."""
    from no_human.core.orchestrator import Orchestrator
    return Orchestrator._screen_memories_for_terms.__get__(object(), Orchestrator)


def _mem(title="A rule", content="Always run the tests before opening a PR."):
    return {"title": title, "content": content}


def test_a_clean_rule_still_injects(screen):
    """The control. A screen that holds everything is not a screen."""
    kept, held = screen([_mem()])
    assert len(kept) == 1
    assert held == []


def test_a_rule_whose_CONTENT_names_a_banned_term_is_held(screen):
    term = BANNED_TERMS[0]
    kept, held = screen([_mem(content=f"When deploying to {term}, drain first.")])
    assert kept == []
    assert held == ["A rule"], "the held rule must be NAMED so it can be cleaned"


def test_a_rule_whose_TITLE_names_a_banned_term_is_held(screen):
    """The observed case named the project in the TITLE, and titles are what
    the audit event prints — so a title-only match is the likeliest shape."""
    term = BANNED_TERMS[0]
    kept, held = screen([_mem(title=f"The {term} project uses a custom runner")])
    assert kept == []
    assert len(held) == 1


def test_one_dirty_rule_does_not_suppress_the_clean_ones(screen):
    """Holding is per-rule. An operator with one bad noun in one rule must not
    silently lose the rest of their knowledge base."""
    term = BANNED_TERMS[0]
    kept, held = screen([
        _mem(title="clean one"),
        _mem(title="dirty", content=f"see the {term} runbook"),
        _mem(title="clean two"),
    ])
    assert [m["title"] for m in kept] == ["clean one", "clean two"]
    assert held == ["dirty"]


def test_nothing_is_deleted_from_the_store(screen):
    """Held, not dropped: the store is the operator's. The screen is a pure
    function over the list it is given and never touches persistence — asserted
    by handing it a dict it could mutate and checking it did not."""
    term = BANNED_TERMS[0]
    original = _mem(title="dirty", content=f"the {term} runbook")
    snapshot = dict(original)
    screen([original])
    assert original == snapshot


def test_the_screen_agrees_with_the_publish_guard(screen):
    """The screen must not be a second, weaker matcher.

    The repo has been burned by a guard whose local matcher was looser than the
    product's real one, so this asserts the two agree rather than trusting that
    the screen calls it: every term the publish guard finds in a rule must cause
    that rule to be held.
    """
    for term in BANNED_TERMS[:5]:
        text = f"context around {term} and more words"
        assert find_banned_terms(text), "precondition: the guard sees this term"
        kept, held = screen([_mem(content=text)])
        assert kept == [] and len(held) == 1, f"screen disagreed with the guard"


def test_every_assignment_to_active_memories_goes_through_the_screen():
    """The wiring, which the tests above cannot see.

    Every test in this file calls the screen directly, so all of them pass even
    if nothing calls it — which is the shape of guard this repo has shipped
    before and had to come back and fix. The claim in the docstring is not "the
    screen works", it is "memories cannot reach a prompt unscreened", and that
    claim lives at the assignment sites.

    Asserted against the real source rather than by driving the orchestrator,
    deliberately: the point of screening at the assignment chokepoint is that a
    FUTURE site is covered without anyone remembering this file exists. A test
    that exercised today's two call sites would not fail when a third is added
    unscreened, which is the regression actually worth catching.
    """
    import re
    from pathlib import Path

    import no_human.core.orchestrator as orch

    src = Path(orch.__file__).read_text()
    assignments = [
        line.strip()
        for line in src.splitlines()
        if re.match(r"\s*self\._active_memories\s*(,[^=]*)?=", line)
    ]
    assert assignments, "no assignment found — this guard has stopped guarding"
    unscreened = [a for a in assignments if "_screen_memories_for_terms" not in a]
    assert not unscreened, (
        "these assignments put memories into the prompt path without screening "
        "them for banned terms:\n  " + "\n  ".join(unscreened)
    )


def test_a_screen_failure_keeps_the_rule_rather_than_dropping_it(monkeypatch, screen):
    """If the matcher itself raises, the rule is kept.

    Deliberate, and the opposite of the publish guard's fail-closed stance. This
    screen sits on the path that gives the coder its knowledge; a matcher bug
    that silently emptied every prompt would degrade every run for a reason
    nobody could see. The publish guard still fails closed at the boundary that
    actually publishes, which is where refusing is the safe default.
    """
    import no_human.eval.vendor_terms as vt

    def boom(_text):
        raise RuntimeError("matcher exploded")

    monkeypatch.setattr(vt, "find_banned_terms", boom)
    kept, held = screen([_mem()])
    assert len(kept) == 1 and held == []
