import { useEffect, useRef, useState } from "react";
import { approveTask, fetchDiff, fetchTask, sendBack } from "./api.js";

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

export default function SlideOver({ taskId, onClose }) {
  const [task, setTask] = useState(null);
  const [diff, setDiff] = useState("");
  const [tab, setTab] = useState("details");
  const [busy, setBusy] = useState(false);
  const [sbOpen, setSbOpen] = useState(false);
  const [sbMsg, setSbMsg] = useState("");
  const [flash, setFlash] = useState(null);

  useEffect(() => {
    fetchTask(taskId).then(setTask).catch(() => {});
    fetchDiff(taskId).then(setDiff).catch(() => {});
  }, [taskId]);

  const isAwaiting = task?.status === "awaiting_approval";
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

  return (
    <>
      <div className="slideover-backdrop" onClick={onClose} />
      <div className="slideover" role="dialog" aria-modal="true">
        {/* header */}
        <div className="so-header">
          <div className="so-header-text">
            <div className="so-id">{task?.id ?? taskId}</div>
            <div className="so-title">{task?.title ?? "Loading…"}</div>
          </div>
          {task && (
            <span className={`so-status-pill ${pillClass}`}>
              {task.status}
            </span>
          )}
          <button className="so-close" onClick={onClose}>✕</button>
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
      {task.blocker && (
        <section>
          <div className="so-section-label" style={{ color: "var(--red)" }}>Blocker</div>
          <div className="so-description" style={{ color: "var(--red)" }}>
            {JSON.stringify(task.blocker, null, 2)}
          </div>
        </section>
      )}
      {task.repo_path && (
        <section>
          <div className="so-section-label">Repo</div>
          <div style={{ fontSize: 11, color: "var(--text-dim)" }}>{task.repo_path}</div>
        </section>
      )}
    </>
  );
}

function ReviewTab({ task }) {
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

  return (
    <>
      <section>
        <div className="so-section-label">
          Reviewer verdict — {allPassed
            ? <span style={{ color: "var(--green)" }}>PASSED</span>
            : <span style={{ color: "var(--red)" }}>FAILED</span>
          }
        </div>
        <div className="so-checklist">
          {checklist.items.map((item, i) => (
            <div key={i} className={`checklist-item ${item.passed ? "pass" : "fail"}`}>
              <div className="checklist-icon">{item.passed ? "✓" : "✗"}</div>
              <div>
                <div className="checklist-text">{item.criterion}</div>
                {item.evidence && (
                  <div className="checklist-evidence">{item.evidence}</div>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>
      {lastAttempt?.test_results && (
        <section>
          <div className="so-section-label">Test results</div>
          <TestResultCard result={lastAttempt.test_results} />
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
