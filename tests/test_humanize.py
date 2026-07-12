"""Compact number formatting (humanize_count)."""

from no_human.core.humanize import humanize_count


def test_below_thousand():
    assert humanize_count(0) == "0"
    assert humanize_count(999) == "999"


def test_k_range():
    assert humanize_count(1000) == "1.0k"
    assert humanize_count(1500) == "1.5k"


def test_just_under_million():
    assert humanize_count(999_999) == "1000.0k"


def test_million_boundary():
    assert humanize_count(1_000_000) == "1.0M"


def test_m_range():
    assert humanize_count(2_500_000) == "2.5M"
