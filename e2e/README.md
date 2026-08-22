# Board E2E (Playwright)

End-to-end UI verification for the web board. Not part of the default `pytest`
run (needs a browser); run it manually or in a job that installs browsers.

The board today is **three lanes** — Needs Answer, Working, Review PR — plus
two sidebar **Outcomes** pages, Done and Failed, that are not shown on the
board itself (`web/src/boardLanes.js`). There is no "Waiting"/"Parked" lane:
tasks that are waiting on something (`blocked` with a wake condition,
`paused_quota`) surface in the Working lane with their own waiting tag
instead.

`e2e/board_e2e.py` never hardcodes a lane name, a status→lane mapping, or a
demo card count — every one of those is derived at run time from
`e2e/lane_model.py` (which parses `web/src/boardLanes.js` and reads the
shared `testdata/lane_conformance.json` fixture) and from
`e2e/serve_demo.py`'s own `DEMO_TASKS`-derived helpers. `tests/test_board_e2e_lane_model.py`
is a browser-free pytest module that pins those same expectations against
their sources of truth, so a future lane-model change fails in CI (no
browser needed) instead of only being caught the next time someone happens
to run this script by hand.

## Run

```bash
# one-time: install the optional e2e deps + a browser
uv sync --group e2e
uv run playwright install chromium

# build the SPA so the API serves it
cd web && npm install && npm run build && cd ..

# serve a TEMP demo DB (the real ~/.no_human DB is never touched) ...
uv run python e2e/serve_demo.py 8488 &

# ... then drive the UI
NH_E2E_BASE=http://127.0.0.1:8488 uv run python e2e/board_e2e.py
```

## What it checks

- The three board lanes (Needs Answer, Working, Review PR) render, in that
  order, with Needs Answer first; the two Outcomes lanes (Done, Failed) are
  never rendered as board lanes.
- Every demo task's status (and, for `blocked`, its wake condition) routes to
  the lane `testdata/lane_conformance.json` pins for it — no task vanishes
  from the UI, and each per-lane card total (including the Working lane's
  "N live · N queued · N waiting" breakdown) matches the count derived from
  `serve_demo.DEMO_TASKS`.
- The accordion drawer: collapsed `.lane-more`/`.lane-stale-divider` sections
  expand before cards are counted; waiting-tag text renders for the two
  auto-resolving parked cards (`blocked` with a wake condition, `paused_quota`).
- Slide-over: status/progress label, review checklist with **cited
  evidence**, attempts, and a native (no Monaco/CDN) diff view.
- **Approve** records `approved_at` via the API and shows a "never merges"
  confirmation — the demo config pins `approve_merge.enabled=false`, so this
  exercises the non-merging path; status stays `awaiting_approval` and the
  task stays in the Review PR lane.
- **Reply** on a parked task with a real blocker (`awaiting_input`): the
  DecisionPanel — not the generic action bar — surfaces the blocker's own
  question, and its reply action opens the same modal **Send-back** uses.
- **Send-back** on a second, distinct `awaiting_approval` task: stores
  feedback and returns the task to `implementing`.
- The live-update **WebSocket** connects (the `.nh-ws-dot.live` indicator).
- Basic accessibility: Escape/Space keyboard interaction with the SlideOver,
  `aria-labelledby` wiring, and initial focus on the close button.
- Settings overlay: Projects tab (seeded project) and Rules tab (seeded
  rule) both render their seeded data.
- The two Outcomes pages (Done, Failed): each opens from the sidebar nav and
  shows exactly as many rows as `serve_demo.outcome_counts()` derives.

Screenshots are written to `$NH_E2E_SHOTS` (default `/tmp`):
`nh_e2e_1_board.png` … `nh_e2e_8_outcomes.png`.

This harness found two real bugs during Phase 5 review: the resume base-branch
defect and the missing `uvicorn[standard]` WebSocket dependency.
