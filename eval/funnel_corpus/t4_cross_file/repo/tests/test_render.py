from tinytodo.render import as_text


def test_an_undone_item_renders_an_empty_box():
    assert as_text([{"id": 1, "title": "buy milk", "done": False}]) \
        == "1  [ ] buy milk"


def test_a_done_item_renders_a_crossed_box():
    assert as_text([{"id": 2, "title": "buy milk", "done": True}]) \
        == "2  [x] buy milk"


def test_nothing_renders_as_an_empty_string():
    assert as_text([]) == ""
