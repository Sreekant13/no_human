"""The shared renderer for `attempts.full_final_text` — the surface the PR
body's `_SUMMARY_TRUNCATED_MARKER` (4000-char cap) points a reader at.

`TaskOut.full_report` (`api/models.py`) and `nh task show` (`cli/commands.py`)
both call `render_full_report` instead of each rolling its own filter, so
the two surfaces cannot drift into disagreeing about what a coder's report
is allowed to say. It is coder-authored text, exactly like the PR summary,
so it passes the SAME two steps before any display: the drop-marker filter
`_pr_body` already applies (`Orchestrator._filter_summary_paragraphs`), and
the outbound scrub every forge-bound field passes through
(`vcs.outbound_scrub.scrub_outbound`; contract pinned by
`tests/test_outbound_scrub.py`). It is deliberately UNCAPPED — that is the
entire reason this surface exists: `_SUMMARY_MAX_CHARS` stays at 4000 for
the PR body, and this is where the tail that cut from goes.
"""

from __future__ import annotations


def render_full_report(text: str | None) -> str | None:
    """Filter and scrub *text* for display, or `None` if there is nothing to show.

    Returns `None` for empty/whitespace-only input (nothing was ever written,
    or the coder's report was blank) — callers render that as "no report",
    not as an empty section. A field that scrubs down to nothing (every
    vendor term) renders as `Orchestrator._SUMMARY_FILTERED_PLACEHOLDER`
    rather than raising: a report a human explicitly asked to read must show
    SOMETHING, loud, not silently disappear.
    """
    if not text or not text.strip():
        return None
    # Lazy imports: both modules sit downstream of `core`/`vcs` package
    # __init__ graphs that import back into this one (the same cycle
    # `outbound_scrub.py` names in its own lazy-import comment for
    # `eval.vendor_terms`) — a top-level import here would make importing
    # `report_surface` a circular import for any caller that pulls in
    # `orchestrator` or `vcs` first.
    from .orchestrator import Orchestrator
    from ..vcs.outbound_scrub import ScrubDrainedError, scrub_outbound

    filtered = Orchestrator._filter_summary_paragraphs(text)
    filtered = Orchestrator._close_orphaned_block(filtered)
    try:
        return scrub_outbound(filtered, "task_full_report")
    except ScrubDrainedError:
        return Orchestrator._SUMMARY_FILTERED_PLACEHOLDER
