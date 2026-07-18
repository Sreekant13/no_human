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
                "total_nh_tokens": self.total_nh_tokens,
                "total_orig_tokens": self.total_orig_tokens,
                "corrections_avoided": self.corrections_avoided,
                "honest_escalation_rate": round(self.honest_escalation_rate, 4),
            },
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
                             created_at=data.get("created_at", ""),
                             label=data.get("label", ""))


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
    First run (no previous) always passes — it becomes the baseline.
    """
    if previous is None:
        return NorthStarGate(True, ["first run — baseline recorded"])
    reasons: list[str] = []
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
    ratio = agg["median_token_ratio"]
    lines = [
        "# North-star benchmark — no_human vs the operator's real sessions",
        "",
        f"> Run: {card.created_at or 'n/a'}  ·  label: {card.label or 'n/a'}. "
        "Generated by `nh bench run` — do not edit by hand.",
        "",
        "## Headline",
        "",
        f"- **Success (goal satisfied, unattended): {agg['satisfied']}/"
        f"{agg['total'] - agg['skipped']} ran ({agg['success_rate']:.0%})**"
        f"  ·  skipped (non-runnable): {agg['skipped']}",
        f"- **Median COST ratio (price-weighted, cache-aware): "
        f"{agg['median_cost_ratio'] if agg['median_cost_ratio'] is not None else 'n/a'}**"
        " — <1.0 means no_human was cheaper than the babysat session",
        f"- Median token ratio (non-cache in/out only): "
        f"{ratio if ratio is not None else 'n/a'} — blind to cache burn; "
        "nh side includes coder+reviewer, NOT yet planner/supervisor (B2)",
        f"- Total non-cache tokens: nh {agg['total_nh_tokens']:,} vs original "
        f"{agg['total_orig_tokens']:,}",
        f"- **Original-session follow-ups avoided (proxy for corrections): "
        f"{agg['corrections_avoided']}**",
        f"- Honest-escalation rate on gated tasks: "
        f"{agg['honest_escalation_rate']:.0%}",
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
