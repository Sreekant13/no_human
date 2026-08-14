import { makeEndpointGate } from "./queueHealthGate.js";
import { detailMessage } from "./apiError.js";

// Both arms of the old `import.meta.env.DEV ? "" : ""` were the empty string —
// vite folded it to "" in every build, so this is byte-identical at runtime.
// Written literally because `import.meta.env` is undefined outside vite, and
// that one expression made this whole module unimportable under `node --test`:
// api.test.mjs can now exercise these functions for real instead of asserting
// over their source text. (connectWS still reads import.meta.env, but only when
// called, and it needs a browser anyway.)
const BASE = "";

/** Guard against the SPA catch-all returning index.html instead of JSON. */
function _jsonSafe(r, fallback) {
  const ct = (r.headers.get("content-type") || "");
  if (!ct.includes("application/json")) return fallback;
  return r.json();
}

export async function fetchTasks() {
  const r = await fetch(`${BASE}/api/tasks`);
  if (!r.ok) throw new Error(`GET /api/tasks → ${r.status}`);
  return r.json();
}

export async function fetchTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}`);
  if (!r.ok) throw new Error(`GET /api/tasks/${id} → ${r.status}`);
  return r.json();
}

export async function fetchDiff(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/diff`);
  if (!r.ok) return "";
  return r.text();
}

export async function createTask({ title, description, repo_path, project_id, kind, priority, acceptance_criteria, source, external_id }) {
  const r = await fetch(`${BASE}/api/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, description, repo_path, project_id, kind, priority, acceptance_criteria, source, external_id }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `POST /api/tasks → ${r.status}`));
  }
  return r.json();
}

export async function uploadAttachment(taskId, file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${BASE}/api/tasks/${taskId}/attachments`, {
    method: "POST", body: fd,
  });
  // detailMessage, like every other mutating call here: the server explains WHY
  // (a 409 reason, a 422 naming the offending field) and a bare status code
  // threw that away, leaving the operator unable to tell a REFUSED action from
  // a network blip. This one also has a 413/415 the operator can act on.
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `upload ${file.name} → ${r.status}`));
  }
  return r.json();
}

export async function approveTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/approve`, { method: "POST" });
  if (!r.ok) {
    // The server explains WHY (409: the task is no longer awaiting approval).
    // A bare status code left the operator with no way to tell a rejected
    // approval from a network blip.
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `POST approve → ${r.status}`));
  }
  return r.json();
}

export async function finishReview(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/finish-review`, { method: "POST" });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST finish-review → ${r.status}`));
  }
  return r.json();
}

export async function replyTask(id, answer) {
  const r = await fetch(`${BASE}/api/tasks/${id}/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST reply → ${r.status}`));
  }
  return r.json();
}

// W2.4: answer a blocker by choosing option N (1-based). The server applies
// the option's action (if any) and resumes — the only path that may apply
// actions, and it runs on a human's click.
export async function chooseBlockerOption(id, choose) {
  const r = await fetch(`${BASE}/api/tasks/${id}/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ choose }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST reply(choose) → ${r.status}`));
  }
  return r.json();
}

export async function sendBack(id, message) {
  const r = await fetch(`${BASE}/api/tasks/${id}/send-back`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!r.ok) {
    // The 409s here say "task is already done" / "task is cancelled"
    // (api/app.py send_back) - the reason the send-back did not take.
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST send-back → ${r.status}`));
  }
  return r.json();
}

// ── Intake grill ────────────────────────────────────────────────────────────

export async function grillStep({ title, description, repo_path, project_id, qa_history }) {
  const r = await fetch(`${BASE}/api/grill`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, description, repo_path, project_id, qa_history: qa_history || [] }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `POST /api/grill → ${r.status}`));
  }
  return r.json();
}

// ── Grill SSE streaming ─────────────────────────────────────────────────────

export function grillStepSSE({ title, description, repo_path, project_id, qa_history }, onEvent, onResult, onError, onEval) {
  // POST-based SSE: we need to fetch as a stream since EventSource only does GET.
  const ctrl = new AbortController();
  (async () => {
    try {
      const r = await fetch(`${BASE}/api/grill/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, description, repo_path, project_id, qa_history: qa_history || [] }),
        signal: ctrl.signal,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        if (onError) onError(new Error(detailMessage(d, `POST /api/grill/stream → ${r.status}`)));
        return;
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const data = JSON.parse(line.slice(6));
            if (data.kind === "done") { return; }
            if (data.kind === "eval_verdict") {
              if (onEval) onEval(data);
            } else if (data.kind === "grill_result" || data.kind === "grill_question") {
              if (onResult) onResult(data);
            } else if (data.kind === "error") {
              if (onError) onError(new Error(data.text || "grill error"));
            } else {
              if (onEvent) onEvent(data);
            }
          } catch { /* skip malformed */ }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError" && onError) onError(err);
    }
  })();
  return { close: () => ctrl.abort() };
}

// ── Task lifecycle ──────────────────────────────────────────────────────────

export async function pauseTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/pause`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST pause → ${r.status}`)); }
  return r.json();
}

export async function resumeTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/resume`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST resume → ${r.status}`)); }
  return r.json();
}

export async function cancelTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/cancel`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST cancel → ${r.status}`)); }
  return r.json();
}

export async function retryTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/retry`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST retry → ${r.status}`)); }
  return r.json();
}

// ── Knowledge management ────────────────────────────────────────────────────

export async function fetchRules() {
  const r = await fetch(`${BASE}/api/rules`);
  if (!r.ok) throw new Error(`GET /api/rules → ${r.status}`);
  return r.json();
}

export async function addRule({ title, content, tags, project }) {
  const r = await fetch(`${BASE}/api/rules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, content, tags: tags || [], project }),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST rules → ${r.status}`)); }
  return r.json();
}

export async function removeRule(id) {
  const r = await fetch(`${BASE}/api/rules/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `DELETE rules → ${r.status}`)); }
  return r.json();
}

export async function fetchSkills() {
  const r = await fetch(`${BASE}/api/skills`);
  if (!r.ok) throw new Error(`GET /api/skills → ${r.status}`);
  return r.json();
}

export async function addSkill({ title, content, tags, project }) {
  const r = await fetch(`${BASE}/api/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, content, tags: tags || [], project }),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST skills → ${r.status}`)); }
  return r.json();
}

export async function removeSkill(id) {
  const r = await fetch(`${BASE}/api/skills/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `DELETE skills → ${r.status}`)); }
  return r.json();
}

export async function fetchLearnings({ active = false } = {}) {
  const r = await fetch(`${BASE}/api/learnings?active=${active}`);
  if (!r.ok) throw new Error(`GET /api/learnings → ${r.status}`);
  return r.json();
}

export async function fetchQuarantineCounts() {
  const r = await fetch(`${BASE}/api/memories/quarantine`);
  if (!r.ok) throw new Error(`GET /api/memories/quarantine → ${r.status}`);
  return r.json();
}

export async function confirmLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/confirm`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST confirm → ${r.status}`)); }
  return r.json();
}

export async function rejectLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/reject`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST reject → ${r.status}`)); }
  return r.json();
}

// Memory lifecycle C: the retire? section — stale ACTIVE (confirmed) rules,
// suggest-only. Read-only; nothing here archives anything.
export async function fetchRetireCandidates({ days = 90 } = {}) {
  const r = await fetch(`${BASE}/api/learnings/retire-candidates?days=${days}`);
  if (!r.ok) throw new Error(`GET retire-candidates → ${r.status}`);
  return r.json();
}

// The human's explicit yes to a retire? suggestion. Reversible server-side
// (archive, never delete); idempotent (a second call reports
// `already_archived` rather than erroring).
export async function retireLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/retire`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST retire → ${r.status}`)); }
  return r.json();
}

export async function fetchConfig() {
  const r = await fetch(`${BASE}/api/config`);
  if (!r.ok) throw new Error(`GET /api/config → ${r.status}`);
  return r.json();
}

// The running `nh` version and distribution channel, for the browser path
// where there is no desktop bridge to read it from. Never throws a version
// out of thin air: the caller treats a failure as "unknown", which is what it
// was before this existed. `published` fails closed — an older server that
// only ever returned `{version}` (or a malformed body) reads as unpublished,
// never as a command that might not resolve.
export async function fetchVersion() {
  const r = await fetch(`${BASE}/api/version`);
  if (!r.ok) throw new Error(`GET /api/version → ${r.status}`);
  const d = await r.json();
  const version = typeof d?.version === "string" && d.version ? d.version : null;
  const distName = typeof d?.dist_name === "string" && d.dist_name ? d.dist_name : null;
  const published = d?.published === true;
  return { version, distName, published };
}

export async function fetchProfiles() {
  const r = await fetch(`${BASE}/api/profiles`);
  if (!r.ok) return [];
  return r.json();
}

// Auth status for the Settings Account panel. Returns null when the endpoints
// are absent (a build without the auth endpoints) so the panel degrades to an
// "unavailable" note instead of throwing. The payload never contains a token.
export async function fetchAuthStatus() {
  try {
    const r = await fetch(`${BASE}/api/auth/status`);
    if (!r.ok) return null;
    return r.json();
  } catch { return null; }
}

// Write an OAuth token for a profile. On a 422 refusal (a metered API key, an
// empty token, or a newline) `_put` throws Error(detail) — a human-facing
// message written to be shown verbatim. Never returns the token.
export async function setAuthToken(profile, token) {
  return _put("/api/auth/token", { profile, token });
}

// C3-G3: the repos the operator knows (for the repo-understanding picker).
export async function fetchRepos() {
  try {
    const r = await fetch(`${BASE}/api/repos`);
    if (!r.ok) return [];
    // await so a malformed body rejects INSIDE this try (the fetchMetrics
    // fix, PR #111; its review found these two siblings).
    return await r.json();
  } catch {
    return [];
  }
}

// C3-G3: what no_human understands about one known repo (profile + cached
// repo map + matched playbooks). Null when the repo is unknown/unavailable.
export async function fetchRepoUnderstanding(path) {
  try {
    const r = await fetch(`${BASE}/api/repo?path=${encodeURIComponent(path)}`);
    if (!r.ok) return null;
    // await so a malformed body rejects INSIDE this try (the fetchMetrics
    // fix, PR #111; its review found these two siblings).
    return await r.json();
  } catch {
    return null;
  }
}

// C3-G4: cross-task full-text search over the failure/fix record. [] on any
// error or empty query so the search box degrades quietly.
export async function searchEvents(q) {
  if (!q || !q.trim()) return [];
  try {
    const r = await fetch(`${BASE}/api/search?q=${encodeURIComponent(q)}`);
    if (!r.ok) return [];
    return await r.json();   // await so a malformed body rejects INSIDE this try
  } catch {
    return [];
  }
}

// The north-star numbers straight from the record (PRs merged/opened,
// tokens-per-PR, review verdicts, cache economics). Null when unavailable so
// Stats degrades to its client-side aggregates.
export async function fetchMetrics() {
  try {
    const r = await fetch(`${BASE}/api/metrics`);
    if (!r.ok) return null;
    // await so a malformed body rejects INSIDE this try (same fix as
    // searchEvents) — un-awaited, a 200-with-bad-JSON rejected outside the
    // catch and left Stats' loader spinning forever (PR #108 review, low).
    return await r.json();
  } catch {
    return null;
  }
}

// The latest north-star bench card, for the instrument-trust surface on Stats.
// Contract: GET /api/bench/latest (a separate endpoint from /api/metrics, which
// carries TASK metrics only). 404 = the endpoint exists but no run is recorded
// -> a "no run yet" sentinel so the UI says so instead of showing zeros. Any
// other non-ok / network error / non-object body -> null, so the section hides
// gracefully on a build that does not expose the endpoint yet. Never throws.
export async function fetchBenchLatest() {
  try {
    const r = await fetch(`${BASE}/api/bench/latest`);
    if (r.status === 404) return { norun: true };
    if (!r.ok) return null;
    const data = await r.json();
    return data && typeof data === "object" && !Array.isArray(data) ? data : null;
  } catch {
    return null;
  }
}

export async function fetchProjects() {
  const r = await fetch(`${BASE}/api/projects`);
  if (!r.ok) return [];
  return r.json();
}

export async function createProject({ name, repo_paths, primary_repo }) {
  const r = await fetch(`${BASE}/api/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, repo_paths, primary_repo }),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST projects → ${r.status}`)); }
  return r.json();
}

export async function scaffoldRepo(parent, name) {
  const r = await fetch(`${BASE}/api/repos/scaffold`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent, name }),
  });
  // The backend's `detail` names WHICH validation failed - surface it verbatim.
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST repos/scaffold → ${r.status}`)); }
  return r.json();
}

export async function updateProject(id, body) {
  const r = await fetch(`${BASE}/api/projects/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `PUT projects → ${r.status}`)); }
  return r.json();
}

export async function deleteProject(id) {
  const r = await fetch(`${BASE}/api/projects/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `DELETE projects → ${r.status}`)); }
  return r.json();
}

export async function fetchTaskEvents(taskId) {
  const r = await fetch(`${BASE}/api/tasks/${taskId}/events`);
  if (!r.ok) return [];
  return r.json();
}

export async function fetchSubtasks(taskId) {
  const r = await fetch(`${BASE}/api/tasks/${taskId}/subtasks`);
  if (!r.ok) return [];
  return r.json();
}

export async function postReviewComments(taskId, items = null) {
  const r = await fetch(`${BASE}/api/tasks/${taskId}/post-review-comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(detailMessage(d, `POST post-review-comments → ${r.status}`));
  }
  return r.json();
}

export const fetchQueueHealth = makeEndpointGate(
  () => fetch(`${BASE}/api/queue/health`));

export async function fetchWorkerStatus() {
  const r = await fetch(`${BASE}/api/worker/status`);
  if (!r.ok) return { running: false, inflight: 0, max_workers: 0 };
  return r.json();
}

// ── Onboarding wizard ────────────────────────────────────────────────────────

async function _put(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `PUT ${path} → ${r.status}`));
  }
  return r.json();
}

async function _post(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(detailMessage(d, `POST ${path} → ${r.status}`)); }
  return r.json();
}

export async function fetchOnboardingStatus() {
  const r = await fetch(`${BASE}/api/onboarding/status`);
  if (!r.ok) throw new Error(`GET onboarding/status → ${r.status}`);
  return r.json();
}
export const detectRepos       = (root)    => _post("/api/onboarding/repos/detect", { root });

// Auto-discovery over the conventional clone roots. No body and no root
// parameter by design (see the endpoint's docstring): the scan is bound to the
// user's home, so a plain GET is the whole request.
export const discoverRepos = async (limit) => {
  const qs = limit ? `?limit=${encodeURIComponent(limit)}` : "";
  const r = await fetch(`${BASE}/api/repos/discover${qs}`);
  if (!r.ok) throw new Error(`GET repos/discover → ${r.status}`);
  return r.json();
};

export const onboardRepo       = (repo_path) => _post("/api/onboarding/repos/onboard", { repo_path });

// PROVE a repo's commands by really running them, streaming the real output.
// POST-based SSE (same shape as grillStepSSE above — EventSource is GET-only).
// `onFrame` receives every frame; the caller decides what to render. Returns a
// handle whose close() aborts the stream (the run itself is bounded server-side).
export function proveRepoSSE({ repo_path, test_cmd, install_cmd, timeout }, onFrame, onError) {
  const ctrl = new AbortController();
  (async () => {
    try {
      const r = await fetch(`${BASE}/api/onboarding/repos/prove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_path, test_cmd, install_cmd, timeout }),
        signal: ctrl.signal,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        if (onError) onError(new Error(detailMessage(d, `POST /api/onboarding/repos/prove → ${r.status}`)));
        return;
      }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const data = JSON.parse(line.slice(6));
            if (data.kind === "stream_end") return;
            if (data.kind === "error") {
              if (onError) onError(new Error(data.text || "prove failed"));
              return;
            }
            if (onFrame) onFrame(data);
          } catch { /* skip malformed */ }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError" && onError) onError(err);
    }
  })();
  return { close: () => ctrl.abort() };
}

export const confirmRepoProfile = (repo_path) => _post("/api/onboarding/repos/confirm", { repo_path });

export async function fetchReadiness() {
  const r = await fetch(`${BASE}/api/onboarding/readiness`);
  if (!r.ok) throw new Error(`GET onboarding/readiness → ${r.status}`);
  return r.json();
}
export const extractHistory    = ()        => _post("/api/onboarding/history/extract", {});
export const analyzeHistory    = (days = 30) => _post("/api/onboarding/history/analyze", { days });
export const confirmRules      = (ids)     => _post("/api/onboarding/rules/confirm", { ids });
export const completeOnboarding = (payload) => _post("/api/onboarding/complete", payload);
export const generateDocs      = (repo_path) => _post("/api/onboarding/docs/generate", { repo_path });

// ── Integrations (status registry; secrets never returned) ──────────────────
/**
 * The configured-integrations registry.
 *
 * THROWS on a failed request. It used to swallow every non-ok response into
 * `{integrations: []}`, which is indistinguishable from a healthy server
 * answering "nothing is configured" — so a 500, a dead server or a proxy error
 * all rendered as "Jira is not configured", sending the operator to Settings to
 * fix a token that was never the problem. "I could not ask" and "the answer is
 * none" are different facts and the caller has to be able to tell them apart.
 *
 * The callers that genuinely don't care (the composer's optional Backlog
 * pointer, the settings list) keep their own `.catch`.
 */
export async function fetchIntegrations() {
  let r;
  try {
    r = await fetch(`${BASE}/api/integrations`);
  } catch {
    // fetch() rejects only when the request never got an answer at all.
    throw new Error("the no_human server did not answer");
  }
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `GET /api/integrations → ${r.status}`));
  }
  return _jsonSafe(r, { integrations: [] });
}
// Run a live health check for one integration; returns the updated status.
export const testIntegration = (name) =>
  _post(`/api/integrations/${encodeURIComponent(name)}/test`, {});
// Save an integration's settings-form fields (dirty fields only — see
// Integrations.jsx). Returns the refreshed status card + its `fields` array;
// never a secret value. Throws with the server's 422/404 `detail` message.
export const saveIntegrationConfig = (name, fields) =>
  _put(`/api/integrations/${encodeURIComponent(name)}/config`, { fields });

// ── Onboarding "Connect your tools" (config.yaml only, NEVER a secret) ──────
// One card per block under DEFAULT_CONFIG["integrations"], discovered server-
// side, so a new integration appears with no change here. `secrets` carries
// only env-var NAMES + a `set` bool.
export async function fetchIntegrationSetup() {
  const r = await fetch(`${BASE}/api/integrations/setup`);
  if (!r.ok) return { integrations: [] };
  return _jsonSafe(r, { integrations: [] });
}
// Persist one integration's non-secret settings. The server refuses (422) any
// field that would put a credential in config.yaml.
export const saveIntegrationSetup = (name, values) =>
  _put(`/api/integrations/${encodeURIComponent(name)}/setup`, { values });

// Task 1.6: browse/pick a configured tracker's tickets for the Backlog page.
// Throws with the server's 503 (unconfigured) / 502 (upstream error) `detail`
// message — the page surfaces that text as-is.
//
// ONE implementation for both trackers: /api/integrations/{tracker}/issues have
// the same contract and return the same row shape (TrackerIssueOut), so the
// page has one code path per row whichever tracker it came from.
async function _trackerIssues(tracker, q, limit) {
  const params = new URLSearchParams({ q: q || "", limit: String(limit) });
  const r = await fetch(`${BASE}/api/integrations/${tracker}/issues?${params}`);
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `GET ${tracker}/issues → ${r.status}`));
  }
  return r.json();
}

async function _trackerIssue(tracker, key) {
  const r = await fetch(
    `${BASE}/api/integrations/${tracker}/issues/${encodeURIComponent(key)}`);
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detailMessage(detail, `GET ${tracker}/issues/${key} → ${r.status}`));
  }
  return r.json();
}

export async function searchJiraIssues(q, limit = 50) {
  return _trackerIssues("jira", q, limit);
}
export async function searchLinearIssues(q, limit = 50) {
  return _trackerIssues("linear", q, limit);
}

// SCRUM-9: fetch ONE issue in full when it's picked — the browse list above
// truncates description to 2000 chars (list payload), so the composer prefill
// must not be built from that brief alone. Throws with the server's 503/502
// `detail`, same convention as searchJiraIssues.
export async function fetchJiraIssue(key) {
  return _trackerIssue("jira", key);
}
export async function fetchLinearIssue(key) {
  return _trackerIssue("linear", key);
}

/** Browse both trackers, or whichever ones are configured. `trackers` is the
 * list of names from the integrations registry. */
export function searchTrackerIssues(tracker, q, limit = 50) {
  return tracker === "linear" ? searchLinearIssues(q, limit) : searchJiraIssues(q, limit);
}
export function fetchTrackerIssue(tracker, key) {
  return tracker === "linear" ? fetchLinearIssue(key) : fetchJiraIssue(key);
}

export async function suggestPaths(path) {
  const r = await fetch(`${BASE}/api/fs/suggest?path=${encodeURIComponent(path || "")}`);
  if (!r.ok) return { suggestions: [] };
  return r.json();
}

// ── Phase 4a: SSE live event stream ──────────────────────────────────────────
export function connectTaskSSE(taskId, onEvent, onDone) {
  const url = `${BASE}/api/tasks/${encodeURIComponent(taskId)}/events/stream`;
  const es = new EventSource(url);
  // W2.3: transient errors must NOT end the stream — closing here silently
  // froze long-running tasks in the UI. EventSource reconnects natively and
  // replays Last-Event-ID (the server keys frames by event ts), so we only
  // give up after sustained failure with nothing received in between.
  let consecutiveErrors = 0;
  es.onmessage = (msg) => {
    consecutiveErrors = 0;
    try {
      const data = JSON.parse(msg.data);
      if (data.kind === "done") { es.close(); if (onDone) onDone(); return; }
      if (onEvent) onEvent(data);
    } catch { /* ignore malformed */ }
  };
  es.onerror = () => {
    consecutiveErrors += 1;
    if (consecutiveErrors >= 10) { es.close(); if (onDone) onDone(); }
    // else: let the native reconnect (with Last-Event-ID) do its job.
  };
  return es; // caller can es.close() to unsubscribe
}

// ── WebSocket ───────────────────────────────────────────────────────────────

export function connectWS(onMessage, { onOpen, onClose, makeSocket } = {}) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const host = import.meta.env.DEV ? "127.0.0.1:8420" : location.host;
  const url = `${proto}://${host}/ws`;
  const ws = (makeSocket || ((u) => new WebSocket(u)))(url);
  ws.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data));
    } catch {
      /* ignore malformed frame */
    }
  };
  ws.onopen = () => { if (onOpen) onOpen(); };
  // Both routed to the same handler: a paired error+close from the same
  // socket is ONE disconnect, deduped by the reconnector (wsReconnect.js),
  // not here.
  ws.onclose = () => { if (onClose) onClose(); };
  ws.onerror = () => { if (onClose) onClose(); };
  return ws;
}
