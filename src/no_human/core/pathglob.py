"""ONE shared glob matcher for repo-relative path patterns.

Before this module there were three independent glob-to-regex translators in
this repo (`core/merge_policy.py`, `review/verifiers.py`, and
`core/project_config._is_scoped_glob`) that had drifted to slightly different
semantics by accident, not by design. This module is the single
implementation the first two now both delegate to (`merge_policy.py`'s was
the strictest/most complete of the two — char-class (`[...]`/`[!...]`)
support, a fail-soft literal-escape fallback for an unterminated `[`, and a
compiled-pattern cache — so it is the one that moved here verbatim).

``project_config._is_scoped_glob`` stays separate on purpose: it answers a
different question (a predicate — "is this glob *scoped* under a project
subtree" — not "does this path match this glob") and consolidating it here
would blur that boundary rather than clarify it.

Semantics, matching `fnmatch`/shell glob conventions with directory-aware
`*`/`**` (unlike bare `fnmatch`, whose `*` wrongly crosses `/`):

* ``*``  matches any run of characters *except* ``/`` within one path segment.
* ``**`` crosses directory boundaries: a trailing ``docs/**`` matches both
  ``docs/x.md`` and ``docs/a/b.md``; a non-trailing ``**`` matches zero or
  more whole directory segments.
* ``?``  matches exactly one non-``/`` character.
* ``[...]``/``[!...]`` are POSIX-style character classes; an unterminated
  ``[`` falls back to a literal (escaped) match of the whole segment rather
  than raising.
* A pattern that fails to compile to a regex (``re.error``) is treated as
  never matching, rather than raising — untrusted, hand-edited glob lists
  (a `.no_human/merge_policy.yaml`, a verifier config) must never crash a
  gate.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath


def normalize_path(p: str) -> str:
    """Normalize a path string for matching: backslashes to ``/``, strip a
    leading ``./``, strip leading ``/``, collapse via ``PurePosixPath``."""
    p = p.replace("\\", "/")
    p = p.removeprefix("./")
    p = p.lstrip("/")
    return PurePosixPath(p).as_posix()


def _translate_segment(seg: str) -> str:
    if seg == "**":
        return ""  # handled specially by the caller
    out = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = seg.find("]", i + 2)
            if j == -1:
                # unterminated class: fall back to a literal escape of the
                # whole segment rather than raising.
                return re.escape(seg)
            cls = seg[i:j + 1]
            if cls.startswith("[!"):
                cls = "[^" + cls[2:]
            out.append(cls)
            i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _glob_to_regex(pattern: str) -> str:
    segments = pattern.split("/")
    n = len(segments)
    pieces: list[str] = []
    for idx, seg in enumerate(segments):
        is_last = idx == n - 1
        if seg == "**":
            if is_last:
                # A trailing `**` gets an alternative consuming the
                # remainder: one or more path segments joined by "/", so
                # `docs/**` matches both `docs/x.md` and `docs/a/b.md`.
                pieces.append(r"[^/]+(?:/[^/]+)*")
            else:
                # A non-trailing `**` matches zero or more whole directory
                # segments, each already including its trailing "/".
                pieces.append(r"(?:[^/]+/)*")
        else:
            pieces.append(_translate_segment(seg))
            if not is_last:
                pieces.append("/")
    return "".join(pieces)


_COMPILE_CACHE: dict[str, re.Pattern[str] | None] = {}


def compile_glob(pattern: str) -> re.Pattern[str] | None:
    """Compile *pattern* to an anchored regex, cached. ``None`` if the
    translated pattern fails to compile (``re.error``) — never raises."""
    if pattern not in _COMPILE_CACHE:
        try:
            _COMPILE_CACHE[pattern] = re.compile(_glob_to_regex(pattern))
        except re.error:
            _COMPILE_CACHE[pattern] = None
    return _COMPILE_CACHE[pattern]


def matches(path: str, patterns: list[str]) -> bool:
    """True if *path* (normalized) fullmatches any glob in *patterns*."""
    norm = normalize_path(path)
    for pattern in patterns:
        rx = compile_glob(pattern)
        if rx is not None and rx.fullmatch(norm):
            return True
    return False
