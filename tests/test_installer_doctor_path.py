"""SCRUM-119: the DMG reader gets a real install-health command path.

`docs/quickstart.md` used to steer a `.dmg` reader past section 3 with no
replacement — section 3 was their only pointer to `nh doctor`, and `nh doctor`
itself is documented as `uv run nh doctor` (a source-install invocation) with
no equivalent for someone who only has the installed app.

The fix is documentation-only, deliberately: `nh doctor` is already a
`@cli.command` in `src/no_human/cli/commands.py`, and `packaging/nh_entry.py`
statically imports `no_human.cli.commands` so PyInstaller's static analysis
bundles every subcommand into the frozen `nh` binary the app ships — no new
CLI surface was needed, just a documented path to the one that already
exists. These tests pin that path so it cannot silently regress back to
"see section 3" with nothing to replace it.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
QUICKSTART = (REPO / "docs" / "quickstart.md").read_text(encoding="utf-8")
INSTALLER = (REPO / "docs" / "INSTALLER.md").read_text(encoding="utf-8")

# The exact path a DMG reader runs, resolved from packaging/nh-server.spec /
# electron-builder.config.cjs's extraResources mapping (Contents/Resources).
NESTED_BINARY_PATH = "no_human.app/Contents/Resources/nh-server/nh"


def test_quickstart_dmg_section_gives_a_verification_command_not_just_a_skip():
    """The `.dmg` fast-path in quickstart.md must itself contain a runnable
    install-health command, not just tell the reader to skip past section 3
    (which is the bug this task fixes)."""
    dmg_section = QUICKSTART.split("## 1. Install prerequisites", 1)[0]
    assert "nh doctor" in dmg_section, (
        "the .dmg section of quickstart.md dropped its own `nh doctor` "
        "pointer -- a DMG reader has no way to verify their install again")
    assert NESTED_BINARY_PATH in dmg_section, (
        "the .dmg section must give the concrete path to the nested binary "
        "(no source checkout, no `uv run`), not just name the command")


def test_installer_doc_documents_the_nested_binary_doctor_invocation():
    """INSTALLER.md must show a friend the literal command, reachable from
    the installed .app, with no source install and no `uv run`."""
    assert NESTED_BINARY_PATH in INSTALLER
    section = INSTALLER.split("## Verify your install is real", 1)[1].split(
        "## Why the packaging looks the way it does", 1)[0]
    assert "uv run nh doctor" not in section, (
        "the friend-facing verification command must invoke the nested "
        "binary directly, not the source-install `uv run nh doctor` form")


def test_installer_doc_shows_both_healthy_and_unhealthy_exit_codes():
    """The doc must show real pass AND fail evidence -- not just one path --
    since a reader needs to recognise both a healthy and a broken install."""
    section = INSTALLER.split("## Verify your install is real", 1)[1].split(
        "## Why the packaging looks the way it does", 1)[0]
    assert "no contradictions, no evidence gaps" in section
    assert "CODING BACKEND UNUSABLE" in section


def test_installer_doc_states_evidence_scope_honestly():
    """A reviewer finding from the prior attempt: claims about the frozen
    binary's behaviour must say whether they were actually run, or admit
    they weren't (rather than reading as derived-from-source silently)."""
    section = INSTALLER.split("## Verify your install is real", 1)[1].split(
        "## Why the packaging looks the way it does", 1)[0]
    assert "Scope of this evidence" in section
    assert "electron-builder" in section


def test_installer_doc_covers_gatekeeper_on_the_nested_binary():
    """F6 review flagged this exact gap: quarantine/Gatekeeper refusing the
    NESTED binary (not just the .app) is the one failure mode a source read
    cannot rule out on its own, and it belongs in troubleshooting."""
    section = INSTALLER.split("## Verify your install is real", 1)[1]
    assert "spctl" in section
    assert "quarantine" in section.lower()
