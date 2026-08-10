import pytest

from tinytodo.store import MAX_ITEMS, ListFull, Store


def test_the_limit_is_what_the_module_says_it_is():
    assert MAX_ITEMS == 200


def test_adding_past_the_limit_raises():
    s = Store()
    for i in range(MAX_ITEMS):
        s.add(f"item {i}")
    with pytest.raises(ListFull):
        s.add("one too many")


def test_items_are_numbered_from_one():
    s = Store()
    assert s.add("first")["id"] == 1
    assert s.add("second")["id"] == 2
