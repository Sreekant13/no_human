#!/usr/bin/env python3
"""Rewrite drifted `file.py:LINE[-LINE]` citations, mechanically.

`tests/test_readme_claims.py` tolerates small drift (±5 lines, see
`_CITATION_DRIFT_WINDOW`) so an unrelated edit above a citation does not turn
the suite red — but a drifted citation should still get re-anchored, not left
to rely on the tolerance forever. This script finds every drifted legacy
`path:line[-line]` citation across docs/security.md, docs/eval.md,
docs/KNOWN_ISSUES.md and rewrites both the doc text and the matching
CITATION_TABLE row to the line the content now lives on.

It imports the checker's own `_locate_line_citation`/`CITATION_TABLE` by path
rather than re-implementing the search, so this script can never disagree
with what `test_doc_citations_resolve_to_the_code_they_describe` actually
checks.

`--check` (default): read-only; reports drift and exits 1 if anything needs
attention. `--apply`: writes. A citation whose content cannot be found at all
(deleted, reworded, moved beyond the window) is never guessed at — it is
reported as unfixable and left for a human, same as an ambiguous match (the
raw citation text occurs zero or more than once in the doc, or in the
CITATION_TABLE literal). Every fixable drift in a run is written together;
this file never writes a doc without its matching table row, or vice versa.

This file is classified `ship`: it is doc-maintenance tooling useful to any
reader carrying the same line-citation convention, not export machinery.

Standard library only. No dependencies, by requirement (see
CONTRIBUTING.md's "do not add to the stack").
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The exact bracketing of the CITATION_TABLE literal in
# tests/test_readme_claims.py — used to scope table-row rewrites to that
# tuple, never touching a similarly-quoted string elsewhere in the file.
_CITATION_TABLE_START = "CITATION_TABLE = (\n"
_CITATION_TABLE_END = "\n)\n\nassert len(CITATION_TABLE) >= 20,"


def _load_checker():
    """The checker owns the citation grammar; load it by path so this script
    can never define a second, divergent copy of `_locate_line_citation`."""
    path = REPO / "tests" / "test_readme_claims.py"
    spec = importlib.util.spec_from_file_location("_nh_test_readme_claims", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class Drift:
    doc: str
    old_raw: str
    new_raw: str
    resolve_path: str


@dataclass(frozen=True)
class Unfixable:
    doc: str
    raw: str
    reason: str


def _new_spec(tail: str, found_line: int) -> str:
    """The replacement `line` or `line-line` spec, preserving the cited
    range's original length (a shift moves the whole span, it does not
    resize it)."""
    if "-" in tail:
        start_s, end_s = tail.split("-", 1)
        delta = found_line - int(start_s)
        return f"{found_line}-{int(end_s) + delta}"
    return str(found_line)


def plan(mod, rows) -> tuple[list[Drift], list[Unfixable]]:
    """Classify every legacy line-form row in *rows*: drifted (fixable),
    missing (unfixable — nothing to anchor to), or fine (neither, skipped).

    Symbol-form rows are drift-immune by construction and are never
    considered here — this script's whole job is the legacy-line surface.
    """
    drifts: list[Drift] = []
    unfixable: list[Unfixable] = []
    for doc, raw, resolve_path, token in rows:
        tail = raw.split(":", 1)[1]
        if not mod._LEGACY_LINE_SPEC_RE.match(tail):
            continue  # symbol citation — out of scope for this script
        status, found_line, detail = mod._locate_line_citation(resolve_path, tail, token)
        if status in ("exact", "unresolved"):
            continue
        if status == "missing":
            unfixable.append(Unfixable(
                doc, raw,
                detail or f"{token!r} not found near `{raw}` in {resolve_path}"))
            continue
        prefix = raw.split(":", 1)[0]
        new_raw = f"{prefix}:{_new_spec(tail, found_line)}"
        drifts.append(Drift(doc, raw, new_raw, resolve_path))
    return drifts, unfixable


def rewrite(text: str, raw: str, new_raw: str) -> str | None:
    """Replace the single backtick-wrapped occurrence of *raw* in *text*
    with *new_raw*. Pure. Returns None — never guesses — if `` `raw` ``
    occurs zero or more than once."""
    needle = f"`{raw}`"
    if text.count(needle) != 1:
        return None
    return text.replace(needle, f"`{new_raw}`", 1)


def _table_slice(text: str) -> tuple[int, int]:
    start = text.index(_CITATION_TABLE_START) + len(_CITATION_TABLE_START)
    end = text.index(_CITATION_TABLE_END, start)
    return start, end


def rewrite_table_row(text: str, raw: str, new_raw: str) -> str | None:
    """Replace the single double-quoted occurrence of *raw* inside the
    CITATION_TABLE literal in *text* with *new_raw*. Pure. Returns None —
    never guesses — if `"raw"` occurs zero or more than once in that slice,
    or the CITATION_TABLE literal cannot be located at all."""
    try:
        start, end = _table_slice(text)
    except ValueError:
        return None
    body = text[start:end]
    needle = f'"{raw}"'
    if body.count(needle) != 1:
        return None
    new_body = body.replace(needle, f'"{new_raw}"', 1)
    return text[:start] + new_body + text[end:]


def _apply_all(
    mod, drifts: list[Drift]
) -> tuple[dict[Path, str] | None, list[Unfixable]]:
    """Build every new file text in memory; only hand any of them back if
    EVERY drift resolved on both surfaces (doc + table row) — an
    all-or-nothing batch, so a partial apply can never leave a doc and its
    CITATION_TABLE row pointing at different lines."""
    table_path = REPO / "tests" / "test_readme_claims.py"
    doc_texts: dict[Path, str] = {}
    table_text = table_path.read_text(encoding="utf-8")
    unresolved: list[Unfixable] = []

    for d in drifts:
        doc_path = mod._CITATION_DOC_PATHS[d.doc]
        doc_text = doc_texts.get(doc_path, doc_path.read_text(encoding="utf-8"))
        new_doc_text = rewrite(doc_text, d.old_raw, d.new_raw)
        if new_doc_text is None:
            unresolved.append(Unfixable(
                d.doc, d.old_raw,
                f"`{d.old_raw}` does not occur exactly once in {doc_path} "
                f"— will not guess which occurrence to rewrite"))
            continue
        new_table_text = rewrite_table_row(table_text, d.old_raw, d.new_raw)
        if new_table_text is None:
            unresolved.append(Unfixable(
                d.doc, d.old_raw,
                f'"{d.old_raw}" does not occur exactly once in CITATION_TABLE '
                f"— will not guess which row to rewrite"))
            continue
        doc_texts[doc_path] = new_doc_text
        table_text = new_table_text

    if unresolved:
        return None, unresolved
    doc_texts[table_path] = table_text
    return doc_texts, unresolved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="reanchor_citations.py",
        description="Report or rewrite drifted file.py:LINE[-LINE] "
                     "citations in docs/security.md, docs/eval.md, "
                     "docs/KNOWN_ISSUES.md and their CITATION_TABLE rows in "
                     "tests/test_readme_claims.py.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                       help="read-only (default): report drift, write nothing")
    mode.add_argument("--apply", action="store_true",
                       help="rewrite the doc and CITATION_TABLE row for "
                            "every drifted citation")
    args = ap.parse_args(argv)

    try:
        mod = _load_checker()
    except Exception as exc:  # the checker failed to import or parse
        print(f"FAIL: could not load tests/test_readme_claims.py: {exc}")
        print("VERDICT=FAIL")
        return 2

    drifts, unfixable = plan(mod, mod.CITATION_TABLE)

    for u in unfixable:
        print(f"FAIL: {u.doc} `{u.raw}` — {u.reason}")

    if not drifts and not unfixable:
        print("VERDICT=OK")
        return 0

    for d in drifts:
        verb = "re-anchoring" if args.apply else "would re-anchor"
        print(f"DRIFT: {d.doc} `{d.old_raw}` -> `{d.new_raw}` ({verb})")

    if not args.apply:
        print("VERDICT=FAIL")
        return 1

    if not drifts:
        # Nothing fixable to write; the unfixable rows above still block.
        print("VERDICT=FAIL")
        return 1

    texts, unresolved = _apply_all(mod, drifts)
    if texts is None:
        for u in unresolved:
            print(f"FAIL: {u.doc} `{u.raw}` — {u.reason}")
        print("VERDICT=FAIL")
        return 1

    for path, new_text in texts.items():
        path.write_text(new_text, encoding="utf-8")
    print(f"applied {len(drifts)} re-anchor(s)")
    print("VERDICT=" + ("FAIL" if unfixable else "OK"))
    return 1 if unfixable else 0


if __name__ == "__main__":
    raise SystemExit(main())
