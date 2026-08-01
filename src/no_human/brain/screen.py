"""The screens a pulled rule must pass before it can reach a coder prompt.

Ordered cheap-and-structural first. Every refusal is a NAMED reason, surfaced by
``nh brain list``, because "nothing was injected" and "everything was refused"
look identical from outside.

The load-bearing screens are structural — ``T1`` (type allowlist), ``T3``
(visibility fails closed), and the fact that these rows are not in ``memories``
at all (``store`` module docstring). ``T2``, the content screen, is a deny-list
over free text and is DEFENCE IN DEPTH, not the control: this repo has already
learned that regex-over-prose heuristics need many adversarial rounds and are
better replaced by marker shapes and closed-class allowlists. It is written that
way here — closed lists of markers, not entropy or English.

``T8`` inbound is the product's OWN banned-term screen, the same matcher
``Orchestrator._screen_memories_for_terms`` applies to the LOCAL learning store.
A remote source is strictly more dangerous than the local one, which already
leaked once, so it gets at least the same screen and it gets it here rather than
being trusted to meet it downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Far below the server's 20,000-character ceiling. A remote source must not be
#: able to displace the task from its own prompt. PROPOSED sizing, not measured:
#: it is a starting bound, and the reason it is safe to start small is that the
#: closed state (zero remote rules) is the current product.
MAX_CONTENT_CHARS = 600
MAX_TITLE_CHARS = 200
MAX_RULES_PER_PROMPT = 40
MAX_TOTAL_CHARS = 12_000

#: T2, and the one that was missing. ``render._entry`` interpolates ``rule_id``
#: verbatim into the coder prompt, and this screen built its text out of
#: ``title`` and ``content`` alone — so ``rule_id`` reached the prompt through
#: no screen at all. Prompt-injection text, credential markers, code fences and
#: banned terms all passed with ``ok=True``, and an 11,000-character id landed
#: 11,717 bytes in a prompt whose content cap is 600.
#:
#: The bound is a CLOSED CLASS rather than another deny-list. The control plane
#: issues ULIDs; anything that is not an identifier-shaped token is refused
#: without asking what it contains, which is the shape this repo has learned to
#: prefer over regex-over-free-text.
MAX_RULE_ID_CHARS = 64
_RULE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,%d}\Z"
                      % (MAX_RULE_ID_CHARS - 1))

#: T1. The closed class in the first increment is exactly this. In particular
#: ``skill`` is REFUSED: ``Orchestrator._write_skill_memories`` writes a skill
#: verbatim to ``.claude/skills/<name>/SKILL.md``, which a coding agent
#: auto-loads as instruction — the MemoryTrap shape. A remote item must never
#: reach the filesystem, and the server accepts an arbitrary ``type`` string, so
#: the allowlist has to live on this side.
ALLOWED_TYPES = frozenset({"rule"})

#: Only GLOBAL in the first increment. Project scoping needs a stable project
#: key (``sha256(normalised git remote)[:16]``) that this product does not have
#: — ``project_profiles.repo_path`` is an absolute local path carrying the OS
#: username. Non-GLOBAL rules sync and are stored INERT.
ALLOWED_SCOPES = frozenset({"GLOBAL"})

#: T2 markers. Closed list, matched literally, case-insensitively. Each entry is
#: a string this product's own prompts use structurally; a remote rule that
#: contains one is trying to look like the harness.
_PROMPT_MARKERS = (
    "[SUPERVISOR",
    "ACCEPTANCE CRITERIA:",
    "IMPLEMENTATION PLAN",
    "RULE ADHERENCE",
    "Confirmed rules/skills from past experience",
    "You are implementing a software task",
    "You are reviewing",
    "no_human orchestration harness",
)

_CREDENTIAL_MARKERS = (
    "sk-ant-",
    "AKIA",
    "-----BEGIN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ghp_",
    "github_pat_",
)

_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_ABS_PATH = re.compile(r"(^|\s)(/[A-Za-z0-9._-]+/|[A-Za-z]:\\)")
_FENCE = re.compile(r"```|~~~")
#: Anything outside printable text plus tab/newline. A rule is prose.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None


def effective_visibility(_declared: str | None) -> str:
    """T3, and it is the crux of the design: ALWAYS ``coder``.

    A remote rule reaches the coder prompt and nothing else. ``coder+reviewer``
    is a promotion the control plane can grant, and this client ignores it —
    there is not even a local switch to turn it on, because the enforcement that
    would make it safe does not exist: nothing in ``review/reviewer.py`` stops a
    RULE from flipping a verdict FAIL→PASS or suppressing a class of finding.
    Review ANGLES already have exactly that monotonic machinery
    (``merge_angle_findings``: "Angles ADD findings; they can flip pass→fail,
    never fail→pass"); rules do not, and until they are merged through the same
    path a promoted remote rule would remove the one gate the product rests on.

    The argument for shipping without it: a bad coder-visible rule produces a
    bad diff that the independent gate still reviews. A bad gate-visible rule
    removes the reviewer.
    """
    return "coder"


def screen(item: dict) -> Verdict:
    """Structural + content screens for ONE delta item. No I/O, no LLM.

    THE RULE FOR ADDING A FIELD: if ``render._entry`` interpolates it, it is
    bounded before it reaches a prompt — the free-text fields (``rule_id``,
    ``title``, ``content``) by the screens below, and the provenance NUMBERS
    (``version``, ``manifest_version``) by ``render._int_or_unknown``, which can
    only ever emit digits or ``?``. That sentence is the whole of the fix for
    the defect this function shipped with — ``rule_id`` was rendered and not
    screened — and the text every content screen below runs over is assembled
    from exactly the free-text fields that reach a prompt, so adding one to the
    renderer without adding it here is a visible omission rather than an
    invisible one.

    ``version`` is checked here TOO, and named, rather than left to the
    renderer's coercion. It was previously safe only because ``upsert_rule``
    happens to wrap it in ``int()`` at the sync boundary — a different mechanism
    in a different file from the one this docstring names — so a malformed
    version reached the store as a silent 0 instead of a refusal somebody can
    read in ``nh brain list``.
    """
    kind = str(item.get("type") or "rule")
    if kind not in ALLOWED_TYPES:
        return Verdict(False, f"type-not-allowed:{kind}")

    scope = str(item.get("scope") or "GLOBAL")
    if scope not in ALLOWED_SCOPES:
        return Verdict(False, f"scope-not-global:{scope}")

    status = str(item.get("status") or "")
    if status != "active":
        # tombstoned / superseded. Fail closed: a withdrawn rule stops being
        # injected the moment its withdrawal reaches this machine.
        return Verdict(False, f"not-active:{status or 'unknown'}")

    declared = str(item.get("visibility") or "coder")
    if declared not in ("coder", "coder+reviewer"):
        # Unknown visibility fails CLOSED. A value this build does not
        # understand is not a value to guess about.
        return Verdict(False, f"unknown-visibility:{declared}")

    # Structural and cheap, and BEFORE the length checks: an id that is not an
    # identifier is refused for its shape, whatever it happens to contain.
    rule_id = str(item.get("rule_id") or "")
    if not _RULE_ID.match(rule_id):
        return Verdict(False, "rule-id-not-an-identifier")

    # A provenance number, and it reaches the prompt. `bool` is excluded on
    # purpose: `True` is an int in Python and "rule vTrue" is not a version.
    raw_version = item.get("version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, (int, str)):
        return Verdict(False, "version-not-an-integer")
    try:
        if int(str(raw_version)) < 0:
            return Verdict(False, "version-not-an-integer")
    except (TypeError, ValueError):
        return Verdict(False, "version-not-an-integer")

    title = str(item.get("title") or "")
    content = str(item.get("content") or "")
    if not title.strip() or not content.strip():
        return Verdict(False, "empty")
    if len(title) > MAX_TITLE_CHARS:
        return Verdict(False, "title-too-long")
    if len(content) > MAX_CONTENT_CHARS:
        return Verdict(False, "content-too-long")

    # Every field that reaches a prompt, in one string. `rule_id` is here for
    # the same reason it has a shape check above and for one more: the term
    # screen at the bottom is the product's own banned-term inventory, and it
    # correctly rejected a term in `title` or `content` while returning ok=True
    # for the same term in `rule_id`.
    text = f"{rule_id}\n{title}\n{content}"
    if _CONTROL_CHARS.search(text):
        return Verdict(False, "control-characters")
    lowered = text.lower()
    for marker in _PROMPT_MARKERS:
        if marker.lower() in lowered:
            return Verdict(False, "prompt-marker")
    for marker in _CREDENTIAL_MARKERS:
        if marker.lower() in lowered:
            return Verdict(False, "credential-shaped")
    if _URL.search(text):
        return Verdict(False, "contains-url")
    if _ABS_PATH.search(text):
        return Verdict(False, "contains-absolute-path")
    if _FENCE.search(text):
        return Verdict(False, "contains-code-fence")

    hits = banned_terms(text)
    if hits:
        # T8 inbound. Named without repeating the term: the reason is a signal,
        # not a place to re-publish the thing being screened for.
        return Verdict(False, f"banned-term:{len(hits)}")

    return Verdict(True)


def banned_terms(text: str) -> list:
    """The product's own vendor/employer term inventory, applied inbound.

    Imported lazily and fail-OPEN on import error, matching how
    ``Orchestrator._screen_memories_for_terms`` treats the same matcher: a
    screen that errors must not silently drop a rule. Fail-open here is not a
    trust hole — the rule still had to be signed, chained and structurally
    screened to get this far.
    """
    try:
        from ..eval.vendor_terms import find_banned_terms
    except Exception:  # noqa: BLE001
        return []
    try:
        return list(find_banned_terms(text) or [])
    except Exception:  # noqa: BLE001
        return []
