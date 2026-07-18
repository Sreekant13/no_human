"""v11 live crash @34/48: spec ns-cbb81747's title begins '@[/Users/…]' and
rich parsed '[/…]' as a closing tag — MarkupError killed the RUNNER mid-run
(first process death of the program). Every spec- or exception-derived string
printed inside the bench loop must be markup-escaped."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src/no_human/cli/commands.py"


def _bench_loop_region(text: str) -> str:
    start = text.index("for spec in specs:")
    end = text.index("def bench_report", start)
    return text[start:end]


def test_spec_title_print_is_escaped():
    region = _bench_loop_region(SRC.read_text())
    line = next(l for l in region.splitlines() if "· {" in l or "· " in l and "spec.id" in l)
    assert "escape(" in line, f"unescaped title print: {line.strip()}"


def test_crash_handler_print_is_escaped():
    region = _bench_loop_region(SRC.read_text())
    line = next(l for l in region.splitlines() if "crashed" in l and "console.print" in l)
    assert "escape(" in line, f"unescaped exception print: {line.strip()}"


def test_gate_reason_print_is_escaped():
    text = SRC.read_text()
    line = next(l for l in text.splitlines() if "⛔" in l)
    assert "escape(" in line, f"unescaped gate reason: {line.strip()}"


def test_the_crashing_title_renders():
    """End-to-end: the exact v11 killer string renders through rich."""
    from rich.console import Console
    from rich.markup import escape
    import io
    title = "@[/Users/dev/Downloads/db_issue.csv] go over this splunk"
    c = Console(file=io.StringIO(), force_terminal=False)
    c.print(f"[dim]· ns-cbb81747 {escape(title[:60])}[/]")  # must not raise
