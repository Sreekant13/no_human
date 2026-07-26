"""Test-tampering guard (§3.4): net reduction in tests/assertions => fail closed."""

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
    ]
    unguarded = [
        "web/src/App.jsx",
        "web/src/overviewStrip.js",
        "web/e2e/board.mjs",
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
