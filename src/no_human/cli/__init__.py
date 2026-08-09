"""Shared rendering helpers for the ``nh`` command line."""

from __future__ import annotations

import sys

from rich.console import Console
from rich.markup import escape

__all__ = ["print_path_error", "stdio_is_interactive"]


def stdio_is_interactive() -> bool:
    """Is there a human at a terminal on BOTH ends?

    Textual takes the screen and does not give it back until a key says so, so
    without a terminal there is no key and no exit: `nh </dev/null` ran
    forever, wrote nothing to stdout, and painted a full screen of escapes to
    stderr. That wedges a CI job, a wrapper script, and — since no_human's own
    coder agents run shell commands — an agent's run. Worse, the SIGINT that
    eventually kills it exits 0, so the wedge reads as success.

    A stream that has been closed or replaced raises instead of answering;
    guessing "interactive" there is how the hang comes back.

    Lives here rather than in ``shell.py`` because the shell is not the only
    caller any more: `nh task add`'s scoping grill stops at a ``click.prompt``
    the same way, and importing it from ``shell`` would drag Textual into every
    task-add. Same question, one answer.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            if not stream.isatty():
                return False
        except (AttributeError, ValueError, OSError):
            return False
    return True


def print_path_error(console: Console, prefix: str, detail: str) -> None:
    """Print an error whose text contains a filesystem path, verbatim.

    Rich's default rendering mangles paths two ways, and both of them reach
    real users:

    * It word-wraps to the console width and *folds* any token longer than the
      remaining line, so ``/a/very/long/path`` comes out split across two lines
      with a newline inserted mid-token. 80 columns is not an edge case — it is
      what Rich falls back to whenever stdout is not a terminal, so it is what
      every ``nh ... > err.log``, every pipe and every CI runner gets.
    * It reads square brackets as console markup, so a directory literally
      named ``a[b]c`` is printed as ``ac`` — silently, with no error.

    Either way the user is shown something that is not the path: they cannot
    copy it, paste it, or grep for it. ``escape`` keeps the text literal;
    ``soft_wrap`` hands wrapping back to the terminal, which reflows a long
    line instead of rewriting it.

    ``prefix`` is rendered as markup (it carries the ``[red]`` label);
    ``detail`` is the literal text and is always escaped.
    """
    console.print(f"{prefix} {escape(detail)}", soft_wrap=True)
