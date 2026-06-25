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

from .extractor import Message, Transcript

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


def analyze_transcript(transcript: Transcript) -> list[Finding]:
    """Scan a single transcript for user corrections and rules."""
    findings: list[Finding] = []
    seen_sigs: set[str] = set()

    for idx, msg in enumerate(transcript.messages):
        if msg.role != "user":
            continue

        for pattern, category, desc in _COMPILED:
            if pattern.search(msg.content):
                sig = f"{category}:{pattern.pattern}:{transcript.cascade_id}"
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)

                title = f"{desc} ({transcript.title})"
                findings.append(Finding(
                    category=category,
                    title=title[:120],
                    content=msg.content,
                    source_transcript=transcript.cascade_id,
                    source_title=transcript.title,
                    tags=["history", category, "user_correction"],
                    source_message=idx,
                ))

    return findings


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
    """Prompt for the optional LLM extraction pass. Asks for a coarse importance
    LABEL (low|med|high) — explicitly NOT a numeric score (constraint #3)."""
    user_msgs = [
        (i, m.content) for i, m in enumerate(transcript.messages) if m.role == "user"
    ][:_LLM_USER_MSG_CAP]
    convo = "\n".join(
        f"[msg {i}] {c.strip()[:_LLM_MSG_CHARS]}" for i, c in user_msgs
    )
    return (
        "You are mining a developer's past chat transcript for DURABLE lessons "
        "they taught an AI assistant — rules, anti-patterns, skills, and facts "
        "worth remembering across sessions.\n\n"
        "For each lesson, extract:\n"
        "  - category: one of rule | anti_pattern | skill | fact\n"
        "  - rule: the durable instruction, in your own concise words\n"
        "  - anti_pattern: the specific behaviour being corrected (or \"\")\n"
        "  - source_message: the [msg N] index this came from\n"
        "  - importance: a COARSE LABEL of low | med | high — NOT a number, NOT a score\n\n"
        "Only extract lessons that would help on a FUTURE, different task. Skip "
        "one-off context. Do not invent lessons that aren't in the text.\n\n"
        f"Conversation title: {transcript.title}\n"
        f"User messages:\n{convo or '(none)'}\n\n"
        "Output EXACTLY this and nothing after it:\n"
        "FINDINGS_JSON_START\n"
        '{"findings": [\n'
        '  {"category": "rule", "rule": "...", "anti_pattern": "",\n'
        '   "source_message": 3, "importance": "high"}\n'
        "]}\n"
        "FINDINGS_JSON_END\n"
    )


def parse_llm_findings(text: str, transcript: Transcript) -> list[Finding]:
    """Parse the LLM's structured output into Findings. Fail-soft: unparseable
    output yields no findings (the heuristic pass already ran)."""
    import json

    m = _LLM_JSON.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        log.warning("LLM analyzer: unparseable JSON block; skipping")
        return []
    out: list[Finding] = []
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
        out.append(Finding(
            category=category,
            title=f"{rule[:110]} ({transcript.title})"[:120],
            content=content,
            source_transcript=transcript.cascade_id,
            source_title=transcript.title,
            tags=["history", category, "llm", f"importance:{importance}"],
            source_message=src_msg,
            importance=importance,
        ))
    return out


async def analyze_transcript_llm(transcript: Transcript, llm_call) -> list[Finding]:
    """Optional LLM second pass for one transcript. ``llm_call`` is an async
    ``(prompt) -> str``. Additive to the heuristic pass; never replaces it."""
    prompt = build_llm_prompt(transcript)
    try:
        raw = await llm_call(prompt)
    except Exception as exc:  # noqa: BLE001 — LLM failure must not break ingest
        log.warning("LLM analyzer call failed: %s", exc)
        return []
    return parse_llm_findings(raw, transcript)
