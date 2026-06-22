# Board E2E (Playwright)

End-to-end UI verification for the web board. Not part of the default `pytest`
run (needs a browser); run it manually or in a job that installs browsers.

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

- All nine board lanes render (incl. **Parked** for blocked/paused_quota).
- Every task status maps to a lane — no task vanishes from the UI.
- Slide-over: status pill, review checklist with **cited evidence**, attempts,
  diff tab graceful empty state.
- **Approve** records approval (never merges — status stays awaiting_approval).
- **Send-back** stores feedback and returns the task to implementing.
- The live-update **WebSocket** connects (the `nh-ws-dot.live` indicator).

Screenshots are written to `$NH_E2E_SHOTS` (default `/tmp`):
`nh_e2e_1_board.png` … `nh_e2e_4_sendback.png`.

This harness found two real bugs during Phase 5 review: the resume base-branch
defect and the missing `uvicorn[standard]` WebSocket dependency.
