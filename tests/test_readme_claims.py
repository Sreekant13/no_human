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

# RETARGET (2026-08-01). The README was cut from 255 lines to ~120: the config
# table moved to docs/configuration.md and the gate/limits detail — which
# carried every ``file.py:LINE`` citation — moved to docs/verification.md.
#
# The claims did not go away and they did not get weaker, so neither do the
# guards. Two of them now read the UNION of the surfaces the claim can live on
# rather than the README alone. That is a STRENGTHENING in both directions: a
# stale default is now caught wherever it is written, and moving a claim back
# onto the front page re-arms the same check without an edit here. What is
# unchanged is every assertion, including the mandatory-hit assertions that stop
# a guard passing vacuously once its subject leaves a surface.
DOCUMENTED_SURFACES = (
    README,
    REPO / "docs" / "configuration.md",
    REPO / "docs" / "verification.md",
)


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def documented() -> str:
    """README + the docs pages the front page delegates its claims to.

    Concatenated with a blank line between, so no regex can match across the
    seam between two files.
    """
    missing = [p for p in DOCUMENTED_SURFACES if not p.exists()]
    assert not missing, (
        f"surface(s) named here do not exist: {[p.name for p in missing]} — a "
        f"guard pointed at a missing file would silently check nothing"
    )
    return "\n\n".join(p.read_text(encoding="utf-8") for p in DOCUMENTED_SURFACES)


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


def test_config_table_documents_the_required_keys(documented):
    present = set(_config_rows(documented))
    missing = REQUIRED_ROWS - present
    assert not missing, f"config table no longer documents: {sorted(missing)}"


def test_every_documented_default_matches_config(documented):
    """Walks EVERY dotted config row rather than a hand-kept list, so a newly
    documented default is covered the day it is added.

    Only scalars are compared. A list or dict is rendered for humans (
    `[main, master, release/*]`) and matching that text to a Python repr would
    fail on formatting, not on truth.
    """
    wrong = []
    for key, stated in _config_rows(documented).items():
        actual = _resolve(key)
        if actual is _MISSING or isinstance(actual, (list, dict)):
            continue
        norm = stated.strip()
        if norm.lower() in {"null", "none", "*(none)*", "(none)", "—"}:
            norm = None
        elif norm.lower() in {"true", "false"}:
            norm = norm.lower() == "true"
        # "" and "(none)" are the same claim to a reader.
        if norm is None and actual in (None, ""):
            continue
        if isinstance(actual, bool):
            if norm is not actual:
                wrong.append(f"{key}: docs say {stated!r}, config says {actual!r}")
        elif str(norm) != str(actual):
            wrong.append(f"{key}: docs say {stated!r}, config says {actual!r}")
    assert not wrong, "config table disagrees with DEFAULT_CONFIG:\n  " + "\n  ".join(wrong)


def test_prose_default_matches_config(documented):
    """The original defect lived in PROSE — the troubleshooting row told users to
    raise max_turns_per_attempt from a number that was never the default.

    Reads the union (see DOCUMENTED_SURFACES): the 2026-08-01 rewrite moved the
    prose statement of this default to docs/verification.md along with the rest
    of the bounded-loop paragraph. Every stated spelling on every surface is
    still checked, and the mandatory-hit assertion still fails if the claim
    disappears from all of them.
    """
    readme = documented
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


def test_blocker_category_count_matches_the_enum(documented):
    """The README said "8-category" for a 10-member enum.

    The 2026-07-30 rewrite restated the same claim in words ("one of ten
    categories") — it now reads both spellings. That is a STRENGTHENING: the old
    digit-only pattern would have waved through "eight categories" without a
    word. Union-scoped 2026-08-01: the count is now stated on TWO surfaces (the
    README bullet and the docs/verification.md paragraph it links to), and the
    "every stated count must agree" assertion below is exactly what makes that
    worth checking on both.
    """
    n = len(BlockerCategory)
    stated = _stated_category_counts(documented)
    assert stated, (
        f"docs state no blocker-category count at all; the taxonomy has {n} "
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


# A citation names a SYMBOL, not a line. Two spellings are accepted, and they
# are the only two the docs may use:
#
#   1. a markdown link whose text is the symbol:  [`_make_guard_hook`](../src/...py)
#   2. prose:  `_verify_citations` in `reviewer.py`
#              `A`, `B` and `C` in [`core/bounds.py`](../src/...py)
#
# Form 2 anchors on the FILE and walks backwards over the run of backticked
# identifiers immediately before it, so a list of symbols sharing one file is
# captured whole rather than only its last member.
_SYMBOL = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
_SYMBOL_LINK_RE = re.compile(
    r"\[`(" + _SYMBOL + r")`\]\(([^)]+\.py)\)"
)
#: `A`, `B` and `C` in `file.py` — the file optionally wrapped in a link.
_SYMBOL_PROSE_RE = re.compile(
    r"((?:`" + _SYMBOL + r"`(?:,|\s+and\b|\s+)\s*)*`" + _SYMBOL + r"`)"
    r"\s+in\s+(?:\[)?`([\w/]+\.py)`"
)
_BACKTICKED = re.compile(r"`(" + _SYMBOL + r")`")


def _resolve_source(path: str) -> list[Path]:
    """Every file a reader could land on following a cited path.

    A slashed path is tried from the repo root and then from the package root
    (``agent/guard.py`` means ``src/no_human/agent/guard.py``). A bare basename
    must resolve to exactly ONE file under src/no_human: an ambiguous citation
    fails rather than being skipped, because a citation the reader cannot follow
    is the defect, not an exemption.
    """
    if "/" in path:
        for base in (REPO, REPO / "src" / "no_human", REPO / "src"):
            candidate = (base / path).resolve()
            if candidate.exists():
                return [candidate]
        return []
    return sorted((REPO / "src" / "no_human").rglob(path))


def _defined_symbols(source: Path) -> set[str]:
    """Module-level names, classes, and ``Class.method`` pairs defined in a file."""
    import ast

    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: set[str] = set()

    def visit(node, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(prefix + child.name)
                # One level of nesting is enough for `Class.method`; a citation
                # deeper than that is not a citation a reader can follow.
                if isinstance(child, ast.ClassDef):
                    visit(child, prefix + child.name + ".")
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        names.add(prefix + target.id)
            elif isinstance(child, ast.AnnAssign):
                if isinstance(child.target, ast.Name):
                    names.add(prefix + child.target.id)
            elif isinstance(child, (ast.If, ast.Try)):
                visit(child, prefix)

    visit(tree)
    return names


def _documented_symbol_citations(text: str) -> list[tuple[str, str]]:
    """Every (symbol, path) pair the docs cite, in either accepted spelling."""
    cites: list[tuple[str, str]] = [
        (sym, path) for sym, path in _SYMBOL_LINK_RE.findall(text)
    ]
    for group, path in _SYMBOL_PROSE_RE.findall(text):
        for sym in _BACKTICKED.findall(group):
            cites.append((sym, path))
    return cites


def test_documented_source_citations_resolve(documented):
    """Every symbol the front page or its docs cite must still be defined.

    RETARGET (2026-08-01), third time for this guard. It checked ``file.py:LINE``
    citations, and could only catch a line PAST THE END of the file — its own
    docstring said so in red: ``config.py:676 -> config.py:1`` passed. A survey
    of all 170 line citations in tracked docs found that residual risk had gone
    from theoretical to typical: of the 17 in docs/verification.md alone, 9
    pointed at code with nothing to do with the sentence citing them
    (``config.py:713-726`` was cited for ``bounds`` and landed in the planning
    block; ``orchestrator.py:845`` was cited for budget enforcement and landed
    in ``_emit_review``). Every one of those passed the old check.

    Line numbers rot on every edit above them, so the docs were converted to
    cite SYMBOLS and this guard was converted to match. That trades a weak check
    of a fragile thing for a strong check of a stable one: a symbol that is
    renamed or deleted fails here by name, which is the actual defect a reader
    hits. The floor of 10 is unchanged and still cannot pass vacuously.

    🔴 What this still does NOT check, so nobody over-reads a green run: that the
    cited symbol does what the prose says it does. ``_gate_verdict`` could be
    gutted to ``return True`` and this stays green. Existence and reachability
    are what a test can own; whether the code means what the sentence claims is
    left to the human, as before.
    """
    cites = _documented_symbol_citations(documented)
    assert len(cites) >= 10, (
        f"only {len(cites)} symbol citations found across "
        f"{[p.name for p in DOCUMENTED_SURFACES]}; this guard is the only thing "
        f"checking them and it must not pass vacuously"
    )
    bad: list[str] = []
    for symbol, path in cites:
        hits = _resolve_source(path)
        if len(hits) != 1:
            bad.append(
                f"{symbol} in {path}: path resolves to {len(hits)} files"
                f"{' — disambiguate with a path prefix' if len(hits) > 1 else ''}"
            )
            continue
        defined = _defined_symbols(hits[0])
        if symbol not in defined:
            bad.append(
                f"{symbol} is not defined in {hits[0].relative_to(REPO)} — "
                f"renamed or deleted, and the docs still send readers to it"
            )
    assert not bad, "docs cite source symbols that do not resolve:\n  " + "\n  ".join(bad)


# Every symbol below MUST be found by the parser above, in the file named. This
# is the mandatory-hit half: the floor of 10 proves *something* is parsed, but
# not that the sentences carrying the load-bearing claims are among them. A
# rewording that moved one of these out of an accepted spelling would otherwise
# drop it silently and still clear the floor on the others.
MANDATORY_CITATIONS = (
    ("_make_guard_hook", "claude_backend.py"),   # the PreToolUse safety hook
    ("_gate_verdict", "reviewer.py"),            # verdict recomputed, not trusted
    ("_verify_citations", "reviewer.py"),        # hallucinated-location demotion
    ("_FORGE_MERGE", "guard.py"),                # the merge ban
    ("DEFAULT_CONFIG", "config.py"),             # every documented default
    ("assert_subscription_mode", "config.py"),   # one billing path per run
)


@pytest.mark.parametrize("symbol,basename", MANDATORY_CITATIONS)
def test_load_bearing_claim_still_cites_its_symbol(documented, symbol, basename):
    cited = {
        (sym, Path(path).name) for sym, path in _documented_symbol_citations(documented)
    }
    assert (symbol, basename) in cited, (
        f"no documented citation of `{symbol}` in {basename} was parsed. Either "
        f"the claim was reworded past the two accepted citation spellings — fix "
        f"the wording, not this list — or it was dropped, and the guard above "
        f"was about to check one citation fewer without saying so."
    )


# REMOVED (2026-07-30): test_architecture_tree_lists_every_package.
#
# It pinned the README's `src/no_human/` package tree to the filesystem. The
# rewrite deleted that tree (PLAN.md is the architecture surface; the front page
# duplicated it), so the guard's subject exists on no surface in this repo and
# there is nowhere to re-point it.
#
# It was first kept behind a `skipif` that re-armed if a tree ever returned.
# That was wrong twice over and review caught both:
#   1. the skip marker itself trips no_human's tamper guard (skip/xfail 0->1
#      reads as a neutered test), and README.md advertises that exact rule -
#      shipping it would have meant a front page describing a gate the commit
#      trips;
#   2. the arming regex was byte-identical to the extractor, so it only armed
#      for the ONE tree format the old README happened to use. Six realistic
#      tree formats were tried against it, each omitting a real package: it
#      caught two and silently skipped four, including the output of `tree(1)`.
#      An unarmed guard that reports "skipped" is worse than a deleted one,
#      because the skip reads as coverage.
#
# Coverage is not lost: `test_readme_source_citations_resolve` above checks the
# claims that replaced the tree. What is genuinely gone is the completeness
# check, and only because nothing on any surface claims to enumerate any more.


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
def test_retired_false_claim_has_not_returned(documented, claim, why):
    """Union-scoped (2026-08-01). A retired claim is retired from the PRODUCT,
    not from one file — moving the prose that used to carry it onto a docs page
    must not move it out of this guard's reach."""
    assert claim.lower() not in documented.lower(), (
        f"docs reintroduce a claim review already disproved: {claim!r} — {why}"
    )


def test_every_documented_cli_command_exists(documented):
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
    for name, sub in re.findall(r"^nh (\S+)(?:\s+(\S+))?", documented, re.M):
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


@pytest.mark.parametrize("surface", DOCUMENTED_SURFACES, ids=lambda p: p.name)
def test_local_links_resolve(surface):
    """A broken link on the front page is the cheapest possible own-goal.

    Widened 2026-08-01 from the README to every DOCUMENTED_SURFACE. It is
    PARAMETRISED rather than fed the concatenated text, because a relative link
    resolves against the file that wrote it: ``blockers.md`` and
    ``../src/no_human/config.py`` in docs/verification.md mean different paths
    from the same strings in README.md. Resolving them all against the repo root
    — what the union fixture would force — would report false breaks and, worse,
    silently pass a real one that happened to exist at the root.

    docs/verification.md alone carries 19 local links and had no check at all
    between the 2026-08-01 relocation and this widening.
    """
    text = surface.read_text(encoding="utf-8")
    # `[text](path "title")` is valid Markdown — the title is not part of the
    # path, so stop at the first whitespace or the link resolves to nothing.
    targets = [t.split()[0] for t in re.findall(r"\]\((?!https?:)([^)#]+)", text) if t.strip()]
    assert targets, f"{surface.name} has no local links — is this guard pointed at the right file?"
    broken = [t for t in targets if not (surface.parent / t).exists()]
    assert not broken, f"{surface.name} links to missing files: {broken}"


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
