import json

from tinytodo.cli import STORE, main
from tinytodo.render import as_json, as_text

ITEMS = [{"id": 1, "title": "buy milk", "done": False},
         {"id": 2, "title": "call the dentist", "done": True}]


def test_as_json_round_trips_every_field():
    assert json.loads(as_json(ITEMS)) == ITEMS


def test_as_json_of_nothing_is_an_empty_array():
    assert json.loads(as_json([])) == []


def test_the_cli_json_flag_prints_json(capsys):
    STORE._items.clear()
    assert main(["add", "buy", "milk"]) == 0
    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"id": 1, "title": "buy milk", "done": False}]


def test_list_without_the_flag_is_unchanged(capsys):
    STORE._items.clear()
    main(["add", "buy", "milk"])
    assert main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "1  [ ] buy milk"


def test_as_text_is_untouched():
    assert as_text(ITEMS) == "1  [ ] buy milk\n2  [x] call the dentist"
