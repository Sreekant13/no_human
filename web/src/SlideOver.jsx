import { useEffect, useRef, useState, useCallback } from "react";
import { approveTask, fetchDiff, fetchTask, replyTask, sendBack } from "./api.js";

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
  const [tab, setTab] = useState("details");
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
  const isParked = task?.status === "awaiting_input" || task?.status === "blocked";
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
          {["details", "review", "diff", "attempts"].map((t) => (
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
          {tab === "details"  && <DetailsTab task={task} />}
          {tab === "review"   && <ReviewTab task={task} />}
          {tab === "diff"     && <DiffTab diff={diff} />}
          {tab === "attempts" && <AttemptsTab task={task} />}
        </div>

        {/* action bar */}
        {task && (
          <div className="so-actions">
            <button
              className="btn btn-approve"
              onClick={handleApprove}
              disabled={!isAwaiting || busy}
            >
              {busy ? "…" : "Approve"}
            </button>
            <button
              className="btn btn-sendback"
              onClick={() => setSbOpen(true)}
              disabled={busy}
            >
              Send back
            </button>
            {isParked && (
              <button
                className="btn btn-reply"
                onClick={() => setReplyOpen(true)}
                disabled={busy}
              >
                Reply
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
    <div
      style={{
        padding: "8px 12px",
        background: "rgba(245,158,11,0.1)",
        border: "1px solid var(--amber-dim)",
        borderRadius: 3,
        fontSize: 11,
        color: "var(--amber)",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        gap: 8,
      }}
    >
      <span>{msg}</span>
      <button
        onClick={onDismiss}
        style={{
          background: "none", border: "none", color: "var(--amber)",
          cursor: "pointer", fontFamily: "inherit", fontSize: 11,
        }}
      >
        ✕
      </button>
    </div>
  );
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
          <div style={{ fontSize: 11, color: "var(--text-dim)" }}>{task.repo_path}</div>
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

function ReviewTab({ task }) {
  const [rawOpen, setRawOpen] = useState(false);
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

  return (
    <>
      {tamperFlag && (
        <div className="tamper-banner">
          TAMPER DETECTED — test count reduced between attempts
        </div>
      )}
      <section>
        <div className="so-section-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span>Reviewer verdict —{" "}
            {allPassed
              ? <span style={{ color: "var(--green)" }}>PASSED</span>
              : <span style={{ color: "var(--red)" }}>FAILED</span>
            }
          </span>
          {ciUrl && (
            <a
              href={ciUrl}
              target="_blank"
              rel="noreferrer"
              className="ci-link"
            >
              CI pipeline →
            </a>
          )}
        </div>
        <div className="so-checklist">
          {checklist.items.map((item, i) => (
            <div key={i} className={`checklist-item ${item.passed ? "pass" : "fail"}`}>
              <div className="checklist-icon">{item.passed ? "✓" : "✗"}</div>
              <div>
                <div className="checklist-text">{item.label}</div>
                {item.evidence && (
                  <div className="checklist-evidence">{item.evidence}</div>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>
      {testResults && (
        <section>
          <div className="so-section-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span>Test results</span>
            {!tamperFlag && (
              <span style={{ fontSize: 10, color: "var(--green)" }}>clean</span>
            )}
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
    <div style={{
      background: "var(--bg-panel)",
      border: "1px solid var(--border)",
      borderRadius: 3,
      padding: "10px 12px",
      fontSize: 11,
      display: "flex",
      flexDirection: "column",
      gap: 6,
    }}>
      <div style={{ display: "flex", gap: 16 }}>
        <span style={{ color: "var(--green)" }}>✓ {passed ?? 0} passed</span>
        {(failed ?? 0) > 0 && (
          <span style={{ color: "var(--red)" }}>✗ {failed} failed</span>
        )}
        <span style={{ color: "var(--text-dim)" }}>{total ?? 0} total</span>
      </div>
      {output && (
        <div style={{
          marginTop: 4,
          fontFamily: "inherit",
          fontSize: 10,
          color: "var(--text-dim)",
          whiteSpace: "pre-wrap",
          maxHeight: 120,
          overflow: "auto",
        }}>
          {output.slice(0, 2000)}
        </div>
      )}
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
            <div style={{ fontSize: 10, color: "var(--red)", marginTop: 4 }}>
              {a.failure_reason}
            </div>
          )}
        </div>
      ))}
    </>
  );
}
