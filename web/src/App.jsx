import { useEffect, useReducer, useRef, useState } from "react";
import { connectWS, createTask, fetchTasks, fetchProfiles, fetchWorkerStatus, fetchOnboardingStatus } from "./api.js";
import Board from "./Board.jsx";
import Settings from "./Settings.jsx";
import Onboarding from "./Onboarding.jsx";

const NEEDS_YOU_STATUSES = new Set(["awaiting_approval", "awaiting_input", "escalated"]);
const PROGRESS_STATUSES  = new Set(["pending", "context", "planning", "implementing", "reviewing", "testing"]);

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

function NewTaskModal({ onClose, onCreated }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [kind, setKind] = useState("feature");
  const [priority, setPriority] = useState("medium");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [profiles, setProfiles] = useState([]);
  const [customRepo, setCustomRepo] = useState(false);

  useEffect(() => {
    fetchProfiles().then((p) => {
      setProfiles(p || []);
      if (p && p.length > 0) setRepoPath(p[0].repo_path);
    });
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!title.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await createTask({
        title: title.trim(),
        description: description.trim() || null,
        repo_path: repoPath.trim() || null,
        kind,
        priority,
        acceptance_criteria: [],
      });
      onCreated();
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sendback-overlay" onClick={onClose}>
      <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
        <div className="sendback-label">New Task</div>
        <form onSubmit={handleSubmit}>
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
          {profiles.length > 0 && !customRepo ? (
            <div className="new-task-row">
              <select
                className="new-task-select"
                style={{ flex: 1 }}
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
              >
                {profiles.map((p) => (
                  <option key={p.repo_path} value={p.repo_path}>
                    {p.name}{p.confirmed ? '' : ' (unconfirmed)'}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn btn-sendback"
                style={{ fontSize: '0.75rem', padding: '0.3rem 0.6rem' }}
                onClick={() => { setCustomRepo(true); setRepoPath(''); }}
              >other</button>
            </div>
          ) : (
            <div className="new-task-row">
              <input
                className="new-task-input"
                style={{ flex: 1 }}
                placeholder="Repo path, e.g. ~/git/my-project"
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
              />
              {profiles.length > 0 && (
                <button
                  type="button"
                  className="btn btn-sendback"
                  style={{ fontSize: '0.75rem', padding: '0.3rem 0.6rem' }}
                  onClick={() => { setCustomRepo(false); setRepoPath(profiles[0]?.repo_path || ''); }}
                >list</button>
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
            <button type="submit" className="btn btn-approve" disabled={!title.trim() || busy}>
              {busy ? "…" : "Create"}
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

  if (onboarded === false) {
    return <Onboarding onComplete={() => setOnboarded(true)} />;
  }

  if (fetchError) {
    return (
      <div className="nh-shell">
        <header className="nh-header">
          <div className="nh-logo">no_human<span> · operator terminal</span></div>
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
        <div className="nh-logo">
          no_human<span> · operator terminal</span>
        </div>
        <nav className="nh-nav">
          <button className={`nh-nav-btn${page === "board" ? " active" : ""}`} onClick={() => setPage("board")}>Board</button>
          <button className={`nh-nav-btn${page === "settings" ? " active" : ""}`} onClick={() => setPage("settings")}>Settings</button>
        </nav>
        <div className="nh-header-right">
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
