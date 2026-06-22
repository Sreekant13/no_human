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

export async function approveTask(id) {
  const r = await fetch(`${BASE}/api/tasks/${id}/approve`, { method: "POST" });
  if (!r.ok) throw new Error(`POST approve → ${r.status}`);
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
