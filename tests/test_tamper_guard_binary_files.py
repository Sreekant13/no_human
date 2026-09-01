"""Repro + mirror tests: a binary file (e.g. a `.tgz` fixture) under `tests/`
must be skipped by the tamper guard, not crash it.

Incident: 2026-08-09, SWE-bench run halted at scoring — the guard read a
binary blob as text and a `UnicodeDecodeError` propagated out of
`tamper_check_between`, killing the whole run after all the compute was
spent. `tests/test_tamper_guard.py`, `tests/test_tamper_guard_evasions.py`
and `tests/test_tamper_adjudication.py` are out of scope for this fix and are
not touched here; this module only adds new, independent coverage.
"""

import subprocess
from pathlib import Path

from no_human.testing import tamper_guard


def _init_repo(path: Path) -> None:
    """Create a minimal git repo with one commit. Mirrors the helper in
    tests/test_tamper_guard.py (kept local, per the plan, to avoid touching
    that file)."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"],
                   capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"],
                   capture_output=True, check=True)


# --- AC1: a binary file under tests/ no longer raises; text files still counted --- #

def test_tamper_check_between_skips_a_binary_fixture_under_tests(tmp_path):
    """Fails before the fix with UnicodeDecodeError raised by _git_show."""
    from no_human.testing.runner import tamper_check_between

    repo = tmp_path / "binary_fixture_repo"
    repo.mkdir()
    _init_repo(repo)

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    real_test = tests_dir / "test_real.py"
    real_test.write_text("def test_a():\n    assert f() == 1\n    assert g() == 2\n")
    fixture = tests_dir / "fixture.tgz"
    fixture.write_bytes(bytes(range(128))[::-1] + b"\x00\x8f\xfe")

    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   capture_output=True, check=True)

    real_test.write_text(
        "def test_a():\n    assert f() == 1\n    assert g() == 2\n    assert h() == 3\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "add assertion"],
                   capture_output=True, check=True)

    report = tamper_check_between(repo)  # must not raise
    assert report.tampered is False
    assert report.assertions_after > report.assertions_before


def test_check_skips_binary_content_and_counts_the_text_file():
    text_before = "def test_a():\n    assert f() == 1\n"
    text_after = "def test_a():\n    assert f() == 1\n    assert g() == 2\n"
    binary = "\x00garbage� binary blob \x00"

    before_with_binary = {"tests/test_real.py": text_before, "tests/fixture.tgz": binary}
    after_with_binary = {"tests/test_real.py": text_after, "tests/fixture.tgz": binary}
    before_without = {"tests/test_real.py": text_before}
    after_without = {"tests/test_real.py": text_after}

    report_with = tamper_guard.check(before_with_binary, after_with_binary)  # must not raise
    report_without = tamper_guard.check(before_without, after_without)

    assert report_with.tests_before == report_without.tests_before
    assert report_with.tests_after == report_without.tests_after
    assert report_with.assertions_before == report_without.assertions_before
    assert report_with.assertions_after == report_without.assertions_after
    assert report_with.tautologies_before == report_without.tautologies_before
    assert report_with.tautologies_after == report_without.tautologies_after
    assert report_with.reasons == []


def test_deleting_a_binary_fixture_is_not_tampering():
    binary = "\x00binary\x00content\x8f"
    text = "def test_a():\n    assert f() == 1\n"
    before = {"tests/test_real.py": text, "tests/fixture.tgz": binary}
    after = {"tests/test_real.py": text}

    report = tamper_guard.check(before, after)

    assert report.tampered is False
    assert not any("test file deleted" in r for r in report.reasons)


def test_git_show_of_a_binary_blob_does_not_raise(tmp_path):
    from no_human.testing.runner import _git_show

    repo = tmp_path / "git_show_binary_repo"
    repo.mkdir()
    _init_repo(repo)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    fixture = tests_dir / "fixture.tgz"
    fixture.write_bytes(bytes(range(128))[::-1] + b"\x00\x8f\xfe")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   capture_output=True, check=True)

    src = _git_show(repo, "HEAD", "tests/fixture.tgz")  # must not raise

    assert isinstance(src, str)


# --- AC2: text-file behavior is unchanged (mirror of today's semantics) ----------- #

def test_unparseable_text_test_file_still_counts_exactly_as_today():
    # `def test_a(:` is invalid Python -> ast.parse raises SyntaxError inside
    # _mask_python_string_literals, so count_tautologies falls back to the raw
    # scan. Literal values pinned against the current implementation.
    source = "def test_a(:\n    assert True\n    assert x == x\n"
    assert tamper_guard.count_tests(source) == 1
    assert tamper_guard.count_assertions(source) == 2
    assert tamper_guard.count_tautologies(source) == 2
    assert tamper_guard.is_binary_content(source) is False


def test_text_file_assertion_drop_still_fires_alongside_a_binary_fixture():
    binary = "\x00tgz\x00bytes"
    before = {
        "tests/test_real.py": "def test_a():\n    assert f() == 1\n    assert g() == 2\n",
        "tests/fixture.tgz": binary,
    }
    after = {
        "tests/test_real.py": "def test_a():\n    assert f() == 1\n",
        "tests/fixture.tgz": binary,
    }

    report = tamper_guard.check(before, after)

    assert report.tampered is True
    assert any(
        "tests/test_real.py" in r and "assertions" in r for r in report.reasons
    )


def test_a_text_file_with_replacement_chars_is_still_counted():
    # Negative control: the sniff is NUL-only, so a `�` (decode
    # replacement char) with no NUL byte must NOT be treated as binary.
    source = "def test_a():\n    assert f() == 1  # �\n"
    assert tamper_guard.is_binary_content(source) is False
    assert tamper_guard.count_tests(source) == 1
    assert tamper_guard.count_assertions(source) == 1

    before = {"tests/test_real.py": "def test_a():\n    assert f() == 1  # �\n"}
    after = {
        "tests/test_real.py":
            "def test_a():\n    assert f() == 1  # �\n    assert g() == 2\n"
    }
    report = tamper_guard.check(before, after)
    assert report.tampered is False
    assert report.assertions_after == 2
