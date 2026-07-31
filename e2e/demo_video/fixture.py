"""The one world both demo videos record.

Two surfaces — the React board and the `nh` conversational shell — have to show
the SAME ticket moving through the SAME five moments, or a side-by-side reads
as two unrelated clips. Everything that could drift between them lives here:
the task id, the title, the pipeline stages, the burn numbers, the plan, the
diff, the review checklist and the event trail. The board harness serves these
dicts over a stubbed HTTP API; the shell harness hands the same dicts to a fake
``NhClient``. Neither invents a payload of its own.

Payload shapes are the server's, not this module's invention:

* board rows are ``api/models.py:TaskSummaryOut``
* the drawer row is ``TaskOut`` with ``attempts: [AttemptOut]``
* shell event frames are what ``api/app.py:task_events_stream`` yields, AFTER
  its source/kind filter — which is why no reviewer-sourced frame appears here
  (see ``EVENT_TRAIL``)
* grill frames are what ``api/app.py:grill_stream`` yields

Nothing here calls an LLM, opens a database or starts a scheduler. A demo that
cannot be re-recorded is the defect this replaces.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# The ticket                                                                   #
# --------------------------------------------------------------------------- #

#: Fixed, so the two videos show the same eight characters. The board prints
#: `task.id.slice(0, 8)` (Board.jsx `card-id`); the shell prints `id[:8]`
#: (shell_lanes.py `_short`). This IS the synchronising device between the two
#: panes — see camera.py for why it had to be the id and not a clock.
HERO_ID = "4f2a9c31d8e04b6a9c17e2b5a3d90f84"
HERO_SHORT = HERO_ID[:8]

#: What the human types, verbatim, in both surfaces. 33 chars — chosen so the
#: request reads as one line, no longer a hard truncation budget: the
#: conversation pane WRAPS now (shell.py `min_width=0`; the 40-column cut this
#: budget was working around is fixed — see camera.py NOTES).
HERO_PROMPT = "add mul(a, b) to calc.py + a test"
HERO_TITLE = "Add mul(a, b) to calc.py"
HERO_DESC = "multiply two numbers, with a test"
HERO_CRITERIA = [
    "mul(a, b) returns a * b",
    "a test covers mul()",
    "non-numbers raise TypeError",
]

REPO_PATH = "/Users/you/git/calc"
REPO_NAME = "calc"
BRANCH = f"no-human/{HERO_SHORT}"
PR_URL = f"https://github.com/you/calc/pull/128"

# A fixed wall clock. Nothing in either surface renders sub-minute elapsed time
# (Board.jsx `relativeTime` floors to minutes; the shell has no clock at all),
# so these only have to be stable, not live.
NOW = "2026-07-30T09:41:02+00:00"
T_MINUS_9M = "2026-07-30T09:32:02+00:00"
T_MINUS_21M = "2026-07-30T09:20:02+00:00"
#: The browser's Date.now() is pinned here (gui_frames installs it), 40 s after
#: NOW, so the hero card's age reads "<1m" and the backdrop reads 9m / 21m on
#: every run. Without a pinned clock the ages drift with wall time and two
#: recordings of the same fixture differ.
BROWSER_NOW = "2026-07-30T09:41:42+00:00"


# --------------------------------------------------------------------------- #
# Pipeline stages — the quantity that advances in lockstep on both surfaces    #
# --------------------------------------------------------------------------- #

#: (start_seconds, status, live_status, burn_tokens, turns).
#: `status` drives the board's progress bar (taskProgress: context 15 →
#: implementing 55 → testing 88 → reviewing 75 → awaiting_approval 96) and the
#: shell's lane row detail text. Same field, two renderings.
#:
#: RETIMED for the five-beat cut. The old grid ran the whole pipeline inside
#: beat 2 and parked on awaiting_approval for the remaining 10 s, because only
#: one beat needed a running task. Four of the five beats now watch a DIFFERENT
#: surface of the same task, and three of them are only honest while the task is
#: at a particular point:
#:
#:   beat 2  the plan     `context.spec` exists only after planning, and
#:                        SpecTab prints "Spec not generated yet" for
#:                        pending/context/planning — so the task has to be
#:                        IMPLEMENTING before the light lands on it.
#:   beat 3  the agents   the System view's live dots and its "Running — …"
#:                        banner need `isActive && claimed`, i.e. a status in
#:                        (pending, context, planning, implementing, testing,
#:                        reviewing). Parked on awaiting_approval every node
#:                        clamps to "done" and nothing is running.
#:   beat 4  the diff     `/diff` serves the last attempt's commit, so the
#:                        coder has to have committed: testing onwards.
#:   beat 5  the review   `review_checklist` only exists at awaiting_approval.
STAGES: tuple[tuple[float, str, str | None, int, int], ...] = (
    (4.90, "pending", "queued", 0, 0),
    (5.06, "context", "gathering context", 40_000, 1),
    (5.22, "planning", "3 proposals -> 1 plan", 96_000, 1),
    # Lands mid-drift as beat 2's spotlight travels to the Spec section, so the
    # plan is already on screen when the light gets there.
    (5.58, "implementing", "Edit calc/calc.py", 380_000, 3),
    (12.06, "testing", "Run `pytest -q`", 812_000, 6),
    (12.86, "reviewing", "independent review", 1_240_000, 7),
    # The pipeline finishes 0.3 s before beat 3 ends, so the beat about the
    # agents ends on all five stages going green and the board's banner turning
    # to "Waiting for you". It also has to be here rather than later for a
    # reason that is about the SHELL: the shell has ONE detail pane, the live
    # event stream writes into it, and `RichLog` auto-scrolls to the newest
    # line. An event arriving after `/diff` would push the diff off camera,
    # which is beat 4. The run being over by 14.10 is what leaves that pane
    # still and readable for the two beats that need it.
    (14.10, "awaiting_approval", None, 1_612_000, 7),
)

#: When the human's approval lands (both surfaces). Inside beat 5 (19.00-25.00)
#: with enough room after it for the confirmation to be read before the fade.
APPROVE_AT = 22.60


def stage_at(t: float) -> tuple[str, str | None, int, int] | None:
    """The hero's (status, live_status, burn, turns) at time ``t``, or None
    before it exists. A single definition so the two harnesses cannot skew."""
    current = None
    for start, status, live, burn, turns in STAGES:
        if t >= start:
            current = (status, live, burn, turns)
    return current


def approved_at(t: float) -> str | None:
    return NOW if t >= APPROVE_AT else None


def _revision(t: float) -> int:
    """How many times the hero row has changed by time ``t``."""
    return (sum(1 for start, *_ in STAGES if t >= start)
            + (1 if approved_at(t) else 0))


def updated_at(t: float) -> str:
    """The hero's `updated_at`, which MOVES.

    It was pinned to :data:`NOW` for the whole clip, and that was invisible
    while the drawer only ever opened on a finished task. It is not invisible
    now: `Board.jsx` re-fetches the open drawer only when the selected task's
    `updated_at` changes on a WebSocket frame (`prevUpdatedAtRef` ->
    `setRefreshKey`), so a frozen stamp means a drawer frozen on whatever the
    task looked like the instant it was opened — which for this cut is
    `status: "context"`, four beats of "Spec not generated yet", and no
    Approve button ever appearing. The real server bumps the column on every
    status change; so does this.

    Seconds, inside the same minute as :data:`NOW`, because `relativeTime`
    floors to whole minutes and the card must keep reading "<1m".
    """
    return f"2026-07-30T09:41:{2 + _revision(t):02d}+00:00"


# --------------------------------------------------------------------------- #
# Review evidence (the board's beat 3)                                         #
# --------------------------------------------------------------------------- #

#: The change, as `GET /api/tasks/{id}/diff` serves it: `git diff <sha>` output,
#: plain text (the endpoint is a ``PlainTextResponse`` and the board's
#: ``fetchDiff`` reads ``r.text()`` — a JSON body renders as JSON on camera).
#:
#: Every line number the review cites below is READ OFF THIS DIFF, because beat
#: 4 and beat 5 put the two side by side and a viewer can check them. Applying
#: the hunks gives:
#:
#:   calc.py           6  \"\"\"Multiply two numbers.\"\"\"   <- the nit
#:                     9              raise TypeError("numbers only")
#:                    10      return a * b
#:   test_calc.py     14      assert mul(3, 4) == 12
#:                    18      with pytest.raises(TypeError):
#:
#: Two files, because the trail commits "2 files", the tests report 3 passed
#: (test_add + the two below) and the checklist cites a test — a one-file diff
#: on camera contradicts all three. No body line exceeds 47 characters, so the
#: shell's detail pane renders the whole diff without wrapping a line of code
#: in half (:data:`DIFF_PANE_COLS`).
DIFF = """diff --git a/calc.py b/calc.py
index 8f1a2b3..c4d5e6f 100644
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,10 @@
 def add(a, b):
     return a + b
+
+
+def mul(a, b):
+    \"\"\"Multiply two numbers.\"\"\"
+    for x in (a, b):
+        if not isinstance(x, (int, float)):
+            raise TypeError("numbers only")
+    return a * b
diff --git a/test_calc.py b/test_calc.py
index 1c2d3e4..9a8b7c6 100644
--- a/test_calc.py
+++ b/test_calc.py
@@ -10 +10,10 @@ def test_add():
     assert add(2, 2) == 4
+
+
+def test_mul():
+    assert mul(3, 4) == 12
+
+
+def test_mul_rejects_strings():
+    with pytest.raises(TypeError):
+        mul("3", 4)
"""

#: Usable columns in the shell's detail pane at a 100-column terminal — the
#: same measurement as the conversation pane (both RichLogs get region 55, less
#: the round border and `padding: 0 1`, less a column for the scrollbar the
#: diff always causes). Every DIFF line has to clear it.
DIFF_PANE_COLS = 50


#: A PASS with non-blocking findings — the product's actual severity-class
#: design, not a green-tick fantasy. It also happens to be the only shape that
#: renders cited evidence: SlideOver.jsx only prints `.ci-evidence` for an item
#: with `passed: false`, so an all-green checklist shows labels and nothing to
#: read. The verdict line reads "PASSED — 1 non-blocking finding (1 nit)".
#:
#: Every `file`/`line` here is a line of :data:`DIFF`. They used to be neither:
#: the checklist cited `calc.py:6 — return a * b` while the diff put that
#: statement on line 9 and never showed test_calc.py at all. Nothing caught it
#: because no beat had ever put the diff on camera.
REVIEW_CHECKLIST = {
    "passed": True,
    "items": [
        {"label": "mul(a, b) returns a * b", "passed": True,
         "evidence": "calc.py:10 — return a * b", "file": "calc.py", "line": 10},
        {"label": "a test covers mul()", "passed": True,
         "evidence": "test_calc.py:14 — assert mul(3, 4) == 12",
         "file": "test_calc.py", "line": 14},
        {"label": "non-numbers raise TypeError", "passed": True,
         "evidence": "calc.py:9 raises; test_calc.py:18 asserts it",
         "file": "calc.py", "line": 9},
        {"label": "docstring does not mention the TypeError", "passed": False,
         "severity": "nit", "comment": "name the raise in the docstring",
         "evidence": "calc.py:6 — the docstring omits the raise",
         "file": "calc.py", "line": 6},
    ],
}

#: The PLAN — `task.context["spec"]`, which the orchestrator writes once the
#: planner has run (`orchestrator.py:3690`, `ctx["spec"] = spec.as_dict()`).
#: Beat 2 is this object rendered by `SlideOver.jsx:SpecTab`, so the keys are
#: exactly `core/task.py:TaskSpec.as_dict()` — five, no more, no fewer; an
#: invented key renders nothing and a missing one collapses a section.
#:
#: It is deliberately the plan for THIS diff: the two files it names are the two
#: files :data:`DIFF` touches, the test plan is the two tests that diff adds,
#: and `verification` is the command whose output :data:`TEST_RESULTS` reports.
#: Beat 2 and beat 4 are four seconds apart on camera.
SPEC = {
    "files_to_change": ["calc.py", "test_calc.py"],
    "approach": (
        "Add `mul(a, b)` next to `add` in calc.py. Reject non-numeric "
        "arguments up front with `TypeError` rather than letting `*` fall "
        "through to sequence repetition, which is the surprising behaviour "
        "the ticket's third criterion is about."
    ),
    "test_plan": (
        "Two tests in test_calc.py: `test_mul` for the product, "
        "`test_mul_rejects_strings` for the `TypeError`. No new fixtures."
    ),
    "out_of_scope": [
        "changing add()",
        "a general numeric-coercion helper",
    ],
    "verification": "pytest -q test_calc.py",
}

TEST_RESULTS = {
    "ran": True, "ok": True, "passed": 3, "failed": 0, "errors": 0,
    "total": 3, "tamper_flag": False,
    "output": "collected 3 items\n\ntest_calc.py ...                    [100%]\n\n3 passed in 0.41s",
}


# --------------------------------------------------------------------------- #
# The shell's event trail (the shell's beat 3)                                 #
# --------------------------------------------------------------------------- #

#: Which model holds which role. `orchestrator.py:_active_models` reads these
#: off the live objects, so the four keys are fixed: coder / planner / reviewer
#: / supervisor. The values are the project's four fixed model tiers.
MODELS = {
    "coder": "claude-sonnet-5",
    "planner": "claude-opus-5",
    "reviewer": "claude-opus-5",
    "supervisor": "claude-sonnet-5",
}
AUTH_PROFILE = "personal"

#: The `models` event's own text, built the way `_emit_models` builds it.
MODELS_TEXT = " · ".join(f"{r}={m}" for r, m in MODELS.items()) + \
    f" · auth={AUTH_PROFILE}"


def _ev(kind: str, text: str, source: str = "orchestrator", **extra) -> dict:
    """One serialised event frame, in the shape BOTH readers receive it.

    `api/app.py:_format_events` (the REST log the board seeds from and the
    shell's `/logs` replays) and `task_events_stream` (the live SSE the System
    view and the shell's follower read) emit the same four keys plus a small,
    kind-specific tail. `ts` is filled in by :data:`EVENT_TRAIL` from the
    frame's own due time so the two harnesses cannot disagree about ordering.
    """
    return {"kind": kind, "text": text, "source": source, **extra}


#: (due_seconds, frame) — the task's event log, released a frame at a time.
#:
#: THIS IS WHAT BEAT 3 IS. `SlideOver.jsx:SystemTab` derives the whole agent
#: board from this list and nothing else: `eventRoles.js:eventSource` maps each
#: frame to a node, `modelsByNode` reads the `models` frames for the per-lane
#: model label, `discoverSubagents` finds the children, and
#: `functionalities.js:groupFunctionalities` groups them into the five stages.
#: A role with no frame here is a lane that reads "not started yet".
#:
#: Every `source` below is one the shipped product actually stamps, and every
#: frame clears the allow-list both serialisers share:
#:
#:   "orchestrator"        `Orchestrator.emit` — passes on source alone.
#:   "agent"               CODER_ROLE (orchestrator.py:107). NOT "coder": that
#:                         was this fixture's own invention, it fails
#:                         `is_agent_session`, and every one of the old trail's
#:                         tool_use frames would have been DROPPED by the real
#:                         `_format_events` before reaching either surface.
#:   "planner:<lens>"      an MoA proposer (`source.startswith(PLANNER_ROLE)`).
#:   "aggregator"          the synthesis step. Folded onto the Planner node.
#:
#: The Supervisor and the Reviewer appear WITHOUT a source of their own, and
#: that is not a shortcut: the orchestrator emits `supervisor_decision` and
#: `tamper` itself (orchestrator.py:5719, 2595) with source "orchestrator", and
#: `eventRoles.js:ORCHESTRATOR_EMITS_FOR` re-attributes exactly those kinds to
#: the Supervisor and the Reviewer. That mapping exists because the backend
#: does this.
#:
#: NOTE — what is STILL not here. The reviewer's own verdict. `_emit_review`
#: stamps `source: "reviewer"`, which is not in either allow-list, so
#: `review PASS` is dropped before it is serialised (the KNOWN BUG comment at
#: `api/app.py:_format_events` has the measured 4-in/1-out). Scripting one here
#: would put a line on camera the shipped product cannot print. The Reviewer
#: node therefore lights up from its tamper check, and the verdict itself is
#: shown where it IS real: the board's Review section, which reads the
#: attempt's `review_checklist` off the task row, not off this stream.
_TRAIL: tuple[tuple[float, dict], ...] = (
    # ---- beat 1's tail: the task exists, the roles are named ---------------
    (4.98, _ev("kind", "task kind: feature", task_kind="feature")),
    (5.14, _ev("models", MODELS_TEXT, models=dict(MODELS))),
    (5.30, _ev("state", "context")),
    # ---- beat 2 (5.20-9.80): it plans before it codes ----------------------
    (5.62, _ev("state", "planning")),
    (5.94, _ev("tool_use", "Read calc/calc.py", "planner:test-first")),
    (6.30, _ev("tool_use", 'Grep "def mul" in calc/', "planner:risk")),
    (6.66, _ev("subagent_start", "survey the test conventions",
               "planner:test-first", task_id="sub-plan-1",
               task_type="general-purpose")),
    (7.28, _ev("subagent_done", "3 conventions, 1 fixture",
               "planner:test-first", task_id="sub-plan-1", status="completed")),
    (7.72, _ev("agent_text", "one plan from three proposals", "aggregator")),
    # The attempt begins: `_emit_models` runs again, once per attempt
    # (orchestrator.py:1749). It lands here so it is still the top line of the
    # shell's 8-row detail pane when beat 3 opens on it.
    (8.10, _ev("models", MODELS_TEXT, models=dict(MODELS))),
    (8.46, _ev("state", "implementing")),
    (8.92, _ev("tool_use", "Read calc/calc.py", "agent")),
    (9.44, _ev("tool_use", "Edit calc/calc.py", "agent")),
    # ---- beat 3 (9.80-14.40): the agents, running -------------------------
    # Eleven frames in four seconds. That is the one beat where a burst is the
    # subject, and it is the BOARD that has to stay readable through it — the
    # System view's lanes and counts, not the shell's scrolling pane.
    (10.06, _ev("supervisor_decision", "continue - on plan")),
    (10.62, _ev("subagent_start", "find every call site of add()", "agent",
                task_id="sub-code-1", task_type="general-purpose")),
    (11.18, _ev("tool_use", "Edit calc/test_calc.py", "agent")),
    (11.62, _ev("subagent_done", "2 call sites, both safe", "agent",
                task_id="sub-code-1", status="completed")),
    (12.06, _ev("tool_use", "Run `pytest -q`", "agent")),
    (12.46, _ev("tests", "3 passed in 0.41s")),
    (12.86, _ev("state", "reviewing")),
    # The Reviewer node's only frame — and it is a real one. See the note above
    # on why its verdict cannot be here.
    (13.22, _ev("tamper", "none - 228 assertions, +6")),
    (13.58, _ev("lint", "clean")),
    (13.78, _ev("commit", f"{BRANCH} 2 files")),
    (13.96, _ev("pr_open", PR_URL)),
    # Nothing after 13.96. Beats 4 and 5 are the shell reading its own detail
    # pane (`/diff`, then `/logs`), and a live frame arriving into it would
    # scroll whichever of the two is on camera off the top.
)

#: The trail with `ts` stamped from each frame's own due time — one definition,
#: so the SSE cursor the shell keeps and the `ts`-ordered merge the System view
#: does can never disagree with the order the frames were released in.
EVENT_TRAIL: tuple[tuple[float, dict], ...] = tuple(
    (due, dict(frame, ts=round(due, 2))) for due, frame in _TRAIL
)


def events_at(t: float) -> list[dict]:
    """`GET /api/tasks/{id}/events` at time ``t`` — every frame due by now."""
    return [dict(frame) for due, frame in EVENT_TRAIL if t >= due]


# --------------------------------------------------------------------------- #
# The grill exchange (both surfaces' beat 1)                                   #
# --------------------------------------------------------------------------- #

#: Frames exactly as `POST /api/grill/stream` emits them. Round 1 asks one
#: question; the answer produces the spec. Every string is short enough to
#: survive the shell's 43-column conversation pane.
GRILL_ROUND_1: tuple[dict, ...] = (
    {"kind": "tool_use", "source": "grill", "text": "Read calc/calc.py"},
    {"kind": "tool_use", "source": "grill", "text": 'Grep "def mul" in calc/'},
    {"kind": "grill_question", "source": "grill", "type": "question", "round": 1,
     "question": "mul() on non-numbers - what?",
     "suggestions": ["raise TypeError", "let Python's * decide"]},
)

GRILL_ANSWER = "raise TypeError"

GRILL_ROUND_2: tuple[dict, ...] = (
    {"kind": "eval_verdict", "source": "grill", "verdict": "ok",
     "rationale": "criteria are testable"},
    {"kind": "grill_result", "source": "grill", "type": "done",
     "title": HERO_TITLE, "description": HERO_DESC,
     "acceptance_criteria": list(HERO_CRITERIA)},
)


# --------------------------------------------------------------------------- #
# The rest of the board                                                        #
# --------------------------------------------------------------------------- #

def _row(**kw):
    """A TaskSummaryOut row with the server's defaults filled in."""
    row = {
        "id": "", "external_id": None, "source": "board", "title": "",
        "status": "pending", "kind": "feature", "priority": "medium",
        "pr_url": None, "created_at": T_MINUS_21M, "updated_at": NOW,
        "repo_name": REPO_NAME, "description_short": None, "attempt_count": 0,
        "last_turns": None, "blocker_question": None, "blocker_category": None,
        "blocker_wake_condition": None, "blocker_human_stopped": False,
        "last_activity": NOW, "backend": "claude", "total_tokens": None,
        "total_cache_read": None, "total_cache_creation": None,
        "total_review_tokens": None, "total_review_cache_read": None,
        "total_review_cache_creation": None, "total_aux_tokens": None,
        "total_aux_cache_read": None, "total_aux_cache_creation": None,
        "wall_seconds": None, "parent_id": None, "has_spec": False,
        "live_status": None, "subtask_progress": None, "cancelled": False,
        "approved_at": None, "claimed": False, "pr_conflict_rounds": 0,
        "max_pr_conflict_rounds": 3, "lane": None,
    }
    row.update(kw)
    return row


ANSWER_ID = "9b71ce02aaaa4444bbbb5555cccc6666"
WORKING2_ID = "7e40b1a5333344445555666677778888"
REVIEW2_ID = "a55c9d10999988887777666655554444"

#: The board the ticket lands on. Never empty — a demo that opens on an empty
#: board or a bare prompt shows nothing about what the product does.
BACKDROP = (
    _row(id=ANSWER_ID, title="Empty-input behaviour is ambiguous",
         status="awaiting_input", lane="answer", created_at=T_MINUS_21M,
         updated_at=T_MINUS_21M, last_activity=T_MINUS_21M,
         blocker_question="What should sum([]) return - 0, or raise?",
         blocker_category="AMBIGUITY", attempt_count=2, last_turns=9,
         description_short="sum() over an empty list is unspecified",
         total_tokens=210_000, total_cache_read=2_950_000),
    _row(id=WORKING2_ID, title="Paginate GET /events", status="implementing",
         lane="working", claimed=True, live_status="Edit api/events.py",
         created_at=T_MINUS_9M, updated_at=T_MINUS_9M, last_activity=T_MINUS_9M,
         attempt_count=1, last_turns=4,
         description_short="cursor pagination, 100 per page",
         total_tokens=540_000, total_cache_read=6_100_000),
    _row(id=REVIEW2_ID, title="Cache the repo profile lookup",
         status="awaiting_approval", lane="review", created_at=T_MINUS_21M,
         updated_at=T_MINUS_21M, last_activity=T_MINUS_21M,
         pr_url="https://github.com/you/calc/pull/127", attempt_count=1,
         last_turns=5, description_short="profile resolution hit disk per task",
         total_tokens=430_000, total_cache_read=5_050_000),
)


def hero_row(t: float) -> dict | None:
    """The hero as the BOARD list endpoint would return it at time ``t``."""
    stage = stage_at(t)
    if stage is None:
        return None
    status, live, burn, turns = stage
    running = status in ("pending", "context", "planning", "implementing",
                         "testing", "reviewing")
    stamp = updated_at(t)
    return _row(
        id=HERO_ID, title=HERO_TITLE, status=status, lane=_lane(status),
        source="board", created_at=NOW, updated_at=stamp, last_activity=stamp,
        description_short=HERO_DESC, claimed=running,
        live_status=live if running else None,
        attempt_count=1 if turns else 0, last_turns=turns or None,
        has_spec=True,
        total_tokens=(burn // 8) or None,
        total_cache_read=(burn - burn // 8) or None,
        pr_url=PR_URL if status == "awaiting_approval" else None,
        approved_at=approved_at(t),
    )


def _lane(status: str) -> str:
    if status == "awaiting_approval":
        return "review"
    if status in ("awaiting_input", "escalated"):
        return "answer"
    return "working"


def board_at(t: float) -> list[dict]:
    """`GET /api/tasks` at time ``t`` — hero first so it lands at the top of
    its lane (laneView sorts by recency and the hero is always the newest)."""
    hero = hero_row(t)
    return ([hero] if hero else []) + [dict(r) for r in BACKDROP]


def hero_detail(t: float) -> dict:
    """`GET /api/tasks/{id}` — TaskOut, with the attempt the drawer reads."""
    stage = stage_at(t) or ("pending", "queued", 0, 0)
    status, _live, burn, turns = stage
    reviewed = status == "awaiting_approval"
    attempt = {
        "id": "att-1", "attempt_number": 1, "branch_name": BRANCH,
        "commit_sha": "c4d5e6f7a8b9", "pr_url": PR_URL if reviewed else None,
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
    # The plan appears in `context` only once planning has run — the
    # orchestrator writes `ctx["spec"]` after the planner returns
    # (orchestrator.py:3690), and SpecTab's own empty state says "Spec not
    # generated yet" for pending/context/planning. Handing it over earlier
    # would put a plan on camera before the thing that writes it has run.
    ctx: dict = {}
    if status not in ("pending", "context", "planning"):
        ctx["spec"] = dict(SPEC)
    if approved_at(t):
        ctx["approved_at"] = approved_at(t)
    return {
        "id": HERO_ID, "claimed": not reviewed, "external_id": None,
        "source": "board", "title": HERO_TITLE, "description": HERO_DESC,
        "status": status, "kind": "feature", "priority": "medium",
        "acceptance_criteria": list(HERO_CRITERIA), "repo_path": REPO_PATH,
        "blocker": None, "context": ctx, "parent_id": None,
        "created_at": NOW, "updated_at": updated_at(t),
        "attempts": [attempt] if turns else [],
        "total_tokens": burn // 8, "total_cache_read": burn - burn // 8,
        "total_cache_creation": None, "total_review_tokens": 210_000 if reviewed else None,
        "total_review_cache_creation": None,
        "total_review_cache_read": 1_900_000 if reviewed else None,
        "total_aux_tokens": None, "total_aux_cache_read": None,
        "total_aux_cache_creation": None, "wall_seconds": 214.0,
        "attempt_count": 1 if turns else 0,
    }
