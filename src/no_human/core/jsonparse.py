"""Lenient JSON parsing for LLM-produced output.

Models routinely emit JSON that is *almost* valid but contains invalid
backslash escapes inside string values — e.g. a filesystem/URL path like
``\\job\\...`` or a regex like ``\\d+`` written without doubling the
backslash. ``json.loads`` rejects these with ``Invalid \\escape``. When such a
crash happens in a verdict parser (reviewer, evaluator, judge), the caller
fails closed and a legitimate result is discarded — burning a task attempt for
reasons that have nothing to do with the code under review.

``loads_lenient`` first tries strict parsing (so well-formed JSON is never
altered), and only on failure repairs stray backslash escapes and retries. If
it still cannot parse, it raises ``json.JSONDecodeError`` so existing
callers' error handling is preserved unchanged.
"""

from __future__ import annotations

import json
from typing import Any

# The only characters that may follow a backslash in a valid JSON string.
_VALID_ESCAPE_NEXT = set('"\\/bfnrt')
_HEX = set("0123456789abcdefABCDEF")


def repair_json_escapes(s: str) -> str:
    """Double any backslash that does not begin a valid JSON escape sequence.

    Valid escapes (``\\"`` ``\\\\`` ``\\/`` ``\\b`` ``\\f`` ``\\n`` ``\\r``
    ``\\t`` ``\\uXXXX``) are left untouched — including already-doubled
    backslashes, which are consumed as a pair so they are never corrupted.
    Any other ``\\X`` is turned into ``\\\\X`` so the backslash survives as a
    literal character in the parsed string.
    """
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt in _VALID_ESCAPE_NEXT:
                out.append(c)
                out.append(nxt)
                i += 2
                continue
            if (
                nxt == "u"
                and i + 6 <= n
                and all(ch in _HEX for ch in s[i + 2 : i + 6])
            ):
                out.append(s[i : i + 6])
                i += 6
                continue
            # Invalid escape (e.g. \e, \d, \x, trailing \, or \u with bad hex):
            # double the backslash so it becomes a literal backslash.
            out.append("\\\\")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def loads_lenient(s: str) -> Any:
    """``json.loads`` with a repair pass for invalid backslash escapes.

    Strict parsing is attempted first, so valid JSON is returned byte-for-byte
    identical. Raises ``json.JSONDecodeError`` if the string cannot be parsed
    even after repair.
    """
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(repair_json_escapes(s))
