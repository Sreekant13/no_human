import { useEffect, useReducer, useRef, useState } from "react";
import { connectWS, createTask, fetchTasks, fetchProjects, fetchWorkerStatus, fetchOnboardingStatus, grillStep, grillStepSSE } from "./api.js";
import Board from "./Board.jsx";
import Settings from "./Settings.jsx";
import Onboarding from "./Onboarding.jsx";
import { LegionLogo } from "./Logo.jsx";

const NEEDS_YOU_STATUSES = new Set(["awaiting_approval", "awaiting_input", "escalated"]);
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

function fmtAge(seconds) {
  if (seconds < 60) return "<1m";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function OverviewStrip({ tasks }) {
  const needsYou   = tasks.filter(t => NEEDS_YOU_STATUSES.has(t.status));
  const failed     = tasks.filter(t => t.status === "failed").length;
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
          </>
      }
      <span className="ov-sep">·</span>
      <span>{inProgress} working</span>
      {oldestSec !== null && (
        <>
          <span className="ov-sep">·</span>
          <span>oldest waiting: <span className="ov-oldest">{fmtAge(oldestSec)}</span></span>
        </>
      )}
    </div>
  );
}

function tasksReducer(state, action) {
  switch (action.type) {
    case "set":
      return action.tasks;
    case "sync": {
      const map = Object.fromEntries(state.map((t) => [t.id, t]));
      action.tasks.forEach((t) => { map[t.id] = t; });
      return Object.values(map);
    }
    default:
      return state;
  }
}

function Spinner() {
  return <span className="grill-spinner" />;
}

function NewTaskModal({ onClose, onCreated }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [kind, setKind] = useState("feature");
  const [priority, setPriority] = useState("medium");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [projects, setProjects] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [customRepo, setCustomRepo] = useState(false);
  // B2: grill state
  const [grillMode, setGrillMode] = useState(false);
  const [grillQA, setGrillQA] = useState([]);
  const [grillQuestion, setGrillQuestion] = useState(null);
  const [grillAnswer, setGrillAnswer] = useState("");
  const [grillResult, setGrillResult] = useState(null);
  const [grillEvents, setGrillEvents] = useState([]);
  const grillStreamRef = useRef(null);
  useEffect(() => {
    fetchProjects().then((p) => {
      setProjects(p || []);
      if (p && p.length > 0) setSelectedProjectId(p[0].id);
    });
  }, []);

  async function handleSubmit(e) {
    if (e) e.preventDefault();
    if (!title.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await createTask({
        title: grillResult?.title || title.trim(),
        description: grillResult?.description || description.trim() || null,
        repo_path: customRepo ? repoPath.trim() || null : (repoPath || null),
        project_id: !customRepo && selectedProjectId ? selectedProjectId : null,
        kind,
        priority,
        acceptance_criteria: grillResult?.acceptance_criteria || [],
      });
      onCreated();
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function _grillParams(qaOverride) {
    return {
      title: title.trim(), description: description.trim() || null,
      repo_path: customRepo ? repoPath.trim() || null : null,
      project_id: !customRepo && selectedProjectId ? selectedProjectId : null,
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
    );
  }

  function startGrill() {
    if (!title.trim() || busy) return;
    setBusy(true); setError(null); setGrillMode(true);
    setGrillQA([]); setGrillQuestion(null); setGrillResult(null);
    _startGrillSSE(_grillParams([]));
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
          <div style={{ padding: '0.5rem 0' }}>
            <div style={{ fontWeight: 600, marginBottom: '0.3rem' }}>{grillResult.title}</div>
            {grillResult.description && <div style={{ fontSize: '0.85rem', color: 'var(--fg-dim)', marginBottom: '0.5rem' }}>{grillResult.description}</div>}
            <div style={{ fontSize: '0.8rem', fontWeight: 600, marginBottom: '0.2rem' }}>Acceptance Criteria:</div>
            {grillResult.acceptance_criteria.map((ac, i) => <div key={i} style={{ fontSize: '0.8rem', color: 'var(--green)', paddingLeft: '0.5rem' }}>{i + 1}. {ac}</div>)}
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
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '2rem 1rem', gap: '1rem' }}>
            <Spinner />
            <div style={{ color: 'var(--text)', fontSize: '0.9rem' }}>Exploring the codebase...</div>
            {grillEvents.length > 0 && (
              <div style={{ width: '100%', maxHeight: 120, overflowY: 'auto', fontSize: '0.75rem', color: 'var(--fg-dim)', fontFamily: 'var(--font-mono)', borderTop: '1px solid var(--border)', paddingTop: '0.4rem' }}>
                {grillEvents.map((ev, i) => (
                  <div key={i} style={{ padding: '0.1rem 0', opacity: i === grillEvents.length - 1 ? 1 : 0.6 }}>
                    {ev.kind === 'tool_use' ? `⚙ ${ev.text}` : ev.text}
                  </div>
                ))}
              </div>
            )}
            {grillEvents.length === 0 && (
              <div style={{ color: 'var(--text-dim)', fontSize: '0.75rem' }}>This usually takes 15–30 seconds</div>
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
    // While processing the answer, show a loading overlay that blocks all interaction.
    if (busy) {
      return (
        <div className="sendback-overlay">
          <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
            <div className="sendback-label" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span>Intake Grill</span>
              <span style={{ fontSize: '0.7rem', color: 'var(--fg-dim)' }}>Round {grillQuestion.round}/{maxRounds}</span>
            </div>
            <div style={{ height: 3, background: 'var(--border)', borderRadius: 2, marginBottom: '0.5rem' }}>
              <div style={{ height: '100%', width: `${progressPct}%`, background: 'var(--c-building)', borderRadius: 2, transition: 'width 0.3s' }} />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '2rem 1rem', gap: '1rem' }}>
              <Spinner />
              <div style={{ color: 'var(--text)', fontSize: '0.9rem' }}>Processing your answer...</div>
              <div style={{ color: 'var(--text-dim)', fontSize: '0.75rem' }}>Refining the next question based on your input</div>
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
          <div className="sendback-label" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>Intake Grill</span>
            <span style={{ fontSize: '0.7rem', color: 'var(--fg-dim)' }}>Round {grillQuestion.round}/{maxRounds}</span>
          </div>
          <div style={{ height: 3, background: 'var(--border)', borderRadius: 2, marginBottom: '0.5rem' }}>
            <div style={{ height: '100%', width: `${progressPct}%`, background: 'var(--c-building)', borderRadius: 2, transition: 'width 0.3s' }} />
          </div>
          {grillQA.length > 0 && (
            <div style={{ maxHeight: 100, overflowY: 'auto', fontSize: '0.75rem', color: 'var(--fg-dim)', borderBottom: '1px solid var(--border)', paddingBottom: '0.4rem', marginBottom: '0.4rem' }}>
              {grillQA.map((qa, i) => (
                <div key={i} style={{ marginBottom: '0.3rem' }}>
                  <div><strong>Q{i+1}:</strong> {qa.question}</div>
                  <div style={{ paddingLeft: '0.5rem', color: 'var(--green)' }}>→ {qa.answer}</div>
                </div>
              ))}
            </div>
          )}
          <div style={{ fontWeight: 500, margin: '0.5rem 0' }}>{grillQuestion.question}</div>
          {grillQuestion.suggestions.map((s, i) => <button key={i} type="button" className="btn btn-sendback" style={{ display: 'block', width: '100%', textAlign: 'left', marginBottom: '0.3rem', fontSize: '0.8rem' }} onClick={() => setGrillAnswer(s.replace(/^[A-D]:\s*/, ''))}>{s}</button>)}
          <textarea className="sendback-textarea" placeholder="Your answer (or click a suggestion)" value={grillAnswer} onChange={(e) => setGrillAnswer(e.target.value)} rows={2} />
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="button" className="btn btn-sendback" style={{ fontSize: '0.75rem' }}
              onClick={async () => {
                setBusy(true); setError(null);
                try {
                  const step = await grillStep({ title: title.trim(), description: description.trim() || null, repo_path: customRepo ? repoPath.trim() || null : null, project_id: !customRepo && selectedProjectId ? selectedProjectId : null, qa_history: [...grillQA, { question: grillQuestion.question, answer: "(skip — use what you have)" }] });
                  if (step.type === "done") { setGrillResult(step); setGrillQuestion(null); } else { setGrillResult({ title: step.title || title.trim(), description: step.description || description.trim(), acceptance_criteria: step.acceptance_criteria || [] }); setGrillQuestion(null); }
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
    <div className="sendback-overlay" onClick={onClose}>
      <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
        <div className="sendback-label">New Task</div>
        <form onSubmit={(e) => { e.preventDefault(); startGrill(); }} style={busy ? { opacity: 0.6, pointerEvents: 'none' } : {}}>
          <input
            className="new-task-input"
            placeholder="Task title (required)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            autoFocus
          />
          <textarea
            className="sendback-textarea"
            placeholder="Description (optional)"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
          />
          {!customRepo ? (
            <>
              {projects.length > 0 ? (
                <div className="new-task-row">
                  <label style={{ fontSize: '0.75rem', color: 'var(--fg-dim)', whiteSpace: 'nowrap' }}>Project</label>
                  <select
                    className="new-task-select"
                    style={{ flex: 1 }}
                    value={selectedProjectId}
                    onChange={(e) => setSelectedProjectId(e.target.value)}
                  >
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} ({p.repo_paths.length} repo{p.repo_paths.length !== 1 ? 's' : ''})
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="btn btn-sendback"
                    style={{ fontSize: '0.7rem', padding: '0.2rem 0.5rem' }}
                    onClick={() => { setCustomRepo(true); setRepoPath(''); }}
                    title="Use a repo path not in any project"
                  >custom repo</button>
                </div>
              ) : (
                <div style={{ fontSize: '0.8rem', color: 'var(--fg-dim)', padding: '0.3rem 0' }}>
                  No projects yet.
                  <button
                    type="button"
                    className="btn btn-sendback"
                    style={{ fontSize: '0.7rem', padding: '0.2rem 0.5rem', marginLeft: '0.5rem' }}
                    onClick={() => setCustomRepo(true)}
                  >use repo path</button>
                </div>
              )}
              {/* If a project has multiple repos, let user pick which one to target */}
              {selectedProjectId && (() => {
                const proj = projects.find(p => p.id === selectedProjectId);
                if (!proj || proj.repo_paths.length <= 1) return null;
                return (
                  <div className="new-task-row">
                    <label style={{ fontSize: '0.75rem', color: 'var(--fg-dim)', whiteSpace: 'nowrap' }}>Target repo</label>
                    <select
                      className="new-task-select"
                      style={{ flex: 1 }}
                      value={repoPath || proj.primary_repo || proj.repo_paths[0]}
                      onChange={(e) => setRepoPath(e.target.value)}
                    >
                      {proj.repo_paths.map((rp) => (
                        <option key={rp} value={rp}>
                          {rp.split('/').pop()}{rp === proj.primary_repo ? ' (primary)' : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                );
              })()}
            </>
          ) : (
            <div className="new-task-row">
              <input
                className="new-task-input"
                style={{ flex: 1 }}
                placeholder="Repo path, e.g. ~/git/my-project"
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
              />
              {projects.length > 0 && (
                <button
                  type="button"
                  className="btn btn-sendback"
                  style={{ fontSize: '0.7rem', padding: '0.2rem 0.5rem' }}
                  onClick={() => { setCustomRepo(false); setSelectedProjectId(projects[0]?.id || ''); setRepoPath(''); }}
                >back to projects</button>
              )}
            </div>
          )}
          <div className="new-task-row">
            <select className="new-task-select" value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="feature">feature</option>
              <option value="bugfix">bugfix</option>
              <option value="ci_fix">ci_fix</option>
              <option value="test_gap">test_gap</option>
              <option value="investigation">investigation</option>
              <option value="code_review">code_review</option>
            </select>
            <select className="new-task-select" value={priority} onChange={(e) => setPriority(e.target.value)}>
              <option value="high">high</option>
              <option value="medium">medium</option>
              <option value="low">low</option>
            </select>
          </div>
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-approve" disabled={!title.trim() || (!selectedProjectId && !repoPath.trim()) || busy}>
              {busy ? "Exploring repo\u2026" : "Next \u2192"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function App() {
  const [tasks, dispatch] = useReducer(tasksReducer, []);
  const [wsLive, setWsLive] = useState(false);
  const [fetchError, setFetchError] = useState(null);
  const [showNewTask, setShowNewTask] = useState(false);
  const [page, setPage] = useState("board");
  const [workerStatus, setWorkerStatus] = useState(null);
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
    }
    poll();
    const id = setInterval(poll, 10000);
    return () => clearInterval(id);
  }, []);

  // WebSocket
  useEffect(() => {
    function connect() {
      const ws = connectWS((msg) => {
        if (msg.tasks) dispatch({ type: "sync", tasks: msg.tasks });
        if (msg.worker) setWorkerStatus(prev => ({ ...prev, ...msg.worker, running: true }));
      });
      ws.onopen = () => setWsLive(true);
      ws.onclose = () => {
        setWsLive(false);
        setTimeout(connect, 3000);
      };
      wsRef.current = ws;
    }
    connect();
    return () => wsRef.current?.close();
  }, []);

  if (onboarded === null) {
    return (
      <div className="nh-shell">
        <header className="nh-header"><Brand /></header>
        <div className="nh-center">
          <span className="grill-spinner" style={{ width: 28, height: 28 }} />
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

  return (
    <div className="nh-shell">
      <header className="nh-header">
        <Brand />
        <nav className="nh-nav">
          <button className={`nh-nav-btn${page === "board" ? " active" : ""}`} onClick={() => setPage("board")}>Board</button>
          <button className={`nh-nav-btn${page === "settings" ? " active" : ""}`} onClick={() => setPage("settings")}>Settings</button>
        </nav>
        <div className="nh-header-right">
          <span className="legion-credit">Developed by eyalgolan</span>
          {page === "board" && (
            <button className="btn btn-new-task" onClick={() => setShowNewTask(true)}>+ New Task</button>
          )}
          {workerStatus?.running && (
            <div
              className={`nh-ws-dot live`}
              style={{ background: workerStatus.inflight > 0 ? 'var(--accent)' : 'var(--green)' }}
              title={`worker: ${workerStatus.inflight}/${workerStatus.max_workers} tasks`}
            />
          )}
          <div className={`nh-ws-dot${wsLive ? " live" : ""}`} title={wsLive ? "live" : "reconnecting"} />
        </div>
      </header>
      {page === "board" && (
        <>
          <OverviewStrip tasks={tasks} />
          <Board tasks={tasks} />
        </>
      )}
      {page === "settings" && <Settings />}
      {showNewTask && (
        <NewTaskModal
          onClose={() => setShowNewTask(false)}
          onCreated={() => fetchTasks().then((ts) => dispatch({ type: "set", tasks: ts }))}
        />
      )}
    </div>
  );
}
