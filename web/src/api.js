import { makeEndpointGate } from "./queueHealthGate.js";

const BASE = import.meta.env.DEV ? "" : "";

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

export async function createTask({ title, description, repo_path, project_id, kind, priority, acceptance_criteria, backend }) {
  const r = await fetch(`${BASE}/api/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, description, repo_path, project_id, kind, priority, acceptance_criteria, backend }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || `POST /api/tasks → ${r.status}`);
  }
  return r.json();
}

export async function uploadAttachment(taskId, file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${BASE}/api/tasks/${taskId}/attachments`, {
    method: "POST", body: fd,
  });
  if (!r.ok) throw new Error(`upload ${file.name} → ${r.status}`);
  return r.json();
}

export async function approveTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/approve`, { method: "POST" });
  if (!r.ok) throw new Error(`POST approve → ${r.status}`);
  return r.json();
}

export async function finishReview(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/finish-review`, { method: "POST" });
  if (!r.ok) throw new Error(`POST finish-review → ${r.status}`);
  return r.json();
}

export async function replyTask(id, answer) {
  const r = await fetch(`${BASE}/api/tasks/${id}/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer }),
  });
  if (!r.ok) throw new Error(`POST reply → ${r.status}`);
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
  if (!r.ok) throw new Error(`POST reply(choose) → ${r.status}`);
  return r.json();
}

export async function sendBack(id, message) {
  const r = await fetch(`${BASE}/api/tasks/${id}/send-back`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!r.ok) throw new Error(`POST send-back → ${r.status}`);
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
    throw new Error(detail.detail || `POST /api/grill → ${r.status}`);
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
        if (onError) onError(new Error(d.detail || `POST /api/grill/stream → ${r.status}`));
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
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST pause → ${r.status}`); }
  return r.json();
}

export async function resumeTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/resume`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST resume → ${r.status}`); }
  return r.json();
}

export async function cancelTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/cancel`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST cancel → ${r.status}`); }
  return r.json();
}

export async function retryTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/retry`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST retry → ${r.status}`); }
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
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST rules → ${r.status}`); }
  return r.json();
}

export async function removeRule(id) {
  const r = await fetch(`${BASE}/api/rules/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `DELETE rules → ${r.status}`); }
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
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST skills → ${r.status}`); }
  return r.json();
}

export async function removeSkill(id) {
  const r = await fetch(`${BASE}/api/skills/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `DELETE skills → ${r.status}`); }
  return r.json();
}

export async function fetchLearnings({ active = false } = {}) {
  const r = await fetch(`${BASE}/api/learnings?active=${active}`);
  if (!r.ok) throw new Error(`GET /api/learnings → ${r.status}`);
  return r.json();
}

export async function confirmLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/confirm`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST confirm → ${r.status}`); }
  return r.json();
}

export async function rejectLearning(id) {
  const r = await fetch(`${BASE}/api/learnings/${id}/reject`, { method: "POST" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST reject → ${r.status}`); }
  return r.json();
}

export async function fetchConfig() {
  const r = await fetch(`${BASE}/api/config`);
  if (!r.ok) throw new Error(`GET /api/config → ${r.status}`);
  return r.json();
}

export async function fetchProfiles() {
  const r = await fetch(`${BASE}/api/profiles`);
  if (!r.ok) return [];
  return r.json();
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
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST projects → ${r.status}`); }
  return r.json();
}

export async function updateProject(id, body) {
  const r = await fetch(`${BASE}/api/projects/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `PUT projects → ${r.status}`); }
  return r.json();
}

export async function deleteProject(id) {
  const r = await fetch(`${BASE}/api/projects/${id}`, { method: "DELETE" });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `DELETE projects → ${r.status}`); }
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
    throw new Error(d.detail || `POST post-review-comments → ${r.status}`);
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
  if (!r.ok) throw new Error(`PUT ${path} → ${r.status}`);
  return r.json();
}

async function _post(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `POST ${path} → ${r.status}`); }
  return r.json();
}

export async function fetchOnboardingStatus() {
  const r = await fetch(`${BASE}/api/onboarding/status`);
  if (!r.ok) throw new Error(`GET onboarding/status → ${r.status}`);
  return r.json();
}
export const detectRepos       = (root)    => _post("/api/onboarding/repos/detect", { root });
export const onboardRepo       = (repo_path) => _post("/api/onboarding/repos/onboard", { repo_path });
export const extractHistory    = ()        => _post("/api/onboarding/history/extract", {});
export const analyzeHistory    = (days = 30) => _post("/api/onboarding/history/analyze", { days });
export const confirmRules      = (ids)     => _post("/api/onboarding/rules/confirm", { ids });
export const completeOnboarding = (payload) => _post("/api/onboarding/complete", payload);
export const generateDocs      = (repo_path) => _post("/api/onboarding/docs/generate", { repo_path });

// ── TRACKER settings (used by Settings page; board import removed) ──────────────
export async function fetchTrackerSettings() {
  const r = await fetch(`${BASE}/api/settings/tracker/boards`);
  if (!r.ok) return { boards: [] };
  return _jsonSafe(r, { boards: [] });
}
export async function fetchTrackerTransport() {
  const r = await fetch(`${BASE}/api/settings/tracker/transport`);
  if (!r.ok) return { transport: "unconfigured" };
  return _jsonSafe(r, { transport: "unconfigured" });
}
export const updateTrackerBoards = (boards) => _put("/api/settings/tracker/boards", { boards });

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

export function connectWS(onMessage) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const host = import.meta.env.DEV ? "127.0.0.1:8420" : location.host;
  const ws = new WebSocket(`${proto}://${host}/ws`);
  ws.onmessage = (e) => {
    try {
      onMessage(JSON.parse(e.data));
    } catch {
      /* ignore malformed frame */
    }
  };
  ws.onerror = () => {};
  return ws;
}
