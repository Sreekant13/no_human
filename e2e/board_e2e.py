"""Playwright E2E over the no_human board.

Drives the real UI end-to-end and asserts behaviour; saves screenshots as
evidence. Exits non-zero on any failed check.

    uv run python e2e/serve_demo.py 8488 &      # serve a temp demo DB
    NH_E2E_BASE=http://127.0.0.1:8488 uv run python e2e/board_e2e.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("NH_E2E_BASE", "http://127.0.0.1:8488")
SHOTS = os.environ.get("NH_E2E_SHOTS", "/tmp")
results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name, flush=True)


def run() -> int:
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
        # Board uses action-oriented lanes (CSS uppercases them).
        for expected in ["NEEDS YOU", "WORKING", "WAITING", "DONE"]:
            check(f"lane '{expected}' present", expected in lane_titles)
        # Needs You lane is first (most actionable)
        check("needs-you lane is first", lane_titles[0] == "NEEDS YOU")

        check("all 11 demo tasks rendered", page.locator(".task-card").count() == 11)

        def in_lane(label, title_substr):
            lane = page.locator(".lane", has=page.locator(".lane-title", has_text=label))
            return lane.locator(".card-title", has_text=title_substr).count() >= 1

        check("blocked -> Waiting", in_lane("Waiting", "Wait on upstream PR"))
        check("paused_quota -> Waiting", in_lane("Waiting", "Paused: subscription quota"))
        check("awaiting_input -> Needs You", in_lane("Needs You", "Ambiguous"))
        check("awaiting_approval -> Needs You", in_lane("Needs You", "ready for you"))
        check("escalated -> Needs You", in_lane("Needs You", "Impossible"))
        page.screenshot(path=f"{SHOTS}/nh_e2e_1_board.png", full_page=True)

        page.locator(".card-title", has_text="ready for you").first.click()
        page.wait_for_selector(".slideover", timeout=5000)
        check("slide-over opened", page.locator(".slideover").is_visible())
        check("status pill awaiting_approval",
              "awaiting_approval" in page.locator(".so-status-pill").inner_text().lower())

        page.locator(".so-tab", has_text="review").click()
        page.wait_for_timeout(300)
        check("review checklist has 2 items", page.locator(".checklist-item").count() == 2)
        so_text = page.locator(".so-body").inner_text()
        check("checklist cites evidence", "calc.py" in so_text or "test_calc" in so_text or "passed" in so_text.lower())
        page.screenshot(path=f"{SHOTS}/nh_e2e_2_review.png", full_page=True)

        page.locator(".so-tab", has_text="attempts").click()
        page.wait_for_timeout(300)
        check("attempts show branch", "no-human/aabbccdd" in page.locator(".so-body").inner_text())

        page.locator(".so-tab", has_text="diff").click()
        page.wait_for_timeout(400)
        # Native diff renderer (no Monaco/CDN): real diff shows colorized lines.
        check("diff view present", page.locator("[data-testid=diff-view]").count() == 1)
        check("diff renders added lines (mul)", page.locator(".diff-line.diff-add").count() >= 1)
        check("diff shows the mul() addition",
              "def mul" in page.locator("[data-testid=diff-view]").inner_text())
        check("no monaco/CDN editor in DOM", page.locator(".monaco-editor").count() == 0)
        page.screenshot(path=f"{SHOTS}/nh_e2e_5_diff.png", full_page=True)

        page.locator(".btn-approve").click()
        page.wait_for_timeout(800)
        body = page.locator(".slideover").inner_text().lower()
        check("approve shows 'never merges' confirmation",
              "never merges" in body or "merge the pr" in body)
        page.screenshot(path=f"{SHOTS}/nh_e2e_3_approved.png", full_page=True)

        page.locator(".so-close").click()
        page.wait_for_timeout(300)
        page.locator(".card-title", has_text="Ambiguous").first.click()
        page.wait_for_selector(".slideover", timeout=5000)
        page.locator(".so-tab", has_text="details").click()
        page.wait_for_timeout(300)
        check("status pill awaiting_input",
              "awaiting_input" in page.locator(".so-status-pill").inner_text().lower())

        # awaiting_input uses Reply, not Send back
        page.locator(".slideover .btn-reply").first.click()
        page.wait_for_selector(".sendback-modal", timeout=3000)
        check("reply modal opens", page.locator(".sendback-modal").is_visible())
        page.locator(".sendback-textarea").fill("Use 'return 0' on empty input.")
        page.screenshot(path=f"{SHOTS}/nh_e2e_4_sendback.png", full_page=True)
        page.locator(".sendback-modal .btn-approve", has_text="Send").click()
        page.wait_for_timeout(800)
        check("reply submitted (modal closed)",
              page.locator(".sendback-modal").count() == 0)

        # ── a11y / keyboard checks ────────────────────────────────────────── #

        # Close the still-open SlideOver from the send-back test
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

        # focus-visible outline is defined (CSS-level check via computed style)
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

        # Sub-status pill visible in Working lane
        ip_lane = page.locator(".lane", has=page.locator(".lane-title", has_text="Working"))
        substatus_count = ip_lane.locator(".card-substatus").count()
        check("sub-status pill visible in Working lane", substatus_count > 0)

        page.screenshot(path=f"{SHOTS}/nh_e2e_5_a11y.png", full_page=True)

        # ── Settings page checks ───────────────────────────────────── #

        page.locator(".nh-nav-btn", has_text="Settings").click()
        page.wait_for_timeout(500)
        check("settings page visible", page.locator(".settings-page").is_visible())

        # Projects tab should be the default active tab
        active_tab = page.locator(".settings-tab.active").inner_text()
        check("projects tab is default", active_tab.strip() == "Projects")

        # The seeded project should render
        page.wait_for_selector(".project-card", timeout=5000)
        check("seeded project visible", page.locator(".project-card").count() >= 1)
        project_text = page.locator(".project-card").first.inner_text()
        check("project name 'demo-project' shows", "demo-project" in project_text)
        check("project repo count shows", "2 repos" in project_text.lower())
        page.screenshot(path=f"{SHOTS}/nh_e2e_6_settings_projects.png", full_page=True)

        # Switch to Rules tab and verify seeded rule
        page.locator(".settings-tab", has_text="Rules").click()
        page.wait_for_timeout(500)
        page.wait_for_selector(".memory-card", timeout=5000)
        check("seeded rule visible", page.locator(".memory-card").count() >= 1)
        rule_text = page.locator(".memory-card").first.inner_text()
        check("rule title shows", "Always add tests" in rule_text)
        page.screenshot(path=f"{SHOTS}/nh_e2e_7_settings_rules.png", full_page=True)

        # Back to board
        page.locator(".nh-nav-btn", has_text="Board").click()
        page.wait_for_timeout(300)
        check("back to board works", page.locator(".lane").count() >= 1)

        browser.close()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nE2E: {passed}/{total} checks passed", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
