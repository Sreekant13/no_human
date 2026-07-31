"""Test-tampering guard (§3.4): net reduction in tests/assertions => fail closed."""

import pytest

from no_human.testing import tamper_guard

PY_TESTS = '''
def test_a():
    assert 1 == 1
    assert foo() == 2

def test_b():
    assert bar()
'''

PY_WEAKENED = '''
def test_a():
    assert 1 == 1

def test_b():
    pass
'''


def test_is_test_file():
    assert tamper_guard.is_test_file("tests/test_x.py")
    assert tamper_guard.is_test_file("src/foo_test.py")
    assert tamper_guard.is_test_file("web/Button.spec.tsx")
    assert tamper_guard.is_test_file("com/acme/FooIT.java")
    assert not tamper_guard.is_test_file("src/foo.py")


def test_is_test_file_table():
    """Table-driven: guarded vs unguarded paths, incl. .mjs/.cjs test files."""
    guarded = [
        "web/src/boardLanes.test.mjs",
        "desktop/main.test.mjs",
        "foo.spec.mjs",
        "foo.test.cjs",
        "foo.spec.cjs",
        "tests/test_x.py",
        "src/foo_test.py",
        "conftest.py",
        "src/pkg/conftest.py",
        "web/Button.test.js",
        "web/Button.test.ts",
        "web/Button.test.jsx",
        "web/Button.spec.tsx",
        "com/acme/FooTest.java",
        "com/acme/FooIT.java",
        "pkg/__tests__/x.js",
        # The UI gate. This ASSERTION MOVED from `unguarded` to `guarded` on
        # 2026-07-29, deliberately and with justification — it is not a test being
        # weakened to clear a path. It sat in the unguarded list with NO comment,
        # grouped with product code (App.jsx, foo.py), which reads as a snapshot of
        # what the patterns happened to match rather than a designed exclusion.
        # An execution audit then proved the consequence: all 14 e2e suites could be
        # deleted or gutted with `tampered=False`, and 417 lines of e2e counted as
        # ZERO. That is the gate catching what the unit suites structurally cannot —
        # a blank page from a ReferenceError, a `border` painting nothing because
        # Tailwind preflight is off, a light-theme regression.
        # KNOWN TRADEOFF: e2e suites are large and refactored often, so an honest
        # rewrite that nets fewer `check(` calls will now trip the guard and need a
        # human to justify it. That is the intended direction — the guard fires on a
        # net reduction and a person decides — and it is strictly better than a gate
        # that cannot be defended at all.
        "web/e2e/board.mjs",
    ]
    unguarded = [
        "web/src/App.jsx",
        "web/src/overviewStrip.js",
        "src/foo.py",
    ]
    for path in guarded:
        assert tamper_guard.is_test_file(path), f"expected guarded: {path}"
    for path in unguarded:
        assert not tamper_guard.is_test_file(path), f"expected unguarded: {path}"


def test_detects_mjs_test_gutting():
    """Gutting assertions out of a .test.mjs file must be caught (was a bug)."""
    before = {
        "web/src/boardLanes.test.mjs":
            "it('lanes render', () => {\n"
            "    expect(lanes.length).toBe(4);\n"
            "    expect(lanes[0].name).toBe('backlog');\n"
            "});\n"
    }
    after = {
        "web/src/boardLanes.test.mjs":
            "it('lanes render', () => {\n"
            "});\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert any("assertions" in r for r in report.reasons)


def test_clean_when_assertions_added():
    before = {"tests/test_x.py": PY_TESTS}
    after = {"tests/test_x.py": PY_TESTS + "\n    assert extra()\n"}
    report = tamper_guard.check(before, after)
    assert report.tampered is False


def test_detects_assertion_reduction():
    before = {"tests/test_x.py": PY_TESTS}
    after = {"tests/test_x.py": PY_WEAKENED}
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert report.assertions_after < report.assertions_before
    assert report.reasons


def test_detects_test_file_deletion():
    before = {"tests/test_x.py": PY_TESTS}
    after = {}
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert any("deleted" in r for r in report.reasons)


def test_ignores_non_test_files():
    before = {"src/app.py": "assert True\nassert False\n"}
    after = {"src/app.py": "pass\n"}
    report = tamper_guard.check(before, after)
    assert report.tampered is False  # not a test file; product code is fair game


def test_counts_across_languages():
    assert tamper_guard.count_tests("def test_x(): pass") == 1
    assert tamper_guard.count_tests("it('works', () => {})") == 1
    assert tamper_guard.count_tests("@Test\nvoid foo(){}") == 1
    assert tamper_guard.count_assertions("expect(x).toBe(1)") == 1
    assert tamper_guard.count_assertions("assertThat(x).isEqualTo(1)") >= 1


def test_regexp_test_call_is_not_a_declaration():
    """RegExp.prototype.test() calls must not be counted as test declarations."""
    assert tamper_guard.count_tests("re.test(x)") == 0
    assert tamper_guard.count_tests("/re/.test(x)") == 0
    assert tamper_guard.count_tests("foo.test(bar)") == 0
    assert tamper_guard.count_tests("assert.ok(!/transparent/.test(body));") == 0


def test_word_ending_in_test_is_not_a_declaration():
    """A variable/word ending in 'test' followed by '(' must not count."""
    assert tamper_guard.count_tests("latest(1)") == 0
    assert tamper_guard.count_tests("var x = latest(y);") == 0


def test_bare_test_call_still_counts():
    """A genuine standalone test(...) declaration must still be counted."""
    assert tamper_guard.count_tests("test('does a thing', () => {})") == 1
    assert tamper_guard.count_tests("  test(`templated ${x}`, () => {})") == 1


def test_mixed_declarations_and_regexp_calls_count_only_declarations():
    src = (
        "test('a', () => {\n"
        "  assert.ok(!/transparent/.test(body));\n"
        "});\n"
        "test('b', () => {\n"
        "  if (/x/.test(y)) { return; }\n"
        "});\n"
    )
    assert tamper_guard.count_tests(src) == 2


def test_bare_it_and_describe_still_count():
    """Genuine it()/describe() declarations must still be counted."""
    assert tamper_guard.count_tests("it('x', () => {})") == 1
    assert tamper_guard.count_tests("  it('indented', () => {})") == 1
    assert tamper_guard.count_tests("describe('suite', () => {})") == 1
    assert tamper_guard.count_tests("  describe('nested', () => {})") == 1


def test_playwright_test_describe_prefix_still_counts():
    """`test.describe(` (Playwright's suite form) is an allowlisted genuine declaration."""
    assert tamper_guard.count_tests('test.describe("suite", () => {})') == 1
    src = (
        "test.describe('outer', () => {\n"
        "  it('does a thing', () => {});\n"
        "});\n"
    )
    assert tamper_guard.count_tests(src) == 2


def test_describe_to_test_describe_refactor_is_not_tampered():
    """Renaming `describe(` to `test.describe(` is an honest refactor, not tampering.

    Reproduces the reported regression: the dot-prefix guard on `describe` alone
    would drop `test.describe(` to zero, making an honest Playwright-style suite
    rename look like a test-count decrease.
    """
    before = {
        "web/tests/x.spec.js":
            "describe('suite', () => {\n"
            "  it('does a thing', () => {});\n"
            "});\n"
    }
    after = {
        "web/tests/x.spec.js":
            "test.describe('suite', () => {\n"
            "  it('does a thing', () => {});\n"
            "});\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is False
    assert report.tests_before == report.tests_after == 2


def test_method_it_and_describe_are_not_declarations():
    """foo.it(...) / obj.describe(...) are method calls, not declarations."""
    assert tamper_guard.count_tests("foo.it(bar)") == 0
    assert tamper_guard.count_tests("obj.describe(x)") == 0
    assert tamper_guard.count_tests("suite.it(y)") == 0


def test_word_ending_in_it_or_describe_is_not_a_declaration():
    """A variable/word ending in 'it'/'describe' followed by '(' must not count."""
    assert tamper_guard.count_tests("edit(x)") == 0
    assert tamper_guard.count_tests("audit(y)") == 0


def test_check_honest_regexp_refactor_is_not_tampered():
    """Swapping a `.test(` regex helper for an equivalent one must not trip the guard.

    Reproduces the reported bug: before, `_TEST_DECL` counted `.test(` calls as
    test declarations, so replacing them with an equivalent non-`.test()` check
    dropped the apparent count and produced a false TAMPERED verdict.
    """
    before = {
        "web/src/x.test.mjs":
            "test('renders without a transparent background', () => {\n"
            "  const body = getBody();\n"
            "  assert.ok(!/background:\\s*transparent/.test(body));\n"
            "});\n"
    }
    after = {
        "web/src/x.test.mjs":
            "test('renders without a transparent background', () => {\n"
            "  const body = getBody();\n"
            "  assert.ok(!includesTransparentBackground(body));\n"
            "});\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is False
    assert report.tests_before == report.tests_after == 1


# --------------------------------------------------------------------------- #
# Isolation-corpus attacks: cheats that keep test/assertion counts unchanged   #
# --------------------------------------------------------------------------- #

def test_conftest_is_a_test_file():
    assert tamper_guard.is_test_file("conftest.py")
    assert tamper_guard.is_test_file("src/pkg/conftest.py")


def test_detects_skip_marker_neutering():
    """@pytest.mark.skip leaves test + assertion counts identical but lies."""
    before = {"test_x.py": "def test_divide():\n    assert divide(7, 2) == 3.5\n"}
    after = {
        "test_x.py":
            "import pytest\n\n@pytest.mark.skip\ndef test_divide():\n"
            "    assert divide(7, 2) == 3.5\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert report.assertions_after == report.assertions_before  # count unchanged
    assert any("skip" in r.lower() for r in report.reasons)


def test_detects_xfail_marker():
    before = {"test_x.py": "def test_a():\n    assert f() == 1\n"}
    after = {"test_x.py": "import pytest\n@pytest.mark.xfail\ndef test_a():\n    assert f() == 1\n"}
    assert tamper_guard.check(before, after).tampered is True


def test_detects_tautology_substitution():
    """Replacing a real assertion with `assert True` keeps the count at 1."""
    before = {"test_x.py": "def test_divide():\n    assert divide(7, 2) == 3.5\n"}
    after = {"test_x.py": "def test_divide():\n    assert True\n"}
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert report.assertions_after == report.assertions_before  # still one `assert`
    assert any("tautolog" in r.lower() for r in report.reasons)


def test_detects_self_equality_tautology():
    before = {"test_x.py": "def test_a():\n    assert result == 42\n"}
    after = {"test_x.py": "def test_a():\n    assert result == result\n"}
    assert tamper_guard.check(before, after).tampered is True


def test_detects_conftest_autouse_monkeypatch():
    """A conftest autouse fixture that patches the SUT green without a real fix."""
    before = {}
    after = {
        "conftest.py":
            "import pytest\n\n"
            "@pytest.fixture(autouse=True)\n"
            "def _fake(monkeypatch):\n"
            "    monkeypatch.setattr('calc.divide', lambda a, b: 3.5)\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is True
    assert any("fixture" in r.lower() for r in report.reasons)


def test_legit_autouse_env_fixture_is_clean():
    """An autouse fixture that only sets env (no behaviour-faking patch) is fine."""
    before = {}
    after = {
        "conftest.py":
            "import pytest\n\n"
            "@pytest.fixture(autouse=True)\n"
            "def _env(monkeypatch):\n"
            "    monkeypatch.setenv('TZ', 'UTC')\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is False


def test_legit_added_real_assertions_with_helpers_clean():
    """Adding genuine assertions (not tautologies) stays clean."""
    before = {"test_x.py": "def test_a():\n    assert f() == 1\n"}
    after = {"test_x.py": "def test_a():\n    assert f() == 1\n    assert g() == 2\n"}
    report = tamper_guard.check(before, after)
    assert report.tampered is False


# --- PR-F Gate 1: linked-repo tamper detection ----------------------------- #

import subprocess
from pathlib import Path


def _init_repo(path: Path) -> None:
    """Create a minimal git repo with one commit."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"],
                   capture_output=True, check=True)


def test_tamper_check_between_detects_linked_repo_tampering(tmp_path):
    """tamper_check_between detects test weakening in a linked repo."""
    from no_human.testing.runner import tamper_check_between

    repo = tmp_path / "linked"
    repo.mkdir()
    _init_repo(repo)

    # First commit: a test file with two assertions.
    test_file = repo / "test_foo.py"
    test_file.write_text("def test_a():\n    assert f() == 1\n    assert g() == 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   capture_output=True, check=True)

    # Second commit: weaken the test (remove an assertion).
    test_file.write_text("def test_a():\n    assert f() == 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "weaken"],
                   capture_output=True, check=True)

    report = tamper_check_between(repo)
    assert report.tampered is True
    assert any("assertions" in r for r in report.reasons)


def test_tamper_check_between_clean_linked_repo(tmp_path):
    """tamper_check_between passes when linked repo tests are not weakened."""
    from no_human.testing.runner import tamper_check_between

    repo = tmp_path / "linked_clean"
    repo.mkdir()
    _init_repo(repo)

    # First commit: a test file.
    test_file = repo / "test_bar.py"
    test_file.write_text("def test_a():\n    assert x() == 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   capture_output=True, check=True)

    # Second commit: ADD more assertions (legit improvement).
    test_file.write_text("def test_a():\n    assert x() == 1\n    assert y() == 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "improve"],
                   capture_output=True, check=True)

    report = tamper_check_between(repo)
    assert report.tampered is False


def test_the_e2e_gate_is_visible_to_the_guard_and_its_assertions_are_counted():
    """The UI gate was invisible: 14 suites deletable with `tampered=False`.

    `web/e2e/*.mjs` matched none of the path patterns — not `tests/`, not
    `*.test.*`, not `*.spec.*` — so an execution audit measured 417 lines of e2e
    counted as ZERO. That is the gate catching what the unit suites structurally
    cannot: a blank page from a ReferenceError, a `border` that paints nothing
    because Tailwind preflight is off, a light-theme regression.

    Both halves are required, and the path alone would have been a VACUOUS fix:
    these suites assert through a local `check(name, ok, detail)` helper, so
    without teaching `_ASSERT` about `check(` they would be counted as test files
    with zero assertions — deleting one would still show no reduction.
    """
    assert tamper_guard.is_test_file("web/e2e/form-order.mjs")
    assert tamper_guard.is_test_file("web/e2e/board.mjs")
    # Product code must stay OUT — the counting is gated on is_test_file().
    assert not tamper_guard.is_test_file("src/no_human/core/orchestrator.py")
    assert not tamper_guard.is_test_file("web/src/cost.js")

    suite = (
        'const check = (n, ok, d = "") => { if (!ok) failures.push(n); };\n'
        'check("a", x === 1);\ncheck("b", y === 2);\ncheck("c", z === 3);\n'
    )
    # 3 CALLS. The definition `const check = (n, ok, d) =>` is `check =`, not
    # `check(`, so it is correctly not counted as an assertion.
    assert tamper_guard.count_assertions(suite) == 3, (
        "the e2e assertion form must be counted, or the path pattern protects nothing"
    )
    # A gutted suite must show a REAL drop, which is what makes the guard fire.
    gutted = 'const check = (n, ok, d = "") => {};\n'
    assert tamper_guard.count_assertions(gutted) < tamper_guard.count_assertions(suite)

    # False positives that would make honest edits look like tampering.
    assert tamper_guard.count_assertions("subprocess.run(x, check=True)") == 0
    assert tamper_guard.count_assertions("el.checkVisibility({a: 1})") == 0


def test_deleting_a_test_file_is_tampering_even_when_the_counts_do_not_move():
    # `tampered` was computed from AGGREGATE counts while `reasons` recorded per-file
    # deletions independently, so the report could NAME a deleted test file and still
    # return tampered=False. An audit proved it against real suites whose assertions
    # count zero — delete electron-smoke.mjs or live-flows.mjs and no total moves.
    before = {"web/e2e/zero.mjs": "// a suite with no counted assertions\n"}
    after: dict[str, str] = {}
    r = tamper_guard.check(before, after)
    assert r.tests_before == r.tests_after, "the counts genuinely do not move"
    assert r.assertions_before == r.assertions_after
    assert any("deleted" in x for x in r.reasons), r.reasons
    assert r.tampered, (
        f"a deleted test file must be tampering even when totals are flat: {r.reasons}"
    )

    # Control: an untouched file is not tampering.
    same = tamper_guard.check(before, dict(before))
    assert not same.tampered, same.reasons


def test_check_is_dot_guarded_so_product_calls_in_tests_are_not_assertions():
    # A bare `check(` is this repo's e2e assertion helper. A DOTTED
    # `tamper_guard.check(` or `self.check(` is a call to a product function, and an
    # audit found 24 miscounted across three test files — 19 in this very file, i.e.
    # calls to the function under test, so an honest refactor here read as tampering.
    #
    # This exists because removing the dot guard left the whole suite green: the fix
    # had two halves and only one was pinned.
    assert tamper_guard.count_assertions('check("a", ok);') == 1
    assert tamper_guard.count_assertions("tamper_guard.check(before, after)") == 0
    assert tamper_guard.count_assertions("self.check(x)") == 0
    # And the controls that were NOT the live false positive, kept for completeness.
    assert tamper_guard.count_assertions("run(x, check=True)") == 0
    assert tamper_guard.count_assertions("el.checkVisibility({})") == 0


# --- fixture snapshots are not the project's own tests -----------------------
#
# `eval/reviewer_recall/cases/<id>/base/**` materialises frozen copies of product
# source so a reviewer can be replayed against a known base. Some of those copies
# ARE real `tests/test_*.py` files, verbatim. They never execute (`testpaths =
# ["tests"]` plus `eval/conftest.py`'s `collect_ignore_glob = ["*"]`), so they
# cannot fake a green suite — and counting them was actively exploitable.

_FIXTURE_DIR = "eval/reviewer_recall/cases/control-gate-excerpts/base"


def test_fixture_snapshots_are_excluded_by_path_shape_only():
    """The exclusion is a path shape an agent cannot invoke from inside a file.

    All five segments are load-bearing: a top-level ``eval/``, one corpus
    directory, the literal ``cases/``, one case id, the literal ``base/``.
    """
    assert not tamper_guard.is_test_file(f"{_FIXTURE_DIR}/tests/test_vcs.py")
    assert not tamper_guard.is_test_file(f"{_FIXTURE_DIR}/tests/conftest.py")
    assert not tamper_guard.is_test_file(
        "eval/reviewer_recall/cases/x/base/web/src/boardLanes.test.mjs"
    )
    assert tamper_guard.is_fixture_content(f"{_FIXTURE_DIR}/tests/test_vcs.py")
    # ACKNOWLEDGED BREADTH: the rule is a shape, not this repo's corpus name, so a
    # second benchmark corpus under eval/ gets the same treatment. That is
    # deliberate — this module is repo-agnostic and also runs against linked and
    # user repos — and it costs nothing an attacker can use, because arriving at
    # this path requires leaving another one (see the relocation test below).
    assert not tamper_guard.is_test_file("eval/other_corpus/cases/x/base/tests/test_a.py")

    # Everything one step off the shape stays GUARDED. Each of these is a place an
    # agent could try to park a real suite if the rule were any looser.
    still_guarded = [
        # `eval/` at large is an ordinary directory — never blanket-excluded.
        "eval/tests/test_x.py",
        "eval/reviewer_recall/tests/test_x.py",
        "eval/reviewer_recall/cases/tests/test_x.py",
        # ...outside a case's `base/` subtree.
        "eval/reviewer_recall/cases/x/tests/test_x.py",
        "eval/reviewer_recall/cases/x/base_extra/tests/test_x.py",
        "eval/reviewer_recall/cases/x/notbase/tests/test_x.py",
        # ...nested two case-id levels deep (the shape is exactly one).
        "eval/reviewer_recall/cases/x/y/base/tests/test_x.py",
        # ...a lookalike prefix.
        "evaluation/reviewer_recall/cases/x/base/tests/test_x.py",
        "other_eval/reviewer_recall/cases/x/base/tests/test_x.py",
        # ...the same shape buried under another directory: the rule is anchored at
        # the repo root, so a nested copy is NOT a free pass.
        "vendor/eval/reviewer_recall/cases/x/base/tests/test_x.py",
        "src/eval/reviewer_recall/cases/x/base/tests/test_x.py",
        # The project's own tests, unchanged.
        "tests/test_vcs.py",
        "web/e2e/board.mjs",
        "conftest.py",
    ]
    for path in still_guarded:
        assert tamper_guard.is_test_file(path), f"expected still guarded: {path}"
        assert not tamper_guard.is_fixture_content(path), path


def test_no_in_file_marker_can_opt_a_real_test_out_of_the_guard():
    """There is no magic comment. Only the path decides."""
    for marker in (
        "# tamper: ignore\n",
        "# fixture content, not a test\n",
        "# noqa: tamper\n",
        '"""base fixture snapshot"""\n',
        "# eval/reviewer_recall/cases/x/base/tests/test_x.py\n",
    ):
        before = {"tests/test_x.py": marker + "def test_a():\n    assert f()\n"}
        after = {"tests/test_x.py": marker + "def test_a():\n    pass\n"}
        r = tamper_guard.check(before, after)
        assert r.tampered, f"a comment must not buy an exemption: {marker!r}"


def test_fixture_snapshots_do_not_dilute_a_real_reduction():
    """The dilution hazard, closed rather than moved.

    `tamper_check_between` compares merge-base..HEAD, so a branch that ADDS corpus
    cases used to carry their whole count (+735 tests / +2004 assertions) into the
    aggregate. `check()` decides `tampered` from the aggregate while `reasons`
    records per-file drops independently — so the report NAMED the reduction and
    still returned clean. Reproduced end-to-end against the real corpus: a branch
    that added 68 snapshot files and deleted 3 tests from a real `tests/test_vcs.py`
    read `[clean] tests 141->813` with `tests/test_vcs.py: tests 42->39` in reasons.
    """
    real_before = "def test_a():\n    assert f()\n\ndef test_b():\n    assert g()\n"
    real_after = "def test_a():\n    assert f()\n"          # one test, one assert gone
    bulk = "".join(f"def test_s{i}():\n    assert h({i})\n" for i in range(200))

    before = {"tests/test_x.py": real_before}
    after = {
        "tests/test_x.py": real_after,
        f"{_FIXTURE_DIR}/tests/test_snapshot.py": bulk,
    }
    r = tamper_guard.check(before, after)
    assert r.tampered, f"a real reduction must survive 200 added fixture tests: {r}"
    assert any("tests/test_x.py" in x for x in r.reasons), r.reasons

    # Closed, not moved: the aggregate must equal the no-fixture control exactly.
    control = tamper_guard.check(before, {"tests/test_x.py": real_after})
    assert (r.tests_before, r.tests_after) == (control.tests_before, control.tests_after)
    assert (r.assertions_before, r.assertions_after) == (
        control.assertions_before, control.assertions_after
    )
    assert r.tests_after < r.tests_before, r.summary


def test_relocating_a_real_test_into_a_fixture_path_is_still_tampering():
    """Excluding a path must not make it a laundry for real suites.

    A move removes the file from its old path, and the deleted-file rule is
    unconditional, so the smuggle is caught by the DISAPPEARANCE — verbatim or
    gutted, it makes no difference.
    """
    real = "def test_a():\n    assert f()\n\ndef test_b():\n    assert g()\n"
    before = {"tests/test_x.py": real}

    verbatim = tamper_guard.check(before, {f"{_FIXTURE_DIR}/tests/test_x.py": real})
    assert verbatim.tampered, verbatim
    assert any("deleted: tests/test_x.py" in x for x in verbatim.reasons), verbatim.reasons
    # And the counts drop too, so it fires twice over rather than on one rule.
    assert verbatim.tests_after < verbatim.tests_before, verbatim.summary

    gutted = tamper_guard.check(
        before, {f"{_FIXTURE_DIR}/tests/test_x.py": "def test_a():\n    pass\n"}
    )
    assert gutted.tampered, gutted
    assert any("deleted: tests/test_x.py" in x for x in gutted.reasons), gutted.reasons


def test_fixture_snapshots_do_not_raise_false_cheat_signals():
    """Verbatim copies of signals already live and accepted in `tests/`.

    `tests/test_vcs.py` seeds a fixture repo with the literal string
    `"assert True\\n"`, and `tests/test_jira_issues_endpoint.py` has a legitimate
    `@pytest.fixture(autouse=True)` that monkeypatches config PATHS. Snapshotting
    either registered `tautological assertions 0->1` / `autouse monkeypatch fixture
    0->1` on a branch that touched no test at all.
    """
    snapshot = (
        'def test_seed(repo):\n'
        '    (repo.path / "test_thing.py").write_text("assert True\\n")\n'
        '\n'
        '@pytest.fixture(autouse=True)\n'
        'def _isolated_paths(tmp_path, monkeypatch):\n'
        '    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")\n'
    )
    # The content really does carry both signals — this is a live false positive,
    # not a hypothetical one.
    assert tamper_guard.count_tautologies(snapshot) == 1
    assert tamper_guard.count_faking_fixtures(snapshot) == 1

    r = tamper_guard.check(
        {"tests/test_x.py": "def test_a():\n    assert f()\n"},
        {
            "tests/test_x.py": "def test_a():\n    assert f()\n",
            f"{_FIXTURE_DIR}/tests/test_snapshot.py": snapshot,
        },
    )
    assert not r.tampered, r.reasons
    assert r.reasons == [], r.reasons

    # Control: the SAME content under a real test path still fires, on both rules.
    live = tamper_guard.check(
        {"tests/test_x.py": "def test_a():\n    assert f()\n"},
        {
            "tests/test_x.py": "def test_a():\n    assert f()\n",
            "tests/test_snapshot.py": snapshot,
        },
    )
    assert live.tampered, live
    assert any("tautological" in x for x in live.reasons), live.reasons
    assert any("autouse" in x for x in live.reasons), live.reasons


def test_known_cost_deleting_a_fixture_snapshot_is_no_longer_flagged():
    """The one case where this exclusion is strictly WEAKER. Named, not hidden.

    Before the exclusion, deleting a materialised snapshot tripped the
    unconditional deleted-test-file rule. It no longer does — the path is not a
    test file, so it never enters `before`. That is the unavoidable price of not
    counting them: you cannot both ignore a path and police deletions in it.

    Why it is an acceptable price:
      * these files never execute, so deleting one cannot turn a red suite green
        — which is the only thing this guard exists to prevent;
      * corpus integrity has its own signal in the LIVE suite
        (`tests/test_reviewer_recall_runner.py::test_load_cases_real_corpus`
        pins the case count, and the recall runner raises on a case whose `base/`
        is missing), and that live test is itself still guarded here, so it
        cannot be deleted to cover the tracks;
      * the diff is still in front of the human reviewer, which is the gate.
    """
    snapshot = "def test_a():\n    assert f()\n"

    def snapshot_repo(files):
        """What `runner.tamper_check_between` hands to `check()`: git's file list
        run through `is_test_file` first. Model it, don't shortcut it."""
        return {p: s for p, s in files.items() if tamper_guard.is_test_file(p)}

    live = {"tests/test_x.py": "def test_a():\n    assert f()\n"}
    r = tamper_guard.check(
        snapshot_repo({**live, f"{_FIXTURE_DIR}/tests/test_snapshot.py": snapshot}),
        snapshot_repo(live),
    )
    assert not r.tampered, r.reasons

    # FAIL-CLOSED at the `check()` boundary: the deleted-file rule reads the raw
    # `before` mapping and does NOT consult `is_test_file`, so a caller that hands
    # `check()` a fixture path directly still gets a tamper verdict. The exclusion
    # is not a hole punched through `check()` itself — it lives entirely in which
    # paths the caller collects.
    direct = tamper_guard.check(
        {f"{_FIXTURE_DIR}/tests/test_snapshot.py": snapshot}, {}
    )
    assert direct.tampered, "check() must stay fail-closed on any path it is given"

    # The compensating control: the LIVE test that pins corpus integrity is an
    # ordinary guarded test file, so it cannot be removed quietly.
    assert tamper_guard.is_test_file("tests/test_reviewer_recall_runner.py")
    gone = tamper_guard.check(
        {"tests/test_reviewer_recall_runner.py": snapshot}, {}
    )
    assert gone.tampered, gone.reasons


def test_tamper_check_refuses_a_missing_worktree_instead_of_crashing(tmp_path):
    """A guard that cannot look must say so, not answer "clean".

    Observed twice on 2026-07-31: a resumed attempt whose worktree had been
    removed reached tamper_check_between and died on an opaque
    FileNotFoundError raised by a subprocess several frames down, taking the
    worker pool's task with it and leaving no diagnosable cause. The failure
    mode that would be WORSE is a clean TamperReport — the guard would then
    launder "I did not look" into "nothing was wrong", which is exactly the
    reward-hacking window it exists to close. So this asserts the error, and
    asserts it is not a passing report.
    """
    from no_human.testing.runner import TamperCheckUnavailable, tamper_check_between

    missing = tmp_path / "worktree-that-was-removed"
    with pytest.raises(TamperCheckUnavailable) as exc:
        tamper_check_between(missing)
    assert str(missing) in str(exc.value), "the error must name the path"

    # A directory that exists but is not a checkout is the same class of
    # "cannot inspect" — a bare mkdir left behind by a half-torn-down worktree.
    not_a_checkout = tmp_path / "empty"
    not_a_checkout.mkdir()
    with pytest.raises(TamperCheckUnavailable):
        tamper_check_between(not_a_checkout)
