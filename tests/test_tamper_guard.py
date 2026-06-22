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
