"""Report-deliverable adequacy guard (C3 — completion quality).

Report-kind tasks (``investigation`` / ``design_doc``) succeed by producing a
REPORT rather than a code diff, and they bypass the code reviewer. The only bar
today is that the report is non-empty (``.strip()`` truthy), so a degenerate
"deliverable" — a bare ``"Done."``, ``"Investigation complete."``, ``"N/A"`` —
ships as a finished task. This guard rejects the unambiguously-inadequate cases
so the bounded loop retries (and, if the agent truly can't produce substance,
escalates honestly) instead of marking a non-answer DONE.

Deliberately CONSERVATIVE / high-precision: a genuinely terse-but-valid
investigation finding ("Root cause: missing null-check at foo.py:42; guard it.")
must NOT be rejected. So it flags only:
  - a report that is a known NON-ANSWER placeholder (normalized-exact match), or
  - a ``design_doc`` shorter than a floor a real document can never be under
    (a design document is inherently multi-paragraph; an investigation finding
    can legitimately be one sentence, so NO length floor is applied to it).

The harder "present but shallow" case (a report with real but thin content) needs
an LLM adequacy judgement and is intentionally out of scope here — a deterministic
length/keyword rule can't separate shallow from terse-correct without over-firing.
"""

from __future__ import annotations

import re

# A design document is inherently multi-paragraph. 200 chars is well below any
# real design doc and well above any placeholder, so the floor never rejects a
# genuine document. Investigations get NO floor (a one-line root cause is valid).
_DESIGN_DOC_MIN_CHARS = 200

# Reports that carry NO deliverable — normalized (lowercased, stripped of
# surrounding punctuation/markdown) and matched EXACTLY, so anything with real
# content beyond the phrase passes.
_PLACEHOLDERS: frozenset[str] = frozenset({
    "", "done", "complete", "completed", "finished", "n/a", "na", "none",
    "nothing", "nothing to report", "no findings", "no changes",
    "no changes needed", "no change needed", "no code changes", "ok", "okay",
    "see above", "see below", "as requested", "task complete", "task completed",
    "investigation complete", "design doc complete", "report complete",
    "pr opened", "done.", "complete.",
})

_NORMALISE = re.compile(r"^[\s#>*_`\-.:]+|[\s#>*_`\-.:]+$")


def _normalise(text: str) -> str:
    """Lowercase and strip surrounding whitespace / markdown punctuation, so
    ``"**Done.**"`` and ``"- done"`` normalise to ``"done"``."""
    return _NORMALISE.sub("", (text or "").strip().lower())


def report_inadequacy(text: str, kind: str) -> str | None:
    """Return a short reason if ``text`` is an inadequate deliverable for a
    report-kind task, else ``None``.

    Only the unambiguous non-deliverables are flagged (see module docstring);
    real content — even terse — passes.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "the report is empty"
    if _normalise(stripped) in _PLACEHOLDERS:
        return f"the report is a placeholder with no deliverable: {stripped!r}"
    if kind == "design_doc" and len(stripped) < _DESIGN_DOC_MIN_CHARS:
        return (f"the design document is too short to be a deliverable "
                f"({len(stripped)} chars < {_DESIGN_DOC_MIN_CHARS})")
    return None
