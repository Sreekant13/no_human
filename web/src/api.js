const BASE = import.meta.env.DEV ? "" : "";

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

export async function createTask({ title, description, repo_path, kind, priority, acceptance_criteria }) {
  const r = await fetch(`${BASE}/api/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, description, repo_path, kind, priority, acceptance_criteria }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || `POST /api/tasks → ${r.status}`);
  }
  return r.json();
}

export async function approveTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/approve`, { method: "POST" });
  if (!r.ok) throw new Error(`POST approve → ${r.status}`);
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

export async function sendBack(id, message) {
  const r = await fetch(`${BASE}/api/tasks/${id}/send-back`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!r.ok) throw new Error(`POST send-back → ${r.status}`);
  return r.json();
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

export async function fetchTaskEvents(taskId) {
  const r = await fetch(`${BASE}/api/tasks/${taskId}/events`);
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

export async function fetchWorkerStatus() {
  const r = await fetch(`${BASE}/api/worker/status`);
  if (!r.ok) return { running: false, inflight: 0, max_workers: 0 };
  return r.json();
}

// ── Onboarding wizard ────────────────────────────────────────────────────────

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
