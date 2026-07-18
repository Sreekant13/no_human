import { test, expect } from "@playwright/test";

// ── Mock data ────────────────────────────────────────────────────────────────

const TASK_WITH_SPEC = {
  id: "t-001",
  title: "Add user profile page",
  status: "implementing",
  kind: "feature",
  priority: "medium",
  description: "Build a user profile page with avatar upload",
  acceptance_criteria: ["Show user name", "Upload avatar", "Edit bio"],
  repo_path: "/src/app",
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  attempt_count: 1,
  attempts: [],
  context: {
    // Shape matches the real backend TaskSpec (core/task.py) produced by
    // TaskSpec.from_plan() — files_to_change/approach/test_plan/out_of_scope/
    // verification. There is no separate "design" object; the backend never
    // populates one.
    spec: {
      files_to_change: ["src/pages/Profile.jsx", "src/components/Avatar.jsx", "src/api/user.js"],
      approach: "Create a responsive user profile page with avatar upload capability. "
        + "Add Profile.jsx and Avatar.jsx, wire them into the router, and extend user.js "
        + "with an upload endpoint.",
      test_plan: "Unit tests for form validation, integration test for upload flow. "
        + "renders user name correctly; handles avatar upload and preview.",
      out_of_scope: ["Social media integration", "Profile privacy settings"],
      verification: "npm test -- src/pages/Profile.test.jsx",
    },
    progress: {
      acceptance_criteria: [
        { status: "done", evidence: "User name renders in header" },
        { status: "in_progress" },
        null,
      ],
    },
  },
};

const TASK_WITH_REVIEW = {
  id: "t-002",
  title: "Fix login bug",
  status: "awaiting_approval",
  kind: "bugfix",
  description: "Fix the login redirect loop",
  acceptance_criteria: ["Login redirects to dashboard"],
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  attempt_count: 2,
  attempts: [
    {
      id: "a1",
      attempt_number: 1,
      review_passed: false,
      review_checklist: {
        items: [
          { label: "Tests pass", passed: true, evidence: "All 12 tests pass" },
          { label: "No redirect loop", passed: false, evidence: "Still loops on /callback" },
          { label: "Session persists", passed: true, evidence: "Cookie set correctly" },
        ],
      },
    },
    {
      id: "a2",
      attempt_number: 2,
      review_passed: false,
      review_checklist: {
        items: [
          { label: "Tests pass", passed: true, evidence: "All 12 tests pass" },
          { label: "No redirect loop", passed: false, evidence: "Still loops on /callback" },
          { label: "Session persists", passed: true, evidence: "Cookie set correctly" },
        ],
        stages: {
          spec_compliance: { passed: true },
          code_quality: { passed: false },
        },
        suggested_next: "Consider using a state machine for auth flow transitions",
      },
    },
  ],
  context: {},
};

const TASK_NO_SPEC = {
  id: "t-003",
  title: "New task pending",
  status: "pending",
  kind: "feature",
  description: "A pending task",
  acceptance_criteria: [],
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  attempts: [],
  context: {},
};

const ALL_TASKS = [TASK_WITH_SPEC, TASK_WITH_REVIEW, TASK_NO_SPEC];

// ── API route mocking ────────────────────────────────────────────────────────

async function mockAPI(page) {
  await page.route("**/api/tasks", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ALL_TASKS) });
  });
  await page.route("**/api/tasks/t-001", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(TASK_WITH_SPEC) });
  });
  await page.route("**/api/tasks/t-002", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(TASK_WITH_REVIEW) });
  });
  await page.route("**/api/tasks/t-003", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(TASK_NO_SPEC) });
  });
  await page.route("**/api/tasks/*/diff", (route) => {
    route.fulfill({ status: 200, contentType: "text/plain", body: "" });
  });
  await page.route("**/api/tasks/*/events", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/tasks/*/events/stream", (route) => {
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "data: {}\n\n" });
  });
  await page.route("**/api/projects", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
  await page.route("**/api/onboarding/status", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ completed: true }) });
  });
  await page.route("**/api/worker/status", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ running: false, inflight: 0, max_workers: 4 }) });
  });
  await page.route("**/ws", (route) => {
    route.abort();
  });
}

// ── Tests ────────────────────────────────────────────────────────────────────

test.describe("SDLC UI Enhancements", () => {

  test.beforeEach(async ({ page }) => {
    await mockAPI(page);
    await page.goto("/");
    // Wait for task cards to appear on the board
    await page.waitForSelector(".task-card", { timeout: 5000 });
  });

  test("SVG icons render instead of emoji characters", async ({ page }) => {
    // Open a task with review data to see pass/fail icons
    await page.locator(".task-card").first().click();
    await page.waitForSelector(".slideover", { timeout: 3000 });

    // Close button should have SVG, not ✕ text
    const closeBtn = page.locator(".so-close");
    await expect(closeBtn.locator("svg.nh-icon")).toBeVisible();

    // Tab list should include "spec"
    const specTab = page.locator(".so-tab", { hasText: "spec" });
    await expect(specTab).toBeVisible();
  });

  test("Spec tab shows structured spec data", async ({ page }) => {
    // Click the task that has spec data (t-001)
    await page.locator(".task-card", { hasText: "Add user profile page" }).click();
    await page.waitForSelector(".slideover", { timeout: 3000 });

    // Click the spec tab
    await page.locator(".so-tab", { hasText: "spec" }).click();

    // Verify spec content renders
    const specTab = page.locator("[data-testid='spec-tab']");
    await expect(specTab).toBeVisible();

    // Files to change
    await expect(specTab.locator(".spec-file-item").first()).toBeVisible();
    await expect(specTab.locator("text=src/pages/Profile.jsx")).toBeVisible();

    // Approach
    await expect(specTab.locator("text=Create a responsive user profile page")).toBeVisible();

    // Test plan
    await expect(specTab.locator("text=Unit tests for form validation")).toBeVisible();

    // Out of scope
    await expect(specTab.locator("text=Social media integration")).toBeVisible();

    // Verification
    await expect(specTab.locator("text=npm test -- src/pages/Profile.test.jsx")).toBeVisible();
  });

  test("Spec tab shows fallback for tasks without spec", async ({ page }) => {
    await page.locator(".task-card", { hasText: "New task pending" }).click();
    await page.waitForSelector(".slideover", { timeout: 3000 });

    await page.locator(".so-tab", { hasText: "spec" }).click();

    const empty = page.locator("[data-testid='spec-empty']");
    await expect(empty).toBeVisible();
    await expect(empty).toContainText("Spec not generated yet");
  });

  test("Activity tab renders markdown, tool result previews, and collapsible thinking", async ({ page }) => {
    const events = [
      { ts: 1000, kind: "tool_use", source: "agent", tool_name: "Read",
        tool_input: { file_path: "/repo/Jenkinsfile" },
        text: "Read repo/Jenkinsfile",
        result_preview: "stage('Build') {\n    sh 'make build'\n}" },
      { ts: 1002, kind: "thinking", source: "agent",
        text: "I should check whether the stage already exists before adding a new one." },
      { ts: 1005, kind: "agent_text", source: "agent",
        text: "Here is the plan:\n\n| File | Change |\n|------|--------|\n| Jenkinsfile | add stage |\n\n**Note:** out of scope items are skipped." },
    ];
    await page.route("**/api/tasks/t-001/events", (route) => {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(events) });
    });

    await page.locator(".task-card", { hasText: "Add user profile page" }).click();
    await page.waitForSelector(".slideover", { timeout: 3000 });
    await page.locator(".so-tab", { hasText: "activity" }).click();

    const feed = page.locator(".activity-feed");
    await expect(feed).toBeVisible();

    // tool_result preview surfaces the actual output, not just the call.
    await expect(feed.locator(".rich-tool-result", { hasText: "make build" })).toBeVisible();

    // Thinking is collapsed by default...
    const thinkingToggle = feed.locator(".rich-thinking-toggle");
    await expect(thinkingToggle).toBeVisible();
    await expect(feed.locator(".rich-thinking-body")).toHaveCount(0);
    // ...and expands to reveal the reasoning text.
    await thinkingToggle.click();
    await expect(feed.locator(".rich-thinking-body")).toContainText("check whether the stage already exists");

    // agent_text renders real markdown: a table with a header cell, and bold text.
    await expect(feed.locator(".md table th", { hasText: "File" })).toBeVisible();
    await expect(feed.locator(".md strong", { hasText: "Note:" })).toBeVisible();
  });

  test("DetailsTab shows criteria tracking with status icons", async ({ page }) => {
    await page.locator(".task-card", { hasText: "Add user profile page" }).click();
    await page.waitForSelector(".slideover", { timeout: 3000 });

    await page.locator(".so-tab", { hasText: "details" }).click();

    const criteriaList = page.locator("[data-testid='criteria-list']");
    await expect(criteriaList).toBeVisible();

    // Should have 3 criteria
    const items = criteriaList.locator(".criterion-tracked");
    await expect(items).toHaveCount(3);

    // First criterion should be "done" with check icon
    const first = items.nth(0);
    await expect(first).toHaveClass(/done/);
    await expect(first.locator("svg.nh-icon")).toBeVisible();

    // Second should be "in_progress" with spinner
    const second = items.nth(1);
    await expect(second).toHaveClass(/in_progress/);
    await expect(second.locator(".criterion-spinner")).toBeVisible();

    // Third should be "not_started"
    const third = items.nth(2);
    await expect(third).toHaveClass(/not_started/);

    // Evidence should show for first criterion
    await expect(first.locator(".criterion-evidence")).toContainText("User name renders in header");
  });

  test("ReviewTab shows two-stage badges and suggested_next", async ({ page }) => {
    await page.locator(".task-card", { hasText: "Fix login bug" }).click();
    await page.waitForSelector(".slideover", { timeout: 3000 });

    await page.locator(".so-tab", { hasText: "review" }).click();

    // Two-stage badges
    const stages = page.locator("[data-testid='review-stages']");
    await expect(stages).toBeVisible();
    await expect(stages.locator(".review-stage-badge.pass")).toContainText("Stage 1");
    await expect(stages.locator(".review-stage-badge.fail")).toContainText("Stage 2");

    // Suggested next
    const hint = page.locator("[data-testid='suggested-next']");
    await expect(hint).toBeVisible();
    await expect(hint).toContainText("state machine for auth flow");

    // SVG icons in checklist items
    const checkIcons = page.locator(".ci-icon svg.nh-icon");
    await expect(checkIcons.first()).toBeVisible();
  });

  test("AttemptsTab shows stagnation warning and pass-rate trend", async ({ page }) => {
    await page.locator(".task-card", { hasText: "Fix login bug" }).click();
    await page.waitForSelector(".slideover", { timeout: 3000 });

    await page.locator(".so-tab", { hasText: "attempts" }).click();

    // Stagnation warning (both attempts have 2/3 pass rate)
    const stagnation = page.locator("[data-testid='stagnation-warning']");
    await expect(stagnation).toBeVisible();
    await expect(stagnation).toContainText("No progress between last two attempts");

    // Pass rate trend
    const trend = page.locator("[data-testid='pass-rate-trend']");
    await expect(trend).toBeVisible();
    await expect(trend).toContainText("2/3");
  });

  test("Stats page shows new SDLC metric cards", async ({ page }) => {
    // Navigate to stats page
    await page.locator(".nh-nav-btn", { hasText: "Stats" }).click();
    await page.waitForTimeout(500);

    // New metric cards should exist (may show "—" since mock data is minimal)
    await expect(page.locator("[data-testid='stat-first-attempt']")).toBeVisible();
    await expect(page.locator("[data-testid='stat-spec-coverage']")).toBeVisible();
    await expect(page.locator("[data-testid='stat-stagnation']")).toBeVisible();

    // Verify labels
    await expect(page.locator("[data-testid='stat-first-attempt']")).toContainText("First-Attempt Pass");
    await expect(page.locator("[data-testid='stat-spec-coverage']")).toContainText("Spec Coverage");
    await expect(page.locator("[data-testid='stat-stagnation']")).toContainText("Stagnation");
  });

  test("Onboarding pipeline shows updated phases", async ({ page }) => {
    // Mock onboarding as not complete to show the wizard
    await page.route("**/api/onboarding/status", (route) => {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ completed: false }) });
    });
    await page.goto("/");

    // Wait for the onboarding flow diagram
    await page.waitForSelector(".ob-flow", { timeout: 5000 });

    const flow = page.locator(".ob-flow");
    await expect(flow).toContainText("intake");
    await expect(flow).toContainText("spec");
    await expect(flow).toContainText("plan");
    await expect(flow).toContainText("build");
    await expect(flow).toContainText("review");
  });

  test("SVG icons in review checklist use proper vector icons", async ({ page }) => {
    await page.locator(".task-card", { hasText: "Fix login bug" }).click();
    await page.waitForSelector(".slideover", { timeout: 3000 });

    await page.locator(".so-tab", { hasText: "review" }).click();

    // Checklist pass/fail icons should be SVGs
    const passIcon = page.locator(".checklist-item.pass .ci-icon svg.nh-icon").first();
    await expect(passIcon).toBeVisible();

    const failIcon = page.locator(".checklist-item.fail .ci-icon svg.nh-icon").first();
    await expect(failIcon).toBeVisible();
  });
});
