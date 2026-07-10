"""The repo-map seed: cheap, capped, cached, and honest about being a hint."""

from __future__ import annotations

import subprocess

import pytest

import no_human.context.repo_map as rm


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)],
                   check=True, capture_output=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "class Engine:\n    def run(self):\n        pass\n"
        "    def _private(self):\n        pass\n"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")
    (tmp_path / "broken.py").write_text("def oops(:\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True)
    return tmp_path


def test_map_lists_files_and_python_symbols(repo):
    m = rm.build_repo_map(repo)
    assert "src/" in m and "calc.py" in m
    assert "add()" in m and "Engine(run)" in m
    assert "_private" not in m, "private methods are noise"
    assert "node_modules" not in m
    assert "broken.py" in m, "a syntax error drops symbols, not the file"


def test_map_respects_the_hard_cap(repo):
    for i in range(300):
        (repo / f"file_{i:03}.py").write_text(f"def fn_{i}():\n    pass\n")
    m = rm.build_repo_map(repo)
    assert len(m) <= rm.MAX_CHARS


def test_cache_is_per_head_and_reused(repo, tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(rm, "_CACHE_DIR", cache_dir)
    first = rm.repo_map(repo)
    files = list(cache_dir.glob("repomap-*.txt"))
    assert len(files) == 1
    # Change the tree WITHOUT a new commit: the cache (keyed on HEAD) answers.
    (repo / "src" / "new.py").write_text("def newer():\n    pass\n")
    assert rm.repo_map(repo) == first
    assert "newer" not in rm.repo_map(repo)
