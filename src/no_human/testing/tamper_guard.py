"""Test-tampering guard (§3.4): block any net reduction in tests/assertions.

Reward hacking — weakening or deleting tests so a broken change goes green — is
documented, not hypothetical. We diff the *test files* separately from product
code and fail closed on a net drop in test-function or assertion count. An
agent-editable green suite is not a trustworthy "done."
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# What counts as a test file, by path.
_TEST_FILE_PATTERNS = (
    r"(^|/)tests?/",
    r"(^|/)test_[^/]+\.py$",
    r"[^/]+_test\.py$",
    r"[^/]+\.test\.[jt]sx?$",
    r"[^/]+\.spec\.[jt]sx?$",
    r"[^/]+Test\.java$",
    r"[^/]+IT\.java$",
    r"(^|/)__tests__/",
)
_TEST_FILE_RE = re.compile("|".join(_TEST_FILE_PATTERNS))

# Test-function declarations.
_TEST_DECL = re.compile(
    r"\bdef\s+test\w*\s*\(|\bit\s*\(|\btest\s*\(|@Test\b|\bdescribe\s*\(",
)

# Assertion-ish calls across python/js/java.
_ASSERT = re.compile(
    r"\bassert\b|\bself\.assert\w+\s*\(|\bexpect\s*\(|\bassert\w*\s*\("
    r"|\bAssert\.|\bassertThat\s*\(|pytest\.raises|\.should\b",
)


def is_test_file(path: str) -> bool:
    return bool(_TEST_FILE_RE.search(path))


def count_tests(source: str) -> int:
    return len(_TEST_DECL.findall(source))


def count_assertions(source: str) -> int:
    return len(_ASSERT.findall(source))


@dataclass
class TamperReport:
    tampered: bool
    tests_before: int
    tests_after: int
    assertions_before: int
    assertions_after: int
    reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        verdict = "TAMPERED" if self.tampered else "clean"
        return (
            f"[{verdict}] tests {self.tests_before}->{self.tests_after}, "
            f"assertions {self.assertions_before}->{self.assertions_after}"
        )


def check(
    before: dict[str, str],
    after: dict[str, str],
) -> TamperReport:
    """Compare test-file snapshots. ``before``/``after`` map path -> content.

    A path absent from ``after`` is treated as a deleted file (its tests and
    assertions drop to zero). Only test files are considered.
    """
    paths = {p for p in before if is_test_file(p)} | {
        p for p in after if is_test_file(p)
    }

    tb = ta = ab = aa = 0
    reasons: list[str] = []
    for p in sorted(paths):
        b_src = before.get(p, "")
        a_src = after.get(p, "")
        b_tests, b_asserts = count_tests(b_src), count_assertions(b_src)
        a_tests, a_asserts = count_tests(a_src), count_assertions(a_src)
        tb += b_tests
        ta += a_tests
        ab += b_asserts
        aa += a_asserts
        if p in before and p not in after:
            reasons.append(f"test file deleted: {p}")
        elif a_tests < b_tests:
            reasons.append(f"{p}: tests {b_tests}->{a_tests}")
        elif a_asserts < b_asserts:
            reasons.append(f"{p}: assertions {b_asserts}->{a_asserts}")

    tampered = ta < tb or aa < ab
    return TamperReport(
        tampered=tampered,
        tests_before=tb,
        tests_after=ta,
        assertions_before=ab,
        assertions_after=aa,
        reasons=reasons,
    )
