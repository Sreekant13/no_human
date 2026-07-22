import { useEffect, useReducer, useRef, useState } from "react";
import { connectWS, createTask, uploadAttachment, fetchTasks, fetchWorkerStatus, fetchQueueHealth, fetchOnboardingStatus, grillStep, grillStepSSE } from "./api.js";
import Board from "./Board.jsx";
import SettingsOverlay from "./Settings.jsx";
import Stats from "./Stats.jsx";
import Onboarding from "./Onboarding.jsx";
import TaskComposer from "./TaskComposer.jsx";
import Outcomes from "./Outcomes.jsx";
import { LegionLogo } from "./Logo.jsx";
import { newlyNeedsYou, notificationBody, titleWithBadge } from "./notifications.js";
import { setFavicon } from "./favicon.js";
import { needsPrUrl } from "./composerKinds.js";
import { hasPrRef } from "./prRefs.js";
import { shouldTriggerNewTask } from "./keyboardShortcut.js";
import { isNeedsYou, isRealFailure } from "./boardLanes.js";
import { ledgerSummary } from "./nightLedger.js";
import { fmtCost } from "./cost.js";
import { tasksReducer } from "./tasksReducer.js";
import { useEscapeKey } from "./useEscapeKey.js";

const PROGRESS_STATUSES  = new Set(["pending", "context", "planning", "implementing", "reviewing", "testing"]);

// Header brand: logo + wordmark + tagline. Used in the main and error headers.
function Brand() {
  return (
    <div className="legion-brand">
      <LegionLogo size={32} />
      <div className="legion-wordmark">
        <span className="legion-name">no_human</span>
        <span className="legion-tag">get the max out of Claude</span>
      </div>
    </div>
  );
}

// ── 1.5: sidebar nav icon set — inline SVG only (CSP forbids remote icon
// fonts/images). Every icon below is paired with a text label by NavRow —
// never rendered alone. 16px, single stroke weight, no fills except the
// small "done" check and the tiny alert dot, both currentColor.
function IconBoard() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="2.5" width="3.4" height="11" rx="1" />
      <rect x="6.3" y="2.5" width="3.4" height="7.5" rx="1" />
      <rect x="11.1" y="2.5" width="3.4" height="9.5" rx="1" />
    </svg>
  );
}
function IconDone() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6.25" />
      <path d="M5.3 8.2l1.9 1.9 3.5-4.2" />
    </svg>
  );
}
function IconFailed() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 1.6l6.7 11.6a1 1 0 0 1-.86 1.5H2.16a1 1 0 0 1-.86-1.5L8 1.6z" />
      <path d="M8 6.2v3.1" />
      <circle cx="8" cy="11.5" r="0.9" fill="currentColor" stroke="none" />
    </svg>
  );
}
function IconStats() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 13.5h12" />
      <path d="M4.5 13.5V9" />
      <path d="M8 13.5V5.5" />
      <path d="M11.5 13.5V7" />
    </svg>
  );
}
function IconGear() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="2.3" />
      <path d="M8 1.8v1.6M8 12.6v1.6M14.2 8h-1.6M3.4 8H1.8M12.1 3.9l-1.1 1.1M5 11.1l-1.1 1.1M12.1 12.1l-1.1-1.1M5 4.9L3.9 3.9" />
    </svg>
  );
}

// A single sidebar nav row: inline-SVG icon + text label, always both — an
// icon-only row is never rendered (label is not conditionally hidden). The
// active row gets a soft filled pill (see .nh-navrow.active in styles.css),
// animated on --dur-fast/--ease-out and prefers-reduced-motion guarded.
function NavRow({ icon, label, active, current, badge, badgeVariant, onClick, title, className = "" }) {
  return (
    <button
      className={`nh-navrow${active ? " active" : ""}${className ? ` ${className}` : ""}`}
      // aria-current marks the current PAGE for a screen reader — only the page-nav
      // rows pass `current`. Settings opens an overlay dialog, not a page, so it is
      // `active` (filled pill) without claiming to be the current page.
      aria-current={current ? "page" : undefined}
      onClick={onClick}
      title={title}
    >
      <span className="nh-navrow-icon" aria-hidden="true">{icon}</span>
      <span className="nh-navrow-label">{label}</span>
      {badge != null && (
        <span className={`nh-navrow-badge${badgeVariant ? ` nh-navrow-badge-${badgeVariant}` : ""}`}>{badge}</span>
      )}
    </button>
  );
}

// A muted, uppercase group header over a set of NavRows — the Claude-app
// "Work" / "Insights" sidebar grouping. Visual grouping only: every row still
// carries its own original handler, so no nav-model change hides here.
function NavGroup({ title, children }) {
  return (
    <div className="nh-navgroup">
      <div className="nh-navgroup-title">{title}</div>
      <div className="nh-navgroup-rows">{children}</div>
    </div>
  );
}

function fmtAge(seconds) {
  if (seconds < 60) return "<1m";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function NightLedger({ tasks }) {
  // The signature module: what no_human did in the last 24h, in the sidebar's
  // dead space. Derived from the same task list as the lanes (one source, one
  // truth) — a quiet board should read "calm instrument", never "dead app".
  const s = ledgerSummary(tasks);
  const cost = fmtCost(s.cost);
  return (
    <div className="nh-ledger" aria-label="last 24 hours summary">
      <div className="nh-ledger-title">last 24h</div>
      {s.quiet
        ? <div className="nh-ledger-quiet">quiet — nothing shipped, nothing owed</div>
        : (
          <div className="nh-ledger-rows">
            <div className="nh-ledger-row"><b>{s.done}</b> shipped</div>
            <div className="nh-ledger-row"><b>{s.parked}</b> waiting on you</div>
            {s.failed > 0 && (
              <div className="nh-ledger-row nh-ledger-bad"><b>{s.failed}</b> failed</div>
            )}
            {cost !== "—" && <div className="nh-ledger-row nh-ledger-cost"><b>{cost}</b> spent</div>}
          </div>
        )}
    </div>
  );
}

function OverviewStrip({ tasks }) {
  const needsYou   = tasks.filter(isNeedsYou);
  const failed     = tasks.filter(isRealFailure).length;
  // Cancels also end in `failed` status and sit in the Failed lane, so the lane's badge
  // counts them. Naming them here keeps the strip and the lane from contradicting each
  // other — "1 failed" over a lane badged 11 is worse than either number alone.
  const cancelled  = tasks.filter(t => t.status === "failed" && t.cancelled).length;
  const inProgress = tasks.filter(t => PROGRESS_STATUSES.has(t.status)).length;

  const oldestSec = needsYou.length > 0
    ? Math.max(...needsYou.map(t => (Date.now() - new Date(t.updated_at || t.created_at).getTime()) / 1000))
    : null;

  const allClear = needsYou.length === 0 && failed === 0;

  return (
    <div className="nh-overview">
      {allClear
        ? <span className="ov-clear">all clear</span>
        : <>
            <span className={needsYou.length > 0 ? "ov-awaiting" : ""}>{needsYou.length} need you</span>
            {failed > 0 && <>
              <span className="ov-sep">·</span>
              <span className="ov-escalated">{failed} failed</span>
            </>}
            {cancelled > 0 && <>
              <span className="ov-sep">·</span>
              <span>{cancelled} cancelled</span>
            </>}
          </>
      }
      {oldestSec !== null && (
        <>
          <span className="ov-sep">·</span>
          <span className={`ov-oldest${oldestSec > 24 * 3600 ? " ov-oldest-stale" : ""}`}>
            oldest {fmtAge(oldestSec)}
          </span>
        </>
      )}
      {/* "0 working" is noise on a phone — the Working lane already says so. The gate AGE is not. */}
      <span className="ov-sep ov-hide-narrow">·</span>
      <span className="ov-hide-narrow">{inProgress} working</span>
    </div>
  );
}

function Spinner() {
  return <span className="grill-spinner" />;
}

function NewTaskModal({ onClose, onCreated }) {
  // The composed spec, handed over by TaskComposer when the operator hits Next.
  // Null until then — the composer owns its own field state (see TaskComposer.jsx).
  const [fields, setFields] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // B2: grill state
  const [grillMode, setGrillMode] = useState(false);
  const [grillQA, setGrillQA] = useState([]);
  const [grillQuestion, setGrillQuestion] = useState(null);
  const [grillAnswer, setGrillAnswer] = useState("");
  const [grillResult, setGrillResult] = useState(null);
  const [grillEvents, setGrillEvents] = useState([]);
  const [evalVerdict, setEvalVerdict] = useState(null);
  const grillStreamRef = useRef(null);
  // Escape closes the dialog — same escape route the overlay-click already gives,
  // but for keyboard users. Suppressed while a submit is in flight so Escape can't
  // discard a task that's already being created. Bound ONLY on the grill branches:
  // the composer binds its own, and a state where both are mounted (a failed grill)
  // would otherwise fire onClose twice.
  const showingGrill = grillMode || Boolean(grillResult);
  useEscapeKey(onClose, !busy && showingGrill);

  async function handleSubmit(e) {
    if (e) e.preventDefault();
    if (!fields || busy) return;
    setBusy(true);
    setError(null);
    try {
      const title = grillResult?.title || fields.title;
      let description = grillResult?.description || fields.description;
      // The grill REWRITES the spec, and its prompt has no instruction to preserve
      // URLs. A code_review task whose refined text dropped the PR link would be
      // failed at the gate (orchestrator: parse_pr_refs over title+description), so
      // re-attach the operator's original reference when the rewrite lost it.
      if (needsPrUrl(fields.kind) && !hasPrRef(`${title} ${description || ""}`)) {
        const original = [fields.prUrl?.trim(), fields.description].find(hasPrRef);
        if (original) description = [description, original].filter(Boolean).join("\n\n");
      }
      const created = await createTask({
        title,
        description,
        repo_path: fields.repoPath,
        project_id: fields.projectId,
        kind: fields.kind,
        priority: fields.priority,
        acceptance_criteria: grillResult?.acceptance_criteria || [],
        // Task 1.6: the hidden marker TaskComposer sets when the task came
        // from a picked Jira ticket — "board" for every typed task, unchanged.
        source: fields.source,
      });
      // Attach any screenshots/documents to the new task (best-effort — a failed
      // upload must not lose the task that was already created).
      for (const f of fields.files || []) {
        try { await uploadAttachment(created.id, f); }
        catch (err) { console.error("attachment upload failed", f.name, err); }
      }
      onCreated();
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  // `spec` defaults to the committed fields; startGrill passes them explicitly
  // because it runs in the same tick as setFields (state is not yet visible).
  function _grillParams(qaOverride, spec = fields) {
    return {
      title: spec.title, description: spec.description,
      // A project resolves its own repo server-side, so repo_path is sent only
      // when there is no project — preserving the pre-composer behaviour.
      repo_path: spec.projectId ? null : spec.repoPath,
      project_id: spec.projectId,
      qa_history: qaOverride ?? [],
    };
  }

  function _startGrillSSE(params) {
    if (grillStreamRef.current) grillStreamRef.current.close();
    setGrillEvents([]);
    grillStreamRef.current = grillStepSSE(
      params,
      (evt) => setGrillEvents((prev) => [...prev.slice(-30), evt]),
      (result) => {
        setBusy(false);
        if (result.type === "done") { setGrillResult(result); setGrillQuestion(null); }
        else { setGrillQuestion(result); }
      },
      (err) => {
        // SSE failed — fall back to sync POST
        grillStep(params)
          .then((step) => {
            if (step.type === "done") { setGrillResult(step); } else { setGrillQuestion(step); }
          })
          .catch((e) => { setError(e.message); setGrillMode(false); })
          .finally(() => setBusy(false));
      },
      (evalData) => setEvalVerdict(evalData),
    );
  }

  function startGrill(spec) {
    if (!spec?.title || busy) return;
    setFields(spec);
    setBusy(true); setError(null); setGrillMode(true);
    setGrillQA([]); setGrillQuestion(null); setGrillResult(null); setEvalVerdict(null);
    _startGrillSSE(_grillParams([], spec));
  }

  function submitGrillAnswer() {
    if (!grillAnswer.trim() || busy) return;
    setBusy(true); setError(null);
    const newQA = [...grillQA, { question: grillQuestion.question, answer: grillAnswer.trim() }];
    setGrillQA(newQA); setGrillAnswer("");
    _startGrillSSE(_grillParams(newQA));
  }

  if (grillResult) {
    return (
      <div className="sendback-overlay" onClick={onClose}>
        <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
          <div className="sendback-label">Refined Spec</div>
          <div className="grill-spec">
            <div className="grill-spec-title">{grillResult.title}</div>
            {evalVerdict && (
              <div className="eval-verdict-card">
                <span className={`eval-badge eval-${evalVerdict.verdict}`}>
                  {evalVerdict.verdict.toUpperCase()}
                </span>
                {evalVerdict.dimensions && (
                  <span className="eval-dims">
                    {Object.entries(evalVerdict.dimensions).map(([k, v]) => (
                      <span key={k} className={`eval-dim ${v ? 'dim-pass' : 'dim-fail'}`}>
                        {k.replace(/_/g, ' ')}
                      </span>
                    ))}
                  </span>
                )}
                {evalVerdict.rationale && (
                  <div className="eval-rationale">{evalVerdict.rationale}</div>
                )}
              </div>
            )}
            {grillResult.description && <div className="grill-spec-desc">{grillResult.description}</div>}
            <div className="grill-spec-section-label">Acceptance Criteria</div>
            <ul className="grill-ac-list">
              {grillResult.acceptance_criteria.map((ac, i) => (
                <li key={i} className="grill-ac-item">
                  <span className="grill-ac-num">{i + 1}.</span>
                  <span>{ac}</span>
                </li>
              ))}
            </ul>
          </div>
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button className="btn btn-approve" disabled={busy} onClick={handleSubmit}>{busy ? "\u2026" : "Create Task"}</button>
          </div>
        </div>
      </div>
    );
  }

  // Loading overlay while grill is exploring (no question yet)
  if (grillMode && !grillQuestion && busy) {
    return (
      <div className="sendback-overlay" onClick={onClose}>
        <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
          <div className="sendback-label">Intake Grill</div>
          <div className="grill-loading">
            <Spinner />
            <div className="grill-loading-text">Exploring the codebase...</div>
            {grillEvents.length > 0 && (
              <div className="grill-explore-log">
                {grillEvents.map((ev, i) => (
                  <div key={i} className="grill-explore-entry" style={{ opacity: i === grillEvents.length - 1 ? 1 : 0.5 }}>
                    {ev.kind === 'tool_use' ? `\u2699 ${ev.text}` : ev.text}
                  </div>
                ))}
              </div>
            )}
            {grillEvents.length === 0 && (
              <div className="grill-loading-hint">This usually takes 15–30 seconds</div>
            )}
          </div>
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    );
  }

  if (grillMode && grillQuestion) {
    const maxRounds = 5;
    const progressPct = Math.min(100, (grillQuestion.round / maxRounds) * 100);
    if (busy) {
      return (
        <div className="sendback-overlay">
          <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
            <div className="grill-header">
              <div className="sendback-label">Intake Grill</div>
              <span className="grill-round-badge">Round {grillQuestion.round}/{maxRounds}</span>
            </div>
            <div className="grill-progress-bar">
              <div className="grill-progress-fill" style={{ width: `${progressPct}%` }} />
            </div>
            <div className="grill-loading">
              <Spinner />
              <div className="grill-loading-text">Processing your answer...</div>
              <div className="grill-loading-hint">Refining the next question based on your input</div>
            </div>
            {error && <div className="new-task-error">{error}</div>}
            <div className="sendback-actions">
              <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            </div>
          </div>
        </div>
      );
    }
    return (
      <div className="sendback-overlay" onClick={onClose}>
        <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
          <div className="grill-header">
            <div className="sendback-label">Intake Grill</div>
            <span className="grill-round-badge">Round {grillQuestion.round}/{maxRounds}</span>
          </div>
          <div className="grill-progress-bar">
            <div className="grill-progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
          {grillQA.length > 0 && (
            <div className="grill-qa-history">
              {grillQA.map((qa, i) => (
                <div key={i} className="grill-qa-item">
                  <div><strong>Q{i+1}:</strong> {qa.question}</div>
                  <div className="grill-qa-answer">{"\u2192"} {qa.answer}</div>
                </div>
              ))}
            </div>
          )}
          <div className="ntm-field">
            <div className="ntm-label">Question</div>
            <div style={{ fontWeight: 500, fontSize: '14px', lineHeight: 1.5, margin: '4px 0 12px' }}>{grillQuestion.question}</div>
          </div>
          {grillQuestion.suggestions.map((s, i) => (
            <button key={i} type="button" className="grill-suggestion" onClick={() => setGrillAnswer(s.replace(/^[A-D]:\s*/, ''))}>{s}</button>
          ))}
          <textarea className="sendback-textarea" placeholder="Your answer (or click a suggestion above)" value={grillAnswer} onChange={(e) => setGrillAnswer(e.target.value)} rows={2} style={{ marginTop: '8px' }} />
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="button" className="btn btn-sendback btn-sm"
              onClick={async () => {
                setBusy(true); setError(null);
                try {
                  const step = await grillStep(_grillParams([...grillQA, { question: grillQuestion.question, answer: "(skip \u2014 use what you have)" }]));
                  if (step.type === "done") { setGrillResult(step); setGrillQuestion(null); } else { setGrillResult({ title: step.title || fields.title, description: step.description || fields.description, acceptance_criteria: step.acceptance_criteria || [] }); setGrillQuestion(null); }
                } catch (err) { setError(err.message); }
                finally { setBusy(false); }
              }}>Skip &amp; finish</button>
            <button className="btn btn-approve" disabled={!grillAnswer.trim()} onClick={submitGrillAnswer}>Answer</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <TaskComposer
      busy={busy}
      error={error}
      // Re-seed after a failed grill: this component was unmounted for the grill's
      // duration, so without `initial` the operator would come back to an empty
      // composer with their prompt, attachments and kind gone.
      initial={fields}
      onStart={startGrill}
      onClose={onClose}
    />
  );
}

export default function App() {
  const [tasks, dispatch] = useReducer(tasksReducer, []);
  const [wsLive, setWsLive] = useState(false);
  const prevTasksRef = useRef([]);
  const [fetchError, setFetchError] = useState(null);
  const [showNewTask, setShowNewTask] = useState(false);
  const [page, setPage] = useState("board");
  // Settings is an overlay dialog (Claude macOS desktop app model), not a
  // routed page — it can open on top of whatever page is showing.
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [pendingOpenId, setPendingOpenId] = useState(null);
  const [workerStatus, setWorkerStatus] = useState(null);
  const [queueHealth, setQueueHealth] = useState(null);
  const doneCount = tasks.filter((t) => t.status === "done").length;
  const failedCount = tasks.filter(isRealFailure).length;
  const cancelledCount = tasks.filter((t) => t.status === "failed" && t.cancelled).length;

  // Theme: persisted choice, else the OS preference. Light mode was fully built
  // but unreachable (no toggle) — this exposes it.
  const [theme, setTheme] = useState(() =>
    localStorage.getItem("nh-theme")
    || (window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark"));
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("nh-theme", theme);
  }, [theme]);
  // Desktop shell (Electron) marks itself via the preload bridge; the class
  // gates the inset-title-bar accommodations (drag region + traffic-light
  // clearance) so the browser experience is untouched.
  useEffect(() => {
    document.documentElement.classList.toggle(
      "nh-in-shell", Boolean(window.nhDesktop?.shell));
  }, []);
  // null = checking; false = needs onboarding; true = onboarded. Fail-open so a
  // missing/old endpoint never blocks an existing user at the board.
  const [onboarded, setOnboarded] = useState(null);
  const wsRef = useRef(null);

  // onboarding gate
  useEffect(() => {
    fetchOnboardingStatus()
      .then((s) => setOnboarded(!!s.completed))
      .catch(() => setOnboarded(true));
  }, []);

  // initial load
  useEffect(() => {
    fetchTasks()
      .then((ts) => { setFetchError(null); dispatch({ type: "set", tasks: ts }); })
      .catch((err) => setFetchError(err?.message || "Cannot reach the no_human API."));
  }, []);

  // Worker status poll
  useEffect(() => {
    function poll() {
      fetchWorkerStatus().then(setWorkerStatus).catch(() => {});
      fetchQueueHealth().then(setQueueHealth).catch(() => {});
    }
    poll();
    const id = setInterval(poll, 10000);
    return () => clearInterval(id);
  }, []);

  // Needs-You notifications (W2.2): the tab title always carries the count;
  // the Notification API fires per NEW arrival when granted and the tab is
  // hidden. Permission is requested on the first user gesture (browsers
  // block gesture-less requests); denied/unsupported degrades to the badge.
  useEffect(() => {
    const needy = tasks.filter(isNeedsYou);
    document.title = titleWithBadge(needy.length);
    const fresh = newlyNeedsYou(prevTasksRef.current, tasks, isNeedsYou);
    prevTasksRef.current = tasks;
    if (fresh.length && typeof Notification !== "undefined"
        && Notification.permission === "granted" && document.hidden) {
      for (const t of fresh.slice(0, 3)) {
        try {
          const n = new Notification("no_human — needs you", { body: notificationBody(t) });
          // Clicking the toast must LAND somewhere: focus the window (the
          // desktop shell restores it from the tray) and open the task that
          // fired it — the operator's next action is always answering it.
          n.onclick = () => {
            window.focus();
            setPage("board");
            // Board is conditionally mounted: an event dispatched here is
            // lost when the operator was on another page (PR #104 review,
            // medium). A pending id survives until Board mounts and opens it.
            setPendingOpenId(t.id);
          };
        } catch { /* a notification is a bonus, never an error */ }
      }
    }
  }, [tasks]);

  // Favicon dot: surface an awaiting_approval task even when the tab title
  // (badge) isn't visible, e.g. a pinned tab.
  useEffect(() => {
    setFavicon(tasks.some((t) => t.status === "awaiting_approval"));
  }, [tasks]);

  useEffect(() => {
    if (typeof Notification === "undefined"
        || Notification.permission !== "default") return undefined;
    const ask = () => { Notification.requestPermission().catch(() => {}); };
    window.addEventListener("pointerdown", ask, { once: true });
    return () => window.removeEventListener("pointerdown", ask);
  }, []);

  // Global 'n' opens the New Task modal (unless typing or a modal is open).
  useEffect(() => {
    function onKeyDown(e) {
      // The drawer autofocuses its close BUTTON, which is not an editable tag — so without
      // this the "n" shortcut opened the composer *behind* the open drawer (z-50 under the
      // drawer's 101), where it held focus invisibly and one Escape then closed both.
      const drawerOpen = Boolean(document.querySelector(".slideover"));
      if (shouldTriggerNewTask(e, { modalOpen: showNewTask || drawerOpen })) {
        // Swallow the keystroke: the composer autofocuses its textarea, so an
        // un-prevented "n" types itself into the prompt it just opened.
        e.preventDefault();
        setShowNewTask(true);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [showNewTask]);

  // Desktop application menu → the board's own navigation. The Electron shell
  // (main process) posts "nh:menu"; drive the SAME page state the tab bar does,
  // so File ▸ New Task and View ▸ Board/Stats/Settings need no parallel UI.
  useEffect(() => {
    const off = window.nhDesktop?.onMenu?.((action) => {
      if (action === "new-task") setShowNewTask(true);
      else if (action === "settings") setSettingsOpen(true);
      else if (action === "board" || action === "stats") setPage(action);
    });
    return off;
  }, []);

  // WebSocket
  useEffect(() => {
    let disposed = false;
    let retry = null;
    function connect() {
      if (disposed) return;
      const ws = connectWS((msg) => {
        if (msg.tasks) dispatch({ type: "sync", tasks: msg.tasks });
        // inflight rides the WS frame; `running` stays the REST poll's
        // authority (a WS frame from a worker-less server said running:true).
        if (msg.worker) setWorkerStatus(prev => ({ ...prev, ...msg.worker }));
      });
      ws.onopen = () => setWsLive(true);
      ws.onclose = () => {
        setWsLive(false);
        if (!disposed) retry = setTimeout(connect, 3000);
      };
      wsRef.current = ws;
    }
    connect();
    return () => {
      // The retry id was never captured, so the cleanup could not cancel it: close() fires
      // onclose, which 3s later built ANOTHER socket that overwrote wsRef and orphaned the
      // previous one — two live sockets both dispatching into the reducer.
      disposed = true;
      if (retry) clearTimeout(retry);
      wsRef.current?.close();
    };
  }, []);

  if (onboarded === null) {
    return (
      <div className="nh-shell">
        <header className="nh-header"><Brand /></header>
        <div className="nh-board board-skeleton" aria-busy="true" aria-label="Loading board">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div className="sk-lane" key={i}>
              <div className="skeleton sk-head" />
              {[0, 1, 2].map((j) => <div className="skeleton sk-card" key={j} />)}
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (onboarded === false) {
    return <Onboarding onComplete={() => setOnboarded(true)} />;
  }

  if (fetchError) {
    return (
      <div className="nh-shell">
        <header className="nh-header">
          <Brand />
          <span className="legion-credit">Developed by eyalgolan</span>
        </header>
        <div className="nh-center">
          <div className="nh-error">
            <div>API unavailable: {fetchError}</div>
            <button
              className="btn btn-sendback btn-mt"
              onClick={() => window.location.reload()}
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  const needYou = tasks.filter(isNeedsYou).length;
  return (
    <div className="nh-shell nh-shell-cc">
      <aside className="nh-sidebar">
        <div className="nh-sidebar-brand"><Brand /></div>
        {/* 1.5: grouped nav, Claude-app style — muted uppercase group headers over
            icon+label rows. Same destinations/handlers as before (Board/Done/Failed/
            Stats all still call setPage); this is visual grouping only. Done/Failed
            move up from the old outlined outcome-pill bar into the Work group. */}
        <nav className="nh-sidenav" aria-label="Primary">
          <NavGroup title="Work">
            <NavRow
              icon={<IconBoard />}
              label="Board"
              active={page === "board"}
              current={page === "board"}
              onClick={() => setPage("board")}
              badge={needYou > 0 ? needYou : null}
              badgeVariant="alert"
              title={needYou > 0 ? `${needYou} need you` : undefined}
            />
            <NavRow
              icon={<IconDone />}
              label="Done"
              active={page === "done"}
              current={page === "done"}
              onClick={() => setPage("done")}
              badge={doneCount}
              badgeVariant="done"
              title="Tasks that shipped"
            />
            <NavRow
              icon={<IconFailed />}
              label="Failed"
              active={page === "failed"}
              current={page === "failed"}
              onClick={() => setPage("failed")}
              badge={failedCount}
              badgeVariant={failedCount > 0 ? "failed" : undefined}
              title={cancelledCount > 0 ? `${failedCount} failed · ${cancelledCount} cancelled` : "Tasks that failed"}
            />
          </NavGroup>
          <NavGroup title="Insights">
            <NavRow
              icon={<IconStats />}
              label="Stats"
              active={page === "stats"}
              current={page === "stats"}
              onClick={() => setPage("stats")}
            />
          </NavGroup>
        </nav>
        <NightLedger tasks={tasks} />
        <div className="nh-sidebar-foot">
          {workerStatus?.running && workerStatus.inflight > 0 && (
            <div className="nh-status-indicator" title={`${workerStatus.inflight} of ${workerStatus.max_workers} worker slots in use`}>
              <div className="nh-ws-dot live" style={{ background: 'var(--accent)' }} />
              <span className="nh-status-label">Working ({workerStatus.inflight})</span>
            </div>
          )}
          {queueHealth?.stuck && (
            <div className="nh-alarm" role="alert" title={queueHealth.stuck_reason}>
              Queue stuck — {queueHealth.open_tasks} open, nothing finishing
            </div>
          )}
          {!queueHealth?.stuck && queueHealth?.eta_minutes != null && queueHealth.open_tasks > 0 && (
            <div className="nh-status-indicator" title={`${queueHealth.completed_in_window} finished in the last ${queueHealth.window_minutes} min`}>
              <div className="nh-ws-dot live" />
              <span className="nh-status-label">
                Drains in ~{queueHealth.eta_minutes < 60
                  ? `${Math.round(queueHealth.eta_minutes)}m`
                  : `${(queueHealth.eta_minutes / 60).toFixed(1)}h`}
              </span>
            </div>
          )}
          {workerStatus && workerStatus.running === false && (
            <div className="nh-alarm" role="alert"
                 title="The API is up but no worker claims tasks — replies and retries will sit as Working forever until nh start runs with a worker.">
              Worker offline — tasks won't progress
            </div>
          )}
          {workerStatus?.watcher_error && (
            <div className="nh-alarm" role="alert"
                 title={`WakeWatcher failed to start: ${workerStatus.watcher_error}. Parked tasks will not wake until the server restarts cleanly.`}>
              Wake watcher down — parked tasks won't wake
            </div>
          )}
          <div className="nh-status-indicator" title={wsLive ? "Browser is connected to the no_human server" : "Connection lost — reconnecting…"}>
            <div className={`nh-ws-dot${wsLive ? " live" : ""}`} />
            <span className="nh-status-label">{wsLive ? "Connected" : "Reconnecting…"}</span>
          </div>
          <div className="nh-sidebar-row">
            <button
              className="nh-theme-toggle"
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
              title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
            >
              {theme === "dark" ? "☀" : "☾"}
            </button>
            <span className="legion-credit">by eyalgolan</span>
          </div>
          {/* Settings pinned at the very bottom, Claude-app placement — opens the
              overlay dialog from task 1.1 (page routing is unchanged). */}
          <NavRow
            icon={<IconGear />}
            label="Settings"
            active={settingsOpen}
            onClick={() => setSettingsOpen(true)}
            className="nh-settings-row"
          />
        </div>
      </aside>
      <main className="nh-main">
        <h1 className="sr-only">
          {page === "board" ? "Task board"
            : page === "done" ? "Done tasks"
            : page === "failed" ? "Failed tasks"
            : page === "stats" ? "Performance"
            : "Settings"}
        </h1>
        {page === "board" && (
          <div className="nh-main-bar">
            <OverviewStrip tasks={tasks} />
            <button className="btn btn-new-task" onClick={() => setShowNewTask(true)}>+ New Task</button>
          </div>
        )}
        {page === "board" && <Board tasks={tasks} pendingOpenId={pendingOpenId} onPendingOpenHandled={() => setPendingOpenId(null)} />}
        {page === "done" && <Outcomes tasks={tasks} lane="done" />}
        {page === "failed" && <Outcomes tasks={tasks} lane="failed" />}
        {page === "stats" && <Stats tasks={tasks} />}
      </main>
      {showNewTask && (
        <NewTaskModal
          onClose={() => setShowNewTask(false)}
          onCreated={() => fetchTasks().then((ts) => dispatch({ type: "set", tasks: ts }))}
        />
      )}
      {settingsOpen && <SettingsOverlay onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
