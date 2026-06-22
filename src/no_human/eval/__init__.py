"""Evaluation & performance harness (PLAN.md Part 21)."""

from .golden import GoldenTask, load_golden_tasks
from .harness import EvalRun, ShadowResult, run_eval, run_shadow
from .judge import IntentJudge, JudgeVerdict, parse_verdict
from .replay import ReplayRunner, TaskScore
from .scorecard import GateResult, Scorecard, ci_gate, render_scorecard

__all__ = [
    "GoldenTask",
    "load_golden_tasks",
    "ReplayRunner",
    "TaskScore",
    "IntentJudge",
    "JudgeVerdict",
    "parse_verdict",
    "Scorecard",
    "GateResult",
    "ci_gate",
    "render_scorecard",
    "EvalRun",
    "ShadowResult",
    "run_eval",
    "run_shadow",
]
