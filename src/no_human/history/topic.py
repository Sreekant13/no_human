"""Is this transcript about SOFTWARE ENGINEERING at all?

no_human mines a developer's chat history for durable engineering lessons. It
reads *every* conversation it can find, and until this module existed nothing in
the pipeline asked what a conversation was ABOUT. The scoping instruction in the
LLM prompt was one sentence — "only extract lessons that would help on a FUTURE,
different task" — under which a conversation about buying a t-shirt is a
perfectly good "task", and its shipping address a perfectly good "lesson". A real
user's home address and phone number were mined out of exactly that conversation
and offered back as standing engineering guidance.

The fix is a gate on the WHOLE transcript, not on individual memories: an
off-topic conversation yields **zero** memories, because filtering its memories
one by one only ever removes the ones a filter happens to recognise.

Two judges, and a finding must survive both:

* **The model** (preferred). :func:`no_human.history.analyzer.build_llm_prompt`
  makes the model state a ``topic`` and *justify* it in ``topic_reason`` before
  it may list any finding; :func:`~no_human.history.analyzer.parse_llm_findings`
  discards everything unless the judgement is ``software_engineering`` with a
  non-empty justification. An unjustified or off-topic judgement yields nothing.
* **This module** (the floor). The heuristic below runs unconditionally, on both
  the heuristic and the LLM pass, because the LLM pass is optional, can be
  unavailable, and can be wrong. It answers a deliberately narrow question — is
  there *any* software-engineering evidence in this conversation? — and is
  default-deny: no evidence means no mining.

The evidence model, and why it is tiered. Single generic words ("test", "fix",
"run") are the failure mode of every keyword filter: "blood test", "fix my
council tax", "run to the shops". So they are only WEAK evidence and FOUR
DISTINCT weak terms are required. Software-specific jargon and multi-word
phrases ("running the tests", "pull request", "stack trace") do not occur in
shopping/travel/medical/admin conversations, so any ONE of them is STRONG and
decides on its own.

The threshold is 4 rather than 3 because 3 was measured too loose: "I want to
review the hotel and check the log book of the trip" cleared it on
check+log+review. Raising it cost nothing on the engineering side, because the
engineering cases that clear the bar clear it on a STRONG signal.

What is deliberately NOT evidence: the session's ``cwd``, ``git_branch`` or
workspace URI. A developer chatting about a t-shirt does it from whatever
directory their editor happened to be in — the real incident's conversation had
a repo cwd. Location is not topic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .extractor import Transcript

# Bound the work on huge transcripts: enough text to find evidence, not the
# whole history.
_MAX_MESSAGES = 200
_MAX_CHARS_PER_MESSAGE = 2000

# --------------------------------------------------------------------------- #
# STRONG evidence — any ONE decides the transcript is software engineering.     #
# Jargon and multi-word phrases that do not appear in non-technical chat.       #
# --------------------------------------------------------------------------- #
_STRONG_PATTERNS: tuple[tuple[str, str], ...] = (
    ("code-fence", r"```"),
    ("shell-prompt", r"(?m)^\s*[$#>]\s+\w+\s+-{1,2}\w"),
    ("diff-hunk", r"(?m)^@@ -\d+"),
    ("traceback",
     r"traceback \(most recent call last\)|stack ?trace|panic:|segmentation fault"
     r"|exception in thread|unhandled (?:exception|rejection)"),
    ("source-file",
     r"[\w./~-]+\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|rb|php|cs|swift"
     r"|c|h|cc|cpp|hpp|scala|sh|bash|zsh|sql|ya?ml|toml|ini|cfg|gradle|tf|proto"
     r"|lock|dockerfile|makefile)\b"),
    # Keyword+identifier shapes only. Deliberately EXCLUDES English-ambiguous
    # keywords (from/return/let/class/public/private/static/import/await): they
    # match "from the shop", "import duties", "await your reply" and would make
    # every conversation look like software.
    ("code-syntax",
     r"\b(?:def|func|fn|impl|struct|interface|const|var|async|elif|lambda)\s+\w"
     r"|\w+\s*\([^)]*\)\s*(?:\{|:|=>|->)"
     r"|\w+\.\w+\(\)"),
    ("vcs",
     r"\b(?:git|commit|commits|repo|repos|repository|rebase|rebased"
     r"|cherry-pick|worktree|stash|gitignore|changelog|monorepo|upstream branch"
     r"|feature branch|merge|merged|merging|merge conflict|pull request|prs?"
     r"|merge request|code review|revert|reverted|force[- ]push|diff)\b"
     r"|\bpush\w*(?:\s+\w+){0,2}\s+to\s+(?:main|master|prod)"),
    ("testing-phrase",
     r"\b(?:run(?:ning)? the tests?|the tests? pass|tests? are (?:red|green|failing"
     r"|passing)|test suite|unit tests?|integration tests?|regression tests?"
     r"|test coverage|test fixture|failing test|pytest|jest|junit|rspec|vitest"
     r"|before pushing|after pushing)\b"),
    ("tooling",
     r"\b(?:npm|npx|yarn|pnpm|pip|uv run|poetry|cargo|gradle|maven|webpack|vite"
     r"|docker|dockerfile|kubernetes|k8s|kubectl|helm|terraform|ansible|jenkins"
     r"|gitlab ci|github actions|ci pipeline|ci/cd|linter|lint|ruff|eslint|mypy"
     r"|prettier|tsc|makefile|compiler|compile[ds]?|build fail\w*)\b"),
    # Jargon only. Deliberately EXCLUDES words that are ordinary English in a
    # shopping/travel/medical conversation: package (a delivery), library, server
    # (a waiter), method (payment method), class, function (kidney function),
    # argument, query, client.
    ("software-domain",
     r"\b(?:codebase|source code|refactor\w*|regexp?|stacktrace|api|apis|endpoint"
     r"|endpoints|sdk|cli|database|schema|migration|sql|json|yaml|http"
     r"|https request|backend|frontend|web server|middleware|runtime"
     r"|dependency|dependencies|module|framework|variable|parameter"
     r"|null pointer|race condition|deadlock"
     r"|memory leak|deploy\w*|rollback|hotfix|feature flag|environment variable"
     r"|env var|config file|log line|log output|stderr|stdout|debugger|breakpoint"
     r"|typescript|javascript|python|golang|rust|kotlin|postgres|sqlite|redis"
     r"|kafka|grpc)\b"),
)

# --------------------------------------------------------------------------- #
# WEAK evidence — ambiguous on its own; FOUR DISTINCT terms are required.       #
# --------------------------------------------------------------------------- #
_WEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("test", r"\btests?(?:ing|ed)?\b"),
    ("fix", r"\bfix(?:e[sd]|ing)?\b"),
    ("run", r"\brun(?:s|ning)?\b"),
    ("build", r"\bbuild(?:s|ing)?\b"),
    ("error", r"\berrors?\b"),
    ("bug", r"\bbugs?\b"),
    ("check", r"\bcheck(?:s|ing|ed)?\b"),
    ("verify", r"\bverif(?:y|ies|ied)\b"),
    ("push", r"\bpush(?:es|ing|ed)?\b"),
    ("file", r"\bfiles?\b"),
    ("script", r"\bscripts?\b"),
    ("command", r"\bcommands?\b"),
    ("version", r"\bversions?\b"),
    ("install", r"\binstall(?:s|ing|ed)?\b"),
    ("output", r"\boutputs?\b"),
    # Ambiguous by design: "discount code", "log book", "bank branch",
    # "performance review" are all ordinary English.
    ("code", r"\bcode\b"),
    ("log", r"\blogs?\b"),
    ("branch", r"\bbranch(?:es)?\b"),
    ("review", r"\breviews?\b"),
)

_MIN_WEAK_SIGNALS = 4

_STRONG = [(name, re.compile(p, re.IGNORECASE)) for name, p in _STRONG_PATTERNS]
_WEAK = [(name, re.compile(p, re.IGNORECASE)) for name, p in _WEAK_PATTERNS]


@dataclass(frozen=True)
class TopicVerdict:
    """Whether a transcript is software engineering, and the evidence for it.

    ``reason`` never quotes the transcript — it names the signals that fired, so
    a decision about a private conversation can be logged without logging any of
    its content.
    """
    is_software: bool
    reason: str

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.is_software


def classify_text(text: str) -> TopicVerdict:
    """Software-engineering verdict for a blob of conversation text."""
    if not text or not text.strip():
        return TopicVerdict(False, "empty transcript")
    for name, rx in _STRONG:
        if rx.search(text):
            return TopicVerdict(True, f"strong signal: {name}")
    weak_hits = [name for name, rx in _WEAK if rx.search(text)]
    if len(weak_hits) >= _MIN_WEAK_SIGNALS:
        return TopicVerdict(True, "weak signals: " + ",".join(sorted(weak_hits)))
    return TopicVerdict(
        False,
        f"no software-engineering evidence ({len(weak_hits)} weak signal(s), "
        f"{_MIN_WEAK_SIGNALS} required)",
    )


def transcript_text(transcript: Transcript) -> str:
    """The text the topic judgement is made on: message bodies plus the title.

    Both roles count — an assistant reply is evidence of what the conversation is
    about. Bounded so a huge transcript cannot dominate ingest time.
    """
    parts: list[str] = []
    if getattr(transcript, "title", ""):
        parts.append(str(transcript.title))
    for msg in (transcript.messages or [])[:_MAX_MESSAGES]:
        content = msg.content or ""
        parts.append(content[:_MAX_CHARS_PER_MESSAGE])
    return "\n".join(parts)


def classify_transcript(transcript: Transcript) -> TopicVerdict:
    """Software-engineering verdict for a whole transcript."""
    return classify_text(transcript_text(transcript))


def is_software_transcript(transcript: Transcript) -> bool:
    """Convenience predicate over :func:`classify_transcript`."""
    return classify_transcript(transcript).is_software
