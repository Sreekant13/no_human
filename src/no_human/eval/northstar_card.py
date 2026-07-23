"""North-star benchmark scorecard + regression gate + report (plan Task A4).

Aggregates per-task ``BenchScore``s into the headline answer the whole program
exists to give: *does no_human complete the operator's real historical tasks
unattended, and at what token cost relative to the original babysat session?*

The gate blocks a key change when the success rate drops or the median token
ratio regresses beyond a threshold — mirroring ``scorecard.ci_gate``'s shape
so both gates read the same way. Never a numeric self-score: every number here
is measured, not judged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from .northstar import BenchScore

RESULTS_DIR = Path(__file__).resolve().parents[3] / "eval" / "results" / "northstar"
REPORT_MD = Path(__file__).resolve().parents[3] / "docs" / "NORTH_STAR_BENCH.md"


@dataclass
class NorthStarCard:
    scores: list[BenchScore] = field(default_factory=list)
    created_at: str = ""
    label: str = ""            # e.g. "baseline" or the change under test
    # How many specs the canonical corpus HAS, independent of what this run
    # loaded. Without it, coverage is a ratio over the loaded set and therefore
    # blind to filtering: pointing --specs-dir at the specs that still resolve
    # makes a broken corpus read as a perfect one. 0 = unknown (older cards).
    corpus_available: int = 0
    # Refusals a human overrode with `nh bench publish --force`. Carried into
    # the saved card and rendered at the top of the report: a forced publish is
    # allowed, but it must never be able to look like a clean one.
    override_reasons: list[str] = field(default_factory=list)

    # ------------------------------ counts --------------------------------- #

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def ran(self) -> list[BenchScore]:
        return [s for s in self.scores if s.outcome_status != "skipped"]

    @property
    def skipped(self) -> int:
        return self.total - len(self.ran)

    @property
    def satisfied(self) -> int:
        return sum(1 for s in self.ran if s.goal_satisfied)

    @property
    def success_rate(self) -> float:
        return self.satisfied / len(self.ran) if self.ran else 0.0

    # ------------------------------- cost ----------------------------------- #

    @property
    def median_token_ratio(self) -> float | None:
        vals = [s.token_ratio for s in self.ran if s.token_ratio is not None]
        return median(vals) if vals else None

    @property
    def median_cost_ratio(self) -> float | None:
        """Price-weighted (cache-aware) ratio — the honest headline; the plain
        token ratio is blind to cache-read, which is ~95% of real burn."""
        vals = [s.cost_ratio for s in self.ran if s.cost_ratio is not None]
        return median(vals) if vals else None

    @property
    def dead_specs(self) -> int:
        """Specs that ran but burned zero tokens — the SDK died before any model
        call. A skip is a decision and is excluded; this counts deaths only."""
        return sum(1 for s in self.ran if not s.nh_tokens)

    @property
    def total_nh_tokens(self) -> int:
        return sum(s.nh_tokens for s in self.ran)

    @property
    def total_orig_tokens(self) -> int:
        return sum(s.orig_tokens for s in self.ran)

    # --------------------------- babysitting -------------------------------- #

    @property
    def corrections_avoided(self) -> int:
        """Follow-up user messages the original sessions needed, for tasks
        no_human completed unattended. HONEST LABEL (review F4): this is a
        PROXY — user_messages-1 counts every follow-up ("thanks", new
        sub-asks), not only true corrections. Classifying real corrections
        needs a utility-model pass (future work); every surface says
        "follow-ups (proxy)" until then."""
        return sum(s.orig_corrections for s in self.ran if s.goal_satisfied)

    @property
    def corrections_avoided_delivered(self) -> int:
        """The part of `corrections_avoided` earned by DELIVERING something.

        `goal_satisfied` is also true for a gated spec that correctly REFUSED
        and handed the task back — the right outcome, but the human still has
        to do that work, so those follow-ups were not avoided. On v13, 251 of
        350 came from refused tasks and one escalated spec alone contributed
        96 (27% of the headline). Splitting it is the difference between an
        honest number and one that flatters by construction."""
        delivered = {"done", "awaiting_approval"}
        return sum(s.orig_corrections for s in self.ran
                   if s.goal_satisfied and s.outcome_status in delivered)

    @property
    def honest_escalation_rate(self) -> float:
        """Of the tasks whose only correct outcome is an honest stop
        (expect_escalation specs), how many stopped honestly (1.0 when none)."""
        must = [s for s in self.ran if s.expected_escalation]
        if not must:
            return 1.0
        return sum(1 for s in must if s.goal_satisfied) / len(must)

    # ---------------------------- persistence ------------------------------- #

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "label": self.label,
            "aggregate": {
                "total": self.total, "skipped": self.skipped,
                "satisfied": self.satisfied,
                "success_rate": round(self.success_rate, 4),
                "median_token_ratio": (round(self.median_token_ratio, 4)
                                       if self.median_token_ratio is not None
                                       else None),
                "median_cost_ratio": (round(self.median_cost_ratio, 4)
                                      if self.median_cost_ratio is not None
                                      else None),
                # Specs that RAN but burned zero tokens: the SDK died before any
                # model call. Published runs are under the refusal threshold by
                # construction, so this is the number that makes a *sub*-
                # threshold saturation legible instead of leaving a reader to
                # scan the per-task table for zeroes.
                "dead_specs": self.dead_specs,
                "total_nh_tokens": self.total_nh_tokens,
                "total_orig_tokens": self.total_orig_tokens,
                "corpus_available": self.corpus_available,
                "corrections_avoided": self.corrections_avoided,
                "corrections_avoided_delivered": self.corrections_avoided_delivered,
                "honest_escalation_rate": round(self.honest_escalation_rate, 4),
            },
            "override_reasons": self.override_reasons,
            "scores": [s.as_dict() for s in self.scores],
        }

    def save(self, path: Path) -> None:
        # Atomic write: the bench checkpoint is re-saved after every spec and
        # the process gets hard-killed on quota saturation ("Stream closed").
        # A truncate-in-place write caught mid-kill would leave a corrupt file
        # and silently discard every completed spec on the next --resume.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2))
        os.replace(tmp, path)

    @staticmethod
    def load(path: Path) -> "NorthStarCard | None":
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        scores = [BenchScore(
            task_id=s["task_id"], title=s["title"],
            outcome_status=s["outcome_status"],
            goal_satisfied=s["goal_satisfied"],
            escalated_honestly=s["escalated_honestly"],
            mergeable=s["mergeable"], nh_tokens=s["nh_tokens"],
            nh_cache_tokens=s["nh_cache_tokens"],
            nh_cache_creation_tokens=s.get("nh_cache_creation_tokens", 0),
            nh_turns=s["nh_turns"],
            nh_wall_clock_s=s["nh_wall_clock_s"],
            orig_tokens=s["orig_tokens"],
            orig_cache_tokens=s.get("orig_cache_tokens", 0),
            orig_cache_creation_tokens=s.get("orig_cache_creation_tokens", 0),
            orig_wall_clock_s=s["orig_wall_clock_s"],
            orig_corrections=s["orig_corrections"],
            expected_escalation=bool(s.get("expected_escalation", False)),
            subset=s.get("subset", "full"),
            project=s.get("project", ""),
            notes=s.get("notes", ""),
            events=s.get("events") or [],
        ) for s in data.get("scores", [])]
        return NorthStarCard(scores=scores,
                             corpus_available=int((data.get('aggregate') or {})
                                                 .get('corpus_available') or 0),
                             created_at=data.get("created_at", ""),
                             label=data.get("label", ""),
                             override_reasons=list(
                                 data.get("override_reasons") or []))


# A run must clear these before it may become the published baseline. They are
# derived from the SCORES rather than from run flags on purpose: a checkpoint
# written by an older build carries no record of its flags, and the incident
# these prevent is precisely that such a file overwrote the report.
MIN_PUBLISHABLE_SPECS = 10
MAX_DEAD_FRACTION = 0.2
# How much of the LOADED spec set may go unmeasured before a run stops being a
# verdict. "Loaded", not "the corpus": `total` counts what this run loaded after
# the subset filter, --specs-dir and --limit, so deleting specs outright makes
# coverage look perfect, and BOTH counts shrink together so the shortfall rule
# cannot see it either. Nothing in the gate catches that on a fresh clone; what
# does is that deleting corpus files is a visible git commit.
# Deliberately its own value, NOT an alias of MAX_DEAD_FRACTION: that one is a
# fraction of the specs that RAN (how saturated was the backend), this is a
# fraction of the LOADED spec set (how much of what this run picked up did we
# actually look at). Different numerator, different denominator, different question —
# aliasing them would mean retuning backend-saturation tolerance silently
# retunes corpus-coverage tolerance. 20% is chosen so a handful of
# legitimately-unrunnable specs cannot veto a run, while a corpus that has
# largely stopped resolving cannot be reported as a result.
MAX_UNMEASURED_FRACTION = 0.2


# How much of the AVAILABLE corpus a run must actually load. Filtering is the
# documented reaction to a coverage refusal ("just run the ones that work"), and
# it defeats every ratio computed over the loaded set — 19 surviving specs of 55
# is a perfect 0/19 unmeasured and clears a 10-spec floor comfortably.
MIN_CORPUS_LOADED_FRACTION = 0.8


def corpus_shortfall(card: NorthStarCard) -> str:
    """Why this run's spec set is too small a slice of the corpus, or ``""``.

    Compares LOADED against AVAILABLE, which is the only comparison that
    survives filtering. Silent when the card does not record an available count
    (older cards, or a run against a corpus of its own).
    """
    available = card.corpus_available
    if not available or card.total >= available * MIN_CORPUS_LOADED_FRACTION:
        return ""
    return (
        f"this run loaded {card.total} of {available} available spec(s) "
        f"({card.total / available:.0%} < {MIN_CORPUS_LOADED_FRACTION:.0%}) — "
        f"a filtered slice cannot stand for the corpus, and filtering to the "
        f"specs that still resolve is exactly how a broken corpus reads as a "
        f"perfect one")


def unmeasured_specs(card: NorthStarCard) -> tuple[int, int]:
    """``(unmeasured, total)`` — specs that yielded no measurement: *skipped*
    (never ran) plus *dead* (ran, but burned zero tokens).

    The denominator is the LOADED spec set, not ``ran``. A skipped spec leaves
    ``ran`` entirely, so a fraction measured against ``ran`` is structurally
    blind to it — which is exactly how a corpus that mostly failed to resolve
    reads as a *higher* success rate over the handful that survived.
    """
    return card.skipped + card.dead_specs, card.total


def publish_refusals(card: NorthStarCard,
                     previous: NorthStarCard | None = None) -> list[str]:
    """Why *card* must not become the published baseline. Empty means it may.

    Three incidents, one root cause — the runner treated every run as
    authoritative, so a probe and a quota death both overwrote the committed
    report and the gate baseline:

    - **dead specs.** A spec that burned zero tokens did not run; the SDK died
      ("Stream closed") before any model call. Those score as failed-and-
      honestly-escalated, which *inflates* honest-escalation while crushing
      success — an infrastructure failure wearing a capability result's clothes.
      Checking a FRACTION rather than "any" is deliberate: the v14 shape was one
      working spec followed by 96 dead ones, which an any-check waves through.
      Skipped specs are excluded — a skip is a decision, not a death.
    - **a slice is not the corpus.** A capped or partial run scored some specs,
      not the benchmark, and must not replace a baseline built from more.
    - **regression gates compare against this file.** Publishing a narrower run
      silently redefines what "no regression" means for every run after it.
    """
    reasons: list[str] = []
    ran = card.ran
    if not ran:
        reasons.append(
            f"nothing ran: {card.total} spec(s), all skipped — a selection "
            f"problem, not a result")
        return reasons

    dead = [s for s in ran if not s.nh_tokens]
    if len(dead) / len(ran) > MAX_DEAD_FRACTION:
        reasons.append(
            f"{len(dead)}/{len(ran)} specs burned zero tokens "
            f"({len(dead) / len(ran):.0%} > {MAX_DEAD_FRACTION:.0%}) — the "
            f"backend was saturated, so this measures the quota, not no_human")

    # Coverage, the SAME question the gate asks, through the SAME helper. The
    # dead rule above asks "was the backend saturated" (dead ÷ ran); this asks
    # "did we look at the benchmark at all" (skipped + dead ÷ loaded). Without
    # it the incident shape published clean: 36 of 55 specs pinned at a repo
    # that no longer exists are SKIPS, so zero were dead, 19 ran (over the
    # floor), and a fresh clone has no baseline to narrow against — every rule
    # passed and a 65%-unmeasured run became the committed baseline.
    shortfall = corpus_shortfall(card)
    if shortfall:
        reasons.append(shortfall)

    unmeasured, loaded = unmeasured_specs(card)
    if loaded and unmeasured / loaded > MAX_UNMEASURED_FRACTION:
        reasons.append(
            f"{unmeasured}/{loaded} specs went unmeasured "
            f"({unmeasured / loaded:.0%} > {MAX_UNMEASURED_FRACTION:.0%}) — "
            f"skipped or dead; too little of the benchmark was measured for "
            f"this run to be the baseline every later run is compared against")

    if len(ran) < MIN_PUBLISHABLE_SPECS:
        reasons.append(
            f"only {len(ran)} spec(s) ran (minimum {MIN_PUBLISHABLE_SPECS}) — "
            f"a probe or a capped run is a slice, not the corpus")

    # Compared on RAN, not total. Every headline — success_rate,
    # honest_escalation_rate, median_cost_ratio — is computed over ran, and
    # skipping is the documented dominant failure mode (the specs pin to local
    # repo paths). A run that loads 56 specs and skips 40 has the same `total`
    # as the baseline and would publish "100% success" measured over 16.
    if previous is not None and len(ran) < len(previous.ran):
        reasons.append(
            f"this run ran {len(ran)} spec(s) but the current baseline "
            f"'{previous.label or 'unlabelled'}' ran {len(previous.ran)} — "
            f"publishing would narrow what every later regression gate checks")

    return reasons


@dataclass
class NorthStarGate:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def northstar_gate(current: NorthStarCard,
                   previous: NorthStarCard | None, *,
                   max_ratio_regression: float = 0.5,
                   max_success_drop: float = 0.0) -> NorthStarGate:
    """Block a key change when the bench regresses vs the previous run.

    - success rate must not drop by more than ``max_success_drop`` (default:
      any drop blocks);
    - median token ratio must not grow by more than ``max_ratio_regression``
      (absolute, e.g. 0.80 → 1.31 blocks at the default 0.5).

    A first run has no baseline to compare against, so it skips the regression
    comparisons — but it must still clear COVERAGE and, because coverage cannot
    see specs that were never loaded, an absolute floor.

    BEHAVIOUR CHANGES for existing `--gate` users, stated rather than buried:

    - a run NARROWER than the baseline now blocks;
    - a run that loads well under the AVAILABLE corpus now blocks, with or
      without a baseline — this is what catches filtering to the specs that
      still resolve;
    - `--limit N --gate` blocks well before `MIN_PUBLISHABLE_SPECS`: the
      loaded-vs-available rule refuses anything under 80% of the corpus, so on
      a 55-spec corpus the real boundary is N < 44. It also blocks against any
      baseline it is narrower than;
    - the FIRST run of any corpus with fewer than `MIN_PUBLISHABLE_SPECS`
      runnable specs now blocks;
    - a baseline published from a `--full` run will red a default (core)
      invocation IF `generated/` is populated. It is empty today, so `--full`
      currently loads the same specs and changes nothing.

    Every comparison above is computed over ``ran``, so a spec that never ran
    LEAVES the denominator instead of depressing it. That makes a broken
    instrument read as an improvement: when most of the corpus fails to resolve
    (or the backend dies), the survivors are the healthy specs and the headline
    goes UP, and without the checks below a run measuring a third of the corpus
    reports a better-than-baseline number and exits 0. ``publish_refusals``
    applies the SAME coverage rule through the same helper, so the two cannot
    disagree about whether a run measured enough to mean anything — they did
    disagree until the incident shape (36 of 55 specs pinned at a repo that no
    longer exists) was rejected by the gate and published clean.

    Coverage is therefore checked BEFORE the first-run early return. It needs
    nothing from *previous*, and ``eval/results/northstar/`` is gitignored — so
    ``previous`` is None in every fresh clone and CI checkout. Gating the check
    behind a baseline would exempt exactly the configuration where a broken
    corpus is most likely to be published as one.

    Two limits worth stating rather than implying:

    - **Coverage is counted, membership is not.** A run that measures the same
      NUMBER of specs drawn from a different (say, easier) population passes
      these checks. Corpus churn is legitimate and frequent here, so a rule
      that blocked any change in spec membership would be permanently red;
      catching a laundered population needs comparing task_ids against the
      baseline, which is deliberately not attempted here.
    - **A skip counts against coverage, though ``publish_refusals`` treats a
      skip as a decision rather than a death.** The two ask different
      questions and the divergence is intentional: a spec that can never run is
      still a spec the benchmark did not measure. The consequence is that
      persistently-unrunnable specs must be REMOVED from the corpus, not left
      to be skipped forever — otherwise their fixed cost eventually crosses the
      ceiling and the gate is red for good.
    """
    reasons: list[str] = []

    shortfall = corpus_shortfall(current)
    if shortfall:
        reasons.append(shortfall)

    unmeasured, total = unmeasured_specs(current)
    if total and unmeasured / total > MAX_UNMEASURED_FRACTION:
        reasons.append(
            f"{unmeasured}/{total} specs went unmeasured "
            f"({unmeasured / total:.0%} > {MAX_UNMEASURED_FRACTION:.0%}) — "
            f"skipped or dead; too little of what this run loaded was "
            f"measured for it to be a verdict on anything")

    if previous is None:
        # A floor on `ran` catches a PROBE (--limit 3). It does NOT catch
        # filtering: 19 surviving specs of 55 clears a 10-spec floor easily,
        # which is why the corpus_shortfall check above exists and this one is
        # not load-bearing for that case. Kept because a first run has no
        # baseline to be narrower than, so nothing else bounds a tiny one.
        if len(current.ran) < MIN_PUBLISHABLE_SPECS:
            reasons.append(
                f"only {len(current.ran)} spec(s) ran and there is no baseline "
                f"to compare against (minimum {MIN_PUBLISHABLE_SPECS}) — a "
                f"slice cannot become the reference for every later run")
        if reasons:
            return NorthStarGate(False, reasons)
        return NorthStarGate(True, ["first run — baseline recorded"])

    if not previous.ran:
        # Reachable via --prev <file>, which accepts any results file. Every
        # comparison below would silently pass against an empty baseline.
        #
        # A reviewer suggested extending this to any baseline under
        # MIN_PUBLISHABLE_SPECS, which is defensible — a one-spec baseline is
        # nearly as meaningless. DECLINED here: it blocks four existing tests
        # whose small fixtures are incidental, and adjusting fixtures so a new
        # rule fits is exactly the move that neutered a test earlier in this
        # PR's history. The empty case is the unambiguous one; a wider floor
        # deserves its own change with its own fixtures.
        reasons.append(
            f"the baseline '{previous.label or 'unlabelled'}' measured no "
            f"specs — there is nothing to compare against")
    if len(current.ran) < len(previous.ran):
        reasons.append(
            f"this run measured {len(current.ran)} spec(s) but the baseline "
            f"'{previous.label or 'unlabelled'}' measured {len(previous.ran)} "
            f"— a narrower run cannot establish 'no regression'")

    drop = previous.success_rate - current.success_rate
    if drop > max_success_drop + 1e-9:
        reasons.append(
            f"success rate dropped {previous.success_rate:.0%} → "
            f"{current.success_rate:.0%}")
    for label, prev_r, cur_r in (
        ("token", previous.median_token_ratio, current.median_token_ratio),
        ("cost", previous.median_cost_ratio, current.median_cost_ratio),
    ):
        if prev_r is not None and cur_r is None:
            # Review finding: a run that LOSES its ratio must not silently
            # skip the cost check — fail closed and make a human look.
            reasons.append(
                f"median {label} ratio unavailable on current run "
                f"(previous had {prev_r:.2f}) — cannot verify cost; blocking")
        elif prev_r is not None and cur_r is not None \
                and cur_r - prev_r > max_ratio_regression:
            reasons.append(
                f"median {label} ratio regressed {prev_r:.2f} → {cur_r:.2f} "
                f"(> +{max_ratio_regression})")
    if reasons:
        return NorthStarGate(False, reasons)
    return NorthStarGate(True, ["no regression vs previous run"])


def render_northstar_md(card: NorthStarCard,
                        history: list[dict[str, Any]] | None = None) -> str:
    """Render docs/NORTH_STAR_BENCH.md content."""
    agg = card.as_dict()["aggregate"]
    # How many ran specs actually HAVE an original cost to compare against.
    # The median is over these, not over `ran`, and publishing it without the
    # denominator overstates its coverage.
    _priced = sum(1 for s in card.ran if s.cost_ratio is not None)
    # Numerator/denominator behind the escalation rate. Computed here
    # rather than added to the aggregate so this does not collide with the
    # same fields arriving on the bench-endpoint branch.
    _gated = [s for s in card.ran if s.expected_escalation]
    _gated_ok = sum(1 for s in _gated if s.goal_satisfied)
    _delivered = agg['satisfied'] - _gated_ok
    ratio = agg["median_token_ratio"]
    lines = [
        "# North-star benchmark — no_human vs the operator's real sessions",
        "",
        f"> Run: {card.created_at or 'n/a'}  ·  label: {card.label or 'n/a'}. "
        "Generated by `nh bench publish` — do not edit by hand.",
        "",
    ]
    if card.override_reasons:
        lines += [
            "> [!WARNING]",
            "> **This run was published with `--force` over the checks below.**",
            "> Read every number here as unverified until they are addressed:",
            *(f"> - {r}" for r in card.override_reasons),
            "",
        ]
    lines += [
        "_Project labels and repo paths in this report are pseudonymised; every "
        "number is exactly as measured. A run may predate the current corpus — "
        "check the run date above before treating it as reproducible._",
        "",
        "## Headline",
        "",
        f"- **Success (goal satisfied, unattended): {agg['satisfied']}/"
        f"{agg['total'] - agg['skipped']} ran ({agg['success_rate']:.0%})**"
        f"  ·  skipped (non-runnable): {agg['skipped']}",
        f"  - of which {_delivered} DELIVERED a change and {_gated_ok} correctly "
        f"ESCALATED — 'satisfied' counts an honest refusal as the right outcome, "
        f"which it is, but only the first group shipped anything",
        f"- **Median COST ratio (price-weighted, cache-aware): "
        f"{agg['median_cost_ratio'] if agg['median_cost_ratio'] is not None else 'n/a'}**"
        f" — over the {_priced} of {agg['total'] - agg['skipped']} ran spec(s) "
        f"with a recorded original cost; <1.0 means no_human was cheaper than "
        f"the babysat session. The nh side counts coder+reviewer, NOT yet "
        f"planner/supervisor (B2), so it UNDERSTATES no_human's real burn.",
        f"- Median token ratio (non-cache in/out only): "
        f"{ratio if ratio is not None else 'n/a'} — blind to cache burn; "
        "nh side includes coder+reviewer, NOT yet planner/supervisor (B2)",
        f"- Total non-cache tokens: nh {agg['total_nh_tokens']:,} over all "
        f"{agg['total'] - agg['skipped']} ran spec(s), vs original "
        f"{agg['total_orig_tokens']:,} over the {_priced} that have a baseline "
        f"at all — NOT a like-for-like pair; use the median cost ratio above",
        # Always rendered, including the 0 case. A published run is under the
        # refusal threshold by construction, so the number a reader needs is
        # confirmation that saturation was checked — not its absence when clean
        # and a silent omission when not.
        f"- Specs that ran but burned zero tokens (backend died before any "
        f"model call): **{agg['dead_specs']}** of {agg['total'] - agg['skipped']}"
        + ("  ⚠ read every figure here with that in mind"
           if agg['dead_specs'] else ""),
        f"- **Original-session follow-ups avoided on DELIVERED tasks: "
        f"{agg['corrections_avoided_delivered']}** (proxy for corrections)"
        f"  ·  a further "
        f"{agg['corrections_avoided'] - agg['corrections_avoided_delivered']} "
        f"belong to tasks no_human correctly ESCALATED — the human still has "
        f"to do those, so they are not savings",
        f"- Honest-escalation rate on gated tasks: "
        f"{agg['honest_escalation_rate']:.0%} "
        f"({_gated_ok}/{len(_gated)}) — one flip moves a small "
        f"denominator several points",
        "",
        "## Per-task",
        "",
        "| task | outcome | satisfied | nh tokens | orig tokens | cost ratio | "
        "orig follow-ups (proxy) | notes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    # PRIVACY: only hand-curated (PR-reviewed) core specs get per-task rows
    # in this git-tracked report; generated specs are verbatim operator
    # conversations and appear only in the aggregates.
    core_scores = [s for s in card.scores if s.subset == "core"]
    hidden = len(card.scores) - len(core_scores)
    for s in core_scores:
        ratio_s = (f"{s.cost_ratio:.2f}" if s.cost_ratio is not None else "—")
        sat = {True: "✅", False: "❌", None: "—"}[s.goal_satisfied]
        lines.append(
            f"| {s.task_id} | {s.outcome_status} | {sat} | {s.nh_tokens:,} | "
            f"{s.orig_tokens:,} | {ratio_s} | {s.orig_corrections} | "
            f"{(s.notes or '')[:80].replace('|', '/')} |")
    if hidden:
        lines.append(f"\n_{hidden} non-core task(s) included in the aggregates only (privacy: raw corpus rows never enter git)._")

    # Per-project view (the operator's suite spans multiple real repos — show
    # cost/quality by project). Aggregate-only (repo name + counts + median
    # cost), so it's privacy-safe over ALL scores, not just core.
    from collections import defaultdict
    proj: dict = defaultdict(lambda: {"n": 0, "ok": 0, "esc": 0, "ratios": []})
    for s in card.scores:
        p = s.project or "?"
        proj[p]["n"] += 1
        proj[p]["ok"] += 1 if s.goal_satisfied else 0
        proj[p]["esc"] += 1 if s.escalated_honestly else 0
        if s.cost_ratio is not None:
            proj[p]["ratios"].append(s.cost_ratio)
    if len(proj) > 1:
        lines += ["", "## Per-project", "",
                  "| project | tasks | satisfied | honest-escalations | median cost |",
                  "|---|---|---|---|---|"]
        for p in sorted(proj):
            a = proj[p]
            med = f"{median(a['ratios']):.3f}" if a["ratios"] else "—"
            lines.append(f"| {p} | {a['n']} | {a['ok']}/{a['n']} | {a['esc']} | {med} |")

    if history:
        lines += ["", "## History (key changes)", "",
                  "| date | label | success | median ratio |", "|---|---|---|---|"]
        for h in history:
            lines.append(f"| {h.get('created_at','')[:10]} | {h.get('label','')} "
                         f"| {h.get('success_rate','')} | "
                         f"| {h.get('median_token_ratio','')} |".replace("| |", "|"))
    return "\n".join(lines) + "\n"
