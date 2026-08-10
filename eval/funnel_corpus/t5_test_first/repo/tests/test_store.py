from tinytodo.store import Store


def test_an_item_without_a_due_day_has_none():
    assert Store().add("buy milk")["due"] is None


def test_a_due_day_is_kept():
    assert Store().add("buy milk", due=10)["due"] == 10


def test_completing_marks_it_done():
    s = Store()
    item = s.add("buy milk", due=3)
    assert s.complete(item["id"])["done"] is True


def test_all_returns_them_in_insertion_order():
    s = Store()
    s.add("first", due=1)
    s.add("second", due=2)
    assert [i["title"] for i in s.all()] == ["first", "second"]
