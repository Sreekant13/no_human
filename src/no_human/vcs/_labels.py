"""Shared label handling for the forge CLIs (`gh` / `glab`).

Both spell the flag ``--label`` and accept it repeated, so one helper serves
both. Lives here rather than in either forge module so neither has to import
the other.
"""

from __future__ import annotations


def is_label_error(stderr: str) -> bool:
    """True if a forge `create` failed specifically because a label doesn't
    exist on the repo (e.g. metrics-core's ``V17`` applied to a repo that never defined
    it). Such a failure must not block the whole PR — the caller retries without
    labels. Kept deliberately narrow so a non-label failure is never mistaken
    for one (which would silently drop a real error)."""
    s = (stderr or "").lower()
    if "label" not in s:
        return False
    return any(
        phrase in s
        for phrase in ("could not add label", "not found", "not a valid label",
                       "does not exist")
    )


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
