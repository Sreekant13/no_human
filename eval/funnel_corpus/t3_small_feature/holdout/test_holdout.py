from tinytodo.store import Store


def _store():
    s = Store()
    for title in ("Buy MILK", "call the dentist", "milk the plan",
                  "post a letter"):
        s.add(title)
    return s


def test_search_matches_a_substring_ignoring_case():
    got = [i["title"] for i in _store().search("milk")]
    assert got == ["Buy MILK", "milk the plan"]


def test_search_returns_whole_item_dicts_in_insertion_order():
    items = _store().search("e")
    assert [i["id"] for i in items] == sorted(i["id"] for i in items)
    assert set(items[0]) == {"id", "title", "done"}


def test_search_with_no_match_returns_an_empty_list():
    assert _store().search("zebra") == []


def test_an_empty_term_matches_everything():
    assert len(_store().search("")) == 4


def test_search_does_not_disturb_the_store():
    s = _store()
    s.search("milk")
    assert len(s.all()) == 4
