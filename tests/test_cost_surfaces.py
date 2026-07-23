"""The two cost surfaces #210 deferred: `nh watch`'s live status and the
golden-eval score both reported NON-CACHE tokens as if it were cost. Cache
reads are the bulk of real burn (a live runaway spent 4M cache-read against 731
non-cache), so the number was a ~5500x under-report on the metric this project
is judged on — the same defect #210 fixed for `nh logs` and `nh agents`.
"""
from __future__ import annotations


def test_watch_status_reports_full_burn_not_non_cache():
    from no_human.cli.tui import _event_burn

    # the fa7be197 runaway shape: tiny non-cache, huge cache-read
    ev = {"kind": "result", "num_turns": 1, "tokens_used": 731,
          "cache_read_tokens": 4_053_498, "cache_creation_tokens": 197_948}
    assert _event_burn(ev) == 4_252_177        # all three, not 731

    # missing cache columns default to 0, never crash
    assert _event_burn({"tokens_used": 500}) == 500
    assert _event_burn({}) == 0
    # None-valued columns (a fresh event) are tolerated
    assert _event_burn({"tokens_used": None, "cache_read_tokens": None}) == 0


def test_watch_SINK_wires_the_burn_into_the_status():
    """The helper being right is not enough — `_sink` must USE it, on the real
    code path. A first version bypassed the constructor (`__new__`) and guessed
    the source; both were wrong (reactive attrs need Textual's __init__, and
    the result branch is gated on `is_agent_session`). This constructs the app
    normally, uses the real coder-session source, and stubs only the log
    widget."""
    from no_human.cli.tui import WatchApp
    from no_human.core.orchestrator import CODER_ROLE

    app = WatchApp(config=object(), task_id="abcd1234")
    app.query_one = lambda *a, **k: type(
        "L", (), {"write": lambda self, *a: None})()

    app._sink({"source": CODER_ROLE, "kind": "result", "num_turns": 1,
               "tokens_used": 731, "cache_read_tokens": 4_053_498,
               "cache_creation_tokens": 197_948})

    assert app._tokens == 4_252_177, app._tokens   # burn, not 731
    assert app._turns == 1


def test_golden_eval_score_reports_full_burn_not_non_cache():
    """`replay.ReplayRunner._score` summed `tokens_used` alone. Driven with the
    fa7be197 runaway shape, the REAL `score.tokens` must be full burn, not the
    731 non-cache. Exercises the wired code path, not a re-computation of it."""
    import asyncio
    import types

    from no_human.eval.replay import ReplayRunner

    r = ReplayRunner.__new__(ReplayRunner)
    r._tamper_free = lambda outcome, attempts: True
    r.judge = None
    golden = types.SimpleNamespace(
        id="g1", title="t", is_red_team=False, impossible=False,
        tempts_tamper=False, known_good_diff=None)
    outcome = types.SimpleNamespace(status=types.SimpleNamespace(value="done"))
    # coder 4,252,177 + review 100 + plan 200 + utility 50 = 4,252,527. The
    # aux groups are what a coder-group-only sum (the reviewer's finding) would
    # miss.
    attempts = [{"tokens_used": 731, "cache_read_tokens": 4_053_498,
                 "cache_creation_tokens": 197_948,
                 "review_tokens_used": 100, "plan_cache_read_tokens": 200,
                 "utility_cache_creation_tokens": 50}]

    score = asyncio.run(r._score(golden, outcome, None, "sha", attempts, 1.0))

    assert score.tokens == 4_252_527, score.tokens   # all 4 groups, not 731
