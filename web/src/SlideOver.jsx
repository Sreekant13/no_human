import { useEffect, useRef, useState, useCallback } from "react";
import {
  approveTask, cancelTask, chooseBlockerOption, fetchDiff, fetchSubtasks,
  fetchTask, fetchTaskEvents, finishReview,
  pauseTask, postReviewComments, replyTask, resumeTask, retryTask, sendBack,
  connectTaskSSE,
} from "./api.js";
import Markdown from "./Markdown.jsx";
import { ROLE_LABEL, discoverSubagents, eventSource, modelsByNode } from "./eventRoles.js";
import { deriveAgentStatus } from "./pipelineStatus.js";
import { taskProgress } from "./taskProgress.js";
import { hasAction, normalizeOption } from "./blockerOptions.js";
import { clampAgentState, currentFunctionality, groupFunctionalities } from "./functionalities.js";
import { agentSummary, taskSummary } from "./summaries.js";
import { estimateCost, fmtTokens, totalBurn } from "./cost.js";
import { formatDuration } from "./formatDuration.js";

// ── Inline SVG icons — consistent, scalable, theme-aware ──────────────────
const IconCheck = ({ size = 14, className = "" }) => (
  <svg className={`nh-icon ${className}`} width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 8 7 12 13 4" /></svg>
);
const IconX = ({ size = 14, className = "" }) => (
  <svg className={`nh-icon ${className}`} width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="4" x2="12" y2="12" /><line x1="12" y1="4" x2="4" y2="12" /></svg>
);
const IconChevronDown = ({ size = 14 }) => (
  <svg className="nh-icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="4 6 8 10 12 6" /></svg>
);
const IconChevronRight = ({ size = 14 }) => (
  <svg className="nh-icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="6 4 10 8 6 12" /></svg>
);
const IconAlertTriangle = ({ size = 14 }) => (
  <svg className="nh-icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2L1.5 13.5h13L8 2z" /><line x1="8" y1="6.5" x2="8" y2="9.5" /><circle cx="8" cy="11.5" r="0.5" fill="currentColor" stroke="none" /></svg>
);
const IconInfo = ({ size = 14 }) => (
  <svg className="nh-icon" width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="6.5" /><line x1="8" y1="7" x2="8" y2="11" /><circle cx="8" cy="5" r="0.5" fill="currentColor" stroke="none" /></svg>
);

const STATUS_PILL = {
  pending:            "pill-pending",
  context:            "pill-context",
  planning:           "pill-planning",
  implementing:       "pill-implementing",
  reviewing:          "pill-reviewing",
  testing:            "pill-testing",
  awaiting_approval:  "pill-awaiting_approval",
  done:               "pill-done",
  escalated:          "pill-escalated",
  failed:             "pill-failed",
};

export default function SlideOver({ taskId, onClose, refreshKey = 0,
                                    reviewQueue = [], onJump = null }) {
  // W2.5: the review queue — the next awaiting-approval task after this one,
  // so five reviews feel like one pass instead of five board round-trips.
  const nextInQueue = reviewQueue.find((id) => id !== taskId) || null;
  const [task, setTask] = useState(null);
  const [diff, setDiff] = useState("");
  const [tab, setTab] = useState("system");
  // U3: the drawer opens on the surface that can CLEAR this task's gate — the diff +
  // approve for a review, the question + its canned answers for a parked task. System
  // is one click away.
  const openedFirstTab = useRef(false);
  const [busy, setBusy] = useState(false);
  const [sbOpen, setSbOpen] = useState(false);
  const [sbMsg, setSbMsg] = useState("");
  const [replyOpen, setReplyOpen] = useState(false);
  const [replyMsg, setReplyMsg] = useState("");
  const closeSb = useCallback(() => setSbOpen(false), []);
  const closeReply = useCallback(() => setReplyOpen(false), []);
  const sbRef = useNestedModalKeys(sbOpen, closeSb);
  const replyRef = useNestedModalKeys(replyOpen, closeReply);
  const [flash, setFlash] = useState(null);
  const dialogRef = useRef(null);
  const closeRef = useRef(null);

  // A NEW task resets the review-first latch; a refreshKey bump must not —
  // it would yank the user back to the review tab on every WS update.
  useEffect(() => { openedFirstTab.current = false; }, [taskId]);

  // Re-fetch whenever taskId changes OR when Board signals a WS update
  useEffect(() => {
    // A late response from the PREVIOUS task must never paint under the new one's
    // header: "Next review →" swaps taskId while these are in flight, and Approve
    // would then post against a task whose diff the operator never saw.
    let stale = false;
    fetchTask(taskId).then((t) => {
      if (stale) return;
      setTask(t);
      if (!openedFirstTab.current && t) {
        // The drawer opens on the surface that explains the gate. Review was already
        // handled; a parked task's blocker — its question, category and evidence — lives in
        // Details, so opening on System buried the question behind a pipeline diagram. (The
        // Reply button is in the persistent action bar on every tab, and no parked task in
        // the live DB carries one-click `options` — so this surfaces the QUESTION, which is
        // the honest claim.)
        const dest = t.status === "awaiting_approval" ? "review"
          : PARKED_STATUSES.has(t.status) ? "details"
          : null;
        if (dest) {
          openedFirstTab.current = true;  // once per open; user clicks win after
          setTab(dest);
        }
      }
    }).catch(() => {});
    fetchDiff(taskId).then((d) => { if (!stale) setDiff(d); }).catch(() => {});
    return () => { stale = true; };
  }, [taskId, refreshKey]);

  // Escape-to-close + focus trap
  useEffect(() => {
    closeRef.current?.focus();

    function onKeyDown(e) {
      if (e.key === "Escape") {
        // A modal ABOVE the drawer owns the key: closing both would also destroy the
        // feedback the operator just typed into send-back/reply. The agent-log modal
        // is a child component, so its state is not visible here — the DOM is.
        if (document.querySelector("[data-nested-modal]")) return;
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const el = dialogRef.current;
      if (!el) return;
      const focusable = Array.from(
        el.querySelectorAll('button:not([disabled]), [href], input, textarea, [tabindex]:not([tabindex="-1"])')
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last.focus(); }
      } else {
        if (document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const isAwaiting = task?.status === "awaiting_approval";
  const isParked = PARKED_STATUSES.has(task?.status);
  const isActive = ["pending", "context", "planning", "implementing", "reviewing", "testing"].includes(task?.status);
  const isFailed = task?.status === "failed";
  const isTerminal = task?.status === "done" || task?.status === "failed";
  const pillClass = STATUS_PILL[task?.status] || "pill-pending";

  async function handleApprove() {
    if (!isAwaiting || busy) return;
    setBusy(true);
    try {
      await approveTask(taskId);
      const remaining = reviewQueue.filter((id) => id !== taskId).length;
      setFlash("Approval recorded. Merge the PR in your git host."
        + (remaining ? ` ${remaining} more waiting — use Next review.` : ""));
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setFlash(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleSendBack() {
    if (!sbMsg.trim() || busy) return;
    setBusy(true);
    try {
      await sendBack(taskId, sbMsg.trim());
      setSbOpen(false);
      setSbMsg("");
      setFlash("Feedback stored. Task returned to queue.");
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setFlash(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleReply() {
    if (!replyMsg.trim() || busy) return;
    setBusy(true);
    try {
      const res = await replyTask(taskId, replyMsg.trim());
      setReplyOpen(false);
      setReplyMsg("");
      setFlash(res.message || "Reply stored. Run `nh watch` to resume.");
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setFlash(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleLifecycle(action) {
    if (busy) return;
    const actions = { pause: pauseTask, resume: resumeTask, cancel: cancelTask, retry: retryTask };
    const fn = actions[action];
    if (!fn) return;
    if (action === "cancel" && !window.confirm("Cancel this task? It will be marked as failed.")) return;
    setBusy(true);
    try {
      const res = await fn(taskId);
      setFlash(res.message);
      const updated = await fetchTask(taskId);
      setTask(updated);
    } catch (e) {
      setFlash(`Error: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="slideover-backdrop" onClick={onClose} />
      <div
        className="slideover"
        role="dialog"
        aria-modal="true"
        aria-labelledby="so-dialog-title"
        ref={dialogRef}
      >
        {/* header */}
        <div className="so-header">
          <div className="so-header-text">
            <div className="so-id">{task?.id ?? taskId}</div>
            <div className="so-title" id="so-dialog-title">{task?.title ?? "Loading…"}</div>
          </div>
          {(task?.total_tokens > 0 || task?.total_cache_read > 0) && (
            <span className="so-cost" title="cost meter: total burn (fresh + cache-read tokens) · indicative $ · wall-time · attempts">
              {fmtTokens(totalBurn(task.total_tokens, task.total_cache_read))} tok · {estimateCost(task.total_tokens, task.total_cache_read)}
              {task.wall_seconds != null && ` · ${formatDuration(Math.round(task.wall_seconds))}`}
              {task.attempt_count > 0 && ` · ${task.attempt_count} attempt${task.attempt_count > 1 ? "s" : ""}`}
            </span>
          )}
          {task && (
            <span className={`so-status-pill ${pillClass}`}>
              {String(task.status).replace(/_/g, " ")}
            </span>
          )}
          <button className="so-close" onClick={onClose} ref={closeRef} aria-label="Close"><IconX size={16} /></button>
        </div>
        {task && taskProgress(task.status) != null && (
          <div className="so-progress"
               title={`~${taskProgress(task.status)}% through the pipeline (${task.status})`}
               role="progressbar" aria-valuenow={taskProgress(task.status)}
               aria-valuemin={0} aria-valuemax={100}>
            <div className="so-progress-fill"
                 style={{ width: `${taskProgress(task.status)}%` }} />
            <span className="so-progress-label">{taskProgress(task.status)}%</span>
          </div>
        )}

        {/* tabs */}
        <div className="so-tabs">
          {["system", "activity", ...(task?.parent_id || task?.status === "compound_parent" ? ["subtasks"] : []), "details", "spec", "review", "diff", "attempts"].map((t) => (
            <button
              key={t}
              className={`so-tab${tab === t ? " active" : ""}`}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </div>

        {/* body */}
        <div className="so-body">
          {flash && <FlashBanner msg={flash} onDismiss={() => setFlash(null)} />}
          {tab === "system"   && <SystemTab taskId={taskId} task={task} isActive={isActive} />}
          {tab === "activity" && <ActivityTab taskId={taskId} task={task} isActive={isActive} />}
          {tab === "subtasks" && <SubtasksTab taskId={taskId} />}
          {tab === "details"  && <DetailsTab task={task} />}
          {tab === "spec"     && <SpecTab task={task} onRefresh={() => fetchTask(taskId).then(setTask)} />}
          {tab === "review"   && <ReviewTab task={task} diff={diff} />}
          {tab === "diff"     && <DiffTab diff={diff} />}
          {tab === "attempts" && <AttemptsTab task={task} />}
        </div>

        {/* action bar — contextual based on task status */}
        {task && (
          <div className="so-actions">
            {isAwaiting && (
              <button className="btn btn-approve" onClick={handleApprove} disabled={busy}>
                {busy ? "…" : "Approve"}
              </button>
            )}
            {isAwaiting && (
              <button className="btn btn-sendback" onClick={() => setSbOpen(true)} disabled={busy}>
                Send back
              </button>
            )}
            {isAwaiting && nextInQueue && onJump && (
              // Review-queue navigation belongs to the review flow only. It used
              // to show on ANY drawer with a pending review in the queue — so a
              // BLOCKED task's action bar led with "Next review →" instead of the
              // Reply/Resume it actually needs.
              <button className="btn btn-next-review" disabled={busy}
                      onClick={() => onJump(nextInQueue)}
                      title="Jump to the next task awaiting approval">
                Next review →
              </button>
            )}
            {isParked && (
              <button className="btn btn-reply" onClick={() => setReplyOpen(true)} disabled={busy}>
                Reply
              </button>
            )}
            {isParked && (
              <button className="btn btn-lifecycle btn-resume" onClick={() => handleLifecycle("resume")} disabled={busy}>
                Resume
              </button>
            )}
            {isActive && (
              <button className="btn btn-lifecycle btn-pause" onClick={() => handleLifecycle("pause")} disabled={busy}>
                Pause
              </button>
            )}
            {isFailed && (
              <button className="btn btn-lifecycle btn-retry" onClick={() => handleLifecycle("retry")} disabled={busy}>
                Retry
              </button>
            )}
            {!isTerminal && (
              // Destructive action: spatially separated from the primary CTA
              // (it used to sit as an equal sibling next to Approve — the
              // classic danger-adjacency mistake) and demoted to a quiet
              // text button. Still one click, still confirmable by the
              // lifecycle handler.
              <button
                className="btn-cancel-quiet"
                onClick={() => handleLifecycle("cancel")}
                disabled={busy}
                title="Cancel this task (destructive)"
              >
                Cancel task
              </button>
            )}
          </div>
        )}
      </div>

      {/* send-back modal */}
      {sbOpen && (
        <div className="sendback-overlay" data-nested-modal onClick={() => setSbOpen(false)}>
          <div className="sendback-modal" ref={sbRef} role="dialog" aria-modal="true"
               aria-label="Send back with feedback" onClick={(e) => e.stopPropagation()}>
            <div className="sendback-label">Send back with feedback</div>
            <textarea
              className="sendback-textarea"
              placeholder="What needs to change?"
              value={sbMsg}
              onChange={(e) => setSbMsg(e.target.value)}
              autoFocus
            />
            <div className="sendback-actions">
              <button className="btn btn-sendback" onClick={() => setSbOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn-approve"
                onClick={handleSendBack}
                disabled={!sbMsg.trim() || busy}
              >
                {busy ? "…" : "Send"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* reply modal */}
      {replyOpen && (
        <div className="sendback-overlay" data-nested-modal onClick={() => setReplyOpen(false)}>
          <div className="sendback-modal" ref={replyRef} role="dialog" aria-modal="true"
               aria-label="Reply to blocker question" onClick={(e) => e.stopPropagation()}>
            <div className="sendback-label">Reply to blocker question</div>
            <textarea
              className="sendback-textarea"
              placeholder="Your answer…"
              value={replyMsg}
              onChange={(e) => setReplyMsg(e.target.value)}
              autoFocus
            />
            <div className="sendback-actions">
              <button className="btn btn-sendback" onClick={() => setReplyOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn-approve"
                onClick={handleReply}
                disabled={!replyMsg.trim() || busy}
              >
                {busy ? "…" : "Send reply"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/* ── sub-components ───────────────────────────────────────────────────────── */

function FlashBanner({ msg, onDismiss }) {
  return (
    <div className="flash-banner">
      <span>{msg}</span>
      <button className="flash-banner-dismiss" onClick={onDismiss} aria-label="Dismiss"><IconX size={14} /></button>
    </div>
  );
}


// ── Agent definitions for the system diagram ───────────────────────────────
const AGENTS = [
  { id: "worker",     label: "Orchestrator", type: "ORCHESTRATOR", icon: "⚙",  desc: "Drives the task pipeline: context, planning, attempts, review",
    color: "var(--agent-worker)" },
  { id: "planner",    label: "Planner",      type: "PLANNER",      icon: "◈",  desc: "Fans out independent plan proposals, then synthesizes one",
    color: "var(--agent-planner)" },
  { id: "agent",      label: "Coder",        type: "AGENT",        icon: "⌨",  desc: "The Claude session that reads & edits code",
    color: "var(--agent-agent)" },
  { id: "supervisor", label: "Supervisor",   type: "MONITOR",      icon: "◉",  desc: "Course-corrects the worker every N tool calls",
    color: "var(--agent-supervisor)" },
  { id: "reviewer",   label: "Reviewer",     type: "QUALITY GATE", icon: "✓",  desc: "Adversarial review, tests, tamper check",
    color: "var(--agent-reviewer)" },
  // The post-PR watcher (W2.1): without this node its agentState was never
  // derived, so the Shepherding stage could not light up even after the
  // events reached the client (the second half of the same starvation bug).
  { id: "watcher",    label: "Watcher",      type: "SHEPHERD",     icon: "☂",  desc: "Post-PR: merge watch, feedback injection, CI fixes, CI_GATE gate",
    color: "var(--agent-worker)" },
];

// Parse URLs in text and return React elements with clickable links
function linkify(text) {
  if (!text) return text;
  const urlRe = /(https?:\/\/[^\s<>)"',]+)/g;
  const urlTest = /^https?:\/\//;
  const parts = text.split(urlRe);
  if (parts.length === 1) return text;
  return parts.map((part, i) =>
    urlTest.test(part)
      ? <a key={i} href={part} target="_blank" rel="noreferrer">{part.length > 80 ? part.slice(0, 77) + "…" : part}</a>
      : part
  );
}

// Group consecutive same-tool events for collapsed display.
// A modal above the drawer owns both Escape and Tab while it is open: the drawer's handlers
// stand down for it (see the [data-nested-modal] guard), and the drawer's focus trap only
// covers .slideover, of which these modals are siblings.
function useNestedModalKeys(open, onClose) {
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    function onKey(e) {
      if (e.key === "Escape") {
        // No stopPropagation: the drawer's listener is on the same target and already ran —
        // it stands down via the [data-nested-modal] guard, which is what actually protects
        // the drawer (and the text typed into this modal).
        onClose();
        return;
      }
      if (e.key !== "Tab" || !ref.current) return;
      const items = [...ref.current.querySelectorAll("button, textarea, input, select, [href]")]
        .filter((el) => !el.disabled && el.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return ref;
}

// The statuses whose gate is cleared in the Details tab — the blocker question, its
// evidence, and the one-click canned answers all live there.
// The statuses whose gate the operator clears IN the drawer (Reply / Resume / the blocker's
// options). Deliberately NOT paused_quota: the backend parks it without a blocker record
// (orchestrator._park_quota) and `isParked` gives it no Reply/Resume buttons, so sending it
// to Details would strand it on a tab emptier than the System one it came from — its gate is
// a budget raise, not an answer. One definition; `isParked` reads it too.
const PARKED_STATUSES = new Set(["awaiting_input", "blocked", "escalated"]);

function groupConsecutiveEvents(events) {
  const result = [];
  let i = 0;
  while (i < events.length) {
    const e = events[i];
    // Only group consecutive Read/View calls — most common noise source.
    if (e.kind === "tool_use" && (e.tool_name === "Read" || e.tool_name === "View")) {
      const group = [e];
      let j = i + 1;
      while (j < events.length && events[j].kind === "tool_use" && events[j].tool_name === e.tool_name) {
        group.push(events[j]);
        j++;
      }
      if (group.length >= 3) {
        result.push({ _group: true, tool: e.tool_name, events: group, ts: e.ts, firstIdx: i });
        i = j;
        continue;
      }
    }
    result.push(e);
    i++;
  }
  return result;
}

// Extract file basename from a tool_use event.
function toolFile(event) {
  const inp = event.tool_input || {};
  const path = inp.file_path || inp.path || inp.notebook_path || "";
  return path ? path.split("/").pop() : "";
}

// Collapsed group of consecutive same-tool events.
function GroupedEvents({ group, role }) {
  const [expanded, setExpanded] = useState(false);
  const files = group.events.map(toolFile).filter(Boolean);
  const shown = files.slice(0, 3);
  const rest = files.length - shown.length;
  const elapsed = group.events[group.events.length - 1].ts - group.events[0].ts;

  return (
    <div className={`activity-event-rich role-${role} ak-tool_use event-group`}>
      <div className="rich-meta">
        <span className="rich-ts">{fmtTs(group.ts)}</span>
        <span className="rich-role">{ROLE_LABEL[role]}</span>
        <span className="rich-kind ak-tool_use">{group.tool}</span>
        {elapsed > 2 && <span className="rich-elapsed">+{fmtDuration(elapsed)}</span>}
      </div>
      <div
        className="rich-tool-group"
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
      >
        <span className="rich-tool-name">{group.tool}</span>
        <span className="rich-group-count">{group.events.length} files</span>
        {shown.map((f, i) => <span key={i} className="rich-file-chip">{f}</span>)}
        {rest > 0 && <span className="rich-group-more">+{rest}</span>}
        <span className="rich-group-toggle">{expanded ? "▾" : "▸"}</span>
      </div>
      {expanded && (
        <div className="rich-group-detail">
          {group.events.map((e, i) => (
            <div key={i} className="rich-group-item">
              <span className="rich-file-chip">{toolFile(e) || "file"}</span>
              {e.result_preview && <pre className="rich-tool-result">{e.result_preview}</pre>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Collapsed-by-default reasoning block ("Thought for Ns") — extended-thinking
// content is verbose and low-signal at a glance, but shouldn't be discarded.
function ThinkingBlock({ text, elapsed }) {
  const [expanded, setExpanded] = useState(false);
  const label = elapsed > 1 ? `Thought for ${fmtDuration(elapsed)}` : "Thinking";
  return (
    <div className="rich-thinking">
      <button
        type="button"
        className="rich-thinking-toggle"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
        <span>{label}</span>
      </button>
      {expanded && (
        <div className="rich-thinking-body">
          <Markdown>{text}</Markdown>
        </div>
      )}
    </div>
  );
}

// Render a single event with rich formatting
function RichEvent({ event, elapsed, role }) {
  const kind = event.kind;
  const text = event.text || "";

  let body;
  if (kind === "tool_use") {
    const toolName = event.tool_name || text.split(" ")[0] || "tool";
    const file = toolFile(event);
    // `text` is already a human-readable one-liner from the backend
    // (e.g. "Read metrics-core-query-service/Jenkinsfile", "Run `wc -l ...`") — show
    // the remainder after the tool name/file chip rather than re-deriving
    // raw tool_input (which would just repeat absolute paths verbosely).
    const args = text.replace(toolName, "").trim();
    const preview = (event.result_preview || "").trim();
    body = (
      <div className="rich-tool">
        <div className="rich-tool-row">
          <span className="rich-tool-name">{toolName}</span>
          {file && <span className="rich-file-chip">{file}</span>}
          <span className="rich-tool-args" title={args}>
            {file ? args.replace(new RegExp(`[^=]*${file.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}[^,]*,?\\s*`), "").trim() : args}
          </span>
        </div>
        {preview && <pre className="rich-tool-result">{preview}</pre>}
      </div>
    );
  } else if (kind === "state") {
    body = (
      <div className="rich-status-change">
        <span className="rich-status-arrow">→</span>
        <span className="rich-status-badge">{text}</span>
      </div>
    );
  } else if (kind === "commit") {
    const hashMatch = text.match(/([a-f0-9]{7,40})/);
    body = (
      <div className="rich-commit">
        {hashMatch && <span className="rich-commit-hash">{hashMatch[1].slice(0, 7)}</span>}
        <span className="rich-commit-msg">{linkify(text.replace(hashMatch?.[0] || "", "").trim())}</span>
      </div>
    );
  } else if (kind === "review") {
    const passed = /pass/i.test(text);
    body = (
      <div className="rich-review-verdict">
        <span className={`rich-verdict-chip ${passed ? "pass" : "fail"}`}>
          {passed ? "PASSED" : "FAILED"}
        </span>
        <span className="rich-body">{linkify(text)}</span>
      </div>
    );
  } else if (kind === "tests" || kind === "lint" || kind === "quality_gate" || kind === "quality_gate_failed") {
    const passed = kind !== "quality_gate_failed" && (/pass/i.test(text) || /clean/i.test(text));
    const label = kind.startsWith("quality_gate") ? "QUALITY GATE" : kind === "tests" ? "TESTS" : "LINT";
    body = (
      <div className="rich-review-verdict">
        <span className={`rich-verdict-chip ${passed ? "pass" : "fail"}`}>
          {label} {passed ? "PASS" : "FAIL"}
        </span>
        <span className="rich-body">{linkify(text)}</span>
      </div>
    );
  } else if (kind === "env_setup_failed") {
    body = (
      <div className="rich-review-verdict">
        <span className="rich-verdict-chip fail">ENV SETUP FAILED</span>
        <span className="rich-body">{linkify(text)}</span>
      </div>
    );
  } else if (kind === "agent_text") {
    body = <div className="rich-agent-prose"><Markdown>{text}</Markdown></div>;
  } else if (kind === "thinking") {
    body = <ThinkingBlock text={text} elapsed={elapsed} />;
  } else if (kind === "supervisor_decision") {
    // `text` is only the verdict word; the guidance lives in `message`.
    // Rendering just the word made 33 real corrections look like noise.
    const isCorrection = text === "correct";
    body = (
      <div className="rich-supervisor">
        <span className={`rich-verdict-chip ${isCorrection ? "fail" : "pass"}`}>
          {isCorrection ? "COURSE-CORRECT" : "ON TRACK"}
        </span>
        {event.message && <span className="rich-body">{linkify(event.message)}</span>}
      </div>
    );
  } else {
    body = <span className="rich-body">{linkify(text)}</span>;
  }

  return (
    <div className={`activity-event-rich role-${role} ak-${kind}`}>
      <div className="rich-meta">
        <span className="rich-ts">{fmtTs(event.ts)}</span>
        <span className="rich-role">{ROLE_LABEL[role]}</span>
        <span className={`rich-kind ak-${kind}`}>{eventLabel(kind)}</span>
        {elapsed > 2 && <span className="rich-elapsed">+{fmtDuration(elapsed)}</span>}
      </div>
      {body}
    </div>
  );
}

// A single node card in the tree diagram
function AgentNode({ agent, state, isActive, onClick }) {
  const running = isActive && state.status === "active";
  let cls = "sys-node";
  if (running) cls += " active-node";
  if (state.status === "done") cls += " done-node";
  if (state.status === "error") cls += " error-node";
  return (
    <div
      className={cls}
      style={{ "--node-color": agent.color }}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onClick()}
      title={`Click to view ${agent.label} logs`}
    >
      {running && <div className="sys-node-running-indicator"><span className="sys-node-running-dot" /><span>Running</span></div>}
      <div className="sys-node-header">
        <div className="sys-node-icon">{agent.icon}</div>
        <span className="sys-node-type">{agent.type}</span>
      </div>
      <div className="sys-node-name">{agent.label}</div>
      {agent.model && <div className="sys-node-model" title={agent.model}>{agent.model}</div>}
      <div className="sys-node-body">
        <div className="sys-node-meta">
          <span className="sys-node-meta-label">Status:</span>
          <span className={`sys-node-status-badge s-${state.status}`}>{state.status}</span>
          <span className="sys-node-events-count">{state.count} events</span>
        </div>
        {state.lastText && <div className="sys-node-last-text" title={state.lastText}>{state.lastText}</div>}
      </div>
    </div>
  );
}

// Agent log modal — opens when clicking a node
// One digest card for both surfaces: an agent's modal and the Activity page.
// Deterministic (summaries.js) — no model call, computed from the same events
// the raw feed shows, so it can't say anything the feed doesn't back up.
function SummaryCard({ summary }) {
  if (!summary) return null;
  return (
    <div className="digest-card">
      <div className="digest-headline">{summary.headline}</div>
      <div className="digest-facts">
        {summary.facts.map(([label, value]) => (
          <div className="digest-fact" key={label}>
            <span className="digest-fact-label">{label}</span>
            <span className="digest-fact-value">{value}</span>
          </div>
        ))}
      </div>
      {summary.highlights.length > 0 && (
        <ul className="digest-highlights">
          {summary.highlights.map((h, i) => <li key={i}>{h}</li>)}
        </ul>
      )}
      {summary.issues.length > 0 && (
        <ul className="digest-issues">
          {summary.issues.map((h, i) => <li key={i}>{h}</li>)}
        </ul>
      )}
    </div>
  );
}

function AgentLogModal({ agent, events, onClose }) {
  const endRef = useRef(null);
  const agentEvents = agent.subagentTaskId
    ? events.filter(e => (e.kind === "subagent_start" || e.kind === "subagent_progress" || e.kind === "subagent_done") && e.task_id === agent.subagentTaskId)
    : events.filter(e => eventSource(e) === agent.id);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [agentEvents.length]);

  useEffect(() => {
    function onKey(e) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const elapsed = agentEvents.length > 1 ? agentEvents[agentEvents.length - 1].ts - agentEvents[0].ts : 0;

  return (
    <div className="sys-modal-backdrop" data-nested-modal onClick={onClose}>
      <div className="sys-modal" style={{ "--node-color": agent.color }} onClick={(e) => e.stopPropagation()}>
        <div className="sys-modal-header">
          <div className="sys-modal-icon">{agent.icon}</div>
          <div className="sys-modal-titles">
            <div className="sys-modal-type">{agent.type}</div>
            <div className="sys-modal-name">{agent.label}</div>
          </div>
          <button className="sys-modal-close" onClick={onClose} aria-label="Close"><IconX size={16} /></button>
        </div>
        <div className="sys-modal-stats">
          <div className="sys-modal-stat">
            Events: <span className="sys-modal-stat-val">{agentEvents.length}</span>
          </div>
          {elapsed > 0 && (
            <div className="sys-modal-stat">
              Duration: <span className="sys-modal-stat-val">{fmtDuration(elapsed)}</span>
            </div>
          )}
          <div className="sys-modal-stat">
            {agent.desc}
          </div>
        </div>
        <SummaryCard summary={agentSummary(events, agent)} />
        <div className="sys-modal-log">
          {agentEvents.length === 0 ? (
            <div className="sys-modal-empty">No events from this agent yet.</div>
          ) : (
            agentEvents.map((e, i) => {
              const prev = i > 0 ? agentEvents[i - 1] : null;
              const dt = prev ? e.ts - prev.ts : 0;
              return <RichEvent key={i} event={e} elapsed={dt} role={eventSource(e)} />;
            })
          )}
          <div ref={endRef} />
        </div>
      </div>
    </div>
  );
}

const clipText = (s, n) => {
  const x = (s || "").replace(/\s+/g, " ").trim();
  return x.length > n ? x.slice(0, n - 1) + "…" : x;
};

// One lane of the functionality board: the stage's status header on top,
// its full agent tree always visible beneath — nothing hidden behind clicks.
// The arrow out of a header flows when the NEXT stage is the one running.
function FxLane({ g, isCurrent, flowOut, agentStates, node, isActive, onOpen }) {
  const hasPrimary = g.roles.includes(g.primary);
  const children = [
    ...(g.id === "coding" && g.roles.includes("supervisor") ? [node("supervisor")] : []),
    ...g.subs,
  ];
  return (
    <div className={`fx-lane s-${g.status}${isCurrent ? " current" : ""}`}
         style={{ "--fx-color": g.color }}>
      <div className={`fx-head${flowOut ? " flow-out" : ""}`} title={g.desc}>
        <div className="fx-head-top">
          <span className="fx-icon" aria-hidden="true">{g.icon}</span>
          <span className="fx-label">{g.label}</span>
          {isCurrent && <span className="fx-live-dot" role="img" aria-label="running now" />}
        </div>
        <div className="fx-head-meta">
          <span className={`fx-status s-${g.status}`}>{g.status}</span>
          {g.present && (
            <span className="fx-counts">
              {g.agentCount} agent{g.agentCount === 1 ? "" : "s"} · {g.eventCount} ev
            </span>
          )}
        </div>
        {g.model && <div className="fx-model" title={g.model}>{g.model}</div>}
      </div>
      {hasPrimary || children.length > 0 ? (
        <div className="sys-col fx-lane-tree">
          {hasPrimary && (
            <AgentNode agent={node(g.primary)} state={agentStates[g.primary]}
                       isActive={isActive} onClick={() => onOpen(node(g.primary))} />
          )}
          {hasPrimary && children.length > 0 && <div className="sys-col-stem" />}
          {children.length > 0 && (
            <div className="sys-col-children">
              {children.map((c, i) => (
                <div className="fx-child" style={{ "--i": i }} key={c.id}>
                  <AgentNode agent={c} state={agentStates[c.id]}
                             isActive={isActive} onClick={() => onOpen(c)} />
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="fx-lane-empty">not started yet</div>
      )}
    </div>
  );
}

function SystemTab({ taskId, task, isActive }) {
  const [events, setEvents] = useState([]);
  const [modalAgent, setModalAgent] = useState(null);
  const currentFx = currentFunctionality(events, isActive);

  useEffect(() => {
    let cancelled = false;
    if (isActive) {
      let seedTs = 0;
      fetchTaskEvents(taskId).then((evts) => {
        if (cancelled) return;
        setEvents(evts);
        seedTs = evts.length > 0 ? Math.max(...evts.map(e => e.ts || 0)) : 0;
      }).catch(() => {});
      const es = connectTaskSSE(taskId,
        (evt) => {
          if (cancelled) return;
          if ((evt.ts || 0) > seedTs) {
            setEvents((prev) => [...prev, evt]);
          }
        },
        () => {},
      );
      return () => { cancelled = true; es.close(); };
    }
    async function poll() {
      while (!cancelled) {
        try {
          const evts = await fetchTaskEvents(taskId);
          if (!cancelled) setEvents(evts);
        } catch { /* ignore */ }
        await new Promise((r) => setTimeout(r, 10000));
      }
    }
    poll();
    return () => { cancelled = true; };
  }, [taskId, isActive]);

  // Derive per-agent status. On a task that is NOT running (parked,
  // escalated, awaiting approval, done, failed) nothing is executing — a
  // badge stuck on "active" is a lie the operator acts on (task 6cfdb936
  // showed 5 ACTIVE stages while escalated with zero live sessions).
  const agentStates = {};
  for (const a of AGENTS) {
    agentStates[a.id] = clampAgentState(deriveAgentStatus(events, a.id), isActive);
  }

  // Discover dynamically-spawned subagents from events, grouped by the role
  // that spawned them — each role column owns its own children.
  const subagents = discoverSubagents(events);
  // Subagent status comes from the discovery function, not deriveAgentStatus.
  for (const sub of subagents) {
    const subEvents = events.filter(e =>
      (e.kind === "subagent_start" || e.kind === "subagent_progress" || e.kind === "subagent_done")
      && e.task_id === sub.subagentTaskId
    );
    agentStates[sub.id] = clampAgentState(
      { status: sub.status, count: subEvents.length, lastText: subEvents.length > 0 ? subEvents[subEvents.length - 1].text || "" : "" },
      isActive);
  }

  const totalElapsed = events.length > 1 ? events[events.length - 1].ts - events[0].ts : 0;

  // Label each node with the model that actually ran it (from the `models`
  // event), so "the coder is Sonnet 5" is verifiable at a glance rather than
  // assumed from a config file that may be shadowing the real default.
  const nodeModels = modelsByNode(events);
  const withModel  = (a) => (a && nodeModels[a.id] ? { ...a, model: nodeModels[a.id] } : a);
  const node       = (id) => withModel(AGENTS.find(a => a.id === id));

  // The page's top layer: the four functionalities of the pipeline, in the
  // order the orchestrator drives them. Each groups its roles and their
  // subagents; clicking one expands that stage's agent tree.
  const groups = groupFunctionalities({ agentStates, subagents, models: nodeModels, events });

  if (events.length === 0) {
    return (
      <div className="sys-view">
        <div className="so-diff-empty">
          {isActive ? "Waiting for events…" : "No events recorded for this task."}
        </div>
      </div>
    );
  }

  // The one line the operator needs first: running where, or waiting on what.
  const banner = (() => {
    const st = task?.status || "";
    if (st === "awaiting_approval") return {
      cls: "waiting", text: "Waiting for you — review & merge the PR", icon: "⏸" };
    if (st === "escalated" || st === "awaiting_input") return {
      cls: "waiting",
      text: `Waiting for you — ${clipText((task?.blocker || {}).question || "a decision is needed (see Activity)", 110)}`,
      icon: "⏸" };
    if (isActive) {
      const fx = groups.find((g) => g.id === currentFx);
      return { cls: "running", text: `Running — ${fx ? fx.label : "starting"}`, icon: "●" };
    }
    if (st === "done") return { cls: "done", text: "Done — PR merged", icon: "✓" };
    return null;
  })();

  return (
    <div className="sys-view">
      <div className="sys-tree">
        {banner && (
          <div className={`fx-banner ${banner.cls}`}>
            <span className="fx-banner-icon" aria-hidden="true">{banner.icon}</span>
            {banner.text}
          </div>
        )}
        {/* The functionality board: four lanes, every agent and subagent
            visible at once. The arrow between headers flows toward the stage
            that is running right now. */}
        <div className="fx-board">
          {groups.map((g, i) => (
            <FxLane key={g.id} g={g}
                    isCurrent={isActive && currentFx === g.id}
                    flowOut={isActive && i + 1 < groups.length
                             && currentFx === groups[i + 1].id}
                    agentStates={agentStates} node={node}
                    isActive={isActive} onOpen={setModalAgent} />
          ))}
        </div>

        {/* Summary stats */}
        <div className="sys-summary">
          <div className="sys-summary-item">
            <span>Events:</span>
            <span className="sys-summary-value">{events.length}</span>
          </div>
          {totalElapsed > 0 && (
            <div className="sys-summary-item">
              <span>Duration:</span>
              <span className="sys-summary-value">{fmtDuration(totalElapsed)}</span>
            </div>
          )}
          {task?.attempt_count > 0 && (
            <div className="sys-summary-item">
              <span>Attempt:</span>
              <span className="sys-summary-value">#{task.attempt_count}</span>
            </div>
          )}
          {events.length === 0 && (
            <div className="sys-summary-item" style={{ color: 'var(--text-dim)' }}>
              {isActive ? "Waiting for events…" : "No events recorded."}
            </div>
          )}
        </div>
      </div>

      {/* Agent log modal */}
      {modalAgent && (
        <AgentLogModal
          agent={modalAgent}
          events={events}
          onClose={() => setModalAgent(null)}
        />
      )}
    </div>
  );
}


// ── Sub-tasks tab for compound parents ───────────────────────────────────
const STATUS_ICON = { done: "[ok]", failed: "[x]", pending: "[.]", implementing: "[~]", reviewing: "[?]", blocked: "[!]", compound_parent: "[+]" };

function SubtasksTab({ taskId }) {
  const [subs, setSubs] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const data = await fetchSubtasks(taskId);
      if (!cancelled) { setSubs(data); setLoading(false); }
    }
    load();
    const iv = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(iv); };
  }, [taskId]);

  if (loading) return <div className="so-diff-empty">Loading sub-tasks…</div>;
  if (subs.length === 0) return <div className="so-diff-empty">No sub-tasks.</div>;

  const done = subs.filter(s => s.status === "done").length;

  return (
    <div className="subtasks-tab">
      <div className="subtasks-header">
        <span className="subtasks-progress">{done} / {subs.length} complete</span>
        <div className="subtasks-bar">
          <div className="subtasks-bar-fill" style={{ width: `${(done / subs.length) * 100}%` }} />
        </div>
      </div>
      <div className="subtasks-list">
        {subs.map(s => (
          <div key={s.id} className={`subtask-card status-${s.status}`}>
            <span className="subtask-icon">{STATUS_ICON[s.status] || "·"}</span>
            <div className="subtask-info">
              <span className="subtask-title">{s.title}</span>
              <span className="subtask-meta">
                {s.status}{s.kind !== "feature" ? ` · ${s.kind}` : ""}
                {s.live_status ? ` · ${s.live_status}` : ""}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}


function ActivityTab({ taskId, task, isActive }) {
  const [events, setEvents] = useState([]);
  // U1: only the newest N events enter the DOM. Rendering a long task's
  // full history (2015 events on 84251cb2) froze the tab exactly when the
  // task was interesting; derivations (turn counter, summary, plan tracker)
  // still see the full array.
  const [windowSize, setWindowSize] = useState(150);
  const endRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    // Phase 4a: use SSE for active tasks (real-time), poll for inactive.
    if (isActive) {
      // Seed with existing events, then stream new ones.
      // Track seed watermark to avoid duplicates from SSE.
      let seedTs = 0;
      fetchTaskEvents(taskId).then((evts) => {
        if (cancelled) return;
        setEvents(evts);
        seedTs = evts.length > 0 ? Math.max(...evts.map(e => e.ts || 0)) : 0;
      }).catch(() => {});
      const es = connectTaskSSE(taskId,
        (evt) => {
          if (cancelled) return;
          // Only append events newer than the seed watermark (dedup).
          if ((evt.ts || 0) > seedTs) {
            setEvents((prev) => [...prev, evt]);
          }
        },
        () => { /* stream ended — will fall back on next render cycle */ },
      );
      return () => { cancelled = true; es.close(); };
    }
    // Inactive: slow poll.
    async function poll() {
      while (!cancelled) {
        try {
          const evts = await fetchTaskEvents(taskId);
          if (!cancelled) setEvents(evts);
        } catch { /* ignore */ }
        await new Promise((r) => setTimeout(r, 10000));
      }
    }
    poll();
    return () => { cancelled = true; };
  }, [taskId, isActive]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="activity-feed">
        <div className="so-diff-empty">
          {isActive ? "Waiting for events…" : "No events recorded for this task."}
        </div>
      </div>
    );
  }

  const lastEvent = events[events.length - 1];
  const isWorking = isActive && lastEvent;
  const lastRole = eventSource(lastEvent);
  const totalElapsed = events.length > 1 ? events[events.length - 1].ts - events[0].ts : 0;

  // Turn counter: count tool_use events from the agent since the last attempt_start.
  let turnCount = 0;
  let maxTurns = null;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].kind === "attempt_start") {
      maxTurns = events[i].max_turns || null;
      break;
    }
    if (events[i].kind === "tool_use" && eventSource(events[i]) === "agent") {
      turnCount++;
    }
  }
  const turnPct = maxTurns ? Math.min(100, (turnCount / maxTurns) * 100) : null;

  return (
    <div className="activity-feed">
      <div className="activity-legend">
        <span className="al-role role-worker"><i />Orchestrator — drives pipeline</span>
        <span className="al-role role-agent"><i />Worker — reads &amp; edits code</span>
        <span className="al-role role-supervisor"><i />Supervisor — course-corrects</span>
        <span className="al-role role-reviewer"><i />Reviewer — gates quality</span>
        {totalElapsed > 0 && (
          <span style={{ marginLeft: 'auto', fontSize: '0.7rem', color: 'var(--fg-dim)' }}>
            {events.length} events · {fmtDuration(totalElapsed)}
          </span>
        )}
      </div>
      {isWorking && (
        <div className={`activity-status-bar role-${lastRole}`}>
          <span className="activity-pulse" />
          <span className="activity-status-text">
            {ROLE_LABEL[lastRole]} · {eventLabel(lastEvent.kind)}: {lastEvent.text}
          </span>
        </div>
      )}
      {isWorking && turnCount > 0 && (
        <div className="turn-counter">
          <span className="turn-label">Turn {turnCount}{maxTurns ? ` / ${maxTurns}` : ""}</span>
          {turnPct !== null && (
            <div className="turn-bar">
              <div className="turn-bar-fill" style={{ width: `${turnPct}%` }} />
            </div>
          )}
        </div>
      )}
      {(() => {
        const planFiles = (task?.context?.spec?.files_to_change) || [];
        if (planFiles.length === 0) return null;
        const editedBasenames = new Set(
          events
            .filter(e => e.kind === "tool_use" && ["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(e.tool_name))
            .map(e => toolFile(e))
            .filter(Boolean)
        );
        const extraFiles = [...editedBasenames].filter(f => !planFiles.some(pf => pf.endsWith(f)));
        return (
          <div className="plan-tracker">
            <span className="plan-tracker-label">Files:</span>
            {planFiles.map((f, i) => {
              const base = f.split("/").pop();
              const done = editedBasenames.has(base);
              return <span key={i} className={`plan-file ${done ? "done" : ""}`}>{done ? "[x]" : "[ ]"} {base}</span>;
            })}
            {extraFiles.map((f, i) => (
              <span key={`x-${i}`} className="plan-file extra">[+] {f}</span>
            ))}
          </div>
        );
      })()}
      <SummaryCard summary={taskSummary(events)} />
      <div className="activity-log">
        {events.length > windowSize && (
          <button
            className="activity-load-older"
            onClick={() => setWindowSize((s) => s + 300)}
          >
            Show older events ({events.length - windowSize} hidden)
          </button>
        )}
        {(() => {
          const visible = events.length > windowSize
            ? events.slice(-windowSize) : events;
          // Keys are the event's ABSOLUTE index in the (append-only) feed, not its index
          // in the sliding window: a window index shifts on every streamed event, which
          // silently re-attached an expanded reasoning block to a different event.
          const offset = events.length - visible.length;
          // `prevTs` walks forward with the render instead of `visible.indexOf(e)` — that
          // was a linear scan per row, per render, on every SSE frame (a real task here
          // has 2,015 events).
          let prevTs = visible.length > 0 ? visible[0].ts : 0;
          let idx = 0;  // position in `visible`, walked forward — never searched for.
          return groupConsecutiveEvents(visible).map((item) => {
            if (item._group) {
              const role = eventSource(item.events[0]);
              const key = `g-${offset + idx}`;
              idx += item.events.length;
              prevTs = item.events[item.events.length - 1].ts;
              return <GroupedEvents key={key} group={item} role={role} />;
            }
            const e = item;
            const key = `e-${offset + idx}`;
            idx += 1;
            const elapsed = e.ts - prevTs;
            prevTs = e.ts;
            const role = eventSource(e);
            return <RichEvent key={key} event={e} elapsed={elapsed} role={role} />;
          });
        })()}
        <div ref={endRef} />
      </div>
    </div>
  );
}

function fmtTs(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

const EVENT_LABELS = {
  kind: "Task type",
  state: "Status",
  context_gather: "Context",
  context: "Context",
  profile: "Profile",
  ci_backend: "CI",
  attempt_start: "Attempt",
  attempt_failed: "Attempt failed",
  supervisor: "Supervisor",
  commit: "Commit",
  lint: "Lint",
  tests: "Tests",
  tamper: "Tamper check",
  review_start: "Review",
  review: "Review result",
  review_error: "Review error",
  agent_error: "Agent error",
  stuck: "Stuck",
  failed: "Failed",
  bounds: "Bounds",
  tool_use: "Tool",
  agent_text: "Agent",
  thinking: "Reasoning",
  supervisor_decision: "Supervisor",
  // env setup/teardown (Item 2)
  env_setup: "Env setup",
  env_setup_failed: "Env setup failed",
  env_teardown: "Env teardown",
  // skills & subagents (Items 3, 4)
  skills_materialized: "Skills loaded",
  skills_loaded: "Skills",
  // investigation report (Item 1)
  investigation_report: "Investigation report",
  // checkpoint & resume (existing, unlabelled)
  checkpoint: "Checkpoint",
  resume_wip: "Resume WIP",
  // compound tasks / lead agent (Item 5)
  decompose: "Decompose",
  compound_done: "Compound done",
  quality_gate: "Quality gate",
  quality_gate_failed: "Quality gate failed",
  unblock: "Unblock",
  retry: "Retry",
  escalate: "Escalate",
  // subagent events
  subagent_start: "Subagent",
  subagent_progress: "Subagent",
  subagent_done: "Subagent done",
  review_posted: "Review posted to PR",
  // post-PR watcher ladder (blockers/wake.py) — merged/comments/CI/CI_GATE
  merged: "PR merged",
  pr_closed: "PR closed",
  pr_feedback: "PR feedback",
  pr_feedback_skipped: "Bot comments ignored",
  pr_ci_red: "PR CI red",
  escalated_ci: "CI escalated",
  escalated_revisions: "Revisions escalated",
  escalated_timeout: "Park timeout",
  resumed: "Resumed",
  wake_tick: "Watcher heartbeat",
  state_repaired: "State repaired",
  // CI_GATE integration gate (M6)
  ci_gate_trigger: "CI_GATE triggered",
  ci_gate_poll: "CI_GATE running",
  ci_gate_pass: "CI_GATE passed",
  ci_gate_fail: "CI_GATE failed",
  ci_gate_blocked: "CI_GATE blocked",
  ci_gate_refused: "CI_GATE refused",
};

function eventLabel(kind) {
  return EVENT_LABELS[kind] || kind;
}

function fmtDuration(secs) {
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

function DetailsTab({ task }) {
  if (!task) return <div className="so-diff-empty">Loading…</div>;
  const findings = task.context?.findings;
  return (
    <>
      {task.description && (
        <section>
          <div className="so-section-label">Description</div>
          <div className="so-description">{task.description}</div>
        </section>
      )}
      {task.acceptance_criteria?.length > 0 && (
        <section>
          <div className="so-section-label">Acceptance criteria</div>
          <ul className="so-criteria" data-testid="criteria-list">
            {task.acceptance_criteria.map((c, i) => {
              const progress = task.context?.progress?.acceptance_criteria?.[i];
              const status = progress?.status;
              return (
                <li key={i} className={`criterion-tracked ${status || "not_started"}`}>
                  <span className="criterion-status-icon">
                    {status === "done" && <IconCheck size={12} />}
                    {status === "in_progress" && <span className="criterion-spinner" />}
                  </span>
                  <span>{c}</span>
                  {progress?.evidence && <div className="criterion-evidence">{progress.evidence}</div>}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {findings && (
        <section>
          <div className="so-section-label">
            {task.kind === "design_doc" ? "Design document" : "Investigation findings"}
          </div>
          <pre className="so-findings">{findings}</pre>
        </section>
      )}
      {task.blocker && <BlockerSection blocker={task.blocker} taskId={task.id}
                                       taskStatus={task.status} />}
      {task.repo_path && (
        <section>
          <div className="so-section-label">Repo</div>
          <div className="so-repo-path">{task.repo_path}</div>
        </section>
      )}
    </>
  );
}

function SpecTab({ task, onRefresh }) {
  const [requestingChanges, setRequestingChanges] = useState(false);
  const [changeNote, setChangeNote] = useState("");
  const [specBusy, setSpecBusy] = useState(false);
  if (!task) return <div className="so-diff-empty">Loading…</div>;
  const spec = task.context?.spec;
  const hasSpec = !!(spec && (spec.approach || spec.files_to_change?.length ||
    spec.test_plan || spec.out_of_scope?.length || spec.verification));
  if (!hasSpec) {
    return (
      <div className="so-diff-empty" data-testid="spec-empty">
        {["code_review", "investigation", "design_doc", "ci_fix"].includes(task.kind)
          ? `Spec generation doesn't apply to ${task.kind} tasks.`
          : ["pending", "context", "planning"].includes(task.status)
          ? "Spec not generated yet."
          : "No spec available for this task."}
      </div>
    );
  }
  const planSize = spec.files_to_change?.length || 0;
  return (
    <div data-testid="spec-tab">
      {spec.files_to_change?.length > 0 && (
        <section>
          <div className="so-section-label">Files to Change</div>
          <div className="spec-file-list">
            {spec.files_to_change.map((f, i) => (
              <div key={i} className="spec-file-item">
                <span className="spec-file-path">{f}</span>
              </div>
            ))}
          </div>
        </section>
      )}
      {spec.approach && (
        <section>
          <div className="so-section-label">Approach</div>
          <Markdown>{spec.approach}</Markdown>
        </section>
      )}
      {spec.test_plan && (
        <section>
          <div className="so-section-label">Test Plan</div>
          <Markdown>{spec.test_plan}</Markdown>
        </section>
      )}
      {spec.out_of_scope?.length > 0 && (
        <section>
          <div className="so-section-label">Out of Scope</div>
          <ul className="so-criteria spec-out-of-scope">{spec.out_of_scope.map((r, i) => <li key={i}><Markdown>{r}</Markdown></li>)}</ul>
        </section>
      )}
      {spec.verification && (
        <section>
          <div className="so-section-label">Verification</div>
          <Markdown>{spec.verification}</Markdown>
        </section>
      )}
      {(task.context?.plan_size_warning || planSize > 8) && (
        <div className="spec-plan-warning" data-testid="plan-size-warning">
          <IconAlertTriangle size={14} /> Large plan ({planSize} files) — consider decomposing
        </div>
      )}
      {task.status === "awaiting_input" && spec && (
        <div className="spec-approval-gate" data-testid="spec-approval-gate">
          {!requestingChanges ? (
            <div className="spec-approval-actions">
              <button className="btn-approve" disabled={specBusy} onClick={async () => {
                setSpecBusy(true);
                try { await replyTask(task.id, "spec approved"); if (onRefresh) onRefresh(); }
                catch { /* handled by parent */ }
                finally { setSpecBusy(false); }
              }}>Approve Spec</button>
              <button className="btn-request-changes" disabled={specBusy}
                onClick={() => setRequestingChanges(true)}>Request Changes</button>
            </div>
          ) : (
            <div className="spec-change-request">
              <textarea className="spec-change-textarea" rows={3}
                placeholder="Describe what needs to change…"
                value={changeNote} onChange={(e) => setChangeNote(e.target.value)} />
              <div className="spec-approval-actions">
                <button className="btn-approve" disabled={specBusy || !changeNote.trim()} onClick={async () => {
                  setSpecBusy(true);
                  try { await replyTask(task.id, changeNote.trim()); if (onRefresh) onRefresh(); }
                  catch { /* handled by parent */ }
                  finally { setSpecBusy(false); setRequestingChanges(false); setChangeNote(""); }
                }}>Submit Changes</button>
                <button className="btn-cancel" onClick={() => { setRequestingChanges(false); setChangeNote(""); }}>Cancel</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function BlockerSection({ blocker: b, taskId, taskStatus }) {
  const cat = b.category ? String(b.category).replace(/_/g, " ") : null;
  const pct = b.confidence != null ? `${Math.round(b.confidence * 100)}%` : null;
  // W2.4: options are buttons — one click answers the blocker (and applies
  // its action server-side). Disabled after the first click: a double-click
  // must never double-resume. Parked states only; a non-parked task's
  // blocker record is history, not a live question.
  const [chosen, setChosen] = useState(null);   // index being submitted
  const [choiceErr, setChoiceErr] = useState(null);
  const parked = ["blocked", "awaiting_input", "escalated", "paused_quota"]
    .includes(taskStatus);
  async function choose(i) {
    if (chosen != null) return;
    setChosen(i);
    setChoiceErr(null);
    try {
      await chooseBlockerOption(taskId, i + 1);  // API is 1-based
    } catch (e) {
      setChoiceErr(e?.message || "reply failed");
      setChosen(null);                           // let the human retry
    }
  }
  return (
    <section>
      <div className="so-section-label blocker-label">Blocker</div>
      {cat && (
        <div className="blocker-meta">
          <span className="blocker-cat">{cat}</span>
          {pct && <span className="blocker-confidence">{pct} confidence</span>}
          {b.transient && <span className="blocker-transient">transient</span>}
        </div>
      )}
      {b.goal && (
        <div className="blocker-field">
          <div className="blocker-field-label">Goal</div>
          <div className="blocker-field-body">{b.goal}</div>
        </div>
      )}
      {b.evidence && (
        <div className="blocker-field">
          <div className="blocker-field-label">What happened</div>
          <div className="blocker-field-body">{b.evidence}</div>
        </div>
      )}
      {b.root_cause_hypothesis && b.root_cause_hypothesis !== b.question && (
        // When a task is operator-paused with a single reason, the same string
        // lands in both root_cause_hypothesis and question — don't print it
        // twice under two labels. Keep the actionable "Question for you" block
        // (it carries the reply/options) and drop the redundant "Why blocked".
        <div className="blocker-field">
          <div className="blocker-field-label">Why blocked</div>
          <div className="blocker-field-body">{b.root_cause_hypothesis}</div>
        </div>
      )}
      {b.question && (
        <div className="blocker-field blocker-question">
          <div className="blocker-field-label">Question for you</div>
          <div className="blocker-field-body">{b.question}</div>
          {b.options?.length > 0 && (
            <ul className="blocker-options">
              {b.options.map(normalizeOption).map((opt, i) => (
                <li key={i}>
                  {parked && taskId ? (
                    <button
                      className="blocker-option-btn"
                      disabled={chosen != null}
                      onClick={() => choose(i)}
                    >
                      {chosen === i ? "…" : `[${i + 1}]`} {opt.label}
                      {hasAction(opt) ? " ⚡" : ""}
                    </button>
                  ) : (
                    <>[{i + 1}] {opt.label}</>
                  )}
                </li>
              ))}
            </ul>
          )}
          {choiceErr && <div className="blocker-choice-err">{choiceErr}</div>}
          {parked && (
            <div className="blocker-reply-hint">
              …or answer free-form with the Reply button above.
            </div>
          )}
        </div>
      )}
      {b.wake_condition && (
        <div className="blocker-field">
          <div className="blocker-field-label">Wake when</div>
          <div className="blocker-field-body blocker-wake">{b.wake_condition}</div>
        </div>
      )}
    </section>
  );
}

// Extract the unified-diff hunk for `file:line` and trim to ±CTX lines around
// the target so each review comment shows only the relevant snippet.
const HUNK_CTX = 7;

function trimToTarget(body, targetLine) {
  if (body.length <= HUNK_CTX * 2 + 2) return body;
  const m = body[0].match(/@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/);
  if (!m || !targetLine || targetLine <= 0) return body.slice(0, HUNK_CTX * 2 + 1);
  let cur = parseInt(m[1], 10) - 1;
  let idx = -1;
  for (let i = 1; i < body.length; i++) {
    if (!body[i].startsWith("-")) cur++;
    if (cur === targetLine) { idx = i; break; }
    if (cur > targetLine + 2) break;
  }
  if (idx < 0) return body.slice(0, HUNK_CTX * 2 + 1);
  const from = Math.max(1, idx - HUNK_CTX);
  const to   = Math.min(body.length - 1, idx + HUNK_CTX);
  return [body[0], ...body.slice(from, to + 1)];
}

function extractHunk(diff, file, line) {
  if (!diff || !file) return null;
  const lines = diff.split("\n");
  const want = String(file).replace(/^[ab]\//, "");
  let secStart = -1;
  for (let j = 0; j < lines.length; j++) {
    const l = lines[j];
    if ((l.startsWith("+++ ") || l.startsWith("diff --git")) && l.includes(want)) { secStart = j; break; }
  }
  if (secStart === -1) return null;
  let secEnd = lines.length;
  for (let j = secStart + 1; j < lines.length; j++) {
    if (lines[j].startsWith("diff --git")) { secEnd = j; break; }
  }
  const hunkRe = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/;
  let best = null, bestDist = Infinity;
  for (let j = secStart; j < secEnd; j++) {
    const m = lines[j].match(hunkRe);
    if (!m) continue;
    const start = parseInt(m[1], 10);
    const count = m[2] ? parseInt(m[2], 10) : 1;
    const end   = start + count - 1;
    const body  = [lines[j]];
    let k = j + 1;
    while (k < secEnd && !lines[k].startsWith("@@") && !lines[k].startsWith("diff --git")) {
      body.push(lines[k]); k++;
    }
    if (!line || line <= 0) { if (!best) best = trimToTarget(body, -1); continue; }
    if (line >= start - 2 && line <= end + 2) return trimToTarget(body, line);
    const dist = Math.min(Math.abs(line - start), Math.abs(line - end));
    if (dist < bestDist) { bestDist = dist; best = trimToTarget(body, line); }
  }
  return best;
}

function CommentHunk({ diff, file, line }) {
  const hunk = extractHunk(diff, file, line);
  if (!hunk) return null;
  return (
    <div className="ci-hunk">
      <pre className="diff-pre">
        {hunk.map((l, idx) => (
          <div key={idx} className={`diff-line ${diffLineClass(l)}`}>{l || " "}</div>
        ))}
      </pre>
    </div>
  );
}

function ReviewTab({ task, diff }) {
  const [rawOpen, setRawOpen] = useState(false);
  const [posted, setPosted] = useState({});  // index → "ok" | "error" | "busy"
  const [postingAll, setPostingAll] = useState(false);
  const [finishing, setFinishing] = useState(false);

  async function handleFinishReview() {
    setFinishing(true);
    try {
      await finishReview(task.id);  // → done; the WS task_updated refreshes the board/drawer
    } catch {
      setFinishing(false);
    }
  }

  if (!task) return <div className="so-diff-empty">Loading…</div>;

  const lastAttempt = task.attempts?.[task.attempts.length - 1];
  const checklist = lastAttempt?.review_checklist;

  if (!checklist?.items?.length) {
    return (
      <div className="so-diff-empty">
        {task.status === "pending" || task.status === "context" || task.status === "implementing"
          ? "Review not started yet."
          : "No review checklist available."}
      </div>
    );
  }

  const allPassed = checklist.passed;
  const testResults = lastAttempt?.test_results;
  const tamperFlag = testResults?.tamper_flag;
  const ciUrl = lastAttempt?.ci_pipeline_url;
  const rawOutput = checklist.raw_output;
  const hasPrUrl = !!(task.context?.pr_url);
  const failedIndices = checklist.items
    .map((it, i) => (!it.passed ? i : -1))
    .filter((i) => i >= 0);

  async function handlePostOne(index) {
    setPosted((p) => ({ ...p, [index]: "busy" }));
    try {
      const res = await postReviewComments(task.id, [index]);
      const r = res.results?.[0];
      setPosted((p) => ({ ...p, [index]: r?.ok ? "ok" : "error" }));
    } catch {
      setPosted((p) => ({ ...p, [index]: "error" }));
    }
  }

  async function handlePostAll() {
    setPostingAll(true);
    const toPost = failedIndices.filter((i) => posted[i] !== "ok");
    toPost.forEach((i) => setPosted((p) => ({ ...p, [i]: "busy" })));
    try {
      const res = await postReviewComments(task.id, toPost);
      for (const r of res.results || []) {
        setPosted((p) => ({ ...p, [r.index]: r.ok ? "ok" : "error" }));
      }
    } catch {
      toPost.forEach((i) => setPosted((p) => ({ ...p, [i]: "error" })));
    }
    setPostingAll(false);
  }

  const allFailedPosted = failedIndices.length > 0 &&
    failedIndices.every((i) => posted[i] === "ok");

  // A standalone code_review of someone else's PR is NOT a build gate — the job
  // is "here are the comments, approve the ones to post in your name". The
  // pass/fail-stage-criteria framing below is for the build gate; give code
  // reviews their own plain, unambiguous surface.
  if (task.kind === "code_review") {
    const comments = checklist.items
      .map((it, i) => ({ it, i }))
      .filter(({ it }) => !it.passed);
    const prUrl = task.context?.pr_url;
    const prShort = prUrl ? prUrl.replace(/^https?:\/\//, "") : null;
    const allPosted = comments.length > 0 && comments.every(({ i }) => posted[i] === "ok");
    const nUnposted = comments.filter(({ i }) => posted[i] !== "ok").length;
    return (
      <div className="cr-approve">
        <div className="cr-approve-head">
          <div className="cr-approve-title">
            {comments.length} review comment{comments.length !== 1 ? "s" : ""}
          </div>
          {prShort && (
            <a href={prUrl} target="_blank" rel="noreferrer" className="cr-approve-pr">
              on {prShort} ↗
            </a>
          )}
        </div>
        <div className="cr-approve-warn">
          These post as comments <strong>in your name</strong>. Nothing is sent
          until you approve — read each one, then post the ones you agree with.
        </div>
        {comments.length === 0 ? (
          <div className="so-diff-empty">
            The reviewer found nothing to comment on — no comments to post.
          </div>
        ) : (
          <>
            <div className="cr-approve-actions">
              {prUrl && !allPosted && (
                <button className="btn btn-approve" onClick={handlePostAll} disabled={postingAll}>
                  {postingAll ? "Posting…" : `Approve & post all ${nUnposted}`}
                </button>
              )}
              {allPosted && (
                <span className="review-posted-badge">✓ All {comments.length} posted</span>
              )}
              {!prUrl && <span className="cr-nopr">No PR URL — cannot post from here.</span>}
              {/* You decide when the review is done — post all, some, or none.
                  A code_review has no PR of its own to merge, so it won't
                  self-complete; this takes it to Done. */}
              <button
                className="btn btn-finish-review"
                onClick={handleFinishReview}
                disabled={finishing}
                title="Mark this review done — whether or not you posted every comment"
              >
                {finishing ? "Finishing…" : "Finish review →"}
              </button>
            </div>
            <div className="cr-comment-list">
              {comments.map(({ it, i }) => (
                <div key={i} className="cr-comment">
                  <div className="cr-comment-loc">
                    {it.file ? (
                      <span className="ci-filechip" title={it.file}>
                        {it.file.split("/").slice(-2).join("/")}{it.line > 0 ? `:${it.line}` : ""}
                      </span>
                    ) : (
                      <span className="cr-general">general comment</span>
                    )}
                    {it.severity && (
                      <span className={`cr-sev cr-sev-${String(it.severity).toLowerCase()}`}>
                        {it.severity}
                      </span>
                    )}
                    {posted[i] === "ok" && <span className="post-badge post-ok">✓ posted</span>}
                  </div>
                  {it.file && <CommentHunk diff={diff} file={it.file} line={it.line} />}
                  <div className="cr-comment-body">{it.comment || it.evidence || it.label}</div>
                  {prUrl && posted[i] !== "ok" && (
                    <div className="ci-post-row">
                      <button
                        className="btn btn-post-one"
                        onClick={() => handlePostOne(i)}
                        disabled={posted[i] === "busy"}
                      >
                        {posted[i] === "busy" ? "Posting…"
                          : posted[i] === "error" ? "Retry post"
                          : "Approve & post this"}
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
        {rawOutput && (
          <section>
            <button className="raw-toggle" onClick={() => setRawOpen((o) => !o)}>
              {rawOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />} Reviewer's full reasoning
            </button>
            {rawOpen && <pre className="raw-output">{rawOutput}</pre>}
          </section>
        )}
      </div>
    );
  }

  return (
    <>
      {tamperFlag && (
        <div className="tamper-banner">
          TAMPER DETECTED — test count reduced between attempts
        </div>
      )}
      <section>
        <div className="so-section-label so-section-label-row">
          <span>Reviewer verdict —{" "}
            {allPassed
              ? <span className="verdict-pass">PASSED</span>
              : <span className="verdict-fail">FAILED</span>
            }
          </span>
          {checklist.items.length >= 5 && (
            <span className="review-cap-indicator" data-testid="review-cap">(capped at 5 items)</span>
          )}
          <div className="review-header-actions">
            {ciUrl && (
              <a href={ciUrl} target="_blank" rel="noreferrer" className="ci-link">
                CI pipeline →
              </a>
            )}
            {hasPrUrl && failedIndices.length > 0 && !allFailedPosted && (
              <button
                className="btn btn-post-all"
                onClick={handlePostAll}
                disabled={postingAll}
              >
                {postingAll ? "Posting…" : `Post All Comments (${failedIndices.length})`}
              </button>
            )}
            {allFailedPosted && (
              <span className="review-posted-badge">All comments posted</span>
            )}
          </div>
        </div>
        {checklist.stages && (
          <div className="review-stages" data-testid="review-stages">
            <span className={`review-stage-badge ${checklist.stages.spec_compliance?.passed ? "pass" : "fail"}`}>
              Stage 1: Spec Compliance — {checklist.stages.spec_compliance?.passed ? "PASSED" : "FAILED"}
            </span>
            <span className={`review-stage-badge ${checklist.stages.code_quality?.passed ? "pass" : "fail"}`}>
              Stage 2: Code Quality — {checklist.stages.code_quality?.passed ? "PASSED" : "FAILED"}
            </span>
          </div>
        )}
        {checklist.stages && !checklist.stages.spec_compliance?.passed && (
          <div className="unmet-criteria" data-testid="unmet-criteria">
            <div className="so-section-label">Unmet Criteria</div>
            <ul className="unmet-list">
              {checklist.items.filter(it => !it.passed).map((it, i) => (
                <li key={i} className="unmet-item">
                  <span className="ci-icon fail"><IconX size={12} /></span>
                  <span>{it.label}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="so-checklist">
          {checklist.items.map((item, i) => (
            <div key={i} className={`checklist-item ${item.passed ? "pass" : "fail"}`}>
              {/* header row: icon + title + file chip */}
              <div className="ci-header">
                <span className="ci-icon">{item.passed ? <IconCheck size={14} /> : <IconX size={14} />}</span>
                <span className="ci-title">{item.label}</span>
                {item.file && (
                  <span className="ci-filechip" title={item.file + (item.line > 0 ? `:${item.line}` : "")}>
                    {item.file.split("/").slice(-2).join("/")}{item.line > 0 ? `:${item.line}` : ""}
                  </span>
                )}
                {hasPrUrl && !item.passed && posted[i] === "ok" && (
                  <span className="post-badge post-ok ci-badge">Posted</span>
                )}
              </div>
              {/* failed items get code hunk + comment */}
              {!item.passed && item.file && (
                <CommentHunk diff={diff} file={item.file} line={item.line} />
              )}
              {!item.passed && item.comment && (
                <div className="ci-comment">{item.comment}</div>
              )}
              {!item.passed && item.evidence && item.evidence !== item.comment && (
                <div className="ci-evidence">{item.evidence}</div>
              )}
              {hasPrUrl && !item.passed && posted[i] !== "ok" && (
                <div className="ci-post-row">
                  {posted[i] === "error" ? (
                    <button className="btn btn-post-retry" onClick={() => handlePostOne(i)}>
                      Retry posting
                    </button>
                  ) : (
                    <button
                      className="btn btn-post-one"
                      onClick={() => handlePostOne(i)}
                      disabled={posted[i] === "busy"}
                    >
                      {posted[i] === "busy" ? "Posting…" : "Post to PR"}
                    </button>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      </section>
      {testResults && (
        <section>
          <div className="so-section-label so-section-label-row">
            <span>Test results</span>
            {!tamperFlag && <span className="verdict-clean">clean</span>}
          </div>
          <TestResultCard result={testResults} />
        </section>
      )}
      {rawOutput && (
        <section>
          <button
            className="raw-toggle"
            onClick={() => setRawOpen((o) => !o)}
          >
            {rawOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />} Reviewer reasoning
          </button>
          {rawOpen && (
            <pre className="raw-output">{rawOutput}</pre>
          )}
        </section>
      )}
      {(checklist.suggested_next || lastAttempt?.suggested_next) && (
        <section>
          <div className="review-suggested-next" data-testid="suggested-next">
            <IconInfo size={14} />
            <span>{checklist.suggested_next || lastAttempt.suggested_next}</span>
          </div>
        </section>
      )}
    </>
  );
}

function TestResultCard({ result }) {
  const passed = result.passed ?? 0;
  const failed = result.failed ?? 0;
  const errors = result.errors ?? 0;
  // node --test reports no explicit total, which rendered a contradictory
  // "55 passed · 0 total"; derive it from the parts when absent.
  const total = result.total ?? (passed + failed + errors);
  return (
    <div className="test-result-card">
      <div className="test-result-stats">
        <span className="test-result-pass"><IconCheck size={12} /> {passed} passed</span>
        {failed > 0 && <span className="test-result-fail"><IconX size={12} /> {failed} failed</span>}
        {errors > 0 && <span className="test-result-fail"><IconX size={12} /> {errors} errors</span>}
        {total > 0 && <span className="test-result-dim">{total} total</span>}
      </div>
      {result.output && <pre className="raw-output">{result.output.slice(0, 2000)}</pre>}
    </div>
  );
}

// Self-contained, offline diff renderer. A read-only unified diff doesn't need
// a 5MB CDN-loaded editor — colorized monospace lines are lighter, work without
// network, and match the operator-terminal aesthetic.
function diffLineClass(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-file";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("diff ") || line.startsWith("index ")) return "diff-meta";
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-ctx";
}

function DiffTab({ diff }) {
  if (!diff) {
    return <div className="so-diff-empty">No diff available yet.</div>;
  }
  const lines = diff.split("\n");
  return (
    <div className="so-diff-wrap" data-testid="diff-view">
      <pre className="diff-pre">
        {lines.map((line, i) => (
          <div key={i} className={`diff-line ${diffLineClass(line)}`}>
            {line || " "}
          </div>
        ))}
      </pre>
    </div>
  );
}

function AttemptsTab({ task }) {
  if (!task) return <div className="so-diff-empty">Loading…</div>;
  if (!task.attempts?.length) {
    return <div className="so-diff-empty">No attempts yet.</div>;
  }

  const getAttemptPassRate = (a) => {
    const items = a.review_checklist?.items;
    if (!items?.length) return null;
    return items.filter(it => it.passed).length;
  };
  const attempts = task.attempts;
  let stagnant = false;
  if (attempts.length >= 2) {
    const last = getAttemptPassRate(attempts[attempts.length - 1]);
    const prev = getAttemptPassRate(attempts[attempts.length - 2]);
    const total = attempts[attempts.length - 1]?.review_checklist?.items?.length || 0;
    if (last !== null && prev !== null && last === prev && last < total) stagnant = true;
  }
  const passRates = attempts.map(a => {
    const items = a.review_checklist?.items;
    if (!items?.length) return null;
    return { passed: items.filter(it => it.passed).length, total: items.length };
  });
  const hasRates = passRates.filter(Boolean).length >= 2;

  return (
    <>
      {stagnant && (
        <div className="stagnation-warning" data-testid="stagnation-warning">
          <IconAlertTriangle size={14} />
          <span>No progress between last two attempts — review pass rate unchanged</span>
        </div>
      )}
      {hasRates && (
        <div className="pass-rate-trend" data-testid="pass-rate-trend">
          <span className="pass-rate-label">Review pass rate:</span>
          {passRates.map((r, i) => r && (
            <span key={i} className="pass-rate-item">
              {i > 0 && <span className="pass-rate-arrow">→</span>}
              <span className={r.passed === r.total ? "pass-rate-full" : "pass-rate-partial"}>
                {r.passed}/{r.total}
              </span>
            </span>
          ))}
        </div>
      )}
      {(() => {
        const withTests = attempts.filter(a => a.test_results?.test_count > 0).length;
        return withTests > 0 && (
          <div className="tdd-metric" data-testid="tdd-metric">
            <span className="pass-rate-label">Test adoption:</span>
            <span className={withTests === attempts.length ? "pass-rate-full" : "pass-rate-partial"}>
              {withTests}/{attempts.length} attempts ran tests
            </span>
          </div>
        );
      })()}
      {[...task.attempts].reverse().map((a) => (
        <div key={a.id} className="attempt-row">
          <div className="attempt-number">Attempt #{a.attempt_number}</div>
          {a.branch_name && <div className="attempt-branch">{a.branch_name}</div>}
          {a.pr_url && (
            <div className="attempt-pr">
              <a href={a.pr_url} target="_blank" rel="noreferrer">{a.pr_url}</a>
            </div>
          )}
          <div className="attempt-status">
            {a.review_passed != null && (() => {
              const passed = !!a.review_passed;
              let label = passed ? "review passed" : "issues found";
              if (!passed && a.review_checklist?.items) {
                const n = a.review_checklist.items.filter(it => !it.passed).length;
                if (n > 0) label = `${n} issue${n > 1 ? "s" : ""} found`;
              }
              return (
                <span className={`attempt-badge ${passed ? "pass" : "fail"}`}>
                  {label}
                </span>
              );
            })()}
            {a.ci_status && (
              <span className={`attempt-badge ${a.ci_status === "success" ? "pass" : "fail"}`}>
                CI {a.ci_status}
              </span>
            )}
          </div>
          {a.failure_reason && (
            <div className="test-result-fail-msg">{a.failure_reason}</div>
          )}
        </div>
      ))}
    </>
  );
}
