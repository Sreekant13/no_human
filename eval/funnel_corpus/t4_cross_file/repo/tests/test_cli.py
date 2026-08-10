from tinytodo.cli import STORE, main


def test_add_then_list(capsys):
    STORE._items.clear()
    assert main(["add", "buy", "milk"]) == 0
    assert main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "1  [ ] buy milk"


def test_no_arguments_is_a_usage_error(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().out


def test_an_unknown_command_is_reported(capsys):
    assert main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().out
