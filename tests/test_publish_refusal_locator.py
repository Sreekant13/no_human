"""The publish refusal printed a locator that rich then deleted.

    f"...would contain {len(found)} disallowed string(s) [{where}]"

`where` is a redacted shape — first letter, stars, length, e.g. `f*(13)` — and
the square brackets were LITERALS in the format string. rich parses `[f*(13)]`
as a markup tag and removes it. Every banned term is lowercase-initial, so
`where` is always lowercase-initial, so the locator was eaten on EVERY real
refusal. Only an uppercase-initial term would have survived, and there are none.

The locator is the entire reason `where` is computed — the code's own comments
say the shape alone cannot be grepped for. So the guard fired correctly and then
withheld the one thing that made the refusal actionable: the operator was told a
term was found and never which.

WHY THESE TESTS ASSERT ON RENDERED OUTPUT. This exact expression sat in an AST
guard's allowlist with the written justification "already-redacted term SHAPES
... safe". That justification was TRUE — the shape cannot carry markup and
cannot crash — and it was about the wrong property. Crash-safety and "the line
works" are different claims, and only rendering distinguishes them. So every
assertion here goes through a real Console and reads the text a human would see.
"""

from __future__ import annotations

import io

import pytest
from click.testing import CliRunner
from rich.console import Console

from no_human.cli.commands import cli
# The publish harness, reused rather than rebuilt: a second copy of the fixture
# would drift from the one the rest of the publish tests are measured against.
from tests.test_bench_publish import _note_card, bench_env  # noqa: F401


def _render(markup: str) -> str:
    """Exactly what a terminal would show for this markup."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=200, no_color=True).print(markup)
    return buf.getvalue()


#: `where` values built the way `_render_report_or_refuse` builds them:
#: `", ".join(f"{t[0]}*({len(t)})" for t in found)`.
WHERES = ["f*(13)", "m*(9), o*(5)", "a*(6), b*(7), c*(8)"]


@pytest.mark.parametrize("where", WHERES)
def test_the_bracketed_form_really_does_lose_the_locator(where):
    """The premise, pinned. If rich ever stops eating this, the fix below is no
    longer load-bearing and someone should know that rather than assume it."""
    eaten = _render(f"contain 2 disallowed string(s) [{where}]")
    assert where not in eaten, (
        "rich no longer deletes a lowercase-initial bracketed span; re-derive "
        f"why the shipped format was changed:\n{eaten!r}")


@pytest.mark.parametrize("where", WHERES)
def test_the_shipped_form_shows_the_locator(where):
    """What the code prints now, rendered."""
    from no_human.cli import commands  # noqa: F401  (import guard: module loads)
    shown = _render(f"[bold red]refusing to publish:[/] the rendered report "
                    f"would contain 2 disallowed string(s): {where}")
    assert where in shown, (
        f"the locator is still not reaching the operator:\n{shown!r}")
    assert "refusing to publish" in shown


def test_the_source_no_longer_wraps_the_locator_in_square_brackets():
    """Anchored to the SOURCE too, because the render tests above would still
    pass if someone reintroduced brackets on a different line."""
    import inspect
    from no_human.cli import commands

    src = inspect.getsource(commands._render_report_or_refuse)
    assert "disallowed string(s)" in src, (
        "the refusal message was reworded — this guard is now measuring "
        "nothing and must be re-anchored, not deleted")
    offending = [l.strip() for l in src.splitlines()
                 if "disallowed string(s)" in l and "[{where}]" in l]
    assert not offending, (
        f"the locator is wrapped in literal square brackets again, which rich "
        f"deletes: {offending}")


def test_every_banned_term_is_lowercase_initial():
    """The reason this was a certainty rather than an edge case. If a term with
    an uppercase initial is ever added, the OLD bug would have become
    intermittent — which is worse, not better."""
    from no_human.eval.vendor_terms import BANNED_TERMS

    assert BANNED_TERMS, "empty vocabulary — this check would pass vacuously"
    uppercase = [t for t in BANNED_TERMS if t[:1].isupper()]
    assert not uppercase, (
        f"{len(uppercase)} term(s) start uppercase; the locator bug this file "
        "documents would have been intermittent rather than total")


# ------------------- the same refusal, through the real CLI ---------------- #
# Everything above renders a string THIS FILE builds. The only tie to shipped
# code was the source grep, and a reintroduction split across two f-string
# fragments would satisfy every assertion above it. These run `nh bench publish`
# for real, on a genuine card with a genuine banned term, and read the bytes the
# operator would actually see.


def test_the_real_cli_refusal_prints_the_locator(bench_env, monkeypatch):
    """The end-to-end claim: guard fires, exit 1, and the locator is THERE.

    Fails on the pre-fix code — `where` is `w*(8)`, `[w*(8)]` is a well-formed
    rich tag, and rich deletes it, so stdout carried the count and no locator.
    """
    import no_human.eval.northstar_card as nc

    # Redaction cleans the note before the guard ever sees it, so the guard
    # cannot fire with it on. Same lever tests/test_bench_publish.py uses to
    # reach this branch.
    monkeypatch.setattr(nc, "redact_for_publish", lambda s: s)
    results, _report = bench_env
    src = results / "v13.json"
    _note_card("ran the windsurf extractor").save(src)  # term-ok: the fixture needs a real banned term

    res = CliRunner().invoke(cli, ["bench", "publish", str(src)])

    assert res.exit_code == 1, res.output
    assert "refusing to publish" in res.output, res.output
    # The one banned term in the card is 8 characters and starts with `w`, so
    # the redacted shape the code builds is exactly this. If the inventory ever
    # drops that term this fails loudly, which is the correct outcome: the
    # fixture would no longer be exercising the guard.
    assert "w*(8)" in res.output, (
        "the CLI refused and told the operator a count with no locator — the "
        f"exact regression this branch fixes:\n{res.output}")


def test_a_bracketed_run_label_survives_into_the_offending_row(
        bench_env, monkeypatch):
    """The same defect class one line down, in the row locator.

    `rows` carries `f"(run label {card.label!r})"` and a run label is free
    operator text. `probe [rerun]` is a well-formed rich tag, so the unescaped
    row printed `(run label 'probe  windsurf')` — a label the operator never
    wrote, cannot recognise, and cannot grep the results JSON for.
    """
    import no_human.eval.northstar_card as nc

    monkeypatch.setattr(nc, "redact_for_publish", lambda s: s)
    # The premise, pinned like the one above it: rich really does eat this.
    assert "[rerun]" not in _render("(run label 'probe [rerun]')"), \
        "rich no longer deletes a lowercase bracketed span; re-derive the fix"

    results, _report = bench_env
    src = results / "v13.json"
    card = _note_card("ran the windsurf extractor")  # term-ok: the fixture needs a real banned term
    # The label has to be dirty for `_dirty(card.label)` to append its row at
    # all — an unqualified label is never printed, so it could never regress.
    card.label = "probe [rerun] windsurf"  # term-ok: the fixture needs a real banned term
    card.save(src)

    res = CliRunner().invoke(cli, ["bench", "publish", str(src)])

    assert res.exit_code == 1, res.output
    assert "offending row(s):" in res.output, (
        f"the row locator never printed — this test measures nothing:\n"
        f"{res.output}")
    assert "probe [rerun]" in res.output, (
        f"rich ate the bracketed span out of the run label, so the row names a "
        f"label that does not exist:\n{res.output}")
