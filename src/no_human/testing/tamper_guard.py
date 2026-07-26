"""Test-tampering guard (§3.4): block reward hacks that fake a green suite.

Reward hacking — weakening or neutering tests so a broken change goes green — is
documented, not hypothetical. We diff the *test files* separately from product
code and fail closed on any of:

  1. a net drop in test-function or assertion count (gutting / deleting tests);
  2. a net increase in skip/xfail/disable markers (neutering a test in place);
  3. a real assertion replaced by a tautology (``assert True``, ``x == x``);
  4. a behaviour-faking ``autouse`` fixture appearing in test-support code
     (e.g. a ``conftest.py`` that monkeypatches the system-under-test green
     without ever fixing the product bug).

An agent-editable green suite is not a trustworthy "done." Rules 2–4 close the
gaps a pure count-based guard misses: a ``@pytest.mark.skip``, an ``assert True``,
or a ``conftest.py`` autouse patch all keep the test/assertion counts unchanged
while making the suite lie. Each rule is deliberately conservative (it fires only
on a *net increase* of an unambiguous cheat signal in a *test* file) so honest
refactors don't trip it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# What counts as a test file, by path. conftest.py is test-support code: it can
# silently rewrite how the whole suite runs, so it must be snapshotted too.
_TEST_FILE_PATTERNS = (
    r"(^|/)tests?/",
    r"(^|/)test_[^/]+\.py$",
    r"[^/]+_test\.py$",
    r"(^|/)conftest\.py$",
    r"[^/]+\.test\.(?:[jt]sx?|[mc]js)$",
    r"[^/]+\.spec\.(?:[jt]sx?|[mc]js)$",
    r"[^/]+Test\.java$",
    r"[^/]+IT\.java$",
    r"(^|/)__tests__/",
)
_TEST_FILE_RE = re.compile("|".join(_TEST_FILE_PATTERNS))

# Test-function declarations. The `test`, `it`, and `describe` alternatives
# use a negative lookbehind (not just \b) to exclude `.test(` / `foo.test(` —
# RegExp.prototype.test() calls, not test declarations — and likewise
# `foo.it(` / `obj.describe(`, while still matching a standalone
# `test(...)` / `it(...)` / `describe(...)` at the start of a line/expression.
# Known tradeoff: the lookbehind cannot distinguish `t.test('sub', fn)` (a
# genuine node:test SUBTEST declaration) from `foo.test(bar)` (a regex call),
# so `t.test(` / `foo.it(` style subtests are deliberately left uncounted.
# One exception is allowlisted explicitly: Playwright's `test.describe(` suite
# form is a genuine declaration and DOES occur live (web/tests/sdlc-ui.spec.js:142),
# so it is matched via a closed-class prefix alternative below rather than the
# dot-guarded `describe` alternative (which would otherwise miscount an honest
# `describe(` -> `test.describe(` refactor as tampering). A repo-wide
# `git grep -nE '(^|[^.\w])[A-Za-z_$][\w$]*\.(test|it|describe)\('` found no other
# live dot-prefixed `it`/`describe` declarations to allowlist.
_TEST_DECL = re.compile(
    r"\bdef\s+test\w*\s*\(|(?<![.\w])it\s*\(|(?<![.\w])test\s*\(|@Test\b"
    r"|(?<![.\w])describe\s*\(|(?<![.\w])test\.describe\s*\(",
)

# Assertion-ish calls across python/js/java.
_ASSERT = re.compile(
    r"\bassert\b|\bself\.assert\w+\s*\(|\bexpect\s*\(|\bassert\w*\s*\("
    r"|\bAssert\.|\bassertThat\s*\(|pytest\.raises|\.should\b",
)

# Skip / xfail / disable markers — neuter a test without touching its body.
_SKIP_MARK = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)\b"
    r"|@unittest\.skip\w*\b"
    r"|@(skip|skipUnless|skipIf|Disabled|Ignore)\b"
    r"|pytest\.skip\s*\(|pytest\.xfail\s*\("
    r"|\b(it|test|describe)\.skip\s*\("
    r"|\bx(it|describe|test)\s*\(",
)

# Tautological assertions — trivially true regardless of the code under test.
# Conservative on purpose: only unambiguous no-ops, not merely simple asserts.
_TAUTOLOGY = re.compile(
    r"\bassert\s+True\b"
    r"|\bassert\s+(?:not\s+False)\b"
    r"|\bassert\s+[0-9]+\s*(?:#|$|,)"          # assert 1  /  assert 1, "msg"
    r"|\bassert\s+(\w+)\s*==\s*\1\b"            # assert x == x
    r"|\bexpect\s*\(\s*true\s*\)\s*\.\s*(?:toBe\s*\(\s*true\s*\)|toBeTruthy)"
    r"|\bassertTrue\s*\(\s*True\s*\)",
)

# Behaviour-faking patch primitives: replace an attribute/object so a function
# *appears* to work without the real fix. Env/path setup is intentionally NOT
# here (setenv/chdir/syspath are legitimate fixture setup).
_FAKE_PATCH = re.compile(
    r"\bmonkeypatch\.setattr\b|\.setattr\s*\(|\bmock\.patch\b"
    r"|\bpatch\.object\b|\bpatch\s*\(|\bMagicMock\b|\breturn_value\b",
)
_AUTOUSE = re.compile(r"autouse\s*=\s*True")


def is_test_file(path: str) -> bool:
    return bool(_TEST_FILE_RE.search(path))


def count_tests(source: str) -> int:
    return len(_TEST_DECL.findall(source))


def count_assertions(source: str) -> int:
    return len(_ASSERT.findall(source))


def count_skips(source: str) -> int:
    return len(_SKIP_MARK.findall(source))


def count_tautologies(source: str) -> int:
    return len(_TAUTOLOGY.findall(source))


def count_faking_fixtures(source: str) -> int:
    """Number of autouse fixtures that also use a behaviour-faking patch.

    A fixture block is approximated as the file when both signals co-occur; we
    count the smaller of (autouse markers, fake-patch calls) so adding one
    autouse monkeypatch.setattr fixture counts as exactly one cheat signal.
    """
    if not _AUTOUSE.search(source):
        return 0
    autouse = len(_AUTOUSE.findall(source))
    patches = len(_FAKE_PATCH.findall(source))
    return min(autouse, patches)


@dataclass
class TamperReport:
    tampered: bool
    tests_before: int
    tests_after: int
    assertions_before: int
    assertions_after: int
    skips_before: int = 0
    skips_after: int = 0
    tautologies_before: int = 0
    tautologies_after: int = 0
    fake_fixtures_before: int = 0
    fake_fixtures_after: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        verdict = "TAMPERED" if self.tampered else "clean"
        return (
            f"[{verdict}] tests {self.tests_before}->{self.tests_after}, "
            f"assertions {self.assertions_before}->{self.assertions_after}, "
            f"skips {self.skips_before}->{self.skips_after}, "
            f"tautologies {self.tautologies_before}->{self.tautologies_after}, "
            f"fake-fixtures {self.fake_fixtures_before}->{self.fake_fixtures_after}"
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
    sb = sa = autb = auta = ffb = ffa = 0
    reasons: list[str] = []
    for p in sorted(paths):
        b_src = before.get(p, "")
        a_src = after.get(p, "")
        b_tests, b_asserts = count_tests(b_src), count_assertions(b_src)
        a_tests, a_asserts = count_tests(a_src), count_assertions(a_src)
        b_skips, a_skips = count_skips(b_src), count_skips(a_src)
        b_taut, a_taut = count_tautologies(b_src), count_tautologies(a_src)
        b_ff, a_ff = count_faking_fixtures(b_src), count_faking_fixtures(a_src)
        tb += b_tests
        ta += a_tests
        ab += b_asserts
        aa += a_asserts
        sb += b_skips
        sa += a_skips
        autb += b_taut
        auta += a_taut
        ffb += b_ff
        ffa += a_ff
        if p in before and p not in after:
            reasons.append(f"test file deleted: {p}")
        elif a_tests < b_tests:
            reasons.append(f"{p}: tests {b_tests}->{a_tests}")
        elif a_asserts < b_asserts:
            reasons.append(f"{p}: assertions {b_asserts}->{a_asserts}")
        if a_skips > b_skips:
            reasons.append(f"{p}: skip/xfail markers {b_skips}->{a_skips} (test neutered)")
        if a_taut > b_taut:
            reasons.append(
                f"{p}: tautological assertions {b_taut}->{a_taut} "
                f"(real assertion replaced by a no-op)")
        if a_ff > b_ff:
            reasons.append(
                f"{p}: autouse monkeypatch fixture {b_ff}->{a_ff} "
                f"(forces green without fixing product code)")

    tampered = ta < tb or aa < ab or sa > sb or auta > autb or ffa > ffb
    return TamperReport(
        tampered=tampered,
        tests_before=tb,
        tests_after=ta,
        assertions_before=ab,
        assertions_after=aa,
        skips_before=sb,
        skips_after=sa,
        tautologies_before=autb,
        tautologies_after=auta,
        fake_fixtures_before=ffb,
        fake_fixtures_after=ffa,
        reasons=reasons,
    )
