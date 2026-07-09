"""Shared label handling for the forge CLIs (`gh` / `glab`).

Both spell the flag ``--label`` and accept it repeated, so one helper serves
both. Lives here rather than in either forge module so neither has to import
the other.
"""

from __future__ import annotations


def label_args(labels: list[str] | None) -> list[str]:
    """Flatten labels into repeated ``--label <name>`` argv pairs.

    Blank entries are dropped and order is preserved (first occurrence wins),
    so a stray empty string in config can't produce ``--label ''``.
    """
    args: list[str] = []
    seen: set[str] = set()
    for label in labels or []:
        name = str(label).strip()
        if name and name not in seen:
            seen.add(name)
            args += ["--label", name]
    return args
