"""The command line: `python -m tinytodo.cli add "buy milk"`."""

import sys

from tinytodo.render import as_text
from tinytodo.store import Store

STORE = Store()


def main(argv=None):
    """Run one command. Returns the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: cli.py [add TITLE | list]")
        return 2
    command, rest = argv[0], argv[1:]
    if command == "add":
        if not rest:
            print("add needs a title")
            return 2
        STORE.add(" ".join(rest))
        return 0
    if command == "list":
        print(as_text(STORE.all()))
        return 0
    print(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
