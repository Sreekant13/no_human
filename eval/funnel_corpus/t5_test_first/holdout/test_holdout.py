from tinytodo.store import Store


def _store():
    s = Store()
    s.add("yesterday", due=9)       # 1 overdue
    s.add("today", due=10)          # 2 due today, not overdue
    s.add("tomorrow", due=11)       # 3 not yet
    s.add("someday")                # 4 no due day
    s.add("long done", due=1)       # 5 overdue but completed
    s.complete(5)
    return s


def test_only_items_strictly_before_today_are_overdue():
    assert [i["id"] for i in _store().overdue(10)] == [1]


def test_an_item_due_today_is_not_overdue():
    assert 2 not in [i["id"] for i in _store().overdue(10)]


def test_an_item_with_no_due_day_is_never_overdue():
    assert 4 not in [i["id"] for i in _store().overdue(9999)]


def test_a_completed_item_is_never_overdue():
    assert 5 not in [i["id"] for i in _store().overdue(9999)]


def test_they_come_back_in_insertion_order_as_whole_item_dicts():
    items = _store().overdue(11)
    assert [i["id"] for i in items] == [1, 2]
    assert set(items[0]) == {"id", "title", "done", "due"}


def test_nothing_overdue_is_an_empty_list():
    assert _store().overdue(0) == []
