import pytest

from tinytodo.store import Store, UnknownItem


def test_unknown_item_is_a_key_error():
    assert issubclass(UnknownItem, KeyError)


def test_completing_an_id_that_was_never_added_raises():
    s = Store()
    s.add("buy milk")
    with pytest.raises(UnknownItem):
        s.complete(999)


def test_the_exception_names_the_id_it_was_given():
    s = Store()
    with pytest.raises(UnknownItem) as exc:
        s.complete(42)
    assert "42" in str(exc.value)


def test_completing_a_known_id_is_unchanged():
    s = Store()
    item = s.add("buy milk")
    assert s.complete(item["id"])["done"] is True
    assert s.all()[0]["done"] is True
