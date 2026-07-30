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
    #
    # The 2026-07-30 rewrite dropped the troubleshooting table that carried the
    # only "(default 500)" spelling and stated the same number as a plain "is
    # 500" instead. The claim did not move surface and it did not go away — only
    # its phrasing changed — so the fix is to widen the pattern, exactly as the
    # paragraph above anticipated, NOT to dictate one wording back into the
    # prose. `is N` and `defaults to N` are added; the mandatory-hit assertion
    # below is unchanged, so this cannot start passing vacuously.
    matches = list(re.finditer(
        r"max_turns_per_attempt`?[^.\n]*?"
        r"(?:\(defaults?\s*(?:to)?\s*:?\s*(\d+)\)|\bis\s+(\d+)\b|\bdefaults?\s+to\s+(\d+)\b)",
        readme))
    assert matches, (
        "no 'max_turns_per_attempt (default N)' / 'is N' prose found in the "
        "README. Either it was reworded past this pattern — widen the pattern — "
        "or the claim was dropped, and this guard was about to pass vacuously."
    )
    for m in matches:
        stated = next(g for g in m.groups() if g is not None)
        assert int(stated) == actual, (
            f"README prose says default {stated}, config says {actual}"
        )


# Counts get written as words as often as digits. Keeping only the digit
# spelling let "ten categories" read as *no claim at all*, so a README that had
# quietly gone stale in words would have passed.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_CATEGORY_COUNT_RE = re.compile(
    r"\b(\d+|" + "|".join(_NUMBER_WORDS) + r")[- ]categor(?:y|ies)\b",
    re.IGNORECASE,
)


def _stated_category_counts(text: str) -> list[int]:
    out = []
    for m in _CATEGORY_COUNT_RE.finditer(text):
        tok = m.group(1).lower()
        out.append(int(tok) if tok.isdigit() else _NUMBER_WORDS[tok])
    return out


def test_blocker_category_count_matches_the_enum(readme):
    """The README said "8-category" for a 10-member enum.

    The 2026-07-30 rewrite restated the same claim in words ("one of ten
    categories"). The claim stayed on this surface, so the guard stays pointed
    here — it now reads both spellings. That is a STRENGTHENING: the old digit-
    only pattern would have waved through "eight categories" without a word.
    """
    n = len(BlockerCategory)
    stated = _stated_category_counts(readme)
    assert stated, (
        f"README states no blocker-category count at all; the taxonomy has {n} "
        f"members ({', '.join(c.name for c in BlockerCategory)}). A count that "
        f"is not stated cannot be checked — restate it or this guard is blind."
    )
    # Presence at ONE site is not enough. The README states the count twice, so
    # re-introducing the original "8-category" defect at the other site passed
    # this test. Every stated count must agree with the enum.
    wrong = sorted({c for c in stated if c != n})
    assert not wrong, (
        f"README claims {wrong} blocker categories; the taxonomy has {n} members"
    )


_SOURCE_CITATION_RE = re.compile(r"`([\w/]+\.py):(\d+)(?:-(\d+))?`")


def test_readme_source_citations_resolve(readme):
    """Every ``file.py:LINE`` the README cites must exist and have that line.

    RETARGET (2026-07-30). This guard was ``test_architecture_tree_lists_every_
    package``: it pinned the README's ``src/no_human/`` tree to the filesystem so
    the tree could not omit or invent a package. The rewrite deleted that tree
    (PLAN.md is the architecture surface; the front page duplicated it), so the
    enumeration it checked exists on no surface at all and cannot be pointed
    somewhere else.

    What replaces it is not a deletion. The rewrite swapped one kind of claim
    about the source layout for another: instead of one tree, the README now
    makes 15 individually checkable claims of the form ``config.py:676``. Those
    are the same defect class this file exists for — "a claim about a component
    that nobody verified" — and NOTHING checked them: ``test_local_links_resolve``
    only sees markdown link targets, and most of these citations are bare code
    spans with no link. Line numbers were never checked by anything at all.

    Coverage traded, stated plainly: the completeness half is gone (no listing
    claims to be exhaustive any more, so there is nothing to be incomplete
    about). The invention half is now checked far more tightly than before —
    per file AND per line number, not per directory.

    A bare basename must resolve to exactly ONE file under src/no_human. An
    ambiguous citation fails rather than being skipped: a citation the reader
    cannot follow is the defect, not an exemption.
    """
    cites = _SOURCE_CITATION_RE.findall(readme)
    assert len(cites) >= 10, (
        f"only {len(cites)} source citations found in the README; this guard is "
        f"the only thing checking them and it must not pass vacuously"
    )
    bad: list[str] = []
    for path, start, end in cites:
        if "/" in path:
            hits = [REPO / path] if (REPO / path).exists() else []
        else:
            hits = sorted((REPO / "src" / "no_human").rglob(path))
        if len(hits) != 1:
            bad.append(
                f"{path}:{start} resolves to {len(hits)} files"
                f"{' — disambiguate with a path prefix' if len(hits) > 1 else ''}"
            )
            continue
        total = len(hits[0].read_text(encoding="utf-8").splitlines())
        last = int(end or start)
        if last > total:
            bad.append(
                f"{path}:{start}-{last} cites past end of file "
                f"({hits[0].relative_to(REPO)} has {total} lines)"
            )
    assert not bad, "README cites source that does not resolve:\n  " + "\n  ".join(bad)


@pytest.mark.skipif(
    not re.search(r"^\s{2}\w+/\s+#", (REPO / "README.md").read_text(encoding="utf-8"), re.M),
    reason="README carries no architecture tree; test_readme_source_citations_resolve "
           "covers source-layout claims instead. Unskips automatically if a tree returns.",
)
def test_architecture_tree_lists_every_package(readme):
    """The tree claimed to enumerate src/no_human/ while omitting real packages
    (ci/, integrations/, notify/, ci_gate/). It is now labelled abridged, but it
    must still not omit a package or invent one.

    Kept, not deleted, and kept ARMED: the skip condition is computed from the
    README itself, so the day anyone puts a package tree back on the front page
    this guard starts enforcing again with no one having to remember it."""
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
    #
    # 🔴 ODD INDICES ONLY, AND THAT IS THE WHOLE POINT. `split("```")` alternates
    # prose, fence, prose, fence… so code blocks are the ODD segments. This filter
    # used to accept ANY segment containing "src/no_human/", so the moment a PROSE
    # sentence cited a real path — e.g. "(`src/no_human/cli/tui.py` builds an
    # orchestrator)" — that prose segment sorted ahead of the real tree, `listed`
    # came back EMPTY, and the test reported all 17 packages missing. It failed on
    # a correct README while passing on a less accurate one, which is the worst
    # possible direction for a guard: it punished making a citation resolvable.
    # This is a STRENGTHENING, not a relaxation — the test now reads the artifact
    # it was always meant to read (a code block) instead of whichever segment
    # happened to match first.
    segs = readme.split("```")
    fences = [b for i, b in enumerate(segs) if i % 2 == 1 and "src/no_human/" in b]
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


BENCH_REPORT = REPO / "docs" / "NORTH_STAR_BENCH.md"


@pytest.fixture(scope="module")
def bench_report() -> str:
    return BENCH_REPORT.read_text(encoding="utf-8")


def test_published_bench_report_is_internally_consistent(bench_report):
    """The published figures must agree with each other.

    RETARGET (2026-07-30). This was ``test_readme_bench_figures_match_the_
    published_report``: it required the README to restate the report's label,
    success fraction, percentage, cost ratio and escalation ratio, so a
    ``bench publish`` could not silently stale the front page. The rewrite
    removed those figures from the README on purpose — the run is self-run and
    its corpus does not resolve on anyone else's machine, so republishing a
    47% headline was the claim least defensible on a public page.

    Asserting that the report contains its own numbers would be vacuous, so the
    coupling is re-pointed at the one thing about the report that IS checkable
    without the README: the headline figures are derived quantities and must
    reconcile. ``docs/NORTH_STAR_BENCH.md`` is machine-written and says "do not
    edit by hand" — and until now NOTHING enforced that. A hand-edited success
    percentage, or a delivered/escalated split that does not sum to the
    satisfied count, now fails here. That is coverage the old test did not have
    at all: it would have happily confirmed the README faithfully echoed a
    doctored report.
    """
    satisfied, ran = (int(x) for x in re.search(
        r"Success \(goal satisfied, unattended\): (\d+)/(\d+)",
        bench_report).groups())
    pct = int(re.search(r"Success \(goal satisfied.*?\((\d+)%\)", bench_report).group(1))
    delivered, escalated = (int(x) for x in re.search(
        r"of which (\d+) DELIVERED a change and (\d+) correctly ESCALATED",
        bench_report).groups())
    esc_pct, esc_n, esc_d = (int(x) for x in re.search(
        r"Honest-escalation rate on gated tasks: (\d+)% \((\d+)/(\d+)\)",
        bench_report).groups())

    assert round(satisfied / ran * 100) == pct, (
        f"published success {satisfied}/{ran} rounds to "
        f"{round(satisfied / ran * 100)}%, but the report states {pct}%"
    )
    assert delivered + escalated == satisfied, (
        f"published split {delivered} delivered + {escalated} escalated = "
        f"{delivered + escalated}, but the report states {satisfied} satisfied"
    )
    assert round(esc_n / esc_d * 100) == esc_pct, (
        f"published honest-escalation {esc_n}/{esc_d} rounds to "
        f"{round(esc_n / esc_d * 100)}%, but the report states {esc_pct}%"
    )


def test_readme_does_not_carry_a_stale_bench_label(readme, bench_report):
    """The front page must not name a bench run other than the published one.

    The original defect was the README describing v8 while linking a v13 report,
    with the suite green throughout. The README no longer quotes figures, but it
    still LINKS the report, so that exact rot is still reachable and this half of
    the old coupling is kept pointed at the README.

    The link assertion is deliberately positive: it fails if someone drops the
    reference entirely, which would end the coupling silently.
    """
    label = re.search(r"label: (\S+)", bench_report).group(1).rstrip(".")
    named = set(re.findall(r"\b(expanded-core-v\d+)\b", readme))
    assert not (named - {label}), (
        f"README names bench run(s) {sorted(named - {label})}; the published "
        f"report is {label!r} — the front page and the report disagree"
    )
    assert "docs/NORTH_STAR_BENCH.md" in readme, (
        "README no longer links docs/NORTH_STAR_BENCH.md; the benchmark claim "
        "and its evidence are no longer connected from the front page"
    )
