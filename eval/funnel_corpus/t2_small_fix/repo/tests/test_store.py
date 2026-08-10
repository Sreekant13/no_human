from tinytodo.store import Store


def test_an_item_starts_undone():
    s = Store()
    assert s.add("buy milk")["done"] is False


def test_completing_marks_it_done():
    s = Store()
    item = s.add("buy milk")
    assert s.complete(item["id"])["done"] is True


def test_the_completion_sticks():
    s = Store()
    item = s.add("buy milk")
    s.complete(item["id"])
    assert s.all()[0]["done"] is True


def test_ids_are_handed_out_in_order():
    s = Store()
    assert [s.add("a")["id"], s.add("b")["id"]] == [1, 2]
