"""The sprint both narrated demo videos record: ten tickets, five handed over.

Same discipline as ``fixture.py`` (the 26 s cut's world), same payload shapes,
different story: a working engineer's sprint backlog, five tickets handed to
no_human at once, five agents running IN PARALLEL to five opened PRs. The
board harness serves these dicts over a stubbed HTTP API; the shell harness
hands the same dicts to a fake ``NhClient``. Neither invents a payload.

Every time in this module is derived from ``narration.py``'s beats — the
narration is the clock, and the fixture is cut to it, not the other way
around: a status change lands just before the narrated line that talks about
it, so the words are never ahead of the screen.

EVERYTHING HERE IS SYNTHETIC. The company, the repo (``shop``), the Jira
project keys (PAY / ORD / NOTIF / PLAT) and every ticket are invented for the
demo; they are written to be PLAUSIBLE — the operator's bar is "tasks that
look real" — but reference no real person, company, product or employer. The
Jira keys ride in the task TITLES because that is where a title from a
tracker sync is visible on both surfaces (the board card shows title + short
hex id; the shell lane line shows the same).
"""

from __future__ import annotations

from . import fixture as fx
from . import narration as nr

# --------------------------------------------------------------------------- #
# The sprint backlog — ten tickets, the way a tracker would hold them          #
# --------------------------------------------------------------------------- #

#: (key, title-without-key, kind, chosen). ``kind`` is one the product ships
#: styling for (task.py: feature|bugfix|ci_fix|traceability|test_gap — the
#: board badges every kind but "feature"). The five CHOSEN tickets cover the
#: brief's spread: two bugs (one of them an investigation), a feature, a flaky
#: test, a refactor. The five left behind stay in the tracker — they are what
#: the engineer keeps, and the narration's "ten tickets" is this list.
BACKLOG: tuple[tuple[str, str, str, bool], ...] = (
    ("PAY-1382", "Webhook retry double-charges on 502", "bugfix", True),
    ("ORD-2211", "Deflake test_checkout_totals on CI", "ci_fix", True),
    ("PAY-1401", "Idempotency keys for the refund API", "feature", True),
    ("NOTIF-412", "Digest emails fire twice on Mondays", "bugfix", True),
    ("ORD-2187", "Extract tax math out of CheckoutService", "feature", True),
    ("PAY-1407", "Chargeback report CSV export", "feature", False),
    ("ORD-2216", "Order search returns deleted SKUs", "bugfix", False),
    ("NOTIF-433", "Unsubscribe link 404s on legacy tokens", "bugfix", False),
    ("PLAT-961", "Rate-limit login attempts by IP", "feature", False),
    ("PLAT-990", "Nightly backup restore drill is manual", "feature", False),
)

CHOSEN: tuple[str, ...] = tuple(k for k, _t, _k, chosen in BACKLOG if chosen)


def title_of(key: str) -> str:
    for k, title, _kind, _chosen in BACKLOG:
        if k == key:
            return f"{k}: {title}"
    raise KeyError(key)


def kind_of(key: str) -> str:
    for k, _title, kind, _chosen in BACKLOG:
        if k == key:
            return kind
    raise KeyError(key)


#: One synthetic repo for the sprint. "shop" and not something longer for the
#: same reason fixture.py's repo is "calc": the PR URL has to fit the shell's
#: 50-column detail pane with its `* pr_open ` prefix.
REPO_NAME = "shop"
REPO_PATH = "/Users/dev/git/shop"

#: 2026-08-03 is a Monday — the narration opens on "Monday." and the pinned
#: clock agrees with it. Seconds start at :00 so per-revision bumps (below)
#: stay inside the minute and every card keeps reading "<1m".
NOW = "2026-08-03T09:14:00+00:00"
BROWSER_NOW = "2026-08-03T09:14:45+00:00"
YESTERDAY = "2026-08-02T16:41:00+00:00"

#: Task ids: fixed 32-hex, so the 8-char short ids the surfaces print are
#: stable per ticket across recordings.
IDS: dict[str, str] = {
    "PAY-1382": "b3d7f2a94c1e48d59a6b7c8d9e0f1a2b",
    "ORD-2211": "c4e8a3b05d2f49e6ab7c8d9e0f1a2b3c",
    "PAY-1401": "d5f9b4c16e3a4af7bc8d9e0f1a2b3c4d",
    "NOTIF-412": "f7b1d6e3805c4c19de0f1a2b3c4d5e6f",
    "ORD-2187": "e6a0c5d27f4b4b08cd9e0f1a2b3c4d5e",
}

PR_URLS: dict[str, str] = {
    "PAY-1382": "https://github.com/you/shop/pull/312",
    "ORD-2211": "https://github.com/you/shop/pull/313",
    "PAY-1401": "https://github.com/you/shop/pull/314",
    "NOTIF-412": "https://github.com/you/shop/pull/315",
    "ORD-2187": "https://github.com/you/shop/pull/316",
}

#: The one ticket both drawers open on. The webhook bug is the hero because it
#: is the operator's own example of "a task that looks real", and because a
#: double-charge is the rare bug whose one-line diff a viewer can verify.
HERO_KEY = "PAY-1382"
HERO_ID = IDS[HERO_KEY]

#: The demo shows exactly one review verdict being read and one Approve being
#: clicked; the other four PRs stay waiting, which is the honest state — the
#: narration's claim is "you read, you approve", not "one click approves five".
APPROVE_AT = 66.40

DESCRIPTIONS: dict[str, str] = {
    "PAY-1382": "gateway retries any non-2xx; one 502 charged twice",
    "ORD-2211": "fails ~1 in 30 on CI; passes locally every time",
    "PAY-1401": "refund POSTs are not safe to retry today",
    "NOTIF-412": "Monday digests enqueue from two schedulers",
    "ORD-2187": "tax rules are inlined in a 900-line service",
}

# --------------------------------------------------------------------------- #
# Per-task pipelines — five staggered timelines, cut to the narration          #
# --------------------------------------------------------------------------- #

#: (t, status, live_status, burn_tokens, turns, attempt). Times are seconds on
#: the shared clock. The stagger is the point: five tasks moving at five
#: paces reads as parallel work, five lockstep tasks reads as a screensaver.
#: Anchors, all derived from narration.py:
#:   * every task is created inside the HANDOFF beat (9.0-17.0)
#:   * every task is RUNNING before PARALLEL's first line is spoken:
#:     the ramp is 16.40-17.40 and "Five agents start" lands at 17.60.
#:     It used to run 17.2-19.6, straight through that line, so the
#:     header read "5 running - 4/5 workers busy - 1 queued" while the
#:     voice claimed five. The stagger is kept (0.25 s apart, so they
#:     still land one by one) but it now FINISHES before the claim
#:   * the hero is reviewable before the GATE beat needs its diff
#:   * ORD-2187 FAILS its first review at 44.0 and goes around again —
#:     attempt 2 — because the gate refusing to rubber-stamp is the beat's
#:     whole claim, and a gate nothing ever bounces off is decoration
#:   * all five are awaiting_approval before PAYOFF opens at 56.0
STAGES: dict[str, tuple[tuple[float, str, str | None, int, int, int], ...]] = {
    "PAY-1382": (
        (9.60, "pending", "queued", 0, 0, 0),
        (16.40, "context", "gathering context", 40_000, 1, 1),
        (18.60, "planning", "3 proposals -> 1 plan", 110_000, 1, 1),
        (21.00, "implementing", "Edit webhooks.py", 390_000, 3, 1),
        (33.00, "testing", "Run `pytest -q`", 820_000, 6, 1),
        (38.40, "reviewing", "independent review", 1_250_000, 7, 1),
        (43.00, "awaiting_approval", None, 1_620_000, 7, 1),
    ),
    "ORD-2211": (
        (10.80, "pending", "queued", 0, 0, 0),
        (16.65, "context", "gathering context", 35_000, 1, 1),
        (19.40, "planning", "3 proposals -> 1 plan", 100_000, 1, 1),
        (22.20, "implementing", "reproduce: pytest x40", 300_000, 2, 1),
        (30.00, "implementing", "Edit test_checkout.py", 520_000, 4, 1),
        (35.00, "testing", "Run `pytest -q` x40", 780_000, 5, 1),
        (41.00, "reviewing", "independent review", 1_000_000, 6, 1),
        (46.00, "awaiting_approval", None, 1_230_000, 6, 1),
    ),
    "PAY-1401": (
        (12.00, "pending", "queued", 0, 0, 0),
        (16.90, "context", "gathering context", 45_000, 1, 1),
        (20.20, "planning", "3 proposals -> 1 plan", 130_000, 1, 1),
        (23.40, "implementing", "Edit refunds_api.py", 480_000, 3, 1),
        (36.60, "testing", "Run `pytest -q`", 900_000, 6, 1),
        (43.50, "reviewing", "independent review", 1_300_000, 7, 1),
        (50.00, "awaiting_approval", None, 1_680_000, 7, 1),
    ),
    "NOTIF-412": (
        (13.20, "pending", "queued", 0, 0, 0),
        (17.15, "context", "gathering context", 30_000, 1, 1),
        (21.00, "planning", "3 proposals -> 1 plan", 90_000, 1, 1),
        (24.60, "implementing", "Grep digest schedulers", 260_000, 2, 1),
        (31.00, "implementing", "Edit scheduler.py", 560_000, 4, 1),
        (37.80, "testing", "Run `pytest -q`", 830_000, 5, 1),
        (44.80, "reviewing", "independent review", 1_100_000, 6, 1),
        (53.00, "awaiting_approval", None, 1_350_000, 6, 1),
    ),
    "ORD-2187": (
        (14.40, "pending", "queued", 0, 0, 0),
        (17.40, "context", "gathering context", 38_000, 1, 1),
        (21.80, "planning", "3 proposals -> 1 plan", 120_000, 1, 1),
        (25.80, "implementing", "Edit checkout_tax.py", 500_000, 3, 1),
        (36.00, "testing", "Run `pytest -q`", 850_000, 5, 1),
        (39.80, "reviewing", "independent review", 1_150_000, 6, 1),
        # The bounce: review FAILED, a finding to fix, attempt 2 begins.
        (44.00, "implementing", "fix review findings", 1_300_000, 8, 2),
        (49.00, "testing", "Run `pytest -q`", 1_500_000, 9, 2),
        (51.60, "reviewing", "independent review", 1_650_000, 10, 2),
        (55.40, "awaiting_approval", None, 1_900_000, 10, 2),
    ),
}

RUNNING = ("pending", "context", "planning", "implementing", "testing",
           "reviewing")


def stage_at(key: str, t: float):
    """(status, live, burn, turns, attempt) for ``key`` at ``t``, or None."""
    current = None
    for start, status, live, burn, turns, attempt in STAGES[key]:
        if t >= start:
            current = (status, live, burn, turns, attempt)
    return current


def approved_at(key: str, t: float) -> str | None:
    if key == HERO_KEY and t >= APPROVE_AT:
        return NOW
    return None


def _revision(key: str, t: float) -> int:
    return (sum(1 for start, *_ in STAGES[key] if t >= start)
            + (1 if approved_at(key, t) else 0))


def updated_at(key: str, t: float) -> str:
    """Moves on every status change — Board.jsx re-fetches an open drawer only
    when the row's `updated_at` advances on a WS frame, exactly as fixture.py
    documents. Seconds only, inside NOW's minute."""
    return f"2026-08-03T09:14:{_revision(key, t):02d}+00:00"


# --------------------------------------------------------------------------- #
# The board                                                                    #
# --------------------------------------------------------------------------- #

#: Yesterday's leftover PR — the one card on the board before the sprint
#: lands. The hook beat opens here: a demo that opens on a stone-empty board
#: shows nothing, and a single stale PR in Review is the most recognisable
#: quiet state a working repo has.
BACKDROP_ID = "a9c3e5f7b1d24680ac8e0f2a4c6e8f0a"
BACKDROP = (
    fx._row(id=BACKDROP_ID, title="PLAT-948: Cap request body size at 2 MB",
            source="jira", external_id="PLAT-948", status="awaiting_approval",
            lane="review", kind="feature", repo_name=REPO_NAME,
            created_at=YESTERDAY, updated_at=YESTERDAY, last_activity=YESTERDAY,
            pr_url="https://github.com/you/shop/pull/309", attempt_count=1,
            last_turns=5, description_short="reject oversize uploads early",
            total_tokens=310_000, total_cache_read=3_600_000),
)


def _lane(status: str) -> str:
    if status == "done":
        return "done"
    if status == "awaiting_approval":
        return "review"
    if status in ("awaiting_input", "escalated"):
        return "answer"
    return "working"


def row_at(key: str, t: float) -> dict | None:
    """The ticket as `GET /api/tasks` would list it at ``t``."""
    stage = stage_at(key, t)
    if stage is None:
        return None
    status, live, burn, turns, attempt = stage
    running = status in RUNNING
    stamp = updated_at(key, t)
    return fx._row(
        id=IDS[key], title=title_of(key), status=status, lane=_lane(status),
        source="jira", external_id=key, kind=kind_of(key),
        repo_name=REPO_NAME, created_at=NOW, updated_at=stamp,
        last_activity=stamp, description_short=DESCRIPTIONS[key],
        claimed=running, live_status=live if running else None,
        attempt_count=attempt, last_turns=turns or None, has_spec=True,
        total_tokens=(burn // 8) or None,
        total_cache_read=(burn - burn // 8) or None,
        pr_url=PR_URLS[key] if status == "awaiting_approval" else None,
        approved_at=approved_at(key, t),
    )


#: When yesterday's leftover PR gets merged and leaves the Review lane.
#:
#: It has to leave. The payoff line is "five pull requests", and a Review lane
#: still holding the backdrop makes the lane badge read 6 while the voice says
#: five — the viewer can count, and one of the two is then wrong. Merging it is
#: also the truthful story: it is yesterday's PR, and a human merged it this
#: morning while the agents worked. `done` is an OUTCOME lane (boardLanes.js),
#: so the card leaves the columns and lands in the Done tally rather than
#: reappearing in Working.
#:
#: Timed behind the open drawer (sprint_gui.OPEN_DRAWER_AT = 24.00) so the
#: card does not visibly wink out of a lane the viewer is looking at.
BACKDROP_DONE_AT = 25.00


def backdrop_at(t: float) -> list[dict]:
    """The leftover PR as of ``t``: in Review until it is merged, Done after."""
    if t < BACKDROP_DONE_AT:
        return [dict(r) for r in BACKDROP]
    return [dict(r, status="done", lane="done", approved_at=NOW,
                 updated_at=NOW, last_activity=NOW) for r in BACKDROP]


def board_at(t: float) -> list[dict]:
    """`GET /api/tasks` at ``t`` — chosen tickets in creation order, then the
    backdrop. Lane placement and in-lane order are the board's own logic."""
    rows = [row_at(key, t) for key in CHOSEN]
    return [r for r in rows if r] + backdrop_at(t)


def inflight_at(t: float) -> int:
    """How many of the five are actually running — the worker widget's number,
    and a small honesty check: it must hit 5 during the parallel beat."""
    n = 0
    for key in CHOSEN:
        stage = stage_at(key, t)
        if stage and stage[0] in RUNNING and stage[0] != "pending":
            n += 1
    return n


def queued_at(t: float) -> int:
    """How many of the five are created but not yet claimed."""
    n = 0
    for key in CHOSEN:
        stage = stage_at(key, t)
        if stage and stage[0] == "pending":
            n += 1
    return n


def queue_health_at(t: float) -> dict:
    """`GET /api/queue/health`, in the flat shape `drainChip.js` documents
    ({workers_busy, max_workers, queue_depth, est_drain_seconds}). The first
    sprint capture served gui_frames' `{"ok": true, "depth": 0}` stub here and
    the header chip read "0/0 workers busy" for the whole clip — over a
    narration saying five agents are running. The chip is on camera in every
    wide shot; it has to tell the fixture's truth.

    While anything queues, an ETA is served rather than None: drainChip
    renders a null estimate as the words "no estimate", which over the whole
    handoff beat reads as a broken widget rather than an honest one."""
    queued = queued_at(t)
    return {"workers_busy": inflight_at(t), "max_workers": 5,
            "queue_depth": queued,
            "est_drain_seconds": 120 if queued else None}


# --------------------------------------------------------------------------- #
# The hero's evidence: plan, diff, review — same bar as fixture.py             #
# --------------------------------------------------------------------------- #

HERO_CRITERIA = [
    "a replayed delivery charges once",
    "replays still return HTTP 200",
    "a regression test covers the 502 retry",
]

#: `git diff` bytes, as `GET /api/tasks/{id}/diff` serves them (plain text).
#: Two files; every review citation below is a line of THIS diff, and the
#: tests re-derive the new-file line numbers rather than trusting these
#: comments. No line exceeds fixture.DIFF_PANE_COLS, so the shell's `/diff`
#: renders it without wrapping a line of code in half.
DIFF = """diff --git a/webhooks.py b/webhooks.py
index 3f9c2d1..b7e4a90 100644
--- a/webhooks.py
+++ b/webhooks.py
@@ -12,3 +12,7 @@ def handle(event):
     charge = parse(event)
+    if Delivery.seen(event.delivery_id):
+        log.info("replay dropped")
+        return Response(status=200)
     apply(charge)
+    Delivery.record(event.delivery_id)
     return Response(status=200)
diff --git a/test_webhooks.py b/test_webhooks.py
index 8d2e4f6..1a5b9c3 100644
--- a/test_webhooks.py
+++ b/test_webhooks.py
@@ -30 +30,8 @@ def test_500_is_retried():
     assert Charge.count() == 1
+
+
+def test_replay_502_charges_once():
+    event = deliver(status=502)
+    deliver_again(event)
+    assert Charge.count() == 1
+    assert Delivery.seen(event.delivery_id)
"""

#: A PASS with one non-blocking finding — the severity-class design, and the
#: only checklist shape that renders cited evidence (SlideOver.jsx prints
#: `.ci-evidence` only for `passed: false` items).
REVIEW_CHECKLIST = {
    "passed": True,
    "items": [
        {"label": "a replayed delivery charges once", "passed": True,
         "evidence": "test_webhooks.py:36 — assert Charge.count() == 1",
         "file": "test_webhooks.py", "line": 36},
        {"label": "replays still return HTTP 200", "passed": True,
         "evidence": "webhooks.py:15 — return Response(status=200)",
         "file": "webhooks.py", "line": 15},
        {"label": "a regression test covers the 502 retry", "passed": True,
         "evidence": "test_webhooks.py:33 — test_replay_502_charges_once",
         "file": "test_webhooks.py", "line": 33},
        {"label": "the replay log names the delivery id", "passed": False,
         "severity": "nit", "comment": "log the id when dropping a replay",
         "evidence": "webhooks.py:14 — the log line carries no id",
         "file": "webhooks.py", "line": 14},
    ],
}

#: `task.context["spec"]` — the planner's output, `TaskSpec.as_dict()` keys
#: exactly. Deliberately the plan for THIS diff: the two files it names are
#: the two files the diff touches, and the narrated GATE beat puts the two
#: side by side.
SPEC = {
    "files_to_change": ["webhooks.py", "test_webhooks.py"],
    "approach": (
        "Record each delivery id once its charge is applied. On a repeated "
        "id, skip the charge and still return 200 — the gateway retries any "
        "non-2xx response, which is exactly how one 502 became two charges."
    ),
    "test_plan": (
        "One regression test in test_webhooks.py: deliver a 502, replay the "
        "same delivery, assert a single charge and the id recorded."
    ),
    "out_of_scope": [
        "a general idempotency store",
        "changing the gateway's retry policy",
    ],
    "verification": "pytest -q test_webhooks.py",
}

TEST_RESULTS = {
    "ran": True, "ok": True, "passed": 5, "failed": 0, "errors": 0,
    "total": 5, "tamper_flag": False,
    "output": ("collected 5 items\n\ntest_webhooks.py .....              "
               "[100%]\n\n5 passed in 1.72s"),
}

BRANCH = f"no-human/{HERO_ID[:8]}"

#: The stage statuses the agent announces as a `state` frame on its event
#: stream. `pending` is the queue, not the agent, and nothing emits it;
#: `awaiting_approval` is the orchestrator parking a finished attempt, and the
#: frame that says so on the stream is `pr_open`, not a state change. Both
#: exemptions are asserted rather than assumed — see
#: `test_narrated_demo.test_the_trail_never_contradicts_the_card_behind_it`.
STREAMED_STATES = ("context", "planning", "implementing", "testing",
                   "reviewing")

#: When the worker picks the hero up: the first stage that is not `pending`.
#: Everything in the trail is measured from a stage of `STAGES[HERO_KEY]`, so
#: this is derived and not written down twice.
RUN_START = next(start for start, status, *_ in STAGES[HERO_KEY]
                 if status != "pending")

#: The stage starts of the hero, by status. The hero's pipeline visits every
#: status once (it does not bounce — ORD-2187 is the one that does), which is
#: what makes a status a usable anchor here; `test_the_hero_visits_each_stage
#: _once` pins that so a future re-author cannot quietly make it ambiguous.
_HERO_STAGE_START: dict[str, float] = {
    status: start for start, status, *_ in STAGES[HERO_KEY]}

#: The two frames the worker emits as it CLAIMS the task — the classifier's
#: verdict and the model line — measured BACK from `RUN_START`, because that is
#: what they mean: they land in the last moment of the queue, just before the
#: first state change. (`fixture.py`'s 26 s trail has the same kind -> models ->
#: state order.)
_CLAIM_FRAMES: tuple[tuple[float, dict], ...] = (
    (-1.10, fx._ev("kind", "task kind: bugfix", task_kind="bugfix")),
    (-0.80, fx._ev("models", fx.MODELS_TEXT, models=dict(fx.MODELS))),
)

#: Everything the agent says INSIDE a stage: (status, seconds into that stage,
#: frame). Offsets, not absolute seconds — that is the whole fix.
#:
#: WHAT WAS WRONG, so the shape is not undone by the next re-author. These times
#: were absolute, and they were written against an OLDER `STAGES` and never
#: retimed when it moved. Two contradictions shipped: `state context` sat at
#: 17.90 while the card had been reading "gathering context" since 16.40, and
#: there was NO `state testing` frame at all — so from 33.00 to 38.45 the card's
#: chip read "Run `pytest -q`" while the agent board behind it still said
#: `implementing`. The drawer shows both at once; a viewer reads both.
#:
#: Now every frame hangs off `STAGES[HERO_KEY]`: the `state` frames ARE the
#: stage starts (generated below, one per `STREAMED_STATES` entry, so one can
#: neither be forgotten nor drift), and every other frame is an offset into the
#: stage it belongs to. Move a stage and the whole trail moves with it.
#:
#: Every frame clears the same serialiser allow-list `fixture.py` documents
#: (sources: orchestrator / agent / planner:* / aggregator; the reviewer's
#: verdict is structurally absent — it lives in `review_checklist`, which is
#: where the board reads it). The trail ENDS inside `reviewing`, before the
#: shell types `/diff`: a live frame after that scrolls the diff off camera.
_STAGE_FRAMES: tuple[tuple[str, float, dict], ...] = (
    ("planning", 0.40, fx._ev("tool_use", "Read webhooks.py",
                              "planner:test-first")),
    ("planning", 0.80, fx._ev("tool_use", 'Grep "delivery_id"',
                              "planner:risk")),
    ("planning", 1.40, fx._ev("subagent_start", "map the webhook retry path",
                              "planner:test-first", task_id="sub-plan-1",
                              task_type="general-purpose")),
    ("planning", 2.00, fx._ev("subagent_done", "gateway retries any non-2xx",
                              "planner:test-first", task_id="sub-plan-1",
                              status="completed")),
    ("planning", 2.15, fx._ev("agent_text", "one plan from three proposals",
                              "aggregator")),
    # The coder's model line, re-announced as the hand-over happens — last
    # frame of `planning`, immediately before `state implementing`.
    ("planning", 2.35, fx._ev("models", fx.MODELS_TEXT,
                              models=dict(fx.MODELS))),
    ("implementing", 0.80, fx._ev("tool_use", "Read webhooks.py", "agent")),
    ("implementing", 1.60, fx._ev("tool_use", "Edit webhooks.py", "agent")),
    ("implementing", 5.00, fx._ev("subagent_start",
                                  "find every caller of handle()", "agent",
                                  task_id="sub-code-1",
                                  task_type="general-purpose")),
    ("implementing", 6.20, fx._ev("subagent_done", "3 callers, none re-enter",
                                  "agent", task_id="sub-code-1",
                                  status="completed")),
    ("implementing", 8.00, fx._ev("tool_use", "Edit test_webhooks.py",
                                  "agent")),
    ("implementing", 10.00, fx._ev("supervisor_decision", "continue - on plan")),
    ("testing", 0.10, fx._ev("tool_use", "Run `pytest -q`", "agent")),
    ("testing", 0.90, fx._ev("tests", "5 passed in 1.72s")),
    ("reviewing", 1.20, fx._ev("tamper", "none - 61 assertions, +2")),
    ("reviewing", 2.40, fx._ev("lint", "clean")),
    ("reviewing", 3.20, fx._ev("commit", f"{BRANCH} 2 files")),
    ("reviewing", 4.00, fx._ev("pr_open", PR_URLS[HERO_KEY])),
)


def _build_trail(stage_start: dict[str, float] | None = None,
                 run_start: float | None = None
                 ) -> tuple[tuple[float, dict], ...]:
    """The hero's trail, in seconds, derived from `STAGES[HERO_KEY]`.

    Sorted by time; a `state` frame wins a tie, so a stage frame written at
    offset 0.00 lands AFTER the state change it belongs to rather than before
    it. Python's sort is stable, so everything else keeps the order it is
    written in above.

    The two anchors are ARGUMENTS with the module's own values as defaults,
    only so a test can feed it a shifted stage table and prove the trail moves
    with it. Nothing in the recorders passes them.
    """
    starts = _HERO_STAGE_START if stage_start is None else stage_start
    zero = RUN_START if run_start is None else run_start
    plan: list[tuple[float, dict]] = [
        (round(zero + offset, 2), frame) for offset, frame in _CLAIM_FRAMES]
    plan += [(starts[status], fx._ev("state", status))
             for status in STREAMED_STATES]
    plan += [(round(starts[status] + offset, 2), frame)
             for status, offset, frame in _STAGE_FRAMES]
    plan.sort(key=lambda row: (row[0], row[1]["kind"] != "state"))
    return tuple(plan)


_TRAIL: tuple[tuple[float, dict], ...] = _build_trail()

EVENT_TRAIL: tuple[tuple[float, dict], ...] = tuple(
    (due, dict(frame, ts=round(due, 2))) for due, frame in _TRAIL
)

#: The first frame the stream carries. Anything that opens the stream LATER
#: gets every frame already due delivered in one captured frame — a dump, not a
#: stream — so the recorders derive their "start following" time from this.
TRAIL_START = EVENT_TRAIL[0][0]


def events_at(t: float) -> list[dict]:
    return [dict(frame) for due, frame in EVENT_TRAIL if t >= due]


def trail_state_at(t: float) -> str | None:
    """The state the EVENT STREAM claims at ``t`` — the last `state` frame at
    or before it, or None before the first one.

    This is what the drawer's System board and the shell's detail pane render.
    The card's chip beside them renders `stage_at`. They are two readouts of
    one quantity and the tests hold them to being equal.
    """
    state = None
    for due, frame in EVENT_TRAIL:
        if due > t:
            break
        if frame["kind"] == "state":
            state = frame["text"]
    return state


def hero_detail(t: float) -> dict:
    """`GET /api/tasks/{hero}` — TaskOut with the attempt the drawer reads."""
    stage = stage_at(HERO_KEY, t) or ("pending", "queued", 0, 0, 0)
    status, _live, burn, turns, attempt = stage
    reviewed = status == "awaiting_approval"
    attempt_row = {
        "id": "att-1", "attempt_number": max(attempt, 1),
        "branch_name": BRANCH, "commit_sha": "b7e4a901c2d3",
        "pr_url": PR_URLS[HERO_KEY] if reviewed else None,
        "review_passed": 1 if reviewed else None,
        "review_checklist": REVIEW_CHECKLIST if reviewed else None,
        "test_results": TEST_RESULTS if reviewed else None,
        "ci_pipeline_id": None, "ci_pipeline_url": None, "ci_status": None,
        "failure_reason": None, "turns_used": turns or None,
        "tokens_used": burn // 8, "cache_read_tokens": burn - burn // 8,
        "cache_creation_tokens": 0,
        "status": "succeeded" if reviewed else "running",
        "started_at": NOW, "completed_at": NOW if reviewed else None,
    }
    ctx: dict = {}
    if status not in ("pending", "context", "planning"):
        ctx["spec"] = dict(SPEC)
    if approved_at(HERO_KEY, t):
        ctx["approved_at"] = approved_at(HERO_KEY, t)
    return {
        "id": HERO_ID, "claimed": not reviewed, "external_id": HERO_KEY,
        "source": "jira", "title": title_of(HERO_KEY),
        "description": DESCRIPTIONS[HERO_KEY], "status": status,
        "kind": kind_of(HERO_KEY), "priority": "high",
        "acceptance_criteria": list(HERO_CRITERIA), "repo_path": REPO_PATH,
        "blocker": None, "context": ctx, "parent_id": None,
        "created_at": NOW, "updated_at": updated_at(HERO_KEY, t),
        "attempts": [attempt_row] if turns else [],
        "total_tokens": burn // 8, "total_cache_read": burn - burn // 8,
        "total_cache_creation": None,
        "total_review_tokens": 240_000 if reviewed else None,
        "total_review_cache_creation": None,
        "total_review_cache_read": 2_100_000 if reviewed else None,
        "total_aux_tokens": None, "total_aux_cache_read": None,
        "total_aux_cache_creation": None, "wall_seconds": 388.0,
        "attempt_count": max(attempt, 1) if turns else 0,
    }


# --------------------------------------------------------------------------- #
# Sanity anchors against the narration (tested, not just documented)           #
# --------------------------------------------------------------------------- #

def _beat(name: str) -> float:
    for beat in nr.BEATS:
        if beat.name == name:
            return beat.start
    raise KeyError(name)


HANDOFF, PARALLEL, GATE, PAYOFF, CLOSE = (
    _beat("handoff"), _beat("parallel"), _beat("gate"), _beat("payoff"),
    _beat("close"))
