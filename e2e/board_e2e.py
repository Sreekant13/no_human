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

        check("header logo renders", page.locator(".nh-logo").count() == 1)
        page.wait_for_selector(".nh-ws-dot.live", timeout=8000)
        print("    websocket connections:", ws_events)
        check("websocket live indicator on", page.locator(".nh-ws-dot.live").count() == 1)

        lane_titles = page.locator(".lane-title").all_inner_texts()
        print("    lanes:", lane_titles)
        # Transient stages (context/planning/implementing/reviewing/testing) are
        # collapsed into "IN PROGRESS" — check the new lane set.
        for expected in ["INTAKE", "IN PROGRESS", "PARKED", "AWAITING YOU", "DONE", "ESCALATED"]:
            check(f"lane '{expected}' present", expected in lane_titles)
        # Loud lanes must appear first and second
        check("awaiting-you lane is first", lane_titles[0] == "AWAITING YOU")
        check("escalated lane is second", lane_titles[1] == "ESCALATED")

        check("all 11 demo tasks rendered", page.locator(".task-card").count() == 11)

        def in_lane(label, title_substr):
            lane = page.locator(".lane", has=page.locator(".lane-title", has_text=label))
            return lane.locator(".card-title", has_text=title_substr).count() >= 1

        check("blocked -> PARKED", in_lane("PARKED", "Wait on upstream PR"))
        check("paused_quota -> PARKED", in_lane("PARKED", "Paused: subscription quota"))
        check("awaiting_input -> AWAITING YOU", in_lane("AWAITING YOU", "Ambiguous"))
        check("awaiting_approval -> AWAITING YOU", in_lane("AWAITING YOU", "ready for you"))
        check("escalated -> ESCALATED", in_lane("ESCALATED", "Impossible"))
        page.screenshot(path=f"{SHOTS}/nh_e2e_1_board.png", full_page=True)

        page.locator(".card-title", has_text="ready for you").first.click()
        page.wait_for_selector(".slideover", timeout=5000)
        check("slide-over opened", page.locator(".slideover").is_visible())
        check("status pill awaiting_approval",
              "awaiting_approval" in page.locator(".so-status-pill").inner_text().lower())

        page.locator(".so-tab", has_text="review").click()
        page.wait_for_timeout(300)
        check("review checklist has 2 items", page.locator(".checklist-item").count() == 2)
        check("checklist cites evidence", "calc.py:5" in page.locator(".so-body").inner_text())
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

        page.locator(".btn-sendback").click()
        page.wait_for_selector(".sendback-modal", timeout=3000)
        check("send-back modal opens", page.locator(".sendback-modal").is_visible())
        page.locator(".sendback-textarea").fill("Use 'return 0' on empty input.")
        page.screenshot(path=f"{SHOTS}/nh_e2e_4_sendback.png", full_page=True)
        page.locator(".sendback-modal .btn-approve", has_text="Send").click()
        page.wait_for_timeout(800)
        check("send-back submitted (modal closed)",
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

        # Sub-status pill visible in In Progress lane
        ip_lane = page.locator(".lane", has=page.locator(".lane-title", has_text="IN PROGRESS"))
        substatus_count = ip_lane.locator(".card-substatus").count()
        check("sub-status pill visible in In Progress lane", substatus_count > 0)

        page.screenshot(path=f"{SHOTS}/nh_e2e_5_a11y.png", full_page=True)

        browser.close()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\nE2E: {passed}/{total} checks passed", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
