"""The README is checked against the code, not against the last person to edit it.

Five review rounds on one README found the same defect four times: a claim about
a component or a default that nobody had verified, and that no test could catch.
Two of those were introduced *while fixing the other two* — "Monaco diff viewer"
became "side-by-side diff view" became "unified diff view", three guesses at one
`<pre>` that a single grep would have settled.

These tests pin the load-bearing, mechanically checkable claims to their
authoritative source: config defaults to ``DEFAULT_CONFIG``, the blocker count to
the enum, the architecture tree to the filesystem. They deliberately do NOT try
to verify prose — a test cannot know whether "adversarial reviewer" is a fair
description. They cover the class of claim that silently rots: numbers, counts,
directory listings, and words we have already been wrong about.

Read a green run correctly: the retired-claim and link tests are NEGATIVE
assertions — they prove nothing was reintroduced, not that anything is present.
An empty README would satisfy them. Only the config-row, blocker-count and
architecture-tree tests assert that something true is actually there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from no_human.blockers.taxonomy import BlockerCategory
from no_human.config import DEFAULT_CONFIG

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def _config_rows(readme: str) -> dict[str, str]:
    """Every `| `a.b` | value | …` row in the README's config tables.

    Splits on the cell delimiter rather than matching a value pattern: a value
    cell that is bolded, or carries a parenthetical, is a legitimate edit and
    must not be reported as a missing row.
    """
    rows: dict[str, str] = {}
    for line in readme.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = cells[0].strip("`* ")
        if re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)+", key):
            value = cells[1].strip()
            # Drop a trailing annotation BEFORE unwrapping the markup: `3`
            # (tune per repo) documents the same default as `3`, but stripping
            # backticks first cannot reach the one hidden behind the ")".
            # Only when something remains, so a bare "(none)" stays the
            # none-sentinel the comparison below expects.
            unannotated = re.sub(r"\s*\([^()]*\)$", "", value).strip()
            if unannotated:
                value = unannotated
            rows[key] = value.strip("`* ")
    return rows


_MISSING = object()


def _resolve(path: str):
    """DEFAULT_CONFIG value for a dotted key, or _MISSING."""
    node = DEFAULT_CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


# Rows that must exist. A generic walk alone would pass on a README with every
# config row deleted; these keep the table itself honest.
REQUIRED_ROWS = {
    "server.port", "llm.primary_model", "llm.review_model",
    "bounds.max_attempts", "bounds.max_turns_per_attempt",
}


def test_required_rows_are_real_config_keys():
    """REQUIRED_ROWS is hand-kept, so on its own it only proves the README still
    says what it used to say. Without this, a key renamed or deleted in
    config.py is caught by neither test: the walk below skips it as _MISSING,
    and this list happily confirms the now-stale README row is still present."""
    unreal = sorted(k for k in REQUIRED_ROWS if _resolve(k) is _MISSING)
    assert not unreal, (
        f"REQUIRED_ROWS names keys that are not in DEFAULT_CONFIG: {unreal} — "
        f"the config changed and the README rows for them are now stale"
    )


def test_config_table_documents_the_required_keys(readme):
    documented = set(_config_rows(readme))
    missing = REQUIRED_ROWS - documented
    assert not missing, f"config table no longer documents: {sorted(missing)}"


def test_every_documented_default_matches_config(readme):
    """Walks EVERY dotted config row rather than a hand-kept list, so a newly
    documented default is covered the day it is added.

    Only scalars are compared. A list or dict is rendered for humans (
    `[main, master, release/*]`) and matching that text to a Python repr would
    fail on formatting, not on truth.
    """
    wrong = []
    for key, documented in _config_rows(readme).items():
        actual = _resolve(key)
        if actual is _MISSING or isinstance(actual, (list, dict)):
            continue
        norm = documented.strip()
        if norm.lower() in {"null", "none", "*(none)*", "(none)", "—"}:
            norm = None
        elif norm.lower() in {"true", "false"}:
            norm = norm.lower() == "true"
        # "" and "(none)" are the same claim to a reader.
        if norm is None and actual in (None, ""):
            continue
        if isinstance(actual, bool):
            if norm is not actual:
                wrong.append(f"{key}: README says {documented!r}, config says {actual!r}")
        elif str(norm) != str(actual):
            wrong.append(f"{key}: README says {documented!r}, config says {actual!r}")
    assert not wrong, "config table disagrees with DEFAULT_CONFIG:\n  " + "\n  ".join(wrong)


def test_prose_default_matches_config(readme):
    """The original defect lived in PROSE — the troubleshooting row told users to
    raise max_turns_per_attempt from a number that was never the default."""
    actual = DEFAULT_CONFIG["bounds"]["max_turns_per_attempt"]
    # A bare finditer guards nothing when the prose is reworded past its one
    # spelling — the loop body never runs and the test passes green. Accept the
    # spellings a writer would actually use, then require at least one hit.
    matches = list(re.finditer(
        r"max_turns_per_attempt`?[^.\n]*?\(defaults?\s*(?:to)?\s*:?\s*(\d+)\)",
        readme))
    assert matches, (
        "no 'max_turns_per_attempt (default N)' prose found in the README. "
        "Either it was reworded past this pattern — widen the pattern — or the "
        "claim was dropped, and this guard was about to pass vacuously."
    )
    for m in matches:
        assert int(m.group(1)) == actual, (
            f"README prose says default {m.group(1)}, config says {actual}"
        )


def test_blocker_category_count_matches_the_enum(readme):
    """The README said "8-category" for a 10-member enum."""
    n = len(BlockerCategory)
    assert f"{n}-category" in readme, (
        f"README does not say '{n}-category'; the taxonomy has {n} members "
        f"({', '.join(c.name for c in BlockerCategory)})"
    )
    # Presence at ONE site is not enough. The README states the count twice, so
    # re-introducing the original "8-category" defect at the other site passed
    # this test. Every stated count must agree with the enum.
    wrong = sorted({m.group(1) for m in re.finditer(r"\b(\d+)-category\b", readme)
                    if int(m.group(1)) != n})
    assert not wrong, (
        f"README also claims {wrong}-category; the taxonomy has {n} members"
    )


def test_architecture_tree_lists_every_package(readme):
    """The tree claimed to enumerate src/no_human/ while omitting real packages
    (ci/, integrations/, notify/, ci_gate/). It is now labelled abridged, but it
    must still not omit a package or invent one."""
    # A package is a directory that actually holds Python, not merely a
    # directory. Deleting an integration leaves its __pycache__ behind (tracker/
    # survives that way on any machine that ran TRACKER before it was removed), and
    # an untracked husk is not a claim the README is failing to make.
    # `__init__.py` alone is too narrow in the other direction: since PEP 420 a
    # directory of modules without one is a real, importable, wheel-shipped
    # package, and forgetting __init__.py on a new package is a common mistake.
    # The search must RECURSE: a namespace package whose direct children are all
    # sub-packages has no top-level .py at all, and a non-recursive glob would
    # wave it through — narrowing coverage below what plain is_dir() caught.
    # A husk is still excluded either way, since __pycache__ holds only .pyc.
    packages = {
        p.name for p in (REPO / "src" / "no_human").iterdir()
        if p.is_dir() and not p.name.startswith(("_", "."))
        and ((p / "__init__.py").exists() or any(p.rglob("*.py")))
    }
    # Scoped to the fenced block that contains the tree: an unrelated second
    # tree elsewhere in the README (e.g. web/src) is a legitimate edit and must
    # not be read as a claim about src/no_human packages.
    fences = [b for b in readme.split("```") if "src/no_human/" in b]
    assert fences, "architecture tree fence not found in README"
    listed = set(re.findall(r"^\s{2}(\w+)/\s+#", fences[0], re.M))
    missing = packages - listed
    invented = listed - packages
    assert not missing, f"architecture tree omits real packages: {sorted(missing)}"
    assert not invented, f"architecture tree lists non-existent packages: {sorted(invented)}"


# Claims proven false by review and fixed. A regression here is not a typo — it
# is the README describing a feature the code does not have.
RETIRED_CLAIMS = [
    ("monaco", "the diff view is native — recheck with: grep -ri monaco web/src/"),
    ("desktop notification", "recheck with: ls src/no_human/notify/"),
    ("5-lane", "recheck with: grep -n BOARD_LANES web/src/boardLanes.js"),
    ("code.example.com/dev", "placeholder clone URL that cannot work"),
    ("tests-passing", "a hard-coded badge asserting a build status nothing checks"),
]


@pytest.mark.parametrize("claim,why", RETIRED_CLAIMS)
def test_retired_false_claim_has_not_returned(readme, claim, why):
    assert claim.lower() not in readme.lower(), (
        f"README reintroduces a claim review already disproved: {claim!r} — {why}"
    )


def test_every_documented_cli_command_exists(readme):
    """Commands are cheap to document and easy to rename out from under a doc.

    Checks the SUBCOMMAND too: ``cli.commands`` is a flat dict of top-level
    names, so a test that stops there passes on `nh task addd` — false
    confidence, which is worse than no test.

    Scope, stated so nobody over-reads a green run: only commands at the start
    of a line (i.e. in the usage code blocks) are checked. Commands mentioned
    inline cannot be, because the README legitimately names one that does NOT
    exist — "There is no `nh stop`" — and a guard that fails on a true sentence
    is worse than a narrower one.
    """
    import click

    from no_human.cli.commands import cli

    unknown: list[str] = []
    for name, sub in re.findall(r"^nh (\S+)(?:\s+(\S+))?", readme, re.M):
        cmd = cli.commands.get(name)
        if cmd is None:
            unknown.append(f"nh {name}")
            continue
        # Only treat the next token as a subcommand when it looks like one —
        # `nh task add <url>` vs `nh approve <id>`.
        if not (sub and re.fullmatch(r"[a-z][a-z-]*", sub)):
            continue  # a placeholder like <id>, a flag, or nothing
        if isinstance(cmd, click.Group):
            if sub not in cmd.commands:
                unknown.append(f"nh {name} {sub}")
            continue
        # Not a group: the token may still be a fixed choice, e.g.
        # `nh config show` is a click.Choice argument, not a subcommand.
        for param in cmd.params:
            choices = getattr(param.type, "choices", None)
            if isinstance(param, click.Argument) and choices:
                if sub not in choices:
                    unknown.append(f"nh {name} {sub}")
                break
    assert not unknown, (
        f"README documents commands that do not exist: {sorted(set(unknown))}"
    )


def test_local_links_resolve(readme):
    """A broken link on the front page is the cheapest possible own-goal."""
    # `[text](path "title")` is valid Markdown — the title is not part of the
    # path, so stop at the first whitespace or the link resolves to nothing.
    targets = [t.split()[0] for t in re.findall(r"\]\((?!https?:)([^)#]+)", readme) if t.strip()]
    broken = [t for t in targets if not (REPO / t).exists()]
    assert not broken, f"README links to missing files: {broken}"
