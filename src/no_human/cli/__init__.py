"""Shared rendering helpers for the ``nh`` command line."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

__all__ = ["print_path_error"]


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
