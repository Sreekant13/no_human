"""Compact human-readable number formatting (pure helper)."""
from __future__ import annotations


def humanize_count(n: int) -> str:
    """Compact number display: 999 -> '999', 1500 -> '1.5k', 2_500_000 -> '2.5M'."""
    if n < 1000:
        return str(n)
    elif n < 1_000_000:
        return f"{n / 1000:.1f}k"
    else:
        return f"{n / 1_000_000:.1f}M"
