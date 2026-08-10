"""One explicit complexity tier per task (C1.5).

The pipeline had three DISJOINT complexity gates — the MoA fan-out (signal
count), extended thinking (files/plan-size/decompose), and the turn budget
(rides on thinking) — so a task could get 3 Opus proposers but no thinking,
and nothing recorded WHICH resourcing a task received. This module computes
one tier, stores it on ``task.context["complexity_tier"]``, and every knob
reads it. Boundaries deliberately mirror the pre-existing gates: the tier
makes resourcing VISIBLE and MEASURABLE first; retuning knobs per tier comes
later, with per-tier cost/quality data from /api/metrics.

Tiers only ever escalate (``upgrade_tier``): misclassifying complex→simple
under-resources and fails the task, so uncertainty errs toward MORE
resources, and later evidence (spec files, plan size) can raise but never
lower the tier.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..intake.surface_advisory import extract_path_token

if TYPE_CHECKING:  # pragma: no cover
    from .task import Task

TIERS = ("trivial", "simple", "standard", "complex")


def _rank(tier: str) -> int:
    return TIERS.index(tier) if tier in TIERS else 1


# --------------------------------------------------------- trivial predicate --
#
# The trivial tier used to be a LABEL: `compute_tier` returned it for any small
# spec and nothing downstream read it. It now buys a fast path (grill skipped,
# utility planner, no skill discovery, bounded review), so its definition has
# to be the fast path's own admission test — mechanical, path-based, and biased
# toward NOT firing. Motivating trace, 2026-08-09: task b3e8813f deleted one
# struck phrase from a single markdown file under `.agents/` and spent 35+
# minutes on intake scoping questions → a 9-turn Opus planning pass → 9 skills
# loaded → a multi-stage Opus review, while the tier computed for it was read
# by nothing.

#: Files the fast path accepts by SUFFIX. Not a promise that every such file
#: is inert — `.txt` includes machine-read manifests (`requirements.txt`,
#: `EXPORT_CLASSIFICATION.txt`), which is why `_GATE_CONTROL_NAMES` below
#: pulls the known machine-read names back OUT by basename.
_PROSE_SUFFIXES = (".md", ".rst", ".txt")

#: Roots whose contents are executed, imported, shipped, or read as
#: instructions by a runtime — a `.md` under any of them is NOT prose for this
#: purpose (`src/**` ships in the wheel, `.claude/skills/*/SKILL.md` is loaded
#: into an agent session, `tests/**` and `testdata/**` are asserted on,
#: `scripts/**` and the hook dirs run). This is the answer to "what about a
#: docs change that alters an executable doc example": such files live under
#: these roots, and anything outside them with a non-prose suffix is rejected
#: below anyway.
_EXECUTED_ROOTS = (
    "src/", "web/", "desktop/", "tests/", "test/", "testdata/", "scripts/",
    ".github/", ".githooks/", ".claude/", ".agents/skills/",
)

#: Suffixes that make a token a FILE rather than prose punctuation. Anything
#: else ("e.g.", "1.5x", "no_human.core") is not a path and is ignored — while
#: a known non-prose suffix is a hard veto. Recognising too few suffixes fails
#: safe (the token is ignored, and a plan/diff naming it escalates the tier);
#: recognising too many only adds vetoes.
_CODE_SUFFIXES = (
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".bash", ".zsh", ".sql",
    ".html", ".css", ".scss", ".go", ".rs", ".java", ".rb", ".tf", ".lock",
    ".svg", ".png", ".jpg", ".gif", ".ico", ".plist", ".xml", ".env",
)

#: At most this many files, because a fast path is a bet on reviewability.
MAX_TRIVIAL_FILES = 2

_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]{1,6}")


def _normalize(path: str) -> str:
    return path.strip().strip("`'\"").removeprefix("./").lstrip("/")


def path_tokens(text: str) -> list[str]:
    """File paths named in free text, deduped and order-preserving.

    Only tokens carrying a suffix this module RECOGNISES count; everything
    else is prose. Used pre-plan, where the ticket text is the only statement
    of what will be touched.
    """
    known = _PROSE_SUFFIXES + _CODE_SUFFIXES
    hits = [
        _normalize(m.group(0))
        for m in _PATH_TOKEN_RE.finditer(text or "")
    ]
    return list(dict.fromkeys(p for p in hits if p.lower().endswith(known)))


def trivial_paths(paths: list[str], *, max_files: int = MAX_TRIVIAL_FILES) -> bool:
    """THE PREDICATE: 1..*max_files* files, every one of them non-executed prose.

    Empty is NOT trivial — a change nobody named a file for is exactly the
    change whose blast radius is unknown.
    """
    seen = list(dict.fromkeys(_normalize(p) for p in paths if _normalize(p)))
    if not seen or len(seen) > max_files:
        return False
    return all(
        p.lower().endswith(_PROSE_SUFFIXES) and not p.startswith(_EXECUTED_ROOTS)
        for p in seen
    )


#: Prose whose one-line diff is high-consequence no matter how small it is.
#: Two families, one rule — and the rule is about the REVIEW, not admission:
#:
#:  * AGENT INSTRUCTIONS. `.agents/skills/**` is rejected outright above (a
#:    SKILL.md is loaded into a live session, so it is not prose at all); the
#:    rest of `.agents/` is an agent DEFINITION, and the motivating trace was a
#:    one-line edit to one of them. Beside them sit the well-known repo-native
#:    instruction files — the SAME list the coder prompt injects, borrowed from
#:    `Orchestrator._REPO_INSTRUCTION_FILES` rather than copied, so a new
#:    convention added there is covered here without anyone remembering to.
#:    Matched by BASENAME AT ANY DEPTH: `docs/CLAUDE.md` carries the same
#:    sentences as the root one, and before this only `.claude/CLAUDE.md` was
#:    caught, by an unrelated root veto. A one-line edit to the file holding
#:    "the agent never merges" is exactly the diff that must not be skimmed.
#:  * GATE CONTROL. `EXPORT_CLASSIFICATION.txt` and `RELEASE_MANIFEST.txt` end
#:    `.txt`, so the suffix test reads them as prose — but `export_guard.py`
#:    and `check_release_manifest.py` read them as their RULES. A one-line
#:    drop→ship reclassification is a leak that would then pass its own gate.
#:    `requirements.txt` / `constraints.txt` are the same shape one step
#:    worse: a `.txt` a MACHINE reads — and installing what a one-line edit
#:    adds runs third-party code. The coder prompt itself directs dependency
#:    edits there (`prompt_blocks`), and every sibling manifest on that list
#:    (`pyproject.toml`, `package.json`, `uv.lock`) already escalates by
#:    suffix; these two were the only entries reading as prose.
#:
#: All of these stay ADMISSIBLE to the fast path. The tier decides how much
#: CEREMONY a change is worth, not how much it is trusted, and 35 minutes of
#: scoping questions and Opus planning is not what makes such an edit safe.
#: What makes it safe is reading the diff — so the guard sits on the REVIEW
#: (`_run_reviewer`), which is also the only place the file set is a FACT
#: rather than a prediction off ticket text. Their smallness IS the risk.
_INSTRUCTION_ROOTS = (".agents/",)
_GATE_CONTROL_NAMES = ("EXPORT_CLASSIFICATION.txt", "RELEASE_MANIFEST.txt",
                       "requirements.txt", "constraints.txt")


def review_must_be_unbounded(paths: list[str]) -> bool:
    """True when any path's diff must get the FULL review (see above).

    Case-INSENSITIVE on the basenames, to match how the files are actually
    resolved: `_repo_instruction_section` opens them with ``Path.is_file()``,
    which on a case-insensitive volume (APFS's default, and NTFS) happily finds
    `Claude.md` for `CLAUDE.md`. A repo committing that spelling gets its rules
    injected into every coder session, so a case-SENSITIVE test here would hand
    exactly that file the bounded review. Folding costs nothing and can only
    escalate.
    """
    from .orchestrator import Orchestrator

    names = {rel.rsplit("/", 1)[-1].casefold()
             for rel in Orchestrator._REPO_INSTRUCTION_FILES}
    names.update(n.casefold() for n in _GATE_CONTROL_NAMES)
    return any(
        p.startswith(_INSTRUCTION_ROOTS)
        or p.rsplit("/", 1)[-1].casefold() in names
        for p in (_normalize(q) for q in paths)
    )


def named_paths(task: "Task") -> list[str]:
    """The file set to judge: the PLAN's when one exists (authoritative), else
    the paths the ticket names."""
    ctx = task.context or {}
    planned = (ctx.get("spec") or {}).get("files_to_change") or []
    if planned:
        return [
            p for p in (extract_path_token(str(f)) for f in planned) if p
        ]
    text = " ".join(
        [task.title or "", task.description or "", *(task.acceptance_criteria or [])]
    )
    return path_tokens(text)


def trivial_enabled(config: dict | None) -> bool:
    """``pipeline.trivial_tier.enabled`` — the off-switch that restores the
    pre-2026-08-09 behaviour. Tolerates the empty-YAML-section shape
    (``pipeline:`` with its body commented out deep-merges to None)."""
    section = (config or {}).get("pipeline") or {}
    return bool((section.get("trivial_tier") or {}).get("enabled", True))


def is_trivial(task: "Task") -> bool:
    """The single carrier every fast-path consumer reads. It is the STORED
    tier, so the off-switch (applied once, where the tier is recorded) and the
    escalation guards (which raise the stored tier) both bind everywhere."""
    return ((task.context or {}).get("complexity_tier")) == "trivial"


def compute_tier(task: "Task", moa_cfg: dict | None = None) -> tuple[str, list[str]]:
    """(tier, fired signal names) from signals knowable at intake.

    Signal set = the MoA gate's (multi-repo, many-criteria on OPERATOR-stated
    criteria, long-spec, ambiguous-spec) plus the thinking gate's post-spec
    signals when already present (files_to_change > 4, plan_size_warning).
    ≥2 signals or a decompose verdict ⇒ complex (the MoA bar); 1 ⇒ standard;
    0 ⇒ simple, or trivial when the spec is demonstrably tiny AND every file
    it names is non-executed prose (``trivial_paths`` — the fast path's
    admission test, since 2026-08-09 the tier BUYS something).
    """
    from .orchestrator import _moa_complexity_signals

    signals = list(_moa_complexity_signals(task, moa_cfg or {}))
    ctx = task.context or {}
    if len((ctx.get("spec") or {}).get("files_to_change") or []) > 4:
        signals.append("many-files")
    if ctx.get("plan_size_warning"):
        signals.append("large-plan")
    verdict = (ctx.get("eval_result") or {}).get("verdict")

    # The complex bar tracks the operator's min_signals when RAISED above
    # the default (an explicit cost knob); 0/1 stay fan-out overrides handled
    # at the MoA gate itself — a bar of 0 would tag every task complex and
    # turn thinking on for everything.
    bar = max(2, int((moa_cfg or {}).get("min_signals", 2)))
    if len(signals) >= bar or verdict == "decompose":
        return "complex", signals
    if len(signals) >= 1:
        return "standard", signals
    stated = ctx.get("original_criteria")
    if stated is None:
        stated = task.acceptance_criteria or []
    if (len(stated) <= 2 and len(task.description or "") < 500
            and not task.linked_repos
            and trivial_paths(named_paths(task))):
        return "trivial", signals
    return "simple", signals


def store_tier(task: "Task", tier: str, signals: list[str]) -> bool:
    """Record the tier on task.context, escalate-only. Returns True when the
    stored tier changed (caller persists + emits)."""
    ctx = task.context or {}
    current = ctx.get("complexity_tier")
    if current and _rank(tier) <= _rank(current):
        return False
    ctx["complexity_tier"] = tier
    ctx["complexity_signals"] = signals
    task.context = ctx
    return True


def is_complex(task: "Task") -> bool:
    return ((task.context or {}).get("complexity_tier")) == "complex"
