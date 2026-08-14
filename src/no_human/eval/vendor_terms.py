"""The vendor/competitor terms that must never enter a tracked artifact.

Hex-encoded for the same reason `tests/test_no_banned_terms.py` encodes them:
a scanner that spells its own targets in plaintext is itself a leak, and every
grep for the term then hits the scanner first.

This lives in `src` rather than in the test because the PUBLISH path needs it.
`nh bench publish` writes `docs/NORTH_STAR_BENCH.md`, which is TRACKED, and its
per-task `notes` column is judge-authored free text quoting real repo contents —
so a publish can introduce a banned term into a clean file with nothing to stop
it. The test suite cannot: its own guard is `xfail(strict=False)` while a
vendor-neutral sweep is in progress, so it reports success either way.

The test imports this list, so there is exactly one definition.

WHY THE LIST IS SPLIT IN TWO
----------------------------
This module SHIPS: `cli/commands.py` and `eval/northstar_card.py` import it, so
`nh bench publish` depends on it wherever no_human runs. But some terms in the
inventory are deployment-specific and are withheld from this distribution — and
keeping them here, encoded or not, would defeat the point: hex is only
unreadable to grep, it is not unreadable, and an encoded inventory in a file
whose docstring explains the encoding is a labelled index, not a redaction.

So the withheld half lives in `_vendor_terms_private.py`, which is not
distributed, and this file carries only the general half.

The import is optional on purpose. Where the supplement is present, the guard
behaves exactly as before — its terms are still detected and still redacted out
of a publish, which is where they are most likely to leak. Where it is absent,
`EXTRA_HEX` is empty and the guard keeps working on the terms that remain.
Removing the withheld entries outright would have disarmed the guard in
precisely the deployments that have that content to redact.
"""

from __future__ import annotations

import re

_t = lambda h: bytes.fromhex(h).decode()  # noqa: E731

# Competitor products and model names — the general half, present everywhere.
_BANNED_HEX = [
    "646576696e", "77696e6473757266", "636f70696c6f74", "636f676e6974696f6e",
    "6169646572", "6f70656e6169", "6770742d34", "6770742d35",
]

# The deployment-specific half, withheld from this distribution. Never spell
# these here.
#
# FAILS CLOSED, deliberately. A bare `except ImportError` also swallows an
# ImportError raised from INSIDE the supplement for any unrelated reason — a bad
# import, a typo, a half-finished edit — and then the publish guard silently
# drops from 18 terms to 8 with the whole suite still green. That is the failure
# that matters, because a deployment that carries the supplement is exactly the
# one with content to redact. `e.name` distinguishes "this module is not here"
# (a distribution without the supplement, the only sanctioned case) from "this
# module is here and broken" (a bug, which must be loud). The supplement's own
# test pins the count at 18.
try:
    from ._vendor_terms_private import EXTRA_HEX as _EXTRA_HEX
except ImportError as e:
    if e.name not in (__package__ + "._vendor_terms_private", "_vendor_terms_private"):
        raise
    _EXTRA_HEX: list[str] = []  # public export: the supplement is not distributed

BANNED_TERMS: list[str] = [_t(h) for h in (*_BANNED_HEX, *_EXTRA_HEX)]


# --------------------------------------------------------------------------- #
# Normalisation: what the READER sees, not what the bytes say.
# --------------------------------------------------------------------------- #
# `nh bench publish` writes judge-authored free text into a tracked MARKDOWN
# file, and markdown lets a term be spelled so that it renders whole while no
# letter-boundary rule can see it. For a term spelled t-e-r-m: `te**rm**`,
# `ter<!-- c -->m`, `te[](rm)`, or the same term cut by a zero-width space,
# which is invisible in the rendered page AND in most editors. Every one of
# those rendered the whole term and passed all four rules below.
#
# The defence already existed in this repo, in the wrong place: the repo-wide
# sweep in `tests/test_no_banned_terms.py` matched each line twice, raw and with
# inline markup stripped. The SHIPPED guard — the one on the publish path — did
# not. This carries it across, and widens it only to the constructs that render
# to nothing at all.

#: Characters that occupy no space when rendered. A term split by one of these
#: is invisible to a reader of the rendered page and, for the first four, to a
#: reader of the raw file too.
#: Written as escapes on purpose: a guard file that pastes the literal
#: characters is one a future editor cannot see, and cannot review.
_INVISIBLE = (
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\u2060"  # word joiner
    "\ufeff"  # zero-width no-break space / BOM
    "\u00ad"  # soft hyphen
)

#: One construct per alternative, all of which a renderer erases.
_NORMALIZE_RE = re.compile(
    # HTML comments: erased outright by every renderer.
    r"(?P<comment><!--[^\n]*?-->)"
    # Inline tags — the same conservative shape the repo-wide sweep uses.
    r"|(?P<tag><[A-Za-z/][^>\n]{0,40}>)"
    # DEGENERATE links only, where one half is empty: `[](x)` and `[x]()` are
    # two ways of writing x with bracket noise wedged into it, and neither is a
    # thing anyone writes on purpose. A normal `[label](target)` link is NOT
    # rewritten — rewriting it would glue link text to URLs across the whole
    # corpus, which is how a normalising matcher starts crying wolf.
    r"|\[\]\((?P<dest>[^)\n]*)\)"
    r"|\[(?P<label>[^\]\n]*)\]\(\)"
    # Emphasis / code-span / strikethrough runs, and the invisibles above.
    r"|(?P<markup>[*_`~" + _INVISIBLE + r"]+)"
)


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """(*normalised text*, *origin index of each of its characters*).

    The index map is what lets the REDACTOR act on a hit the normalised pass
    found: a match at `normalized[s:e]` came from `text[map[s]:map[e-1]+1]`, a
    contiguous run (the map is monotonic), so the redactor can splice out the
    markup and the term together instead of leaving half of a split term behind.
    Detection and redaction therefore agree by construction, which is the
    property `tests/test_vendor_terms.py` pins.
    """
    chars: list[str] = []
    origin: list[int] = []
    pos = 0
    for m in _NORMALIZE_RE.finditer(text):
        for i in range(pos, m.start()):
            chars.append(text[i])
            origin.append(i)
        # A degenerate link keeps its one non-empty half; everything else goes.
        for name in ("dest", "label"):
            if m.group(name) is not None:
                for i in range(*m.span(name)):
                    chars.append(text[i])
                    origin.append(i)
        pos = m.end()
    for i in range(pos, len(text)):
        chars.append(text[i])
        origin.append(i)
    return "".join(chars), origin


def demarkup(text: str) -> str:
    """*text* with render-to-nothing markup and invisible characters removed.

    Used for a SECOND matching pass, never as a replacement for the first: the
    stripping can itself create a false letter boundary (`<term>_<term>` becomes
    one glued word and stops matching), so both forms are scanned.

    Deliberately conservative. It strips emphasis, code-span, strikethrough and
    tag characters, HTML comments, zero-width/soft-hyphen characters, and the
    two degenerate link forms — and nothing else. It does NOT join across lines:
    a term wrapped over a line break still evades, and that is stated rather
    than silently half-fixed, because the repo-wide sweep's `# term-ok` escape
    hatch is per-line and joining lines would change what a marker governs.

    Three shapes are knowingly left undetected rather than made noisy:
    a normal `[label](target)` link splitting a term across label and target
    (rewriting real links glues text to URLs corpus-wide); markup NESTED inside
    a degenerate link's surviving half (normalisation is one pass, not a fixed
    point); and a term split across a line break, as above.
    """
    return _normalize_with_map(text)[0]


def _match_patterns(t: str) -> list[str]:
    """The four match rules for one term, as regexes over UNLOWERED text.

    ONE definition, shared by the detector and the redactor. They were two
    hand-kept copies of the same four rules, and `redact_for_publish` only
    promises what it promises because they agree; a fifth rule added to one copy
    and not the other is exactly the bug this file is closing.
    """
    e = re.escape(t)
    pats = [
        # (1) letter boundaries, case-insensitive. Only LETTERS count as word
        # characters, so a term glued on with a hyphen or underscore
        # (`<term>-metrics-core-pipeline`, the shape that actually slipped a
        # codename into the report) is caught, while ordinary English that
        # merely CONTAINS a term is not. A plain substring match was tried first
        # and is wrong: several banned terms are substrings of ordinary English
        # words, and the refusal is fail-closed with no override — so the
        # operator's only escape would be editing a legitimate measured note to
        # satisfy the guard. The concrete cases live in
        # tests/test_bench_publish.py, which is free to spell them. The flag is
        # scoped INLINE: a global re.IGNORECASE would also fold the boundary
        # classes, re-admitting every ordinary-English case.
        rf"(?<![A-Za-z])(?i:{e})(?![A-Za-z])",
        # (2) camelCase only: the term Titlecased, then an upper-case
        # letter/digit/underscore. Deliberately NOT the all-caps form — that
        # would flag real acronyms that merely start with the letters (a
        # national railway operator's acronym, for one). A Titlecased term may
        # be preceded by a lowercase letter (`getXClient`, and — the case that
        # actually occurs — pasted content with literal \n escapes, which makes
        # every line-start term read as `nX`). Only the lowercase-preceded form
        # is relaxed: an upper-case predecessor would flag ALL-CAPS acronyms.
        rf"(?<![A-Z]){re.escape(t.capitalize())}(?=[A-Z0-9_])",
        # (3) the same glue with the term left lowercase. An all-lowercase glue
        # is still not matched; closing that would re-admit ordinary English.
        rf"(?<![A-Za-z]){e}(?=[A-Z0-9_])",
    ]
    # (4) a term ending in a digit is a model name; its suffix is a letter
    # (`-4o`, `-4-turbo`), so rule (1)'s trailing boundary misses it.
    if t[-1].isdigit():
        pats.append(rf"(?<![A-Za-z])(?i:{e})")
    return pats


#: A term whose OWN spelling contains a character `demarkup` removes — an
#: underscore, say — is normalised out of the text along with the markup that
#: split it, so a second pass looking for its literal spelling would find a form
#: that can no longer be there. Measured, not assumed: without this the split
#: forms of exactly one term in the inventory stayed undetected at every cut
#: point, while the other 26 were caught. It is scanned under its normalised
#: spelling and REPORTED under its real one, so callers still see one vocabulary.
#:
#: Second pass only. The raw pass keeps the literal spelling, so text with no
#: markup in it is matched exactly as it was before this file changed.
_ALIASES: dict[str, str] = {
    t: demarkup(t) for t in BANNED_TERMS
    # A short alias is a liability, not a catch: it has fewer letters to be
    # wrong about and the refusal is fail-closed. Four is the shortest term in
    # the inventory that has ever been safe on its own.
    if demarkup(t) != t and len(demarkup(t)) >= 4
}

#: (reported term, compiled rule) — precompiled because this runs per LINE over
#: every shipped file in the repo-wide sweep, not just over a publish.
_RAW_RULES: list[tuple[str, re.Pattern]] = [
    (t, re.compile(p)) for t in BANNED_TERMS for p in _match_patterns(t)
]
_NORMALIZED_RULES: list[tuple[str, re.Pattern]] = _RAW_RULES + [
    (t, re.compile(p)) for t, alias in _ALIASES.items() for p in _match_patterns(alias)
]


def _find_with(rules: list[tuple[str, re.Pattern]], text: str) -> set[str]:
    """Terms whose rules hit *text*. See `_match_patterns` for the rules."""
    return {t for t, rx in rules if rx.search(text)}


def find_banned_terms(text: str) -> list[str]:
    """Banned terms present in *text*, as decoded strings (deduped, sorted).

    TWO passes, never one: the raw text, and the text with render-to-nothing
    markup and invisible characters removed (`demarkup`). The second pass is
    what catches a term the markup splits — `te**rm**`-shaped emphasis, an
    HTML comment wedged mid-word, a zero-width space, a degenerate link — all
    of which render as the whole term and all of which the four letter-boundary
    rules read as unrelated fragments.

    The passes ADD, they do not replace. Stripping markup can manufacture a
    false letter boundary of its own (`<term>_<term>` normalises to one glued
    word that rule (1) then rejects), so dropping the raw pass would lose hits.

    Both passes run the SAME four rules, so the second pass inherits their
    ordinary-English safety rather than relaxing it. Write a banned term inside
    a longer ordinary word and split it with emphasis — the shape
    `<ter>**m**inology` has, for a term spelled t-e-r-m — and the normalised
    form is the longer word, whose trailing letter fails rule (1)'s boundary.
    Correctly NOT flagged, because the boundary is judged on the form the
    reader actually sees. (`tests/test_vendor_terms.py` pins real instances.)

    KNOWN AND DELIBERATE GAPS, so they are read rather than rediscovered:
    a term split across a LINE BREAK is not caught (`demarkup` does not join
    lines — see its docstring for why); nor is one split by a normal
    `[label](target)` link; nor one whose surviving half still holds nested
    markup. One PRE-EXISTING false positive is also unchanged by this: emphasis
    that isolates a term inside a longer word (`r**<term>**`) matches on the RAW
    pass, because `*` is not a letter — it did so before this file grew a second
    pass and it still does. The normalised pass does not add such a case.
    """
    found = _find_with(_RAW_RULES, text)
    normalized = demarkup(text)
    if normalized != text:
        found |= _find_with(_NORMALIZED_RULES, normalized)
    return sorted(found)


# Home-path shapes the publish guard refuses on (commands.py: `"/Users/" in md`).
# Kept broad: any absolute macOS/Linux home path run, stopped at a table pipe,
# whitespace, or closing bracket/paren so it does not eat the rest of a note.
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^\s|)\]]*")

# The suffix a home-path run is allowed to carry before the stop characters —
# shared with `_HOME_PATH_RE` above so both redactions cut at the same places.
_PATH_RUN = r"[^\s|)\]]*"


def _home_path_pattern() -> re.Pattern | None:
    """A pattern for the PROCESS'S OWN home directory, or None.

    `_HOME_PATH_RE` covers the conventional prefixes (`/Users/`, `/home/`), but
    the publish guard in `cli/commands.py` refuses on `str(Path.home())` too —
    and a home that lives anywhere else (containers, CI, a temp `$HOME`) was
    detected by the guard while being invisible to this redactor: the exact
    mismatch that turned a publishable card into a hard refusal with no
    override. Computed per call, not at import, because `$HOME` can change
    under test and the pattern must follow the same value the guard reads.
    Degenerate homes (`/`, empty) are skipped: substituting them would eat
    every path in the note, and the guard's own membership test is equally
    meaningless for them.
    """
    from pathlib import Path

    home = str(Path.home())
    if len(home.rstrip("/")) <= 1:
        return None
    # The lookahead stops a sibling directory (`<home>2/...`) from matching;
    # the run then swallows the rest of the path up to the usual stops.
    return re.compile(
        re.escape(home.rstrip("/")) + r"(?=[/\s|)\]]|$)" + _PATH_RUN)


def _redact_split_forms(text: str, placeholder: str = "<redacted>") -> str:
    """Remove terms that only the NORMALISED pass of `find_banned_terms` sees.

    The paired half of the split-form detection. Detection alone would have made
    things worse, not better: `nh bench publish` redacts first and then runs the
    guard on the redacted text, so a term the detector newly catches and the
    redactor cannot remove turns a publishable card into a hard refusal with no
    override — the guard crying wolf at the one moment nobody can overrule it.

    Works on the index map rather than on a "term with markup allowed between
    its letters" regex, and that is the whole trick. Such a regex has to decide
    its word boundaries against the RAW text, where `<ter>**m**inology` reads as
    a complete term followed by `*` — a non-letter — so ordinary prose gets a
    hole punched in it. Here the match is made against the normalised form the
    reader actually sees (one longer ordinary word, correctly not a hit) and
    only the resulting span is mapped back, so the boundaries keep their meaning.
    """
    normalized, origin = _normalize_with_map(text)
    if normalized == text:
        return text          # nothing hidden; the raw substitutions cover it
    spans = [(m.start(), m.end())
             for _, rx in _NORMALIZED_RULES
             for m in rx.finditer(normalized)
             if m.end() > m.start()]
    if not spans:
        return text
    # Back to original offsets; the map is monotonic, so each match is one run.
    spans = sorted((origin[s], origin[e - 1] + 1) for s, e in spans)
    merged: list[list[int]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = text
    for s, e in reversed(merged):
        out = out[:s] + placeholder + out[e:]
    return out


def redact_terms(text: str, placeholder: str = "[REDACTED]") -> tuple[str, list[str]]:
    """*text* with every banned term replaced by *placeholder*, plus the terms hit.

    Term-only redaction: unlike `redact_for_publish`, this does NOT touch home
    paths — an outbound PR title/body/commit message legitimately contains
    paths, and home-path redaction is the publish path's concern, not this
    one. Reuses the same detector rules (`_RAW_RULES`, `_redact_split_forms`)
    so a term this function misses is a term `find_banned_terms` also misses —
    there is exactly one definition of "banned", never two drifting copies.

    Same fixed-point loop as `redact_for_publish`, for the identical reason:
    redacting one glued term can expose another whose pass already ran.
    """
    if not text:
        return text, []
    found = find_banned_terms(text)
    if not found:
        return text, []
    out = text
    for _ in range(len(BANNED_TERMS) + 1):
        before = out
        for _, rx in _RAW_RULES:
            out = rx.sub(placeholder, out)
        out = _redact_split_forms(out, placeholder)
        if out == before:
            break
    return out, found


def redact_for_publish(text: str) -> str:
    """Return *text* with every home path and banned term removed, so the
    rendered report is publishable AND reproducible from its raw card.

    The card keeps the original judge notes; only the tracked markdown is
    scrubbed. No measured number lives in a note, so nothing measured changes.

    Correctness is guaranteed by property, not by inspection: the module's own
    `find_banned_terms(redact_for_publish(x)) == []` and `"/Users/" not in
    redact_for_publish(x)` hold for every input (see tests). The banned-term
    substitutions mirror `find_banned_terms`' four match rules exactly — they
    are now literally the same regexes, from `_match_patterns` — so the redactor
    can never leave behind something the guard would flag.

    That mirroring is per-PASS, not just per-rule: the detector runs the four
    rules over the raw text AND over the normalised text, so the redactor does
    both too (`_redact_split_forms` is the second). A detection pass with no
    matching redaction pass is not a tightened guard, it is an unpublishable
    card.

    One subtlety forces the fixed-point loop below rather than a single pass:
    redacting a term can UN-GLUE an earlier one whose iteration has already
    passed. When two banned terms are glued in a camelCase identifier, redacting
    the second one leaves the first exposed at a fresh word boundary (`...>Foo`)
    — but the first term's iteration ran before the second's, so a single pass
    never revisits it and the guard then catches the residual (refusing a card
    that should publish). Re-scanning until the string stops changing removes
    it. Termination is guaranteed because every substitution replaces a banned
    term with a placeholder that contains no banned term, so banned content only
    ever shrinks; the loop cap is a belt-and-suspenders bound, not the real stop.
    """
    if not text:
        return text
    out = _HOME_PATH_RE.sub("<path>", text)
    # The process's own home, wherever it lives — the publish guard refuses on
    # `str(Path.home())` as well as on `/Users/`, and the redactor must clean
    # at least everything the guard refuses on or a clean card cannot publish.
    own_home = _home_path_pattern()
    if own_home is not None:
        out = own_home.sub("<path>", out)
    for _ in range(len(BANNED_TERMS) + 1):
        before = out
        # The detector's own four rules, in its own order. Rules (2) and (3) use
        # a zero-width lookahead, so only the term is replaced and the following
        # character is preserved.
        for _, rx in _RAW_RULES:
            out = rx.sub("<redacted>", out)
        # The pass that mirrors the detector's normalised pass. Inside the loop,
        # not after it: splicing out a split term can expose a plain one, and
        # vice versa, and only the fixed point clears both.
        out = _redact_split_forms(out)
        if out == before:
            break
    return out
