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

import inspect

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


def _orch():
    """A bare Orchestrator instance, no __init__ — only the property is exercised."""
    from no_human.core.orchestrator import Orchestrator
    return Orchestrator.__new__(Orchestrator)


def test_active_memories_is_a_property_not_a_plain_attribute():
    """The structural fact everything below rests on.

    The screen used to sit at each assignment, with a test asserting every
    assignment routed through it. A reviewer defeated that with nine ordinary
    Python forms — tuple targets, `+=`, `setattr`, a `__dict__` write, a slice
    assignment, an alias mutated afterwards — several of which put every
    confirmed memory unscreened into the coder's prompt. That guard was a claim
    about how the code is WRITTEN, and there are unbounded ways to write a store.
    Screening on READ makes the write form irrelevant: there is exactly one way
    to read an attribute.
    """
    from no_human.core.orchestrator import Orchestrator
    attr = inspect.getattr_static(Orchestrator, "_active_memories")
    assert isinstance(attr, property), (
        "_active_memories is a plain attribute again — every write form is "
        "unscreened and the bypasses below are live")
    assert attr.fset is not None and attr.fget is not None


@pytest.mark.parametrize("bypass", [
    "plain",          # self._active_memories = raw
    "augmented",      # self._active_memories += raw
    "setattr",        # setattr(self, "_active_memories", raw)
    "computed_name",  # setattr(self, "_active" + "_memories", raw)
    "slice",          # self._active_memories[:] = raw
    "alias_extend",   # alias = self._active_memories; alias.extend(raw)
    "dunder_dict",    # self.__dict__["_active_memories"] = raw
])
def test_no_write_form_can_put_a_dirty_rule_in_front_of_a_reader(bypass):
    """Every bypass a reviewer found, run as behaviour rather than as source text.

    Each one previously passed a guard that read the source. What matters is not
    whether the line looks like an assignment — it is whether a reader can end up
    holding a rule that names a customer or an employer.
    """
    term = BANNED_TERMS[0]
    dirty = [_mem(title="dirty", content=f"the {term} runbook"), _mem(title="clean")]
    o = _orch()

    if bypass == "plain":
        o._active_memories = dirty
    elif bypass == "augmented":
        o._active_memories = []
        o._active_memories += dirty
    elif bypass == "setattr":
        setattr(o, "_active_memories", dirty)
    elif bypass == "computed_name":
        setattr(o, "_active" + "_memories", dirty)
    elif bypass == "slice":
        o._active_memories = []
        o._active_memories[:] = dirty
    elif bypass == "alias_extend":
        o._active_memories = []
        alias = o._active_memories
        alias.extend(dirty)
    elif bypass == "dunder_dict":
        o.__dict__["_active_memories"] = dirty

    titles = [m["title"] for m in (o._active_memories or [])]
    assert "dirty" not in titles, (
        f"the {bypass!r} write form put a rule naming a banned term where a "
        f"reader can see it: {titles}")


def test_a_reader_still_gets_the_clean_rules():
    """The control. A property that returned nothing would pass every test above
    and silently delete the operator's knowledge base."""
    o = _orch()
    o._active_memories = [_mem(title="clean one"), _mem(title="clean two")]
    assert [m["title"] for m in o._active_memories] == ["clean one", "clean two"]


def test_the_setter_records_what_it_held_so_the_event_can_name_it():
    """Screening on read is the guarantee; the setter screens too, so the audit
    event can name what was held at the moment it was held."""
    term = BANNED_TERMS[0]
    o = _orch()
    o._active_memories = [_mem(title="dirty", content=f"{term} runbook"),
                          _mem(title="clean")]
    assert o._memories_held_for_terms == ["dirty"]


def test_reading_before_any_write_is_empty_not_an_error():
    """Two call sites read it through `getattr(self, ..., None) or []`, so the
    unset case has to be ordinary."""
    assert _orch()._active_memories == []


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


# --------------------------------------------------------------------------- #
# The second route into a prompt — found by review, not by the guard above     #
# --------------------------------------------------------------------------- #

async def test_sessions_source_does_not_put_a_dirty_memory_in_the_prompt(tmp_path):
    """`SessionsSource` reads `memories` with its own SQL and bypassed the screen.

    It never touches `list_memories` / `filter_triggered` / `_active_memories`,
    so the chokepoint screen could not see it — yet its chunk titles reach
    `_context_digest` and go verbatim into the implement prompt. That made the
    docstring's "one chokepoint every consumer reads from" claim false, which
    matters more than the hole itself: an operator reads that claim as the proof.

    Driven through a real store rather than asserted on the SQL string, because
    the defect was a runtime path, not a syntax.
    """
    from no_human.context.sessions import SessionsSource
    from no_human.core.db import Store
    from no_human.core.task import Task

    term = BANNED_TERMS[0]
    async with Store(tmp_path / "nh.db") as store:
        await store.add_memory(
            mem_type="rule", title=f"The {term} deployment runner",
            content="Drain the queue before deploying.", confirmed=True)
        await store.add_memory(
            mem_type="rule", title="Deployment runner conventions",
            content="Drain the queue before deploying.", confirmed=True)

        task = Task.new("fix the deployment runner", repo_path="/tmp/x")
        task.description = "the deployment queue is not drained"
        chunks = await SessionsSource(store).gather(task)

        titles = [c.title for c in chunks]
        assert any("conventions" in t for t in titles), (
            "control failed: the clean memory must still be recalled, or this "
            "test would pass simply by returning nothing")
        for c in chunks:
            assert not find_banned_terms(f"{c.title}\n{c.content}"), (
                f"a banned term reached the prompt via [sessions]: {c.title!r}")


async def test_sessions_source_honours_archived(tmp_path):
    """It queried `memories` directly and never filtered `archived`, so a rule
    the operator had archived came back through this path anyway."""
    from no_human.context.sessions import SessionsSource
    from no_human.core.db import Store
    from no_human.core.task import Task

    async with Store(tmp_path / "nh.db") as store:
        mem_id = await store.add_memory(
            mem_type="rule", title="Deployment runner conventions",
            content="Drain the queue before deploying.", confirmed=True)
        await store.db.execute(
            "UPDATE memories SET archived = 1 WHERE id = ?", (mem_id,))
        await store.db.commit()

        task = Task.new("fix the deployment runner", repo_path="/tmp/x")
        task.description = "the deployment queue is not drained"
        chunks = await SessionsSource(store).gather(task)

        assert not [c for c in chunks if "conventions" in c.title], (
            "an archived memory was recalled into the prompt")
