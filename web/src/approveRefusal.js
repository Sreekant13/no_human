// The board's approve button was failing SILENTLY: a refusal (wrong status,
// merge already in progress, a land failure, or an ancestry/containment
// refusal like "0936e40a3 is not an ancestor of fix/...") reached the client
// as a rejected promise, and nothing rendered it — no toast, no error state,
// no drawer row. The operator saw a dead click and assumed the merge either
// hadn't happened yet or had silently succeeded.
//
// This module is the single place that turns a thrown Error from
// `approveTask` into something a human can read, plus the tiny per-task error
// map used to keep that text on the card until it's dismissed or the task
// leaves awaiting_approval.

/**
 * Classify an Error thrown by `approveTask` into a renderable shape.
 * -> { cause: "timeout" | "refused" | "network", text, status }
 *
 * `approveTask` (api.js) no longer applies a client-side abort timeout: the
 * land is a synchronous 2-4 minute server call, an earlier 30s abort fired on
 * ordinary merges, and aborting the client fetch cannot cancel server-side
 * work anyway — so an "abort" here never truly meant "nothing was merged".
 * The `timeout`/`AbortError` branch stays for whatever else can still tag an
 * error that way (e.g. the browser itself aborting the request), but the text
 * no longer claims a definite outcome the client cannot actually know.
 */
export function classifyApproveError(err) {
  const status = err?.status;

  if (err?.timeout || err?.name === "AbortError") {
    return {
      cause: "timeout",
      text: "Request timeout — no response from the server. The merge may still be running; check the task before retrying.",
      status,
    };
  }

  const message = typeof err?.message === "string" ? err.message.trim() : "";
  if (!message) {
    // The exact hole that produced the silent click: an error with no usable
    // message must still say SOMETHING, naming the status if we have one.
    const suffix = status ? `status ${status}` : "no status";
    return { cause: "refused", text: `Approve refused: (no reason given — ${suffix})`, status };
  }

  // The server's own text (from detailMessage) is carried verbatim — it is
  // the same string `nh approve` prints on the CLI — just labelled so the
  // operator knows it's a refusal, not a generic error.
  const text = message.startsWith("Approve refused: ") ? message : `Approve refused: ${message}`;
  return { cause: "refused", text, status };
}

/** Build the toast payload Board renders for a classified approve error. */
export function approveRefusalToast(taskId, classified) {
  return { id: `approve-refused-${taskId}`, tone: "error", text: classified.text, taskId };
}

/** Immutably record a task's approve error. */
export function setApproveError(map, taskId, classified) {
  return { ...map, [taskId]: classified };
}

/** Immutably clear one task's approve error (the card's dismiss X). */
export function dismissApproveError(map, taskId) {
  if (!(taskId in map)) return map;
  const next = { ...map };
  delete next[taskId];
  return next;
}

/**
 * Drop errors for tasks that are no longer awaiting_approval — the merge
 * either succeeded (the approve then landed) or the task moved on some other
 * way, so a stale refusal banner would be misleading.
 */
export function pruneApproveErrors(map, tasks) {
  const awaiting = new Set(
    (tasks || []).filter((t) => t?.status === "awaiting_approval").map((t) => t.id),
  );
  const next = {};
  let changed = false;
  for (const [id, err] of Object.entries(map)) {
    if (awaiting.has(id)) {
      next[id] = err;
    } else {
      changed = true;
    }
  }
  return changed ? next : map;
}
