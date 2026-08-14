"""The single choke point for outbound forge text — PR titles, PR bodies,
draft-PR titles, commit messages built from task titles, and verification
comments — so no flagged vendor term ever reaches a forge.

Every writer of free text to GitHub/GitLab (`vcs/__init__.py::open_pr`,
`vcs/comment_poster.py::post_to_pr`/`set_pr_title`, `vcs/git.py`'s commit-
message sanitizer, and `vcs/pr_watcher.py`'s reply/upsert comment posters)
must route its text through `scrub_outbound` before the first subprocess call.
A P1 task's own PR title carried flagged terms verbatim because PR titles
built from task titles had no scrub at all — this module closes that gap in
exactly one place instead of patching each call site with its own regex.
"""

from __future__ import annotations

import logging

log = logging.getLogger("no_human.vcs.scrub")

#: Applied consistently across every outbound field this module guards.
PLACEHOLDER = "[REDACTED]"


class ScrubDrainedError(RuntimeError):
    """Raised when scrubbing would leave a field empty/placeholder-only.

    A title or message that is entirely vendor terms scrubs down to nothing
    meaningful — proceeding would silently ship a blank/placeholder-only PR
    title or commit message. The operator must choose new text; this is
    loud, not a silent drop.
    """


def scrub_outbound(text: str, field: str) -> str:
    """*text* with every banned vendor term replaced by `PLACEHOLDER`.

    Emits one ERROR-level log record per call that hits, naming *field* —
    but never the term itself: a scanner (or scrubber) that spells its own
    targets in a log line is itself a leak.

    Raises `ScrubDrainedError` if the scrubbed result is empty or consists
    only of the placeholder (and whitespace) while the original text was not
    empty — refusing to post a field drained of its own meaning.
    """
    if not text:
        return text
    # Imported lazily: `no_human.eval` is a package whose __init__ pulls in
    # the orchestrator/agent/blockers graph, which imports back into `vcs`
    # (e.g. blockers.wake -> vcs.pr_outcome -> vcs.pr_watcher). A top-level
    # import here would make importing any `vcs` module a circular import.
    from ..eval.vendor_terms import redact_terms
    cleaned, terms = redact_terms(text, PLACEHOLDER)
    if not terms:
        return text
    log.error(
        "Scrubbed vendor term from field %s (%d occurrence class(es)) before forge write",
        field, len(terms),
    )
    if cleaned.replace(PLACEHOLDER, "").strip() == "":
        raise ScrubDrainedError(
            f"outbound {field} is entirely vendor terms after scrub; refusing to write"
        )
    return cleaned
