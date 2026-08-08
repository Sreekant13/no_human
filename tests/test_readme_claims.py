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
    ("no auto-merge setting to find", "false absolute — the auto_merge_on_approval "
     "config key IS findable (recheck: grep -n auto_merge_on_approval "
     "src/no_human/config.py); the honest claim is that no code path acts on it"),
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


def test_documented_surfaces_do_not_carry_a_stale_bench_label(
    documented, bench_report
):
    """No documented surface may name a bench run other than the published one.

    The original defect was the README describing v8 while linking a v13 report,
    with the suite green throughout.

    RETARGET (2026-08-01). This read the README alone, and when the operator cut
    the Limits section the README stopped mentioning the benchmark at all. The
    repair attempt made the link assertion CONDITIONAL on a benchmark claim being
    present — which made the whole test vacuous, since the shipped README trips
    none of the trigger words. Proven at the time and re-proven since: mutating
    the published report's label to ``expanded-core-v99`` left this test PASSING.
    The conditional's vocabulary was drawn from the same regex that decided
    whether to check anything, so the mutation could never reach it — a verifier
    built out of the thing it verifies.

    The fix is the one every sibling guard in this file already uses: read
    ``DOCUMENTED_SURFACES`` (:46-50), not the README alone. The claim did not
    disappear when it left the front page — it moved to ``docs/verification.md``,
    which links the report. So the mandatory-hit assertion can go back to being
    unconditional without dictating a word of the README: it is satisfied today
    by ``docs/verification.md:95-115``, it is phrasing-independent, and it fires
    only if the report goes UNREFERENCED from every documented surface, which is
    the rot — a claim separated from its evidence.

    That also restores the file's own policy at :44-45, which forbids removing
    the mandatory-hit assertions that stop a guard passing vacuously. The
    conditional violated it while leaving the policy text unedited.

    Stated plainly so it is not mistaken for coverage: the label-disagreement
    assertion is DORMANT today, because no documented surface names a run label
    at all. It arms itself the moment one does, which is the point of reading
    the union. The mandatory hit below is what makes this test able to fail
    right now — verified by mutation in both directions: dropping the reference
    from every surface fails it, and adding ``expanded-core-v8`` to the README
    fails it. Mutating the published label alone still passes, and that is
    correct rather than vacuous: with no label written down anywhere, there is
    nothing for the report to disagree WITH.
    """
    label = re.search(r"label: (\S+)", bench_report).group(1).rstrip(".")
    named = set(re.findall(r"\b(expanded-core-v\d+)\b", documented))
    assert not (named - {label}), (
        f"documented surfaces name bench run(s) {sorted(named - {label})}; the "
        f"published report is {label!r} — the docs and the report disagree"
    )
    # Mandatory hit. Unconditional on purpose: see the docstring. Matched on the
    # bare filename rather than a path, so moving the reference between surfaces
    # (or writing it as a relative link) does not break the coupling.
    assert "NORTH_STAR_BENCH.md" in documented, (
        "no documented surface links NORTH_STAR_BENCH.md; the benchmark claim "
        "and its evidence are no longer connected from anywhere a reader lands"
    )


# --- egress: the docs must not claim an enumeration they cannot back ---------
#
# README and docs/security.md both said prompts were the only thing that left
# the machine ("The only thing sent about your code is the prompt", "Two things,
# and nothing else"). Both were false when written, and the second was worse
# than the first because it read as an audited enumeration.
#
# These guards are anchored to the MECHANISM, not to wording, and each carries a
# mandatory hit that fails if its subject disappears from the source — the
# policy at :44-45. They check two things a trust document must get right:
# (a) the terminal push is disclosed, and (b) no surface re-asserts a closed
# enumeration of egress, which no process handing an agent an unrestricted shell
# can honestly make.

SECURITY_DOC = REPO / "docs" / "security.md"
VCS_INIT = REPO / "src" / "no_human" / "vcs" / "__init__.py"
CLAUDE_BACKEND = REPO / "src" / "no_human" / "agent" / "claude_backend.py"


@pytest.fixture(scope="module")
def security_doc() -> str:
    return SECURITY_DOC.read_text(encoding="utf-8")


#: Invisible anchors around the push-egress bullet in ``docs/security.md`` §7.
#:
#: RE-ANCHORED 2026-08-02. The previous marker was the bullet's own heading
#: prose (``- **`git push` of the task branch to your git remote**``). That was
#: correct about WHAT to protect and wrong about HOW: three rewordings that left
#: the disclosure completely intact still failed the guard (measured — rewording
#: the heading, restating the no-opt-out sentence, and changing the citation
#: spelling). Anchoring on prose means every edit to the prose is a test change,
#: which trains a reader to "fix" the guard rather than read it.
#:
#: An HTML comment renders as nothing in every Markdown viewer, so the reader
#: never sees these, the author can reword the bullet freely, and DELETING the
#: bullet still takes the anchors with it — which is the asymmetry this guard
#: wants. The strictness that matters is preserved below, on the *content*
#: between the anchors, not on its wording.
PUSH_BULLET_OPEN = "<!-- egress:push -->"
PUSH_BULLET_CLOSE = "<!-- /egress:push -->"
#: Inner anchors, around the sentence that says the egress cannot be turned off.
#: Nested on purpose: without them, "reword freely" would also permit deleting
#: the load-bearing half of the disclosure while leaving a bullet behind, and
#: RED 3 of the red-green matrix (drop only the no-opt-out claim) would stop
#: failing. The anchors travel with the sentence, so deleting it fails.
PUSH_NO_OPTOUT_OPEN = "<!-- egress:push:no-optout -->"
PUSH_NO_OPTOUT_CLOSE = "<!-- /egress:push:no-optout -->"

#: The no-opt-out slice must still make a negative claim about disabling. This
#: is a REQUIRED vocabulary, not a banned one, and the direction matters: an
#: unlisted spelling produces a loud failure on a doc edit (cheap, and the next
#: reader adds the spelling), where dropping the check entirely would let the
#: sentence be gutted in place — "**This ships your source to your git host,
#: and**" — with the anchors still present and the suite still green. That
#: silent direction is the one this file has already been burned by.
_NO_OPTOUT_NEGATIONS = ("no ", "not ", "cannot", "can't", "never", "nothing")
_NO_OPTOUT_DISABLERS = (
    "disable", "disabled", "disables", "turn it off", "turned off",
    "turn off", "switch it off", "switched off", "opt out", "opt-out",
)


def _slice_between(text: str, open_marker: str, close_marker: str) -> str:
    """The text between two markers, or "" if either is missing/out of order."""
    start = text.find(open_marker)
    if start == -1:
        return ""
    end = text.find(close_marker, start + len(open_marker))
    if end == -1:
        return ""
    return text[start + len(open_marker):end]


def _push_egress_bullet(security_doc: str) -> str:
    """Return just the push bullet's text from §7, or "" if it is gone.

    Scoped deliberately. An older version of this guard asserted over the whole
    §7 body, which meant the *fetch* bullet's `vcs/git.py` citation satisfied the
    "the push names a source location" assertion — deleting the push bullet
    entirely left the suite green (reproduced 2026-08-01). The slice is now
    marker-delimited rather than prose-delimited, so it keeps that scoping
    without also failing on rewordings.
    """
    section = security_doc.split("## 7.", 1)
    assert len(section) == 2, "docs/security.md has no '## 7.' egress section"
    return _slice_between(section[1], PUSH_BULLET_OPEN, PUSH_BULLET_CLOSE)


def _pushes_inside_open_pr(source: str) -> bool:
    """True iff ``open_pr`` really contains a ``.push(...)`` **call**.

    Parsed, not grepped. A whole-file substring test for ``repo.push(`` was
    satisfied by the docstring-comment at ``vcs/__init__.py:26``: renaming the
    live call site at :72 to ``repo.pushX(`` left the suite green (reproduced
    2026-08-01). Comments and strings are invisible to the AST, so they cannot
    stand in for the mechanism here.

    RELAXED 2026-08-02: the receiver is no longer constrained to the *name*
    ``repo``. Requiring ``func.value.id == "repo"`` made this wrong on two of
    six refactors that keep the mechanism fully intact — an aliased receiver
    (``_r = repo; _r.push(...)``) and an attribute chain
    (``self.repo.push(...)``) both read as "the push is gone" and would have
    sent a reader to re-check a doc that was still correct. What this guard is
    for is detecting that ``open_pr`` no longer pushes at all; the receiver's
    spelling is not part of that claim. The two checks that carry the weight are
    unchanged: it must be a `Call` (so a comment or a string cannot satisfy it)
    and the attribute must be exactly ``push`` (so RED 2, renaming the live call
    site to ``repo.pushX(``, still fails).

    🔴 What this does NOT check, stated so a green run is not over-read: syntactic
    presence is not reachability. ``if False: repo.push(branch)`` satisfies it.
    Accepted deliberately rather than fixed — deciding reachability needs a
    control-flow analysis, and the failure mode it would buy is someone
    deliberately disguising the removal of the push while leaving the call in
    the source. The realistic defect is the push being deleted, moved or
    renamed, which is what this catches. A reader who wants the stronger claim
    should read ``open_pr``; this guard's job is to fail when the doc's subject
    has left the file.
    """
    import ast

    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, defs) or node.name != "open_pr":
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "push"
            ):
                return True
    return False


def test_the_push_that_ends_every_task_is_disclosed_as_egress(security_doc):
    """`open_pr` pushes the user's source. The trust document must say so.

    Mandatory hit first: a real ``.push(...)`` call inside ``open_pr`` in
    ``vcs/__init__.py``, and `open_pr(` in the orchestrator. If either vanishes
    this test fails loudly rather than passing over a mechanism that is no
    longer there — the failure then means "re-check the doc", not "the doc is
    wrong".

    Then the doc side, anchored to the bullet's invisible HTML-comment markers
    rather than to the §7 body or to the bullet's prose, so that DELETING the
    disclosure is what fails the test and REWORDING it is free. Every arm was
    watched failing; the matrix and the benign-rewording tests below are the
    record.

    WHY THIS IS SEPARATE FROM ``tests/test_egress_disclosure.py``, which also
    guards §7 — do not merge them for tidiness. That guard asks "does every
    module with an outbound call appear ANYWHERE in §7"; this one asks "is the
    ONE channel that cannot be turned off still disclosed, and still true".
    Measured against each other's known positives, neither covers the other:

      known positive                          drift guard   this guard
      delete the whole push bullet ........... FAIL          FAIL
      drop ONLY the no-opt-out sentence ...... pass          FAIL
      gut that claim in place, anchors kept .. pass          FAIL
      strip the push bullet's citation ....... FAIL          FAIL
      plant an undisclosed httpx module ...... FAIL          pass

    The two middle rows are why a merged test would be weaker: the drift guard
    matches module PATHS, so gutting the sentence that says the egress cannot be
    disabled leaves every path in place and it stays green. The last row is why
    this one cannot absorb that guard: it never looks at modules at all.
    """
    vcs_init = VCS_INIT.read_text(encoding="utf-8")
    orch = (REPO / "src" / "no_human" / "core" / "orchestrator.py").read_text(
        encoding="utf-8")
    assert _pushes_inside_open_pr(vcs_init), (
        "vcs/__init__.py no longer calls repo.push(...) inside open_pr — the "
        "egress this guard exists to keep disclosed has moved; re-point it"
    )
    assert "open_pr(" in orch, (
        "the orchestrator no longer calls open_pr — re-point this guard"
    )

    _assert_push_bullet_discloses(security_doc)


def _assert_push_bullet_discloses(security_doc: str) -> None:
    """The doc half of the guard above, over any §7 text.

    Split out so the red-green matrix and the benign-rewording cases below run
    the REAL assertions against mutated documents, instead of a paraphrase of
    them that could drift from what ships.
    """
    bullet = _push_egress_bullet(security_doc)
    assert bullet.strip(), (
        f"docs/security.md §7 no longer carries the {PUSH_BULLET_OPEN}…"
        f"{PUSH_BULLET_CLOSE} bullet; shipping the user's source to their git "
        "host is the largest thing that leaves the machine and it cannot be "
        "an omission. These anchors are HTML comments precisely so that "
        "REWORDING the bullet needs no change here — if you are reading this "
        "message you removed the bullet or its anchors, and the fix is to put "
        "the disclosure back, not to delete this assertion."
    )

    no_optout = _slice_between(
        bullet, PUSH_NO_OPTOUT_OPEN, PUSH_NO_OPTOUT_CLOSE
    ).lower()
    assert no_optout.strip(), (
        f"the push-egress bullet no longer carries a {PUSH_NO_OPTOUT_OPEN}…"
        f"{PUSH_NO_OPTOUT_CLOSE} sentence — that the egress cannot be turned "
        "off is the load-bearing half of the disclosure, not a flourish"
    )
    assert any(n in no_optout for n in _NO_OPTOUT_NEGATIONS) and any(
        d in no_optout for d in _NO_OPTOUT_DISABLERS
    ), (
        f"the no-opt-out sentence ({no_optout.strip()!r}) no longer says that "
        "the push cannot be disabled. If it says so in a spelling this guard "
        "does not know, ADD the spelling to _NO_OPTOUT_NEGATIONS / "
        "_NO_OPTOUT_DISABLERS — the check is a required vocabulary, and a "
        "missing spelling is meant to fail loudly rather than pass silently."
    )

    assert (
        "vcs/__init__.py" in bullet
        or "vcs/git.py" in bullet
        or "GitRepo.push" in bullet
    ), (
        "the push-egress bullet names no source location for the push — a "
        "trust document's claims have to be checkable against the code. Note "
        "the neighbouring fetch bullet also cites vcs/git.py; this assertion "
        "is scoped to the push bullet so that citation cannot satisfy it"
    )


#: Placements of an anchor that are invisible to a reader of the SOURCE but not
#: to a reader of the RENDERED page. Both were measured against a CommonMark
#: renderer while writing the anchors above, and both shipped in a first draft:
#:
#:   * an HTML comment that STARTS a line inside a bullet list is parsed as an
#:     HTML block, which closes the list. The §7 list rendered as three separate
#:     `<ul>`s instead of one.
#:   * an anchor placed immediately after a list marker and immediately before
#:     `**` breaks the emphasis run: `**This ships your source…**` rendered as
#:     literal asterisks, i.e. the load-bearing sentence of a trust document
#:     lost its emphasis on the published page.
#:
#: Neither is detectable by any assertion about the doc's TEXT — the first draft
#: passed every content check in this file. So the shape is pinned here instead.
#: A renderer is deliberately NOT imported: the lean-stack rule forbids adding a
#: dependency, and these two shapes are the whole of what was measured to break.
def _anchor_placement_problems(security_doc: str) -> list[str]:
    bad: list[str] = []
    for n, line in enumerate(security_doc.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("<!--") and "egress:" in stripped:
            bad.append(
                f"docs/security.md:{n}: an egress anchor starts the line. A "
                f"line-initial HTML comment inside a list is an HTML block and "
                f"splits the list; put the anchor mid-line. Line: {stripped!r}"
            )
        if re.search(r"^\s*[-*]\s+<!--\s*/?egress:", line):
            bad.append(
                f"docs/security.md:{n}: an egress anchor directly follows the "
                f"list marker. Immediately before a `**` run this breaks the "
                f"emphasis. Move it after the bullet's bold heading."
            )
    return bad


def test_the_egress_anchors_are_invisible_when_rendered(security_doc):
    """The anchors must not change how docs/security.md renders.

    They exist so the bullet can be reworded without a test edit. An anchor that
    silently reformats a published trust page has cost more than it bought.
    """
    assert "egress:push" in security_doc, (
        "no egress anchors in docs/security.md at all — this shape guard would "
        "pass vacuously; the disclosure guard above is the one that failed first"
    )
    problems = _anchor_placement_problems(security_doc)
    assert not problems, "egress anchors are placed where they alter rendering:\n  " + "\n  ".join(problems)


#: Edits that change how the push-egress bullet READS while leaving what it
#: DISCLOSES completely intact. All three failed the prose-anchored version of
#: this guard (measured 2026-08-02, 3/3 false positives); all three must pass
#: now. They are held here as tests rather than as a note, because a guard whose
#: brittleness was changed without a false-positive suite is unverified — and
#: because the next person to reword the bullet should find out from a green run
#: that they were allowed to.
BENIGN_REWORDINGS = (
    (
        "reworded-heading",
        "- **`git push` of the task branch to your git remote**",
        "- **Pushing the task branch to whichever git remote you configured**",
    ),
    (
        "restated-no-optout",
        "there is no key that disables it",
        "no configuration key can disable it",
    ),
    (
        "symbol-citation-swapped",
        "`GitRepo.push` in `vcs/git.py`",
        "[`GitRepo.push`](../src/no_human/vcs/git.py)",
    ),
)


@pytest.mark.parametrize(
    "old,new", [(o, n) for _, o, n in BENIGN_REWORDINGS],
    ids=[i for i, _, _ in BENIGN_REWORDINGS],
)
def test_rewording_the_push_bullet_is_not_a_finding(security_doc, old, new):
    """A reword that keeps the disclosure must not fail the disclosure guard.

    The guard is deliberately brittle in the direction that matters — deleting
    the bullet, or gutting the no-opt-out sentence, still fails, and that is
    checked by the red-green matrix. Brittleness in THIS direction buys nothing
    and costs trust in the guard, so it is pinned closed.
    """
    assert old in security_doc, (
        f"the fixture for this false-positive case is stale: {old!r} is no "
        f"longer in docs/security.md, so this test is no longer exercising a "
        f"rewording of the shipped text. Re-point it at what the bullet says."
    )
    _assert_push_bullet_discloses(security_doc.replace(old, new))


#: (id, body of ``open_pr``, whether the mechanism is really still there).
#: The four attacks an independent review ran against the strict version, plus
#: the shipped shape and the comment-only shape. The strict version — which also
#: required ``func.value.id == "repo"`` — was WRONG on ``aliased-receiver`` and
#: ``attribute-chain``: it reported the push gone while it was right there.
PUSH_SHAPES = (
    ("shipped", "    pushed_sha = repo.push(branch)\n", True),
    ("aliased-receiver", "    _r = repo\n    pushed_sha = _r.push(branch)\n", True),
    ("attribute-chain", "    pushed_sha = self.repo.push(branch)\n", True),
    ("call-through-index", "    pushed_sha = repos[0].push(branch)\n", True),
    # RED 2: the live call site renamed. The mechanism really is gone.
    ("renamed-call", "    pushed_sha = repo.pushX(branch)\n", False),
    # The defect that made the round-2 version unable to fail: a comment.
    ("comment-only", "    # repo.push(branch) happens here\n    pass\n", False),
    # Moved to a helper: open_pr itself no longer pushes, so this guard should
    # fail loudly and send a reader to re-point it. That is the safe direction.
    ("moved-to-helper", "    pushed_sha = _do_the_push(repo, branch)\n", False),
)


@pytest.mark.parametrize(
    "body,expected", [(b, e) for _, b, e in PUSH_SHAPES],
    ids=[i for i, _, _ in PUSH_SHAPES],
)
def test_the_open_pr_push_detector_reads_calls_not_names(body, expected):
    """`_pushes_inside_open_pr` over every shape the mechanism is known to take.

    Held here so the detector's reach is a regression test rather than a claim
    in a commit message. A comment cannot satisfy it (that was the round-2
    HIGH), renaming the call still fails it, and the receiver's spelling is
    correctly irrelevant.
    """
    assert _pushes_inside_open_pr(f"def open_pr(repo, branch):\n{body}") is expected


def test_the_unbounded_egress_channel_is_named(security_doc):
    """The coder session has Bash and no tool allowlist. Say it, don't imply it.

    Mandatory hit: the default really is `bypassPermissions` and there really is
    no `allowed_tools`/`disallowed_tools` restriction. If someone ADDS a
    restriction, this fails — correctly, because the doc would then be
    overstating the risk and needs rewriting in the other direction.
    """
    backend = CLAUDE_BACKEND.read_text(encoding="utf-8")
    assert 'permission_mode: str = "bypassPermissions"' in backend, (
        "claude_backend no longer defaults to bypassPermissions — the egress "
        "doc's central caveat may now be wrong; re-read it"
    )
    assert "allowed_tools" not in backend, (
        "claude_backend now restricts tools — docs/security.md says the coder "
        "session is unrestricted, and that is no longer true"
    )

    body = security_doc.split("## 7.", 1)[1]
    assert "bypassPermissions" in body, (
        "the egress section does not name the unbounded channel; without it "
        "the rest reads as a complete enumeration, which it is not"
    )


@pytest.mark.parametrize(
    "claim",
    [
        "Only prompts leave your machine",
        "The only thing sent about your code is the prompt",
        "Two things, and nothing else",
    ],
    ids=["only-prompts", "only-thing-sent", "two-things"],
)
def test_no_surface_re_asserts_a_retired_exhaustive_egress_claim(
    documented, security_doc, claim
):
    """A closed list of sentences that shipped and were false.

    Deliberately a regression pin on exact retired strings, not a style rule: a
    general "don't say only" matcher would be a phrasing heuristic, and this
    file has already been burned once by a guard whose vocabulary decided its
    own reach. These three are pinned because each one WAS in the tree.

    Occurrences inside the History subsection are expected — the doc quotes the
    claims in order to retract them — so they are excluded by looking only at
    the text before it.
    """
    haystack = documented + "\n\n" + security_doc.split("### History", 1)[0]
    assert claim.lower() not in haystack.lower(), (
        f"a documented surface asserts {claim!r} again. It is false: open_pr "
        f"pushes the user's source at the end of every task, and the coder "
        f"session's egress is unbounded. See docs/security.md section 7."
    )


@pytest.mark.parametrize("doc_name", ["README.md", "docs/quickstart.md"])
def test_the_onboarding_docs_name_the_command_that_verifies_the_install(doc_name):
    r"""`nh doctor` must appear in the two documents a new user actually reads.

    Found by the adoption harness (ADOPT-4, 2026-08-02). `nh doctor` is the one
    command that tells someone whether their install is real -- it is what
    distinguishes "the commands ran" from "the product works". It was documented
    in `adapters.md`, `configuration.md`, `KNOWN_ISSUES.md` and a design doc, and
    appeared **zero** times in the README and the quickstart. A persona following
    only the public onboarding path could not discover it, which is precisely the
    population that needs it.

    Asserted POSITIVELY and per-document on purpose. The retired-claim tests in
    this file are negative assertions that an empty file would satisfy; this one
    fails if the mention is ever dropped from either document, and naming the
    document in the parametrisation means the failure says which one.
    """
    doc = (REPO / doc_name).read_text(encoding="utf-8")
    assert "nh doctor" in doc, (
        f"{doc_name} never mentions `nh doctor`. It is the only command that "
        f"verifies an install is real, and a user following only the public "
        f"onboarding path has no way to find it. See ADOPT-4."
    )
