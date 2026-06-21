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
