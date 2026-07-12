"""English ordinal formatting (pure helper)."""
from __future__ import annotations


def ordinal(n: int) -> str:
    """Return the English ordinal string for an integer: 1 -> '1st', 11 -> '11th', -2 -> '-2nd'."""
    abs_n = abs(n)
    if 11 <= abs_n % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(abs_n % 10, "th")
    return f"{n}{suffix}"
