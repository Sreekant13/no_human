"""CI result parsers for pytest and Maven surefire output."""

from __future__ import annotations

import re


_SUREFIRE_LINE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)"
)
_PYTEST_SUMMARY = re.compile(
    r"(\d+)\s+(passed|failed|error[s]?)"
)


def parse_surefire(text: str) -> tuple[int, int, int]:
    """Return (passed, failed, errors) from Maven surefire output.

    Sums across all 'Tests run: X, Failures: Y, Errors: Z' lines (there may
    be one per test class).
    """
    total_run = failures = errors = 0
    for m in _SUREFIRE_LINE.finditer(text):
        total_run += int(m.group(1))
        failures += int(m.group(2))
        errors += int(m.group(3))
    passed = total_run - failures - errors
    return passed, failures, errors


def parse_pytest(text: str) -> tuple[int, int, int]:
    """Return (passed, failed, errors) from pytest -q summary lines."""
    passed = failed = errors = 0
    for m in _PYTEST_SUMMARY.finditer(text):
        n, kind = int(m.group(1)), m.group(2)
        if kind == "passed":
            passed = n
        elif kind == "failed":
            failed = n
        else:
            errors = n
    return passed, failed, errors


def parse_results(text: str, parser: str = "pytest") -> tuple[int, int, int]:
    """Dispatch to the right parser. Returns (passed, failed, errors)."""
    if parser == "surefire":
        return parse_surefire(text)
    return parse_pytest(text)
