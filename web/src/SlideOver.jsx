import { useEffect, useRef, useState, useCallback } from "react";
import {
  approveTask, cancelTask, fetchDiff, fetchTask, fetchTaskEvents, pauseTask,
  postReviewComments, replyTask, resumeTask, retryTask, sendBack,
} from "./api.js";

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

export default function SlideOver({ taskId, onClose, refreshKey = 0 }) {
  const [task, setTask] = useState(null);
  const [diff, setDiff] = useState("");
  const [tab, setTab] = useState("activity");
  const [busy, setBusy] = useState(false);
  const [sbOpen, setSbOpen] = useState(false);
  const [sbMsg, setSbMsg] = useState("");
  const [replyOpen, setReplyOpen] = useState(false);
  const [replyMsg, setReplyMsg] = useState("");
  const [flash, setFlash] = useState(null);
  const dialogRef = useRef(null);
  const closeRef = useRef(null);

  // Re-fetch whenever taskId changes OR when Board signals a WS update
  useEffect(() => {
    fetchTask(taskId).then(setTask).catch(() => {});
    fetchDiff(taskId).then(setDiff).catch(() => {});
  }, [taskId, refreshKey]);

  // Escape-to-close + focus trap
  useEffect(() => {
    closeRef.current?.focus();

    function onKeyDown(e) {
      if (e.key === "Escape") { onClose(); return; }
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
  const isParked = task?.status === "awaiting_input" || task?.status === "blocked" || task?.status === "escalated";
  const isActive = ["pending", "context", "planning", "implementing", "reviewing", "testing"].includes(task?.status);
  const isFailed = task?.status === "failed";
  const isTerminal = task?.status === "done" || task?.status === "failed";
  const pillClass = STATUS_PILL[task?.status] || "pill-pending";

  async function handleApprove() {
    if (!isAwaiting || busy) return;
    setBusy(true);
    try {
      await approveTask(taskId);
      setFlash("Approval recorded. Merge the PR in your git host.");
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
          {task && (
            <span className={`so-status-pill ${pillClass}`}>
              {task.status}
            </span>
          )}
          <button className="so-close" onClick={onClose} ref={closeRef} aria-label="Close">✕</button>
        </div>

        {/* tabs */}
        <div className="so-tabs">
          {["activity", "details", "review", "diff", "attempts"].map((t) => (
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
          {tab === "activity" && <ActivityTab taskId={taskId} isActive={isActive} />}
          {tab === "details"  && <DetailsTab task={task} />}
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
              <button className="btn btn-lifecycle btn-cancel" onClick={() => handleLifecycle("cancel")} disabled={busy}>
                Cancel
              </button>
            )}
          </div>
        )}
      </div>

      {/* send-back modal */}
      {sbOpen && (
        <div className="sendback-overlay" onClick={() => setSbOpen(false)}>
          <div className="sendback-modal" onClick={(e) => e.stopPropagation()}>
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
        <div className="sendback-overlay" onClick={() => setReplyOpen(false)}>
          <div className="sendback-modal" onClick={(e) => e.stopPropagation()}>
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
      <button className="flash-banner-dismiss" onClick={onDismiss}>✕</button>
    </div>
  );
}

// Which agent role an event belongs to. The orchestrator's event `kind` is
// already role-specific (the supervisor hook emits `supervisor`, the reviewer
// emits review_*), so we derive the role here for per-role styling without a
// backend change. A `source` field on the event (when present) wins.
const SOURCE_BY_KIND = {
  supervisor: "supervisor",
  review_start: "reviewer", review: "reviewer", review_error: "reviewer",
  tamper: "reviewer",
};
function eventSource(e) {
  return e.source || SOURCE_BY_KIND[e.kind] || "worker";
}
const ROLE_LABEL = { worker: "Worker", supervisor: "Supervisor", reviewer: "Reviewer" };


function ActivityTab({ taskId, isActive }) {
  const [events, setEvents] = useState([]);
  const endRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      while (!cancelled) {
        try {
          const evts = await fetchTaskEvents(taskId);
          if (!cancelled) setEvents(evts);
        } catch { /* ignore */ }
        // Poll faster while active.
        await new Promise((r) => setTimeout(r, isActive ? 2000 : 10000));
      }
    }
    poll();
    return () => { cancelled = true; };
  }, [taskId, isActive]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  if (events.length === 0) {
    return <div className="so-diff-empty">{isActive ? "Waiting for events…" : "No events recorded."}</div>;
  }

  const lastEvent = events[events.length - 1];
  const isWorking = isActive && lastEvent;
  const lastRole = eventSource(lastEvent);

  return (
    <div className="activity-feed">
      <div className="activity-legend">
        <span className="al-role role-worker"><i />Worker — builds</span>
        <span className="al-role role-supervisor"><i />Supervisor — steers</span>
        <span className="al-role role-reviewer"><i />Reviewer — gates</span>
      </div>
      {isWorking && (
        <div className={`activity-status-bar role-${lastRole}`}>
          <span className="activity-pulse" />
          <span className="activity-status-text">
            {ROLE_LABEL[lastRole]} · {eventLabel(lastEvent.kind)}: {lastEvent.text}
          </span>
        </div>
      )}
      <div className="activity-log">
        {events.map((e, i) => {
          const elapsed = i > 0 ? e.ts - events[i - 1].ts : 0;
          const role = eventSource(e);
          return (
            <div key={i} className={`activity-event role-${role} ak-${e.kind}`}>
              <span className="activity-ts">{fmtTs(e.ts)}</span>
              <span className="activity-role">{ROLE_LABEL[role]}</span>
              <span className="activity-text">
                <span className={`activity-kind ak-${e.kind}`}>{eventLabel(e.kind)}</span>
                {" "}{e.text}
              </span>
              {elapsed > 2 && <span className="activity-elapsed">+{fmtDuration(elapsed)}</span>}
            </div>
          );
        })}
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
          <ul className="so-criteria">
            {task.acceptance_criteria.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </section>
      )}
      {task.blocker && <BlockerSection blocker={task.blocker} />}
      {task.repo_path && (
        <section>
          <div className="so-section-label">Repo</div>
          <div className="so-repo-path">{task.repo_path}</div>
        </section>
      )}
    </>
  );
}

function BlockerSection({ blocker: b }) {
  const cat = b.category ? String(b.category).replace(/_/g, " ") : null;
  const pct = b.confidence != null ? `${Math.round(b.confidence * 100)}%` : null;
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
      {b.root_cause_hypothesis && (
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
              {b.options.map((opt, i) => (
                <li key={i}>[{i + 1}] {opt}</li>
              ))}
            </ul>
          )}
          <div className="blocker-reply-hint">
            Reply: <code>nh reply {"{id}"} "&lt;answer&gt;"</code>
          </div>
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
        <div className="so-checklist">
          {checklist.items.map((item, i) => (
            <div key={i} className={`checklist-item ${item.passed ? "pass" : "fail"}`}>
              {/* header row: icon + title + file chip */}
              <div className="ci-header">
                <span className="ci-icon">{item.passed ? "✓" : "✗"}</span>
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
            {rawOpen ? "▼" : "▶"} Reviewer reasoning
          </button>
          {rawOpen && (
            <pre className="raw-output">{rawOutput}</pre>
          )}
        </section>
      )}
    </>
  );
}

function TestResultCard({ result }) {
  const { passed, failed, total, output } = result;
  return (
    <div className="test-result-card">
      <div className="test-result-stats">
        <span className="test-result-pass">✓ {passed ?? 0} passed</span>
        {(failed ?? 0) > 0 && <span className="test-result-fail">✗ {failed} failed</span>}
        <span className="test-result-dim">{total ?? 0} total</span>
      </div>
      {output && <pre className="raw-output">{output.slice(0, 2000)}</pre>}
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

  return (
    <>
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
            {a.review_passed != null && (
              <span className={`attempt-badge ${a.review_passed ? "pass" : "fail"}`}>
                review {a.review_passed ? "pass" : "fail"}
              </span>
            )}
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
