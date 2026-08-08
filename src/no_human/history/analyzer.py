"""Analyze extracted conversation transcripts to bootstrap the learning queue.

Scans user messages for explicit corrections, rules, and anti-patterns that
the user has already communicated to the AI. These become proposed learnings
that a human confirms via ``nh learnings``.

This is deliberately heuristic — pattern-matching on known signal phrases
that users type when correcting AI behavior. It does NOT use an LLM (that
would be expensive and unreliable for mechanical patterns).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..learning.pii import contains_pii
from .extractor import Message, Transcript
from .machinery import is_machinery
from .topic import classify_transcript

log = logging.getLogger("no_human.history")

# Signal phrases that indicate user corrections / explicit rules.
# Each tuple: (pattern, category, description).
_CORRECTION_PATTERNS: list[tuple[str, str, str]] = [
    (r"\bnever\b.*\b(add|commit|push|include)\b", "rule",
     "User stated a 'never do X' rule"),
    (r"\balways\b.*\b(check|verify|test|run|score)\b", "rule",
     "User stated an 'always do X' rule"),
    (r"\bdo\s*n[o']?t\b.*\b(guess|assume|speculate)\b", "rule",
     "User forbade guessing/speculation"),
    (r"\byou\s+(seem|are)\s+(stuck|loop|going\s+in\s+circles?)", "anti_pattern",
     "User flagged the AI as stuck/looping"),
    (r"\bstop\b.*\b(try|guess|speculate)", "anti_pattern",
     "User told AI to stop guessing"),
    (r"\bwrong\s+(branch|file|commit|approach)", "anti_pattern",
     "User flagged wrong target"),
    (r"\bimport.*\b(top|beginning)\b.*file", "rule",
     "User enforced import placement"),
    (r"\bscore\b.*\b10(/|\s*out)", "rule",
     "User enforced scoring discipline"),
    (r"\bno\s+(summary|changes?|modification)", "rule",
     "User required verbatim/exact output"),
    (r"\bremember\b.*\b(this|that|rule|always|never)", "rule",
     "User asked AI to remember something"),
]

# Compiled for efficiency.
_COMPILED = [(re.compile(p, re.IGNORECASE), cat, desc)
             for p, cat, desc in _CORRECTION_PATTERNS]

# Transcript machinery that arrives as *user*-role content but is NOT the user
# typing a rule: slash-command expansions, local/bash command output,
# harness-injected reminders, and compaction/summary preambles. A signal phrase
# matched inside one of these is a false positive — it is what flooded the
# confirm queue with junk proposals titled e.g. "(<local-command-stdout>Set
# model to Sonnet 4…)", and later turned a compaction preamble into a proposed
# standing RULE. A genuine free-text rule never carries these markers, so their
# presence disqualifies the whole message from correction-mining.
#
# The recognition itself lives in `history/machinery.py`, which derives the tag
# families by SHAPE and imports the extractor's own marker list, rather than
# repeating a hand-written tuple that can only ever catch the cases someone
# already thought of. Do not re-inline a list here.


def _is_noise_message(content: str) -> bool:
    """True if the message is transcript machinery, not a user-typed rule."""
    return is_machinery(content)


# ANSI escape sequences (e.g. from a slash-command's stdout) leak into titles.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean_title(title: str) -> str:
    """A transcript's own title field is sometimes command stdout (ANSI codes,
    ``<local-command-stdout>`` blocks). Strip machinery so the proposal title
    the human reads is legible; fall back to a neutral label when nothing
    usable remains."""
    if not title:
        return "untitled conversation"
    t = _ANSI.sub("", title)
    if is_machinery(t):
        return "untitled conversation"
    return t.strip() or "untitled conversation"


def _project_from_workspaces(workspaces: list[str]) -> str:
    """The repo path a conversation happened in, from its workspace URIs. The
    ingester stamps it as the memory's ``project`` so mined rules can be scoped
    to the repo they came from — a conversation in the Metricsdb repo shouldn't
    surface its rules to a no_human task. Strips the ``file://`` scheme so the
    value matches ``task.repo_path`` (a filesystem path). Empty when unknown."""
    for w in workspaces or []:
        if not w:
            continue
        path = w
        if path.startswith("file://"):
            path = path[len("file://"):]
        path = path.rstrip("/")
        if path:
            return path
    return ""


def _project_of(transcript: Transcript) -> str:
    """Project scope for mined rules: workspace URIs (Windsurf) with a  # term-ok: real IDE name
    fallback to the session ``cwd`` (Claude Code transcripts carry no
    workspaces — without the fallback every CC-mined rule was global and a
    personal-repo correction could surface in an enterprise task)."""
    return (_project_from_workspaces(transcript.workspaces)
            or getattr(transcript, "cwd", "") or "")


@dataclass
class Finding:
    """A single learning extracted from a conversation."""
    category: str        # "rule" | "anti_pattern" | "skill"
    title: str
    content: str         # The exact user message
    source_transcript: str  # cascade_id
    source_title: str       # conversation title
    tags: list[str] = field(default_factory=list)
    source_message: int = -1  # 0-based index of the message in the transcript
    importance: str = "med"   # coarse label low|med|high — NEVER a numeric score
    project: str = ""         # repo path the conversation happened in (for scoping)


def analyze_transcript(transcript: Transcript) -> list[Finding]:
    """Scan a single transcript for user corrections and rules.

    Off-topic transcripts yield NOTHING. The judgement is made on the whole
    conversation before any message is scanned, because a conversation about
    shopping, travel, medical matters or personal admin has no engineering
    lessons in it at all — filtering its individual "lessons" only removes the
    ones a filter happens to recognise, which is how a user's home address and
    phone number were mined out of a t-shirt purchase and offered back as
    standing guidance. See `history/topic.py`.
    """
    verdict = classify_transcript(transcript)
    if not verdict.is_software:
        log.debug("skipping off-topic transcript %s (%s)",
                  transcript.cascade_id, verdict.reason)
        return []

    findings: list[Finding] = []
    seen_sigs: set[str] = set()
    project = _project_of(transcript)

    for idx, msg in enumerate(transcript.messages):
        if msg.role != "user":
            continue
        if _is_noise_message(msg.content):
            continue
        # Second layer: a message carrying personal data is never a coding rule.
        # Dropped, not redacted — a redacted shopping fact is still not a rule,
        # and "shipping address: [REDACTED]" is itself a disclosure.
        pii = contains_pii(msg.content)
        if pii is not None:
            log.debug("dropping message %d of %s: personal data (%s)",
                      idx, transcript.cascade_id, pii.kind)
            continue

        for pattern, category, desc in _COMPILED:
            if pattern.search(msg.content):
                sig = f"{category}:{pattern.pattern}:{transcript.cascade_id}"
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)

                title = f"{desc} ({_clean_title(transcript.title)})"
                findings.append(Finding(
                    category=category,
                    title=title[:120],
                    content=msg.content,
                    source_transcript=transcript.cascade_id,
                    source_title=transcript.title,
                    tags=["history", category, "user_correction"],
                    source_message=idx,
                    project=project,
                ))

    return findings


def mine_reply(text: str) -> tuple[str, str] | None:
    """A reusable preference stated in an operator's reply → (category,
    description), or None (2.3, CodeRabbit 'learnings'). Same signal patterns and
    noise filter as transcript mining, so an operator answering a review/blocker
    ("we always run X first") can seed the human-confirmed learning queue — which
    future reviews then apply."""
    if not text or _is_noise_message(text):
        return None
    if contains_pii(text) is not None:
        return None  # personal data is never a reusable engineering preference
    for pattern, category, desc in _COMPILED:
        if pattern.search(text):
            return category, desc
    return None


def analyze_all(transcripts: list[Transcript]) -> list[Finding]:
    """Analyze all transcripts, returning deduplicated findings."""
    all_findings: list[Finding] = []
    for t in transcripts:
        all_findings.extend(analyze_transcript(t))
    log.info("found %d correction patterns across %d transcripts",
             len(all_findings), len(transcripts))
    return all_findings


# --------------------------------------------------------------------------- #
# Optional LLM pass (EVOLUTION_PLAN §1.1) — OFF by default; costs tokens.       #
# --------------------------------------------------------------------------- #

_VALID_CATEGORIES = {"rule", "anti_pattern", "skill", "fact"}
_VALID_IMPORTANCE = {"low", "med", "high"}

_LLM_JSON = re.compile(r"FINDINGS_JSON_START\s*(.*?)\s*FINDINGS_JSON_END", re.DOTALL)

# Cap how much of a transcript we hand the LLM, to bound cost.
_LLM_USER_MSG_CAP = 40
_LLM_MSG_CHARS = 1200


def build_llm_prompt(transcript: Transcript) -> str:
    """Prompt for the optional LLM extraction pass.

    Two things this prompt must do, both of them fixes for shipped defects:

    * ask for a coarse importance LABEL (low|med|high) — explicitly NOT a
      numeric score (constraint #3); and
    * make the model state, and JUSTIFY, whether the conversation is about
      software engineering BEFORE it is allowed to list a single finding. The
      previous scoping instruction was one sentence about "a FUTURE, different
      task", under which buying a t-shirt is a task and a shipping address is a
      durable fact. ``parse_llm_findings`` enforces the judgement — an
      off-topic or unjustified verdict yields zero findings regardless of what
      the model went on to list.
    """
    # Drop PII-bearing messages BEFORE they enter the prompt — the LLM pass runs
    # against the vendor API, so a home address / phone / payment / ID in a user
    # message would otherwise be transmitted off-machine even though the finding
    # it produces is later dropped at persistence. Filtering here (drop, not
    # redact — same policy as learning/pii.py) means the sensitive message never
    # leaves the machine, so the onboarding copy's privacy claim holds for
    # transmission, not just storage. A transcript that is entirely PII yields an
    # empty convo → zero findings, which is the correct privacy outcome.
    user_msgs = [
        (i, m.content) for i, m in enumerate(transcript.messages)
        if m.role == "user" and contains_pii(m.content) is None
    ][:_LLM_USER_MSG_CAP]
    convo = "\n".join(
        f"[msg {i}] {c.strip()[:_LLM_MSG_CHARS]}" for i, c in user_msgs
    )
    return (
        "You are mining a developer's past chat transcript for DURABLE lessons "
        "they taught an AI assistant — rules, anti-patterns, skills, and facts "
        "worth remembering across sessions.\n\n"
        "STEP 1 — JUDGE THE TOPIC FIRST. Decide what this whole conversation is "
        "about, and say so:\n"
        "  - topic: exactly one of software_engineering | other\n"
        "  - topic_reason: one sentence justifying that call, citing what the "
        "conversation is actually about\n"
        "This tool mines SOFTWARE ENGINEERING conversations only. If the "
        "conversation is about shopping, travel, health/medical matters, "
        "personal admin, finance, relationships, or anything else that is not "
        "building or operating software, then topic is \"other\" and you MUST "
        "return an EMPTY findings list — even if the person stated preferences, "
        "corrected you, or told you facts about themselves. Those are not "
        "engineering lessons and must never become standing guidance.\n\n"
        "STEP 2 — only if topic is software_engineering, extract lessons. "
        "For each lesson:\n"
        "  - category: one of rule | anti_pattern | skill | fact\n"
        "  - rule: the durable instruction, in your own concise words\n"
        "  - anti_pattern: the specific behaviour being corrected (or \"\")\n"
        "  - source_message: the [msg N] index this came from\n"
        "  - importance: a COARSE LABEL of low | med | high — NOT a number, NOT a score\n\n"
        "Only extract lessons that would help on a FUTURE, different SOFTWARE "
        "task. Skip one-off context. Do not invent lessons that aren't in the "
        "text.\n"
        "NEVER include personal data in a lesson — no home or shipping "
        "addresses, phone numbers, personal email addresses, payment or bank "
        "details, government ID numbers, or dates of birth. A lesson that needs "
        "any of those to make sense is not a lesson; omit it.\n\n"
        f"Conversation title: {transcript.title}\n"
        f"User messages:\n{convo or '(none)'}\n\n"
        "Output EXACTLY this and nothing after it:\n"
        "FINDINGS_JSON_START\n"
        '{"topic": "software_engineering", "topic_reason": "...",\n'
        ' "findings": [\n'
        '  {"category": "rule", "rule": "...", "anti_pattern": "",\n'
        '   "source_message": 3, "importance": "high"}\n'
        "]}\n"
        "FINDINGS_JSON_END\n"
    )


_SOFTWARE_TOPIC = "software_engineering"


def _llm_topic_allows(data: dict, transcript: Transcript) -> bool:
    """Whether the model's own topic judgement permits mining this transcript.

    Two judges, and BOTH must agree — the model's stated judgement (preferred,
    because it reads the conversation) and the heuristic floor in
    ``history/topic.py`` (because the LLM pass is optional, can be unavailable,
    and can be wrong).

    * A stated ``topic`` other than ``software_engineering`` blocks everything.
    * A stated ``software_engineering`` with an empty ``topic_reason`` also
      blocks: the prompt requires the call to be justified, and an unjustified
      verdict is not a judgement.
    * A MISSING ``topic`` key is not treated as consent. It falls through to the
      heuristic floor, which has already been applied to this transcript.
    """
    if not classify_transcript(transcript).is_software:
        return False
    if "topic" not in data:
        return True  # older/degraded output — the heuristic floor above decided
    topic = str(data.get("topic", "")).strip().lower().replace(" ", "_")
    if topic != _SOFTWARE_TOPIC:
        log.info("LLM analyzer: transcript %s judged off-topic (%s); "
                 "discarding all findings", transcript.cascade_id, topic or "unstated")
        return False
    if not str(data.get("topic_reason", "")).strip():
        log.info("LLM analyzer: transcript %s claimed software_engineering "
                 "without justification; discarding all findings",
                 transcript.cascade_id)
        return False
    return True


def parse_llm_findings(text: str, transcript: Transcript) -> list[Finding]:
    """Parse the LLM's structured output into Findings. Fail-soft: unparseable
    output yields no findings (the heuristic pass already ran).

    Gated twice before anything is returned: the topic judgement
    (:func:`_llm_topic_allows`, whole-transcript) and the personal-data gate
    (per finding). Both fail closed."""
    import json

    m = _LLM_JSON.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        log.warning("LLM analyzer: unparseable JSON block; skipping")
        return []
    if not isinstance(data, dict) or not _llm_topic_allows(data, transcript):
        return []
    out: list[Finding] = []
    project = _project_of(transcript)
    clean_title = _clean_title(transcript.title)
    for raw in (data.get("findings") or []):
        category = str(raw.get("category", "rule")).strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "rule"
        rule = str(raw.get("rule", "")).strip()
        if not rule:
            continue
        anti = str(raw.get("anti_pattern", "")).strip()
        importance = str(raw.get("importance", "med")).strip().lower()
        if importance not in _VALID_IMPORTANCE:
            importance = "med"
        try:
            src_msg = int(raw.get("source_message", -1))
        except (TypeError, ValueError):
            src_msg = -1
        content = rule + (f"\nAnti-pattern: {anti}" if anti else "")
        pii = contains_pii(content)
        if pii is not None:
            log.info("LLM analyzer: dropping a finding from %s carrying "
                     "personal data (%s)", transcript.cascade_id, pii.kind)
            continue
        out.append(Finding(
            category=category,
            title=f"{rule[:110]} ({clean_title})"[:120],
            content=content,
            source_transcript=transcript.cascade_id,
            source_title=transcript.title,
            tags=["history", category, "llm", f"importance:{importance}"],
            source_message=src_msg,
            importance=importance,
            project=project,
        ))
    return out


async def analyze_transcript_llm(transcript: Transcript, llm_call) -> list[Finding]:
    """Optional LLM second pass for one transcript. ``llm_call`` is an async
    ``(prompt) -> str``. Additive to the heuristic pass; never replaces it.

    Off-topic transcripts are not sent to the model AT ALL. That is not only a
    token saving: shipping a user's shopping/medical/personal conversation to an
    inference backend so it can be told to ignore it is itself a disclosure the
    user did not ask for."""
    verdict = classify_transcript(transcript)
    if not verdict.is_software:
        log.debug("not sending off-topic transcript %s to the LLM (%s)",
                  transcript.cascade_id, verdict.reason)
        return []
    prompt = build_llm_prompt(transcript)
    try:
        raw = await llm_call(prompt)
    except Exception as exc:  # noqa: BLE001 — LLM failure must not break ingest
        log.warning("LLM analyzer call failed: %s", exc)
        return []
    return parse_llm_findings(raw, transcript)
