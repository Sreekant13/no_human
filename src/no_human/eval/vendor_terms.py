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

WHY THE LIST IS SPLIT IN TWO (public export, 2026-07-30)
--------------------------------------------------------
This module SHIPS: `cli/commands.py` and `eval/northstar_card.py` import it, so
dropping it from the public export breaks `nh bench publish`. But the operator's
former-employer terms are the one thing that must not appear in the public repo
in ANY form — and a hex-encoded inventory of exactly what was scrubbed, sitting
in a file whose docstring explains the encoding, is a labelled index, not a
redaction. Hex is only unreadable to grep; it is not unreadable.

So the employer half lives in `_vendor_terms_private.py`, which is classified
`drop` in `EXPORT_CLASSIFICATION.txt` and is deleted from the public export. This file carries
only the competitor/model half, which is not employer-identifying.

The import is optional on purpose. In the private repo the supplement is present
and the guard behaves exactly as before — the employer terms are still detected
and still redacted out of a publish, which is where they were most likely to
leak. In the public export the supplement is absent, `EXTRA_HEX` is empty, and
the guard keeps working on the terms that remain. Removing the employer entries
outright would have disarmed the private repo's publish guard, which is the copy
that actually has employer content to redact.
"""

from __future__ import annotations

import re

_t = lambda h: bytes.fromhex(h).decode()  # noqa: E731

# Competitor products and model names. Not employer-identifying — these ship.
_BANNED_HEX = [
    "646576696e", "77696e6473757266", "636f70696c6f74", "636f676e6974696f6e",
    "6169646572", "6f70656e6169", "6770742d34", "6770742d35",
]

# The employer half, absent from the public export. Never spell these here.
#
# FAILS CLOSED, deliberately. A bare `except ImportError` also swallows an
# ImportError raised from INSIDE the supplement for any unrelated reason — a bad
# import, a typo, a half-finished edit — and then the private repo's publish
# guard silently drops from 18 terms to 8 with the whole suite still green. That
# is the failure that matters, because the private repo is the copy that has
# employer content to redact. `e.name` distinguishes "this module is not here"
# (the export, the only sanctioned case) from "this module is here and broken"
# (a bug, which must be loud). `test_private_terms.py` pins the count at 18.
try:
    from ._vendor_terms_private import EXTRA_HEX as _EXTRA_HEX
except ImportError as e:
    if e.name not in (__package__ + "._vendor_terms_private", "_vendor_terms_private"):
        raise
    _EXTRA_HEX: list[str] = []  # public export: the supplement is not distributed

BANNED_TERMS: list[str] = [_t(h) for h in (*_BANNED_HEX, *_EXTRA_HEX)]


def find_banned_terms(text: str) -> list[str]:
    """Banned terms present in *text*, as decoded strings (deduped, sorted).

    Matched on WORD boundaries that treat only letters as word characters, so
    a term glued on with a hyphen or underscore (`<term>-metrics-core-pipeline`,
    the shape that actually slipped a codename into the report) is still
    caught, while ordinary English that merely CONTAINS a term is not.

    A plain substring match was tried first and is wrong: several banned
    terms are substrings of ordinary English words, and the refusal is
    fail-closed with no override — so the operator's only escape would be
    editing a legitimate measured note to satisfy the guard. The concrete
    cases live in tests/test_bench_publish.py, which is free to spell them.
    """
    low = text.lower()
    found = {t for t in BANNED_TERMS
             if re.search(rf'(?<![a-z]){re.escape(t)}(?![a-z])', low)}
    # Judge notes quote code identifiers verbatim, so a term glued into
    # camelCase (`<Term>Client`, `<Term>Producer`) or trailed by a digit slips
    # a letter-boundary rule. The flag is scoped INLINE to the term: a global
    # re.IGNORECASE would also fold the lookahead class, which re-admits every
    # ordinary-English case this rule exists to leave alone.
    for t in BANNED_TERMS:
        e = re.escape(t)
        # camelCase only: the term Titlecased or lowercase, then an upper-case
        # letter/digit/underscore. Deliberately NOT the all-caps form — that
        # would flag real acronyms that merely start with the letters (a national
        # railway operator's acronym, for one). An all-lowercase glue is likewise not matched; the
        # repo-wide scanner has the same blind spot and closing it here would
        # re-admit every ordinary-English case above.
        # A Titlecased term may be preceded by a lowercase letter
        # (`getXClient`, and — the case that actually occurs — pasted content
        # with literal \n escapes, which makes every line-start term read as
        # `nX`). Only the lowercase-preceded form is relaxed: allowing an
        # upper-case predecessor would flag ALL-CAPS acronyms again.
        if re.search(rf'(?<![A-Z]){re.escape(t.capitalize())}(?=[A-Z0-9_])', text):
            found.add(t)
        if re.search(rf'(?<![A-Za-z]){e}(?=[A-Z0-9_])', text):
            found.add(t)
        # A term ending in a digit is a model name; its suffix is a letter
        # (`-4o`, `-4-turbo`), so the letter-boundary rule above misses it.
        if t[-1].isdigit() and re.search(rf'(?<![a-z]){e}', text.lower()):
            found.add(t)
    return sorted(found)


# Home-path shapes the publish guard refuses on (commands.py: `"/Users/" in md`).
# Kept broad: any absolute macOS/Linux home path run, stopped at a table pipe,
# whitespace, or closing bracket/paren so it does not eat the rest of a note.
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^\s|)\]]*")


def redact_for_publish(text: str) -> str:
    """Return *text* with every home path and banned term removed, so the
    rendered report is publishable AND reproducible from its raw card.

    The card keeps the original judge notes; only the tracked markdown is
    scrubbed. No measured number lives in a note, so nothing measured changes.

    Correctness is guaranteed by property, not by inspection: the module's own
    `find_banned_terms(redact_for_publish(x)) == []` and `"/Users/" not in
    redact_for_publish(x)` hold for every input (see tests). The banned-term
    substitutions mirror `find_banned_terms`' four match rules exactly so the
    redactor can never leave behind something the guard would flag.

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
    for _ in range(len(BANNED_TERMS) + 1):
        before = out
        for t in BANNED_TERMS:
            e = re.escape(t)
            # (1) letter-boundary, case-insensitive (mirrors the `low` search).
            out = re.sub(rf"(?<![A-Za-z])(?i:{e})(?![A-Za-z])", "<redacted>", out)
            # (2)+(3) camelCase / glued-identifier forms. Zero-width lookahead,
            # so only the term is replaced and the following char is preserved.
            out = re.sub(rf"(?<![A-Z]){re.escape(t.capitalize())}(?=[A-Z0-9_])",
                         "<redacted>", out)
            out = re.sub(rf"(?<![A-Za-z]){e}(?=[A-Z0-9_])", "<redacted>", out)
            # (4) model names ending in a digit — letter suffix escapes rule (1).
            if t[-1].isdigit():
                out = re.sub(rf"(?<![A-Za-z])(?i:{e})", "<redacted>", out)
        if out == before:
            break
    return out
