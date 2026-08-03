"""`nh bench --help` used to contradict itself about what `report` reads.

    group docstring   : "report -> ... from the published baseline"
    command docstring : "Re-render ... from the latest saved results."

The command was right: `bench_report` loads `RESULTS_DIR / "latest.json"`. The
published baseline is a DIFFERENT file, written by `northstar_card.published_file()`
and saved only by a publish that needed no --force override, precisely so a later
forced publish cannot erase the last trustworthy measurement.

That mattered more than a stale comment. "From the published baseline" describes
the SAFER design — read only what a human blessed — so the help promised a
guarantee the code does not make, and a user reasoning from it would expect
`report` to re-render a cleanly published run when in fact it re-renders whatever
was saved last.

WHY THIS GUARD IS ANCHORED TO THE CODE AND NOT TO THE OTHER DOCSTRING. Two
documents agreeing with each other is not correctness; both can drift together,
and "one doc cites another doc" is exactly how three separate wrong-pointer
defects got shipped in this repo. So the assertion below reads what
`bench_report` actually opens and requires the help to match THAT.
"""

from __future__ import annotations

import inspect
import re

from no_human.cli import commands


def _report_line(group_doc: str) -> str:
    """The `report ->` bullet out of the bench group's help, joined."""
    lines = group_doc.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("report"))
    out = [lines[start]]
    for l in lines[start + 1:]:
        if not l.strip() or re.match(r"\s*\w+\s*(→|->)", l):
            break
        out.append(l)
    return " ".join(x.strip() for x in out)


def test_bench_report_still_reads_latest_json():
    """Premise of the test below, pinned separately so that if the CODE changes
    this fails HERE with a clear reason rather than making the help assertion
    silently wrong in the other direction."""
    # `commands.bench_report` is a click Command, not the function — its body
    # is on `.callback`. inspect.getsource on the Command raises TypeError.
    fn = getattr(commands.bench_report, "callback", commands.bench_report)
    src = inspect.getsource(fn)
    assert '"latest.json"' in src or "'latest.json'" in src, (
        "bench_report no longer reads latest.json — the help guard below is "
        "now anchored to a false premise and must be re-derived, not deleted")


def test_the_group_help_names_the_file_report_actually_reads():
    doc = inspect.getdoc(commands.bench) or ""
    line = _report_line(doc)

    assert "latest" in line.lower(), (
        f"the bench group help does not name what `report` reads:\n  {line}")
    # The specific false claim this guard exists for. "published baseline"
    # describes a safety property the code does not implement.
    assert not re.search(r"from the published baseline", line, re.I), (
        f"the group help still claims `report` reads the published baseline, "
        f"which is a different file that only a clean publish writes:\n  {line}")


def test_the_two_docstrings_do_not_contradict_each_other():
    """They may each be right and still disagree; users see both in one
    `nh bench --help` / `nh bench report --help` pair."""
    group = _report_line(inspect.getdoc(commands.bench) or "")
    cmd = inspect.getdoc(commands.bench_report) or ""
    claims_baseline = [
        name for name, text in (("group", group), ("command", cmd))
        if re.search(r"from the published baseline", text, re.I)]
    assert not claims_baseline, (
        f"{claims_baseline} claim(s) `report` re-renders the published "
        f"baseline; it re-renders results/latest.json")


def test_the_guard_is_not_vacuous():
    """`_report_line` must actually find the bullet. If the group docstring is
    reworded so the bullet disappears, every assertion above passes on an empty
    string — the failure mode that makes a guard dark."""
    line = _report_line(inspect.getdoc(commands.bench) or "")
    assert line.strip(), "no `report` bullet found in the bench group help"
    assert "NORTH_STAR_BENCH" in line, (
        f"the `report` bullet no longer describes the rendered file; the "
        f"anchors moved and this guard is measuring the wrong line:\n  {line}")
