import pytest

from tinytodo.store import Store, UnknownItem


def test_an_item_starts_undone():
    s = Store()
    assert s.add("buy milk")["done"] is False


def test_completing_marks_it_done():
    s = Store()
    item = s.add("buy milk")
    assert s.complete(item["id"])["done"] is True


def test_an_unknown_id_raises():
    with pytest.raises(UnknownItem):
        Store().complete(999)


def test_all_returns_them_in_insertion_order():
    s = Store()
    s.add("first")
    s.add("second")
    assert [i["title"] for i in s.all()] == ["first", "second"]
