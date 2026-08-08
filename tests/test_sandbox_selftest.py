"""The sandbox self-test: does a copy of a repo test the copy?

The check exists because a `src/` layout plus an editable install silently makes
a sandbox validate the ORIGINAL tree. Its own control is the positive case: a
detector that only ever reports "clean" is indistinguishable from one that
cannot see.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

from no_human.eval.sandbox_selftest import top_level_packages, wrong_tree_imports


def _pkg(root: Path, name: str, *, under_src: bool = False) -> Path:
    parent = (root / "src") if under_src else root
    (parent / name).mkdir(parents=True)
    (parent / name / "__init__.py").write_text(f'VALUE = "{root.name}"\n')
    return parent / name


def test_it_finds_packages_at_the_root_and_under_src(tmp_path):
    _pkg(tmp_path, "rootpkg")
    _pkg(tmp_path, "srcpkg", under_src=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    assert top_level_packages(tmp_path) == ["rootpkg", "srcpkg"]


def test_a_root_layout_sandbox_tests_itself(tmp_path):
    """The clean case — the package resolves from cwd, inside the sandbox."""
    work = tmp_path / "work"
    work.mkdir()
    _pkg(work, "thing")
    assert wrong_tree_imports(work) == []


def test_a_package_resolving_outside_the_sandbox_is_REPORTED(tmp_path):
    """The control that matters, and the exact live failure it was written for:
    the sandbox holds its own copy under src/, but the environment resolves the
    name to a DIFFERENT tree — so the agent's tests would grade that one."""
    other = tmp_path / "original"
    _pkg(other, "thing")
    work = tmp_path / "work"
    work.mkdir()
    _pkg(work, "thing", under_src=True)          # sandbox's own copy, not on sys.path

    env = {"PYTHONPATH": str(other), "PATH": "/usr/bin:/bin"}
    problems = wrong_tree_imports(work, env=env)

    assert len(problems) == 1, problems
    assert "thing" in problems[0]
    assert str(other) in problems[0]
    assert "OUTSIDE the sandbox" in problems[0]


def test_putting_the_sandbox_src_first_clears_it(tmp_path):
    """And the fix is detectable too: once the sandbox's own src wins, clean."""
    other = tmp_path / "original"
    _pkg(other, "thing")
    work = tmp_path / "work"
    work.mkdir()
    _pkg(work, "thing", under_src=True)

    env = {"PYTHONPATH": f"{work / 'src'}:{other}", "PATH": "/usr/bin:/bin"}
    assert wrong_tree_imports(work, env=env) == []


def test_an_unimportable_package_is_not_this_checks_business(tmp_path):
    """A package that cannot import at all is a different problem; reporting it
    here would make the signal noisy and get the check ignored."""
    work = tmp_path / "work"
    work.mkdir()
    pkg = _pkg(work, "broken")
    (pkg / "__init__.py").write_text("raise RuntimeError('boom')\n")
    assert wrong_tree_imports(work) == []


def test_a_missing_interpreter_does_not_break_the_run_it_protects(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _pkg(work, "thing")
    assert wrong_tree_imports(work, python="/definitely/not/a/python") == []
