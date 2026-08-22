"""Playwright E2E over the no_human board.

Drives the real UI end-to-end and asserts behaviour; saves screenshots as
evidence. Exits non-zero on any failed check.

    uv run python e2e/serve_demo.py 8488 &      # serve a temp demo DB
    NH_E2E_BASE=http://127.0.0.1:8488 uv run python e2e/board_e2e.py

The board is three lanes (Needs Answer / Working / Review PR) plus two
sidebar Outcomes pages (Done / Failed) — see web/src/boardLanes.js. Nothing
here hardcodes a lane name, a status->lane mapping, or a demo card count:
those all come from e2e/lane_model.py (which itself parses boardLanes.js and
testdata/lane_conformance.json) and e2e/serve_demo.py's own DEMO_TASKS-derived
helpers, so this script and the UI can never silently drift apart again.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane_model  # noqa: E402
import serve_demo  # noqa: E402

from no_human.core.lanes import lane_for  # noqa: E402

BASE = os.environ.get("NH_E2E_BASE", "http://127.0.0.1:8488")
SHOTS = os.environ.get("NH_E2E_SHOTS", "/tmp")
results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name, flush=True)


def _status_str(status: object) -> str:
    return getattr(status, "value", status)


def _lane_total_from_count_text(text: str) -> int:
    """.lane-count is a bare integer for every lane except Working, whose
    header instead breaks the same total into "N live · N queued · N
    waiting" (Board.jsx workingBreakdown) — sum every leading integer so
    both shapes reduce to the same true total."""
    total = 0
    for part in text.split("·"):
        part = part.strip()
        if part[:1].isdigit():
            total += int(part.split()[0])
    return total


def api_list() -> list[dict]:
    with urllib.request.urlopen(f"{BASE}/api/tasks", timeout=5) as resp:
        return json.load(resp)


def find_task_id(tasks: list[dict], title_substr: str) -> str:
    for t in tasks:
        if title_substr in t["title"]:
            return t["id"]
    raise AssertionError(f"no seeded task with title containing {title_substr!r}")


def poll_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.25) -> dict | None:
    """Poll GET /api/tasks until `predicate(tasks)` is truthy; returns the
    tasks snapshot it succeeded on, or None on timeout."""
    deadline = time.monotonic() + timeout_s
    tasks = api_list()
    while time.monotonic() < deadline:
        if predicate(tasks):
            return tasks
        time.sleep(interval_s)
        tasks = api_list()
    return tasks if predicate(tasks) else None


def run() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        ws_events: list[str] = []
        page.on("websocket", lambda ws: ws_events.append(ws.url))
        page.on("console", lambda m: print("    [console error]", m.text)
                if m.type == "error" else None)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector(".task-card", timeout=10000)

        check("header logo renders", page.locator(".legion-logo").count() >= 1)
        page.wait_for_selector(".nh-ws-dot.live", timeout=8000)
        print("    websocket connections:", ws_events)
        check("websocket live indicator on", page.locator(".nh-ws-dot.live").count() >= 1)

        lane_titles = page.locator(".lane-title").all_inner_texts()
        print("    lanes:", lane_titles)
        for expected in lane_model.BOARD_LANE_TITLES:
            check(f"lane '{expected}' present", expected in lane_titles)
        for absent in lane_model.OUTCOME_LANE_TITLES:
            check(f"outcome lane '{absent}' not on board", absent not in lane_titles)
        check(
            "needs-answer lane is first",
            lane_titles[0] == lane_model.label_for("answer").upper(),
        )

        # Expand every collapsed section (Working's top-N .lane-more, Needs
        # Answer's own .lane-stale-divider) before counting cards — .lane-count
        # always shows the true total regardless of collapse state, but
        # `.task-card` counts on the page do not.
        for toggle in page.locator(".lane-more, .lane-stale-divider").all():
            if toggle.get_attribute("aria-expanded") == "false":
                toggle.click()
                page.wait_for_timeout(150)

        check(
            f"all {serve_demo.board_card_count()} demo tasks rendered",
            page.locator(".task-card").count() == serve_demo.board_card_count(),
        )

        def in_lane(label, title_substr):
            lane = page.locator(".lane", has=page.locator(".lane-title", has_text=label))
            return lane.locator(".card-title", has_text=title_substr).count() >= 1

        counts = serve_demo.lane_counts()
        for lane in lane_model.BOARD_LANES:
            lane_locator = page.locator(".lane", has=page.locator(".lane-title", has_text=lane.label))
            text = lane_locator.locator(".lane-count").inner_text()
            check(
                f"'{lane.label}' lane count is {counts[lane.key]}",
                _lane_total_from_count_text(text) == counts[lane.key],
            )

        # Status/wake -> lane routing. The expected lane comes from
        # core.lanes.lane_for() — the same server-authoritative router
        # serve_demo.lane_counts() already uses — not from
        # lane_model.lane_title_for_status()'s fixture-only lookup: only
        # `blocked` is actually wake-sensitive (core/lanes.py), so
        # testdata/lane_conformance.json pins wake=True/False only for one
        # representative status per its own self-consistency check, and
        # legitimately has no (paused_quota, wake=True) case even though the
        # demo's own paused_quota row carries a real wake_condition string.
        # lane_for() has no such gap: it is the router itself, not a fixture
        # lookup, so it answers every (status, wake) combination the demo can
        # produce.
        for spec in serve_demo.DEMO_TASKS:
            blocker = spec.get("blocker") or {}
            wake = blocker.get("wake_condition")
            lane_key = lane_for(serve_demo._synthetic_task(spec))
            if lane_key in lane_model.OUTCOME_LANE_KEYS:
                continue  # routes to an outcome lane, not shown on the board
            lane_label = lane_model.label_for(lane_key)
            check(
                f"{_status_str(spec['status'])} (wake={bool(wake)}) -> {lane_label}",
                in_lane(lane_label, spec["title"]),
            )

        # Waiting-tag text on the two auto-resolving parked cards (isWaiting()).
        check(
            "blocked-with-wake shows its own wake condition",
            in_lane(lane_model.label_for("working"), "Wait on upstream PR")
            and page.locator(".card-waiting-tag", has_text="waits for its own signal").count() >= 1,
        )
        check(
            "paused_quota shows 'waits for quota'",
            page.locator(".card-waiting-tag", has_text="waits for quota").count() >= 1,
        )

        page.screenshot(path=f"{SHOTS}/nh_e2e_1_board.png", full_page=True)

        def open_section(label: str) -> None:
            header = page.locator(".so-section-header", has=page.locator(".so-section-title", has_text=label))
            if header.get_attribute("aria-expanded") != "true":
                header.click()
                page.wait_for_timeout(300)

        # ── Approve flow (first awaiting_approval task) ──────────────────── #

        page.locator(".card-title", has_text="ready for you").first.click()
        page.wait_for_selector(".slideover", timeout=5000)
        check("slide-over opened", page.locator(".slideover").is_visible())
        check(
            "progress label shows ready-for-approval text",
            page.locator(".so-progress-label").inner_text().strip()
            == "Ready — awaiting your approval",
        )

        open_section("Review")
        check("review checklist has 2 items", page.locator(".checklist-item").count() == 2)
        so_text = page.locator(".so-body").inner_text()
        check("checklist cites evidence", "calc.py" in so_text or "test_calc" in so_text or "passed" in so_text.lower())
        page.screenshot(path=f"{SHOTS}/nh_e2e_2_review.png", full_page=True)

        open_section("Attempts")
        check("attempts show branch", "no-human/aabbccdd" in page.locator(".so-body").inner_text())

        open_section("Diff")
        # Native diff renderer (no Monaco/CDN): real diff shows colorized lines.
        check("diff view present", page.locator("[data-testid=diff-view]").count() == 1)
        check("diff renders added lines (mul)", page.locator(".diff-line.diff-add").count() >= 1)
        check("diff shows the mul() addition",
              "def mul" in page.locator("[data-testid=diff-view]").inner_text())
        check("no monaco/CDN editor in DOM", page.locator(".monaco-editor").count() == 0)
        page.screenshot(path=f"{SHOTS}/nh_e2e_5_diff.png", full_page=True)

        tasks_before_approve = api_list()
        mul_id = find_task_id(tasks_before_approve, "ready for you")
        mul_before = next(t for t in tasks_before_approve if t["id"] == mul_id)
        check("approve not yet recorded", not mul_before.get("approved_at"))

        page.locator(".so-actions .btn-approve").click()
        page.wait_for_timeout(800)
        body = page.locator(".slideover").inner_text().lower()
        check("approve shows 'never merges' confirmation",
              "never merges" in body or "merge the pr" in body)
        page.screenshot(path=f"{SHOTS}/nh_e2e_3_approved.png", full_page=True)

        post_approve = poll_until(
            lambda tasks: bool(next((t for t in tasks if t["id"] == mul_id), {}).get("approved_at"))
        )
        mul_after = next((t for t in (post_approve or []) if t["id"] == mul_id), None)
        check("approve records approved_at via the API", bool(mul_after and mul_after.get("approved_at")))
        check(
            "approve does not merge — status stays awaiting_approval",
            bool(mul_after and mul_after["status"] == "awaiting_approval"),
        )
        check("approved task still shown in Review PR lane", in_lane(lane_model.label_for("review"), "ready for you"))

        page.locator(".so-close").click()
        page.wait_for_timeout(300)

        # ── Reply flow (awaiting_input task) ──────────────────────────────── #

        page.locator(".card-title", has_text="Ambiguous").first.click()
        page.wait_for_selector(".slideover", timeout=5000)
        # taskProgress.js has no entry for "awaiting_input" (a parked/terminal
        # status) — taskProgress() returns None and .so-progress-label simply
        # does not render; there is no meaningful forward-% for a parked task.
        check(
            "no progress bar for a parked task (awaiting_input)",
            page.locator(".so-progress-label").count() == 0,
        )

        # A parked task with a real blocker is handled by DecisionPanel, not
        # the generic .so-actions bar: showActionBar is false whenever
        # decisionFor(task) returns non-null (SlideOver.jsx), so the Reply
        # trigger here is DecisionPanel's own "reply" action button
        # (decision-btn-secondary — the only secondary-variant action it
        # renders), not `.so-actions .btn-reply`.
        # DecisionPanel mounts a tick after the slideover itself (it depends on
        # the task's blocker having loaded into SlideOver's own state) — wait
        # for it explicitly rather than racing SlideOver's open animation the
        # way the plain `.is_visible()` check below used to.
        page.wait_for_selector(".decision-panel", timeout=5000)
        check("decision panel shown for parked task", page.locator(".decision-panel").is_visible())
        check(
            "decision panel surfaces the blocker's own question",
            "mul() return on empty input" in page.locator(".decision-panel").inner_text(),
        )
        page.locator(".decision-panel .decision-btn-secondary").click()
        page.wait_for_selector(".sendback-modal", timeout=3000)
        check("reply modal opens", page.locator(".sendback-modal").is_visible())
        page.locator(".sendback-textarea").fill("Use 'return 0' on empty input.")
        page.screenshot(path=f"{SHOTS}/nh_e2e_4_sendback.png", full_page=True)
        page.locator(".sendback-modal .btn-approve", has_text="Send").click()
        page.wait_for_timeout(800)
        check("reply submitted (modal closed)",
              page.locator(".sendback-modal").count() == 0)

        if page.locator(".slideover").is_visible():
            page.locator(".so-close").click()
            page.wait_for_timeout(300)

        # ── Send-back flow (second awaiting_approval task -> implementing) ── #

        div_tasks_before = api_list()
        div_id = find_task_id(div_tasks_before, "Send back: off-by-one")
        div_before = next(t for t in div_tasks_before if t["id"] == div_id)
        check("send-back target starts awaiting_approval", div_before["status"] == "awaiting_approval")

        page.locator(".card-title", has_text="Send back: off-by-one").first.click()
        page.wait_for_selector(".slideover", timeout=5000)
        page.locator(".so-actions .btn-sendback").click()
        page.wait_for_selector(".sendback-modal", timeout=3000)
        check("send-back modal opens", page.locator(".sendback-modal").is_visible())
        page.locator(".sendback-textarea").fill("Guard div() against b == 0.")
        page.locator(".sendback-modal .btn-approve", has_text="Send").click()
        page.wait_for_timeout(800)
        check("send-back submitted (modal closed)", page.locator(".sendback-modal").count() == 0)

        post_sendback = poll_until(
            lambda tasks: next((t for t in tasks if t["id"] == div_id), {}).get("status") == "implementing"
        )
        div_after = next((t for t in (post_sendback or []) if t["id"] == div_id), None)
        check("send-back transitions the task to implementing", bool(div_after and div_after["status"] == "implementing"))

        if page.locator(".slideover").is_visible():
            page.locator(".so-close").click()
            page.wait_for_timeout(300)
        # The board updates live over the websocket (already asserted above) —
        # give it a moment to pick up the send-back transition, then confirm
        # the card moved without a page reload.
        page.wait_for_timeout(500)
        check(
            "sent-back task now shows in Working lane",
            in_lane(lane_model.label_for("working"), "Send back: off-by-one"),
        )

        # ── a11y / keyboard checks ────────────────────────────────────────── #

        if page.locator(".slideover").is_visible():
            page.locator(".so-close").click()
            page.wait_for_timeout(300)

        # Escape closes the SlideOver
        page.locator(".card-title", has_text="ready for you").first.click()
        page.wait_for_selector(".slideover", timeout=5000)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        check("Escape closes SlideOver", page.locator(".slideover").count() == 0)

        # Space key opens a card (keyboard-only interaction)
        card = page.locator(".task-card").first
        card.focus()
        page.keyboard.press("Space")
        page.wait_for_timeout(500)
        check("Space key opens SlideOver", page.locator(".slideover").is_visible())

        # SlideOver has aria-labelledby pointing to a non-empty element
        so_title_id = page.locator("#so-dialog-title").count()
        dialog_label = page.locator("[role=dialog][aria-labelledby=so-dialog-title]").count()
        check("dialog aria-labelledby wired up", so_title_id >= 1 and dialog_label >= 1)

        # Focus trap: close button gets focus on open (first focusable element)
        focused_label = page.evaluate("document.activeElement?.ariaLabel")
        check("close button focused on open", focused_label == "Close")

        # Escape again to close
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # Sub-status pill visible in Working lane (showSubStatus is Working-only)
        working_lane = page.locator(".lane", has=page.locator(".lane-title", has_text=lane_model.label_for("working")))
        substatus_count = working_lane.locator(".card-substatus").count()
        check("sub-status pill visible in Working lane", substatus_count > 0)

        page.screenshot(path=f"{SHOTS}/nh_e2e_5_a11y.png", full_page=True)

        # ── Settings overlay checks ───────────────────────────────────────── #

        page.locator(".nh-navrow", has_text="Settings").click()
        page.wait_for_timeout(500)
        check("settings overlay visible", page.locator(".settings-overlay").is_visible())

        # Projects section should be the default active section
        active_section = page.locator(".settings-overlay-navitem.active").inner_text()
        check("projects section is default", active_section.strip() == "Projects")

        # The seeded project should render
        page.wait_for_selector(".project-card", timeout=5000)
        check("seeded project visible", page.locator(".project-card").count() >= 1)
        project_text = page.locator(".project-card").first.inner_text()
        check("project name 'demo-project' shows", "demo-project" in project_text)
        check("project repo count shows", "2 repos" in project_text.lower())
        page.screenshot(path=f"{SHOTS}/nh_e2e_6_settings_projects.png", full_page=True)

        # Switch to Rules section and verify the seeded rule
        page.locator(".settings-overlay-navitem", has_text="Rules").click()
        page.wait_for_timeout(500)
        page.wait_for_selector(".memory-card", timeout=5000)
        check("seeded rule visible", page.locator(".memory-card").count() >= 1)
        rule_text = page.locator(".memory-card").first.inner_text()
        check("rule title shows", "Always add tests" in rule_text)
        page.screenshot(path=f"{SHOTS}/nh_e2e_7_settings_rules.png", full_page=True)

        page.locator(".settings-overlay-close").click()
        page.wait_for_timeout(300)
        check("settings overlay closes", page.locator(".settings-overlay").count() == 0)

        # ── Outcomes pages (Done / Failed, sidebar-only) ─────────────────── #

        outcome_counts = serve_demo.outcome_counts()

        page.locator(".nh-navrow", has_text="Done").click()
        page.wait_for_timeout(400)
        check("Done outcome page opens", page.locator(".outcome-page").is_visible())
        check("Done outcome title", page.locator(".outcome-title").inner_text().strip() == "Done")
        check(
            f"Done outcome page has {outcome_counts['done']} row(s)",
            page.locator(".outcome-page .stats-tr").count() == outcome_counts["done"],
        )

        page.locator(".nh-navrow", has_text="Failed").click()
        page.wait_for_timeout(400)
        check("Failed outcome page opens", page.locator(".outcome-page").is_visible())
        check("Failed outcome title", page.locator(".outcome-title").inner_text().strip() == "Failed")
        check(
            f"Failed outcome page has {outcome_counts['failed']} row(s)",
            page.locator(".outcome-page .stats-tr").count() == outcome_counts["failed"],
        )
        page.screenshot(path=f"{SHOTS}/nh_e2e_8_outcomes.png", full_page=True)

        # Back to board
        page.locator(".nh-navrow", has_text="In progress").click()
        page.wait_for_timeout(300)
        check("back to board works", page.locator(".lane").count() >= 1)

        browser.close()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nE2E: {passed}/{total} checks passed", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
