"""Detect references to inputs the agent cannot access (C2 — honest handling of
unavailable inputs).

A task may reference a visual or attached input — a pasted ``[Image #N]`` marker,
"see the attached screenshot", "as shown in the mockup below" — that no_human
cannot actually see. Proceeding anyway means fabricating what that input
contained (the over-eager failure mode: the agent answers as if it had seen the
image). This module deterministically detects such **dangling** references
(referential mentions with no accompanying attachment) so intake can ESCALATE
— "please attach it or describe its contents" — instead of guessing.

Design notes:

- **Deliberately HIGH-PRECISION.** It fires only on clearly *referential*
  markers: a pasted ``[Image #N]`` / ``[attachment …]`` bracket, or a visual
  noun bound to a deictic (``attached``/``below``/``above``/``here``/
  ``following``) or an "as shown in …" phrase. Topical mentions — "add a
  screenshot button", "the ``<img>`` tag", "log the error image path" — do NOT
  fire. A false escalation (blocking a task that never needed the input) is more
  annoying than a missed one, so the bar is intentionally high.

- **Generalizes.** It keys on the general property "the task points at an input
  that isn't provided", true of ANY task on ANY repo — not on any particular
  spec. It is a deterministic regex pass, not an LLM judgement (the over-eager
  LLM is exactly the thing being corrected).

- **Attachments are trusted.** If the task carries *any* attachment, the refs
  are assumed satisfied (the operator provided files for this task); mapping a
  specific phrase to a specific file is fragile, so we do not attempt it and we
  do not escalate. Only a referential mention with *no* attachment at all is
  treated as dangling.
"""

from __future__ import annotations

import re
from typing import Any

from ..blockers.taxonomy import Blocker, BlockerCategory

# Visual-artifact nouns that, when pointed at as an *attached* or *shown* item,
# denote an input the agent would need to SEE to do the task. Deliberately
# excludes generic "file/document/attachment" (those collide with feature names
# like "attached-files endpoint"); the pasted-bracket pattern still catches an
# explicit ``[attachment: …]``.
_VISUAL_NOUN = (
    r"screen\s?shots?|images?|diagrams?|mock-?ups?|wireframes?|figures?|"
    r"photos?|pictures?|screencaps?|screen\s?grabs?"
)

# Precision over recall (a false escalation blocks a task that never needed the
# input). Only two signal classes survive — each chosen because it essentially
# never appears topically in a dev task:
#   1. a pasted chat-style bracket marker — [Image #1], [attachment: foo.png],
#      [screenshot]. This is the canonical case (a task pasted from a chat that
#      contained an image the agent can't see) and almost never topical.
#   2. "as shown/seen/depicted/… in the <visual noun>" — the visual noun is
#      MANDATORY and the object of "in", so "as shown in the mockup" fires but
#      "as shown in the code below" (an inline example) does not.
#
# DELIBERATELY DROPPED (two review rounds on PR #172 found these over-fire):
#   - post-deictics above/below/here — collide with position/size idioms
#     ("images above 2MB", "photos below the fold");
#   - the "following" list introducer ("the following documents");
#   - bare pointer verbs see/refer/per/check ("check the images render");
#   - and — the whole "attached"/"enclosed" free-text family. "attached" is a
#     past participle that is grammatically identical in a genuine deictic ("the
#     attached screenshot") and in the ubiquitous attachment-DOMAIN idioms
#     ("images attached to a comment", "the attached-files endpoint", "diagrams
#     attached to each node"). Attachments are a bread-and-butter feature
#     subject, so no free-text "attached <noun>" / "<noun> attached" rule can
#     separate the two without a fragile noun/preposition denylist. A genuine
#     attachment is already covered: if a file is actually attached,
#     ``detect_unavailable_input_refs`` trusts it (no escalation); a pasted image
#     shows up as an [Image #N] bracket, caught by rule 1.
# A pasted chat marker carries a distinguishing SHAPE that a bracketed *code*
# token never does: a "#<n>" index ("[Image #1]") or a ":<payload>" filename
# ("[attachment: repro.png]"). Requiring that shape is what separates a genuine
# marker from a T-SQL bracket-quoted column ("SELECT [attachment] FROM …",
# "WHERE [screenshot] IS NULL"), a Python subscript ("list[Image]"), or a
# markdown/issue tag ("[screenshot] flaky", "[Screenshot] hero is blurry"). A
# bare "[screenshot]"/"[attachment]" is lexically identical to a SQL column, so
# it is deliberately NOT matched (precision-first); a real pasted image is
# "[Image #N]" and a real attachment is "[attachment: <file>]", both of which
# carry the shape. The (?<!\w) lookbehind additionally rejects `x[image #…]`.
# The shape must CLOSE the bracket cleanly — a "#<n>" index at the end
# ("[Image #1]") or a ":<payload>" filename ("[attachment: repro.png]"). Baking
# the closing "]" in (rather than allowing trailing text) rejects a regex range
# "[image #0-9]" and a "[Image #1 extra]"-style non-marker.
_MARKER_SHAPE = r"(?:#\s*\d+\s*|:[^\]]+)\]"
_FOLLOWS_AS_OBJECT = (
    # The visual noun must be the OBJECT of "in" — followed by punctuation, end,
    # or a CLOSED-CLASS function word — not a MODIFIER of a following content
    # noun. This is a principled allowlist (function words are a closed class),
    # unlike a denylist of the infinite content nouns that could follow. So "…
    # the figure," / "… the mockup we discussed" / "… the screenshot below" fire,
    # but "… the images directory" / "… the diagram spec" / "… the figure
    # gallery" (a content noun follows) do not.
    r"(?=[\s]*[.,;:!?)\]]|\s*$|\s+(?:the|a|an|and|or|but|nor|we|you|i|they|it|he|"
    r"she|one|to|for|with|at|by|on|of|from|as|so|than|then|below|above|here|"
    r"there|which|that|who|whose|when|where|while|because|if|this|these|those|"
    r"its|their|our|your|my|his|her)\b)"
)
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?<!\w)\[\s*(?:image|attachment|screenshot|screen\s?shot)\s*"
        rf"{_MARKER_SHAPE}",
        re.I,
    ),
    # "as shown/seen/depicted/… in the <visual noun>" — visual noun MANDATORY and
    # word-bounded (so "image_utils"/"figure_out" do NOT match), the object of
    # "in" (so "as shown in the code below" does not fire), and the head not a
    # modifier (see _FOLLOWS_AS_OBJECT).
    re.compile(
        rf"\bas\s+(?:shown|seen|depicted|pictured|illustrated)\s+in\s+"
        rf"(?:the\s+|this\s+|that\s+|these\s+|those\s+)?(?:{_VISUAL_NOUN})\b"
        + _FOLLOWS_AS_OBJECT,
        re.I,
    ),
)


def find_input_refs(text: str) -> list[str]:
    """Return the referential input markers found in ``text`` (deduped, order
    preserved). Empty when the text only mentions such nouns topically."""
    if not text:
        return []
    seen: dict[str, None] = {}  # insertion-ordered dedup
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            key = " ".join(m.group(0).split()).lower()  # normalise whitespace
            seen.setdefault(key, None)
    return list(seen)


def detect_unavailable_input_refs(
    text: str, attachments: Any | None
) -> list[str]:
    """Dangling references to inputs the agent cannot access.

    Returns the referential markers from ``text`` **only** when NO attachment is
    present (an attachment is trusted to satisfy them). Empty otherwise — no
    referential mention, or the operator attached files.
    """
    refs = find_input_refs(text)
    if not refs:
        return []
    if attachments:  # any attachment present → trust it covers the reference
        return []
    return refs


def missing_input_blocker(refs: list[str], *, goal: str = "") -> Blocker:
    """A human-gated AMBIGUITY blocker naming the unavailable input(s).

    Routes to AWAITING_INPUT (reply-to-resume): the human either attaches the
    file or describes its contents in a reply, and the task continues — never
    proceeding under a fabricated guess of what the input showed.
    """
    quoted = ", ".join(f'"{r}"' for r in refs[:5])
    return Blocker(
        category=BlockerCategory.AMBIGUITY,
        transient=False,
        confidence=0.9,
        goal=goal,
        root_cause_hypothesis=(
            "The task references an input I cannot access — no such file is "
            f"attached: {quoted}. Answering without seeing it would mean "
            "guessing its contents."
        ),
        evidence=f"unavailable input reference(s) with no attachment: {quoted}",
        question=(
            "This task refers to an image/attachment I don't have access to "
            f"({quoted}). Please attach the file, or reply describing its "
            "contents, and I'll continue."
        ),
    )
