from no_human.proc import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    hidden_console_kwargs,
)


def test_windows_flags_hide_console_and_new_group():
    kw = hidden_console_kwargs(new_group=True, platform="win32")
    assert kw == {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}


def test_windows_flags_hide_console_only():
    assert hidden_console_kwargs(platform="win32") == {"creationflags": CREATE_NO_WINDOW}


def test_posix_new_group():
    assert hidden_console_kwargs(new_group=True, platform="darwin") == {"start_new_session": True}


def test_posix_plain():
    assert hidden_console_kwargs(platform="linux") == {}
