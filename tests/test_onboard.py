"""`nh onboard` recon: deterministic derivation, agentic-deriver parsing, and
REAL end-to-end proving on two different ecosystems (Python+pytest and Node)
with no code path differing between them — the Phase-4 DoD."""

import json
import shutil

import pytest

from no_human.onboard import (
    AgentDeriver,
    DeclarationDeriver,
    OnboardEngine,
)
from no_human.profile import PROFILE_RELPATH, ProjectProfile

# --------------------------------------------------------------------------- #
# fixtures: tiny but real repos in tmp dirs                                    #
# --------------------------------------------------------------------------- #


def _python_repo(root, *, passing=True):
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.0'\n\n[tool.ruff]\nline-length = 100\n"
    )
    body = "def test_ok():\n    assert 1 + 1 == 2\n" if passing else \
           "def test_bad():\n    assert 1 + 1 == 3\n"
    (root / "test_demo.py").write_text(body)
    return root


def _node_repo(root):
    (root / "package.json").write_text(json.dumps({
        "name": "demo", "version": "0.0.0",
        "scripts": {"test": 'node -e "process.exit(0)"', "lint": 'node -e "process.exit(0)"'},
    }))
    return root


# --------------------------------------------------------------------------- #
# DeclarationDeriver — reads the repo's own declarations                       #
# --------------------------------------------------------------------------- #


def test_derive_python_pytest(tmp_path):
    d = DeclarationDeriver().derive(_python_repo(tmp_path))
    assert d.ecosystem == "python-pytest"
    tests = [c.command for c in d.of_kind("test")]
    assert tests == ["pytest -q"]            # no lockfile → plain pytest
    assert any(c.command == "ruff check ." for c in d.of_kind("lint"))


def test_derive_python_uv_lock(tmp_path):
    _python_repo(tmp_path)
    (tmp_path / "uv.lock").write_text("# lock\n")
    d = DeclarationDeriver().derive(tmp_path)
    assert [c.command for c in d.of_kind("install")] == ["uv sync"]
    assert [c.command for c in d.of_kind("test")] == ["uv run pytest -q"]
    assert any(c.command == "uv run ruff check ." for c in d.of_kind("lint"))


def test_derive_node_scripts(tmp_path):
    d = DeclarationDeriver().derive(_node_repo(tmp_path))
    assert d.ecosystem == "node"
    assert [c.command for c in d.of_kind("install")] == ["npm install"]
    assert [c.command for c in d.of_kind("test")] == ["npm test"]
    assert d.of_kind("test")[0].source == "package.json:scripts.test"


def test_derive_node_lockfile_npm_ci(tmp_path):
    _node_repo(tmp_path)
    (tmp_path / "package-lock.json").write_text("{}")
    d = DeclarationDeriver().derive(tmp_path)
    assert [c.command for c in d.of_kind("install")] == ["npm ci"]


def test_derive_ci_and_human_gates(tmp_path):
    _python_repo(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [test]\n")
    (tmp_path / "Jenkinsfile").write_text("pipeline {}\n")
    d = DeclarationDeriver().derive(tmp_path)
    assert d.ci == {"backend": "gitlab"}
    assert any("Jenkins" in s for s in d.human_gated_steps)


def test_derive_makefile_fallback(tmp_path):
    # A repo whose only declaration is a Makefile with a test target.
    (tmp_path / "Makefile").write_text("test:\n\techo hi\ninstall:\n\techo dep\n")
    d = DeclarationDeriver().derive(tmp_path)
    assert d.ecosystem == "make"
    assert [c.command for c in d.of_kind("test")] == ["make test"]


# --------------------------------------------------------------------------- #
# AgentDeriver — parses a fenced JSON block, never proves                      #
# --------------------------------------------------------------------------- #


class _FakeBackend:
    def __init__(self, text):
        self._text = text

    async def run(self, prompt, *, cwd, max_turns, effort=None, **kw):
        class _R:
            final_text = self._text
        return _R()


def test_agent_deriver_parses_json_block():
    blob = (
        "Here is what I found:\n```json\n"
        + json.dumps({
            "ecosystem": "rust",
            "ci": {"backend": "github_actions"},
            "human_gated_steps": ["release gated"],
            "candidates": [
                {"kind": "test", "command": "cargo test", "source": "Cargo.toml"},
                {"kind": "bogus", "command": "x", "source": "y"},   # dropped
            ],
        })
        + "\n```\n"
    )
    d = AgentDeriver.parse(blob)
    assert d.ecosystem == "rust"
    assert [c.command for c in d.candidates] == ["cargo test"]   # bogus kind dropped
    assert d.ci == {"backend": "github_actions"}


def test_agent_deriver_no_block_is_empty():
    assert AgentDeriver.parse("no json here").candidates == []


@pytest.mark.asyncio
async def test_agent_deriver_runs_readonly_backend(tmp_path):
    backend = _FakeBackend('```json\n{"candidates": [{"kind": "test", '
                           '"command": "make check", "source": "Makefile"}]}\n```')
    d = await AgentDeriver(backend).derive(tmp_path)
    assert [c.command for c in d.candidates] == ["make check"]


# --------------------------------------------------------------------------- #
# OnboardEngine — DoD: prove TWO ecosystems with the SAME code path            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_onboard_python_pytest_end_to_end(tmp_path):
    repo = _python_repo(tmp_path)
    result = await OnboardEngine().onboard(repo)
    prof = result.profile
    assert prof.ecosystem == "python-pytest"
    assert prof.test_cmd == "pytest -q"
    assert prof.proven.get("test_cmd") is True          # actually ran, exit 0
    # not usable until a human confirms — proof alone is not trust.
    assert prof.is_usable is False
    prof.confirmed = True
    assert prof.is_usable is True
    assert any(p.kind == "test" and p.ok for p in result.proofs)


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
@pytest.mark.asyncio
async def test_onboard_node_end_to_end(tmp_path):
    repo = _node_repo(tmp_path)
    result = await OnboardEngine().onboard(repo)
    prof = result.profile
    assert prof.ecosystem == "node"
    assert prof.test_cmd == "npm test"
    assert prof.proven.get("test_cmd") is True
    assert prof.install_cmd == "npm install"
    assert prof.proven.get("install_cmd") is True


@pytest.mark.asyncio
async def test_onboard_does_not_fake_a_failing_test(tmp_path):
    # A repo whose test FAILS must not be marked proven — no faking.
    repo = _python_repo(tmp_path, passing=False)
    result = await OnboardEngine().onboard(repo)
    prof = result.profile
    assert prof.proven.get("test_cmd") is not True
    assert prof.is_usable is False
    assert any(p.kind == "test" and not p.ok for p in result.proofs)


@pytest.mark.asyncio
async def test_onboard_writes_yaml_and_round_trips(tmp_path):
    repo = _python_repo(tmp_path)
    result = await OnboardEngine().onboard(repo)
    path = result.profile.save()
    assert path == tmp_path / PROFILE_RELPATH
    loaded = ProjectProfile.load(tmp_path)
    assert loaded.test_cmd == "pytest -q"
    assert loaded.proven.get("test_cmd") is True
