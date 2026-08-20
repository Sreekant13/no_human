"""The published wheel must not resolve an MCP SDK without the module we import.

2026-08-20, found by running the exact command the official MCP Registry
publishes for this package:

    $ uvx no-human mcp-serve
    ModuleNotFoundError: No module named 'mcp.server.fastmcp'

`intake/mcp_bridge.py` imports `mcp.server.fastmcp`; the SDK removed that path
in 2.0.0. The requirement was `mcp>=1.28.0` with no upper bound, so a fresh
install from PyPI resolved 2.0.0 and `nh mcp-serve` — a documented entry point,
the Claude Code plugin's command, and the registry listing's command — died at
import. Nothing caught it because every lane that runs the code resolves
through `uv.lock`, which pins 1.29.0: CI, the MCP container, the desktop
bundles and every dev checkout were all testing a version the user never got.

These tests are about the DECLARED bound, not the locked one, because the
declared bound is the only thing a `pip install no-human` obeys.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

#: The first SDK version that does not ship `mcp.server.fastmcp`. Bumping the
#: bound past this without porting the import re-opens the bug.
FIRST_SDK_WITHOUT_FASTMCP = Version("2.0.0")


def _declared_mcp_requirement() -> Requirement:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    mcp = [d for d in deps if Requirement(d).name == "mcp"]
    assert len(mcp) == 1, f"expected exactly one mcp requirement, got {mcp}"
    return Requirement(mcp[0])


def test_the_declared_mcp_requirement_excludes_prereleases_of_that_sdk_too():
    """The bound must reject 2.x pre-releases, not only 2.0.0 final.

    PEP 440's exclusive `<2` already does (`SpecifierSet("<2").contains("2.0.0rc1",
    prereleases=True)` is False), so this passes today without a wider bound —
    it exists to catch the mutation that WOULD re-open the hole: raising the
    cap (`<3` admits every 2.x, pre-releases included).
    """
    req = _declared_mcp_requirement()
    for candidate in ("2.0.0a1", "2.0.0rc1", "2.1.0"):
        assert not req.specifier.contains(Version(candidate), prereleases=True), (
            f"`{req}` admits mcp {candidate}, which has no mcp.server.fastmcp")


def test_the_declared_mcp_requirement_excludes_the_sdk_that_dropped_fastmcp():
    req = _declared_mcp_requirement()
    assert not req.specifier.contains(FIRST_SDK_WITHOUT_FASTMCP, prereleases=True), (
        f"`{req}` allows mcp {FIRST_SDK_WITHOUT_FASTMCP}, which does not ship "
        "`mcp.server.fastmcp` — a fresh `pip install no-human` would resolve it "
        "and `nh mcp-serve` would die at import. Cap the requirement, or port "
        "`no_human/intake/mcp_bridge.py` off `mcp.server.fastmcp` first.")


def test_the_declared_mcp_requirement_still_admits_the_locked_version():
    """The cap must not be so tight that it excludes what we actually run."""
    req = _declared_mcp_requirement()
    lock = (ROOT / "uv.lock").read_text()
    marker = '\nname = "mcp"\nversion = "'
    locked = Version(lock.split(marker, 1)[1].split('"', 1)[0])
    assert req.specifier.contains(locked), (
        f"`{req}` excludes the locked mcp {locked} — the declared bound and the "
        "lockfile disagree about what this package runs on.")


def test_the_module_the_bridge_imports_exists_in_the_installed_sdk():
    """A control: the bound is only meaningful while this import is the one we
    make. If the bridge is ported to a different module path, this test — and
    the constant above — must move with it."""
    import importlib.util

    bridge = (ROOT / "src/no_human/intake/mcp_bridge.py").read_text()
    assert "from mcp.server.fastmcp import" in bridge, (
        "the bridge no longer imports mcp.server.fastmcp — update "
        "FIRST_SDK_WITHOUT_FASTMCP and this file to the new import")
    assert importlib.util.find_spec("mcp.server.fastmcp") is not None, (
        "the installed mcp SDK does not provide mcp.server.fastmcp")
