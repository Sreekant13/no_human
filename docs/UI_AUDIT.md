# UI Audit — Task 5C (2026-07-13)

**Method.** Every flow of the **live** app (real data, built bundle) was driven read-only with
Playwright — all non-GET requests were hard-blocked so nothing on the operator's board could be
mutated — and captured in light, dark and 390px mobile: board, task drawer, Stats, Settings,
composer. Zero console errors across all 15 captures. The gallery was then reviewed by two
fresh-context reviewers: a **staff UI/UX designer** (hierarchy, affordances, states, parity) and a
**staff frontend engineer** (correctness, a11y, CSS health), each required to refute rather than
praise. Findings below are deduplicated, ranked, and each names the file that causes it.

**Standing constraint (from the prior persona walk):** the board is *intentional* and was judged
good. Fix real weaknesses; do **not** reskin for its own sake. The lane model (`boardLanes.js` routes
by *what the human owes*, not by internal status), the single `isNeedsYou` predicate behind five
surfaces, the shared burn definition in `cost.js`, and the `AgentLogModal` are all **good and must
not be broken**.

Status key: ✅ fixed · ⬜ open

---

## BLOCKER

| # | Status | Screen | Defect | Fix |
|---|---|---|---|---|
| B1 | ✅ | Board | **Cards guillotine their own content.** `.task-card` is a flex child with `overflow:hidden` and no `flex-shrink:0`, so a full lane **squashes** every card instead of scrolling. The blocker question, the `att · tok` meta and the PR badge — the things the board exists to show — are sliced off exactly in the Needs-Answer lane (which never collapses, so it always overflows). `styles.css:578` | `.lane-body > .task-card, .lane-body > .failed-group { flex: 0 0 auto }` |
| B2 | ✅ | Board | **Enter does not open a task.** The card's `onKeyDown` never calls `preventDefault()`, so Enter opens the drawer, the drawer autofocuses its close button in the same event flush, and Enter's default activation then *clicks that button* — the drawer opens and closes within one keypress. The board's primary action is keyboard-inoperable. `Board.jsx:208` | `preventDefault()` before `onClick` |
| B3 | ✅ | Drawer, mobile | **The title renders one character per line.** `.so-header` is a nowrap row; the rigid cost/pill/close children collapse `.so-header-text` toward 0 and `word-break: break-word` then breaks per character. The "approve from your phone" path is dead on arrival. `styles.css:2011` | wrap the header at ≤640px; cost onto its own row; `overflow-wrap: anywhere` |
| B4 | ✅ | Drawer | **The answer UI is two tabs from where the drawer opens.** Default tab is `system`, but the blocker's question, its evidence and the one-click canned-answer buttons live in **Details**. Clicking a Needs-Answer card lands on a pipeline diagram. `SlideOver.jsx:58,1315` | contextual default tab (`awaiting_input`/`escalated`/`blocked` → details, `awaiting_approval` → review); pin the blocker block under the header |

## MAJOR

| # | Status | Screen | Defect | Fix |
|---|---|---|---|---|
| M1 | ⬜ | Stats | **Two cost figures disagree by ~10×.** `northStar.js:26` calls `estimateCost(tpp)` with ONE arg, pricing total burn at the fresh rate → "9.99M · est. $29.98"; two tiles below, `Stats.jsx:561` correctly calls `estimateCost(fresh, cache)` → "169.87M · est. $55.54". Taken literally: 13 merged PRs cost $390, and lifetime is $55.54. Cost is the one number that must be trustworthy. | split `tokens_per_pr` into fresh/cache, or derive the blended rate; make `estimateCost` require both args so the footgun can't be re-armed |
| M2 | ✅ | Board | **"11 failed" is a lie** — it is 1 failure and 10 cancellations. `App.jsx:41` counts `status === "failed"` with no `cancelled` filter, while `Stats.jsx:38` does it right. The board's loudest red number is 91% self-inflicted noise, competing with the real "6 need you". | exclude cancelled from the count; group same-title rows in the answer lane as the failed lane already does |
| M3 | ✅ | Drawer | **One Escape closes a nested modal *and* the whole drawer** (two unconditional listeners), destroying feedback the operator just typed into send-back/reply. Those modals are also outside the focus trap. `SlideOver.jsx:88-111,700` | guard the drawer handler while a nested modal is open; give the modals their own trap |
| M4 | ✅ | Drawer | **Unguarded fetch race:** "Next review →" swaps `taskId` while `fetchTask`/`fetchDiff` are in flight; a late response paints task A's diff under task B's title, and Approve posts against B. `SlideOver.jsx:76-85` | stale-flag the effect |
| M5 | ✅ | Board | **Card text is sliced mid-word** (no ellipsis) — a URL or path is cut through a glyph. `overflow-wrap` computes to `normal` under `overflow:hidden`. Measured at 390px: `scrollWidth 250 / clientWidth 180`. `styles.css:654` | `overflow-wrap: anywhere` on `.card-title`, `.card-description`, `.card-blocker-q` |
| M6 | ✅ | Board, 1440px | **The DONE lane is cut off on a MacBook.** 5 lanes × 240px + padding = 1254px into 1208px available, behind a 6px transparent scrollbar. Also: the gate lanes and the Done lane are *exactly* the same width — size, the strongest hierarchy channel, is unused. `styles.css:468` | narrow the lanes; weight the gate lanes (`flex: 1.25`) over Done (`0.85`) |
| M7 | ⬜ | Drawer | **The System tab prints the same three facts twice** (stage row *and* agent cards) and pays ~600px of vertical space for it; `.sys-node-model` has no horizontal padding and clips against the card border. It is the default tab and the lowest-density surface in the app. `styles.css:931` | collapse to the stage chips (which are good) as the click target into `AgentLogModal`; fix the padding |
| M8 | ⬜ | Stats | **The Daily Completions chart is ~90% empty**, has no axis labels or dates, stretches its bars (`preserveAspectRatio="none"`), and draws zero-days as a 1px tick that reads as a broken baseline. It says "the tool is barely used" — the opposite of the data. `Stats.jsx:602` | drop the stretch; label first/mid/last day; distinct zero-day tick; consider a 7-day window |
| M9 | ⬜ | Stats | **Cost is never a headline** (always a subtitle), and the green/plain tone system has no legible rule — "PRs merged: 13" is green because it is non-zero, "Stagnation: 0" is plain although 0 is the best value. Four green tiles in a row means the colour carries no signal. `northStar.js` | promote a Spend tile; colour only where a threshold exists |
| M10 | ✅ | Light theme | **The lane columns vanish in light mode.** `.lane`, `.lane-empty`, `.lane-failed` and the amber "this column is blocking you" glow are all `rgba(255,255,255,…)` literals — invisible on a light canvas. The strongest hierarchy cue on the board is dark-only. `styles.css:473` | token surfaces + `color-mix` glows |
| M11 | ✅ | Drawer | **Sliding-window index keys** — rows are keyed by index into `events.slice(-N)`, so every streamed event shifts every key and an expanded reasoning block re-attaches to a different event. `SlideOver.jsx:1169` | key on `ts`+kind |
| M12 | ✅ | Drawer | **O(n²) in the live feed:** `visible.indexOf(e)` per row, per render, re-run on every SSE frame (a real task here has 2,015 events). `SlideOver.jsx:1176` | use the map index |
| M13 | ⬜ | Settings | The project-card header is a keyboard-inert `<div>`; expanding a project is the only route to its repos and test plan, and it is mouse-only. `Settings.jsx:461` | make it a `<button>` |

## OPERATOR-REQUESTED — board restructure (2026-07-14)

Requested directly by the operator. This supersedes parts of M6 (lane widths) and N3
(the caught-up empty state), because two of the six lanes leave the board entirely.

| # | Status | What |
|---|---|---|
| R1 | ⬜ | **The board shows THREE lanes only: Needs Answer · Working · Review PR.** Done and Failed are outcomes, not gates — they compete for width with the lanes that actually need the human. Removing them gives the three gate lanes the space M6 was fighting for. |
| R2 | ⬜ | **Done and Failed become two buttons in the bottom-left corner, above the "Connected" indicator** — Done with a **green** outline, Failed with a **red** outline. |
| R3 | ⬜ | **Clicking either opens a screen with those tasks in a TABLE view** (not a lane of cards): the table is the right form for an outcome list you scan and sort, and it is where a count, a date, a cost and a PR link belong. |

Design notes for the implementer (verify each before building):
- `boardLanes.js` `LANES` is the single source for the board's columns, and `routeTask()` already
  routes by *what the human owes*. Removing two lanes must NOT change routing — a done/failed task
  still has a lane key; it is just not rendered as a column.
- The counts stay honest: the Failed button's count must use `isRealFailure` (cancels are not
  failures — see M2), and it should surface the cancelled count separately rather than hiding it.
- The task table already exists on Stats (`Stats.jsx` — the task table with tokens/cost/PR columns).
  Reuse that component rather than building a second table.
- Keep the keyboard path: the buttons are `<button>`s, and the table rows must open the same drawer
  the board cards do (Enter included — see B2).

## MINOR

| # | Status | Defect |
|---|---|---|
| N1 | ✅ | `.rich-tool-group` is `role="button"` with no `onKeyDown` — focusable, announced as a button, dead to Enter/Space. `SlideOver.jsx:473` |
| N2 | ⬜ | The "Show N more" lane expander sits below the fold in the very lanes that need it. `styles.css` — make it `position: sticky; bottom: 0` |
| N3 | ⬜ | Prominence inversion: the green "No PRs waiting for review" panel out-shouts every real gate card. A caught-up lane should be a quiet checkmark, not a billboard. |
| N4 | ⬜ | "waiting on you: 5d" — the freshest signal on the board — is truncated to `wa…` on mobile and is 12px grey on desktop. |
| N5 | ⬜ | Composer: the kind chips and repo pills sit *outside* the card's border, unlabeled, while priority (least consequential) sits inside it. |
| N6 | ⬜ | Every card spends its top line — the strongest position — on an 8-char hex id. `Board.jsx:216` |
| N7 | ✅ | Icon-only dismiss button with no accessible name. `SlideOver.jsx:385` |
| N8 | ⬜ | WebSocket reconnect `setTimeout` id is never captured, so the cleanup cannot cancel it. `App.jsx:468` |
| N9 | ⬜ | SSE seed race: events streamed during the initial fetch are replaced away and never re-fetched. `SlideOver.jsx:812` |
| N10 | ⬜ | 16 provably-dead CSS classes (`activity-*` flat feed, `nh-nav*`, `sys-tree*`, `ob-eyebrow`, `checklist-post-action`) — proven dead by enumerating every dynamic `className` template. |
| N11 | ⬜ | Board, Stats and Settings render no `<h1>`; the board page has zero headings, so a screen reader gets no document outline. |
| N12 | ⬜ | `tasksReducer` "sync" never deletes — a task removed server-side lingers until reload. `App.jsx:78` |
| N13 | ⬜ | 320px (iPhone SE 1st gen): the nav overflows behind a suppressed scrollbar; still reachable by swipe, but the affordance is gone. |

---

### Corrections the reviewers forced on the audit ITSELF
- **B4's premise was overstated.** The drawer ALREADY opened on Review for `awaiting_approval`, and
  the **Reply** button lives in the persistent action bar on *every* tab — so the answer path was
  never "two tabs away". No parked task in the live DB carries one-click `options` either. The real,
  smaller win: a parked task's **question** is on screen when the drawer opens, instead of behind a
  pipeline diagram.
- **M7's "the drawer's agent cards clip mid-word" did not reproduce** — those snippets truncate with
  a visible ellipsis; the mid-word slicing was board-specific (M5). Only the two mechanical defects
  inside M7 were real (`.sys-node-model` padding, `.sys-node-last-text` max-width) and are fixed.
  The System-tab **redesign** (it prints the same facts twice and pays ~600px for it) stays open.
- **M2b's first fix was a NO-OP on live data** — `topPrioritised` read `cancelled` off a group's
  representative, and the representative was itself a cancel. Fixed at the source (a real failure
  now outranks a cancel as the group's head) with a test for the composition the app actually runs.

## Already fixed this cycle

- ✅ **Undefined CSS variables** falling back to hardcoded *dark* literals (`--fg`, `--surface`,
  `--surface-raised`, `--panel-2`, `--accent-400`) — the Stats cost figure was rendering at
  **1.28:1** (invisible) in light mode. Plus dark-only tokens (`--accent-300`, `--c-*`) and the
  missing `--role-color` rules for `planner`/`watcher` (which fell back to `#555`). Guarded by
  `web/src/themeVars.test.mjs` (mutation-tested). — **#50**
- ✅ **Settings and the theme toggle were unreachable on a phone** (rendered past the right edge with
  nothing to scroll), and the logo's accessible name was a stale codename. — **#51**
- ✅ **The composer** (Task 5A): Tailwind `border` painting nothing with Preflight off, a failed grill
  destroying the operator's prompt, the PR ref not surviving the grill's rewrite, no focus trap,
  2.85:1 accent text. — **#49**

## Reviewer-confirmed CLEAN (do not re-audit)

- The preflight-off border bug exists **nowhere else** — no `.jsx` outside `TaskComposer.jsx` uses a
  Tailwind utility (proven three ways, including a live sweep of the running app).
- No re-render storm on WebSocket events (the server pushes on change; ~1 frame / 15s at idle).
- No memory leaks except the WS reconnect timer (N8): every `EventSource`, `setInterval`, poll loop
  and debounce timer is torn down.
- The drawer restores focus to the originating card on close.
- The drawer's agent cards truncate with a visible ellipsis (the mid-word slicing is board-specific).
