"""Frozen entry point for the bundled nh server.

PyInstaller's Analysis is static: it only bundles what it can SEE. The imports
below are deliberately explicit rather than dynamic (an importlib call here
silently produced a bundle with no `no_human` package at all) — each one anchors
a subtree the CLI reaches lazily at runtime.
"""

import multiprocessing
import sys

# ---------------------------------------------------------------------------
# Windows: make stdio UTF-8 BEFORE anything imports rich.
#
# THE DEFECT THIS FIXES. `nh start` died on Windows before printing its first
# line, taking the whole server with it:
#
#     File "rich\_win32_console.py", line 402, in write_text
#     File "encodings\cp1255.py", line 19, in encode
#     UnicodeEncodeError: 'charmap' codec can't encode character '⚠'
#     [PYI-30976:ERROR] Failed to execute script 'nh_entry'
#
# The CLI prints a "⚠" (U+26A0) warning, and a frozen build's stdio defaults to
# the machine's ANSI codepage — cp1255 on the Windows host this was found on,
# and any non-Latin codepage would do it. Encoding U+26A0 to that codepage
# raises, so the app spawned a server that exited instantly and the board never
# loaded. It reproduces ONLY in the frozen binary: run from a source venv the
# same command boots normally, which is why no source-side test caught it.
#
# Placed ABOVE the `no_human` imports on purpose. rich builds its Console at
# import time and captures the stream's encoding then, so reconfiguring after
# those imports would be too late to matter.
#
# errors="replace" as well as the encoding: a legacy console that still cannot
# render a glyph must degrade to "?" rather than raise. A crash while printing a
# warning is strictly worse than a warning that prints imperfectly.
if sys.platform == "win32":  # pragma: no cover - platform-specific
    for _stream in (sys.stdout, sys.stderr):
        # Guarded: under pythonw, a closed handle, or a wrapper that is not a
        # TextIOWrapper, `reconfigure` is absent — and failing to adjust the
        # encoding must never be the thing that stops the server booting.
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

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
