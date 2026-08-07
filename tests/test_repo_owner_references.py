"""The repository the shipped product points at must be a repository that exists.

WHY THIS GUARD EXISTS. The org moved from a personal account to `no-human-ai`,
and the references did not all move with it. One of the stragglers was a live
404 in a `ship`-classified file, and another was worse than a dead link: the
published egress disclosure named a *different* owner than the desktop
updater's actual feed config. A security page that misstates which repository
the app phones home to is a factual error in the one document class where an
owner mismatch matters.

WHY IT IS NOT A HAND-WRITTEN FILE LIST. The obvious version of this check is a
list of files someone remembered to look at. That shape has failed in this repo
repeatedly: a list cannot catch the case the guard exists for, because the case
the guard exists for is the file nobody listed. So the second test below
DISCOVERS its own corpus — it asks the export classifier for the whole ship set
and scans every text file in it. Adding a new shipped file brings it under the
guard automatically, with nobody remembering anything.

WHY THE FIRST TEST IS A CROSS-CHECK, NOT A PINNED CONSTANT. Pinning the owner
to a literal here would just create a third copy of the same fact, free to drift
from the other two. Instead the disclosure and the config are read from their
own files and required to AGREE. Neither is the authority; the disagreement is
the defect. If someone changes the feed, this goes red until the page that
discloses it says the same thing.

WHAT THIS CANNOT SEE. Only text files in the ship set, and only the two
reference shapes below (a repo URL, and an `owner:`/`owner=` assignment). A
reference built at runtime by concatenation, or one living in a binary asset, is
invisible here. It also cannot tell you whether the CURRENT owner's repo exists
or is reachable — that needs the network, and this suite does not use it.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_EXPORT_SCRIPT = REPO_ROOT / "scripts" / "build_public_export.py"
_SELF = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()


def _load_export_script():
    spec = importlib.util.spec_from_file_location("_bpe_owner_guard",
                                                  _EXPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # dataclasses needs this registered
    spec.loader.exec_module(module)
    return module


_export = _load_export_script()


def _shipped() -> list[str]:
    """The ship set, from the repo's own classifier — never from a literal list."""
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                                  text=True)
    tracked = sorted(p for p in out.split("\0") if p)
    rules = _export.parse_classification(
        (REPO_ROOT / _export.CLASSIFICATION_NAME).read_text(encoding="utf-8"))
    return _export.classify(rules, tracked).shipped


# The disclosure states the feed in prose; the config states it as a JS literal.
_DISCLOSED_FEED = re.compile(
    r"owner:\s*([A-Za-z0-9._-]+),\s*repo:\s*([A-Za-z0-9._-]+)")
_CONFIGURED_FEED = re.compile(
    r"""owner:\s*["']([^"']+)["']\s*,\s*repo:\s*["']([^"']+)["']""")


def test_the_disclosed_update_feed_is_the_configured_update_feed():
    """`docs/security.md` and the updater config must name the same repository.

    Both halves are asserted to have been FOUND before they are compared. A
    reworded disclosure that no longer matches the shape goes red rather than
    silently comparing nothing — a guard that can quietly stop looking is the
    failure mode this whole file is about.
    """
    disclosure = (REPO_ROOT / "docs" / "security.md").read_text(encoding="utf-8")
    config = (REPO_ROOT / "desktop" / "electron-builder.config.cjs"
              ).read_text(encoding="utf-8")

    disclosed = _DISCLOSED_FEED.search(disclosure)
    configured = _CONFIGURED_FEED.search(config)

    assert disclosed is not None, (
        "docs/security.md no longer states the desktop update feed as "
        "`owner: <owner>, repo: <repo>`. Either the disclosure lost the fact "
        "it exists to publish, or it was reworded and this guard must be "
        "taught the new shape — do not delete the assertion.")
    assert configured is not None, (
        "desktop/electron-builder.config.cjs no longer carries a "
        "`publish: [{ ... owner: \"...\", repo: \"...\" }]` literal. If the "
        "feed moved, this guard must follow it.")

    assert disclosed.groups() == configured.groups(), (
        "The published egress disclosure and the shipped updater config name "
        "DIFFERENT repositories:\n"
        f"  docs/security.md              owner={disclosed.group(1)!r} "
        f"repo={disclosed.group(2)!r}\n"
        f"  electron-builder.config.cjs   owner={configured.group(1)!r} "
        f"repo={configured.group(2)!r}\n"
        "Users read the first and the app obeys the second.")


# Two reference shapes: a repo URL, and an owner assignment. The `_` in the
# lookbehind matters — without it this pattern fires on its own constants.
_RETIRED_OWNER_REFS = re.compile(
    r"github\.com/eyalgolan/"
    r"|(?<![A-Za-z_])owner\s*[:=]\s*[\"']?eyalgolan(?![A-Za-z0-9._-])",
    re.IGNORECASE)


def test_no_shipped_file_points_at_the_retired_personal_owner():
    """Discovered over the whole ship set, not over a remembered list.

    Only *repository* references are matched. The maintainer's identity — the
    same name as a person, in `CLA.md`, in a commit trailer, in a workflow's
    `MAINTAINER:` value — is deliberately NOT a hit: this guard is about where
    the product points, not about who wrote it.
    """
    shipped = _shipped()
    assert len(shipped) > 100, (
        f"the ship set came back as {len(shipped)} file(s) — the classifier "
        "is not reporting a real corpus, so a green result here would mean "
        "nothing")

    offenders: list[str] = []
    scanned = 0
    for rel in shipped:
        if rel == _SELF:                      # the guard states the pattern it hunts
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                          # binary or unreadable: not our corpus
        scanned += 1
        for m in _RETIRED_OWNER_REFS.finditer(text):
            offenders.append(
                f"  {rel}:{text.count(chr(10), 0, m.start()) + 1}: "
                f"{m.group(0).strip()}")

    assert scanned > 100, (
        f"only {scanned} shipped text file(s) were actually read — a scan that "
        "reads almost nothing reports a clean result for the wrong reason")
    assert not offenders, (
        "shipped file(s) still point at the retired personal owner; that "
        "account's repository is a 404 for every reader of the public "
        "repo:\n" + "\n".join(offenders))
