"""AST guard for rich-markup escaping in the bench commands.

WHY THIS EXISTS AS AN AST WALK, when three textual versions came before it.

Each earlier attempt matched something a human wrote down, and each was blind to
the case it existed for — which is always the NEXT line someone writes:

    v1  the FIRST line containing ⛔        -> blind to the other three ⛔ lines
    v2  EVERY line containing ⛔            -> blind to ⚠ lines carrying the same values
    v3  a list of variable NAMES            -> blind to {legacy.label} and {rows}

An independent reviewer found v3's gap by crashing `nh bench run --resume`, which
died with MarkupError and an EMPTY stdout. The value was `legacy.label` — the same
field escaped in five other places under two different names.

So this inverts the polarity. It does not know any variable names. It walks every
`console.print(...)` call and requires that EVERY interpolated value is either

  (a) wrapped in `escape(...)`, or
  (b) provably safe by SHAPE, not by name — see `_is_provably_safe`.

Anything else is an offender.

WHAT THIS DOES **NOT** COVER — read this before trusting it. An earlier version of
this docstring claimed "a new print of a new variable fails by default, which is
the property a guard needs and enumeration cannot have." **That claim was FALSE**
and a fourth independent reviewer measured it. This walks `ast.FormattedValue`
nodes only, so it is BLIND to:

    console.print(card.label)                          # bare Name  -> LIVE CRASH, guard green
    console.print(card.label or "")                    #            -> LIVE CRASH, guard green
    console.print("published " + card.label)           # BinOp concat
    console.print("published {}".format(card.label))   # .format()
    console.print("published %s" % card.label)         # %-format
    console.print(f"{escape(path.name) + card.label}") # escape() present but not at the root

The last one matters twice over: `_wraps_escape` uses `ast.walk`, so an `escape()`
ANYWHERE in the subtree satisfies it — including one applied to a different value.
And `console.print(<bare Name>)` is not a hypothetical shape: it appears **14
times in commands.py** (e.g. :743 `console.print(outcome.detail)`), so the idiom
this guard cannot see is the codebase's own established idiom.

THE MINIMUM FIX, from that reviewer, NOT yet applied: inspect every str-valued
ARGUMENT rather than only FormattedValue, and require `escape()` at the
interpolation ROOT rather than anywhere beneath it.

TWO MORE HOLES, also measured and also open: relocating `@bench.command("publish")`
past the end marker while leaving a comment that names it keeps the region anchor
satisfied and the guard dark (S1); and a helper defined one line ABOVE
`@cli.group("bench")` but called from inside it is out of region entirely (S2) —
`_render_report_or_refuse` is exactly such a helper and merely happens to sit
inside today.

So: this catches the f-string case, which is the one the original v11 crash and
every site fixed on this branch used. It is NOT a general escape guard, and the
region as SHIPPED was independently scanned clean by that reviewer's own tool.
Treat it as a regression test for known shapes, not as a discovery instrument,
until the two fixes above land.

It is also immune to two bypasses the textual versions had:
  - parentheses inside string literals (v3 tracked paren depth by counting
    characters; one extra ")" closed the scan early and hid the rest of the call)
  - multi-value prints where ONE value is escaped and another is not (a per-line
    `"escape(" in line` check is satisfied by the first)
because the AST gives exact call boundaries and one node per interpolation.
"""

from __future__ import annotations

import ast


def _is_provably_safe(node: ast.AST) -> bool:
    """True only for shapes that cannot carry `[/...]`.

    Deliberately narrow and shape-based. Adding a name here is how this guard
    would rot back into an enumeration, so each case is justified:
      - a literal          : the author typed it
      - len(...) / a count : an int
      - an f-string of safe parts : recurse
      - `.value` on an enum member, `:.0%`-style numeric formats are NOT special-
        cased; wrap them in escape() or add a justified case with a comment.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"len", "int", "float", "round", "sum", "abs"}:
            return True
        if node.func.id == "escape":
            return True
    if isinstance(node, ast.JoinedStr):
        return all(_is_provably_safe(v.value)
                   for v in node.values if isinstance(v, ast.FormattedValue))
    if isinstance(node, ast.BinOp):
        return _is_provably_safe(node.left) and _is_provably_safe(node.right)
    return False


def _wraps_escape(node: ast.AST) -> bool:
    """Does this interpolation pass through `escape(...)` anywhere inside it?"""
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                and sub.func.id == "escape"):
            return True
    return False


def find_unescaped_prints(source: str, lo: int, hi: int) -> list[tuple[int, str]]:
    """Every console.print interpolation in lines [lo, hi] that is neither
    escaped nor provably safe. Returns (lineno, rendered-expression) pairs."""
    tree = ast.parse(source)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = (f.attr if isinstance(f, ast.Attribute)
                else f.id if isinstance(f, ast.Name) else "")
        if name != "print":
            continue
        if not (lo <= node.lineno <= hi):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if not isinstance(sub, ast.FormattedValue):
                    continue
                if _wraps_escape(sub.value) or _is_provably_safe(sub.value):
                    continue
                out.append((sub.lineno, ast.unparse(sub.value)))
    return sorted(set(out))
