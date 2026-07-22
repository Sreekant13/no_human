"""Frozen entry point for the bundled nh server.

PyInstaller's Analysis is static: it only bundles what it can SEE. The imports
below are deliberately explicit rather than dynamic (an importlib call here
silently produced a bundle with no `no_human` package at all) — each one anchors
a subtree the CLI reaches lazily at runtime.
"""

import multiprocessing
import sys

# Anchors for static analysis. Order/grouping is for the bundler, not runtime.
import no_human.api.app          # noqa: F401  the FastAPI board + /api surface
import no_human.cli.commands     # noqa: F401  the click entry below
import aiosqlite                 # noqa: F401  DB driver, imported via a string
import uvicorn                   # noqa: F401  ASGI server

from no_human.cli.commands import main

if __name__ == "__main__":
    # A frozen binary that spawns itself re-runs this module rather than a
    # Python interpreter; freeze_support turns that into a no-op child.
    multiprocessing.freeze_support()
    sys.exit(main())
