import { useEffect, useState, useCallback } from "react";
import {
  addRule, addSkill, confirmLearning, fetchConfig, fetchLearnings,
  fetchRules, fetchSkills, rejectLearning, removeRule, removeSkill,
  fetchProjects, createProject, updateProject, deleteProject,
  fetchProfiles, detectRepos, onboardRepo, suggestPaths,
  fetchTrackerSettings, updateTrackerBoards,
} from "./api.js";

const TABS = [
  { key: "projects",  label: "Projects" },
  { key: "rules",     label: "Rules" },
  { key: "skills",    label: "Skills" },
  { key: "learnings", label: "Learnings" },
  { key: "tracker",       label: "TRACKER Boards" },
  { key: "config",    label: "Config" },
];

export default function Settings() {
  const [tab, setTab] = useState("projects");

  return (
    <div className="settings-page">
      <div className="settings-tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`settings-tab${tab === t.key ? " active" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="settings-body">
        {tab === "projects"  && <ProjectsPanel />}
        {tab === "rules"     && <MemoryList kind="rules" fetchFn={fetchRules} addFn={addRule} removeFn={removeRule} />}
        {tab === "skills"    && <MemoryList kind="skills" fetchFn={fetchSkills} addFn={addSkill} removeFn={removeSkill} />}
        {tab === "learnings" && <LearningsPanel />}
        {tab === "tracker"       && <TrackerBoardsPanel />}
        {tab === "config"    && <ConfigPanel />}
      </div>
    </div>
  );
}

/* ── Rules / Skills list ─────────────────────────────────────────────────── */

function MemoryList({ kind, fetchFn, addFn, removeFn }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    fetchFn()
      .then((data) => { setItems(data); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [fetchFn]);

  useEffect(() => { load(); }, [load]);

  async function handleRemove(id) {
    if (!window.confirm(`Remove this ${kind.slice(0, -1)}?`)) return;
    try {
      await removeFn(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="memory-panel">
      <div className="memory-header">
        <h3 className="memory-title">
          {kind === "rules" ? "Confirmed Rules" : "Confirmed Skills"}
          {!loading && <span className="memory-count">{items.length}</span>}
        </h3>
        <button className="btn btn-new-task" onClick={() => setShowAdd(true)}>
          + Add {kind.slice(0, -1)}
        </button>
      </div>
      {error && <div className="settings-error">{error}</div>}
      {loading ? (
        <div className="settings-empty">Loading…</div>
      ) : items.length === 0 ? (
        <div className="settings-empty">
          No confirmed {kind} yet. Add one to inject it into every agent prompt.
        </div>
      ) : (
        <div className="memory-list">
          {items.map((item) => (
            <MemoryCard key={item.id} item={item} onRemove={handleRemove} />
          ))}
        </div>
      )}
      {showAdd && (
        <AddMemoryModal
          kind={kind}
          onClose={() => setShowAdd(false)}
          onSaved={() => { setShowAdd(false); load(); }}
          addFn={addFn}
        />
      )}
    </div>
  );
}

function MemoryCard({ item, onRemove }) {
  const tags = (() => {
    try {
      const t = typeof item.tags === "string" ? JSON.parse(item.tags) : item.tags;
      return Array.isArray(t) ? t : [];
    } catch { return []; }
  })();

  return (
    <div className="memory-card">
      <div className="memory-card-header">
        <span className="memory-card-id">{(item.id || "").slice(0, 8)}</span>
        <span className="memory-card-type">{item.type}</span>
        <button className="memory-card-remove" onClick={() => onRemove(item.id)} title="Remove">✕</button>
      </div>
      <div className="memory-card-title">{item.title}</div>
      {item.content && <div className="memory-card-content">{item.content}</div>}
      {tags.length > 0 && (
        <div className="memory-card-tags">
          {tags.map((t, i) => <span key={i} className="memory-tag">{t}</span>)}
        </div>
      )}
    </div>
  );
}

function AddMemoryModal({ kind, onClose, onSaved, addFn }) {
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!title.trim() || !content.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await addFn({
        title: title.trim(),
        content: content.trim(),
        tags: tags.split(",").map(t => t.trim()).filter(Boolean),
      });
      onSaved();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sendback-overlay" onClick={onClose}>
      <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
        <div className="sendback-label">Add {kind.slice(0, -1)}</div>
        <form onSubmit={handleSubmit}>
          <input
            className="new-task-input"
            placeholder="Title (required)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            autoFocus
          />
          <textarea
            className="sendback-textarea"
            placeholder="Content / description (required)"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={4}
          />
          <input
            className="new-task-input"
            placeholder="Tags (comma-separated, optional)"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
          />
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-approve" disabled={!title.trim() || !content.trim() || busy}>
              {busy ? "…" : "Add"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ── Learnings queue ─────────────────────────────────────────────────────── */

function LearningsPanel() {
  const [pending, setPending] = useState([]);
  const [active, setActive] = useState([]);
  const [view, setView] = useState("pending");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([
      fetchLearnings({ active: false }),
      fetchLearnings({ active: true }),
    ])
      .then(([p, a]) => { setPending(p); setActive(a); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleAction(id, action) {
    try {
      if (action === "confirm") await confirmLearning(id);
      else await rejectLearning(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  const items = view === "pending" ? pending : active;

  return (
    <div className="memory-panel">
      <div className="memory-header">
        <h3 className="memory-title">
          Learning Queue
          {!loading && (
            <span className="memory-count">
              {pending.length} pending · {active.length} active
            </span>
          )}
        </h3>
        <div className="learnings-toggle">
          <button
            className={`settings-tab sm${view === "pending" ? " active" : ""}`}
            onClick={() => setView("pending")}
          >
            Pending
          </button>
          <button
            className={`settings-tab sm${view === "active" ? " active" : ""}`}
            onClick={() => setView("active")}
          >
            Active
          </button>
        </div>
      </div>
      {error && <div className="settings-error">{error}</div>}
      {loading ? (
        <div className="settings-empty">Loading…</div>
      ) : items.length === 0 ? (
        <div className="settings-empty">
          {view === "pending"
            ? "No pending proposals. The agent hasn't proposed new learnings yet."
            : "No active learnings. Confirm pending proposals to activate them."}
        </div>
      ) : (
        <div className="memory-list">
          {items.map((item) => (
            <LearningCard
              key={item.id}
              item={item}
              isPending={view === "pending"}
              onAction={handleAction}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function LearningCard({ item, isPending, onAction }) {
  return (
    <div className="memory-card learning-card">
      <div className="memory-card-header">
        <span className="memory-card-id">{(item.id || "").slice(0, 8)}</span>
        <span className="memory-card-type">{item.type}</span>
      </div>
      <div className="memory-card-title">{item.title}</div>
      {item.content && <div className="memory-card-content">{item.content}</div>}
      {isPending && (
        <div className="learning-actions">
          <button
            className="btn btn-approve btn-sm"
            onClick={() => onAction(item.id, "confirm")}
          >
            Confirm
          </button>
          <button
            className="btn btn-cancel btn-sm"
            onClick={() => onAction(item.id, "reject")}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

/* ── Projects panel ──────────────────────────────────────────────────────── */

function ProjectsPanel() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showAdd, setShowAdd] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    fetchProjects()
      .then((data) => { setProjects(data || []); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  async function handleDelete(id, name) {
    if (!window.confirm(`Delete project "${name}"? This does not delete the repos.`)) return;
    try {
      await deleteProject(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="memory-panel">
      <div className="memory-header">
        <h3 className="memory-title">
          Projects
          {!loading && <span className="memory-count">{projects.length}</span>}
        </h3>
        <button className="btn btn-new-task" onClick={() => setShowAdd(true)}>
          + New Project
        </button>
      </div>
      <p className="settings-hint" style={{ margin: '0 0 0.75rem', fontSize: '0.8rem', color: 'var(--fg-dim)' }}>
        A project groups multiple repos into a single unit of work. When creating a task, you pick a project.
      </p>
      {error && <div className="settings-error">{error}</div>}
      {loading ? (
        <div className="settings-loading">
          <span className="grill-spinner" />
          <span>Loading projects…</span>
        </div>
      ) : projects.length === 0 ? (
        <div className="settings-empty">
          No projects yet. Create one to group your repos.
        </div>
      ) : (
        <div className="memory-list">
          {projects.map((proj) => (
            <ProjectCard
              key={proj.id}
              project={proj}
              onDelete={() => handleDelete(proj.id, proj.name)}
              onUpdated={load}
            />
          ))}
        </div>
      )}
      {showAdd && (
        <AddProjectModal
          onClose={() => setShowAdd(false)}
          onSaved={() => { setShowAdd(false); load(); }}
        />
      )}
    </div>
  );
}

function ProjectCard({ project, onDelete, onUpdated }) {
  const [expanded, setExpanded] = useState(false);
  const [addingRepo, setAddingRepo] = useState(false);
  const [newRepoPath, setNewRepoPath] = useState("");
  const [profiling, setProfiling] = useState(false);
  const [error, setError] = useState(null);

  async function handleAddRepo() {
    const path = newRepoPath.trim();
    if (!path) return;
    setProfiling(true);
    setError(null);
    try {
      await onboardRepo(path);
      const updated = [...project.repo_paths, path];
      await updateProject(project.id, { repo_paths: updated });
      setNewRepoPath("");
      setAddingRepo(false);
      onUpdated();
    } catch (e) {
      setError(e.message);
    } finally {
      setProfiling(false);
    }
  }

  async function handleRemoveRepo(repoPath) {
    if (!window.confirm(`Remove "${repoPath.split('/').pop()}" from this project?`)) return;
    try {
      const updated = project.repo_paths.filter(r => r !== repoPath);
      await updateProject(project.id, { repo_paths: updated });
      onUpdated();
    } catch (e) {
      setError(e.message);
    }
  }

  async function handleSetPrimary(repoPath) {
    try {
      await updateProject(project.id, { primary_repo: repoPath });
      onUpdated();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="memory-card project-card">
      <div className="memory-card-header" style={{ cursor: 'pointer' }} onClick={() => setExpanded(!expanded)}>
        <span className="memory-card-title" style={{ fontWeight: 600, flex: 1 }}>{project.name}</span>
        <span className="memory-card-type">{project.repo_paths.length} repo{project.repo_paths.length !== 1 ? 's' : ''}</span>
        <span style={{ fontSize: '0.75rem', color: 'var(--fg-dim)', marginLeft: '0.5rem' }}>{expanded ? '▾' : '▸'}</span>
        <button className="memory-card-remove" onClick={(e) => { e.stopPropagation(); onDelete(); }} title="Delete project">✕</button>
      </div>
      {expanded && (
        <div style={{ padding: '0.5rem 0' }}>
          {project.repo_paths.length === 0 ? (
            <div style={{ fontSize: '0.8rem', color: 'var(--fg-dim)', padding: '0.25rem 0' }}>No repos in this project yet.</div>
          ) : (
            <div className="project-repos">
              {project.repo_paths.map((rp) => (
                <div key={rp} className="project-repo-row">
                  <span className="project-repo-name">{rp.split('/').pop()}</span>
                  <span className="project-repo-path">{rp}</span>
                  {rp === project.primary_repo ? (
                    <span className="project-repo-primary">primary</span>
                  ) : (
                    <button className="project-repo-action" onClick={() => handleSetPrimary(rp)} title="Set as primary">★</button>
                  )}
                  <button className="project-repo-action danger" onClick={() => handleRemoveRepo(rp)} title="Remove repo">✕</button>
                </div>
              ))}
            </div>
          )}
          {error && <div className="new-task-error" style={{ margin: '0.5rem 0' }}>{error}</div>}
          {addingRepo ? (
            <div className="project-add-repo">
              <PathInputSettings value={newRepoPath} onChange={setNewRepoPath} placeholder="Repo path, e.g. ~/git/my-repo" />
              <button className="btn btn-approve btn-sm" disabled={!newRepoPath.trim() || profiling} onClick={handleAddRepo}>
                {profiling ? <><span className="grill-spinner" style={{ width: 14, height: 14 }} /> Profiling…</> : 'Add'}
              </button>
              <button className="btn btn-sendback btn-sm" onClick={() => { setAddingRepo(false); setNewRepoPath(""); }}>Cancel</button>
            </div>
          ) : (
            <button className="btn btn-sendback btn-sm" style={{ marginTop: '0.5rem' }} onClick={() => setAddingRepo(true)}>+ Add repo</button>
          )}
          <TestPlanEditor project={project} onUpdated={onUpdated} />
        </div>
      )}
    </div>
  );
}

function PathInputSettings({ value, onChange, placeholder }) {
  const [opts, setOpts] = useState([]);
  const listId = "settings-pathlist";
  useEffect(() => {
    let live = true;
    const t = setTimeout(async () => {
      const res = await suggestPaths(value);
      if (live) setOpts(res.suggestions || []);
    }, 150);
    return () => { live = false; clearTimeout(t); };
  }, [value]);
  return (
    <>
      <input
        className="new-task-input" list={listId} value={value}
        placeholder={placeholder} spellCheck={false} autoFocus
        onChange={(e) => onChange(e.target.value)}
        style={{ flex: 1 }}
      />
      <datalist id={listId}>
        {opts.map((o) => (
          <option key={o.path} value={o.path}>{o.is_repo ? "git repo" : "folder"}</option>
        ))}
      </datalist>
    </>
  );
}

/* ── Test-plan editor (PR5) ─────────────────────────────────────────────── */

const GATING_OPTIONS = ["blocking", "advisory", "wake_gated"];

function TestPlanEditor({ project, onUpdated }) {
  const layers = project.test_layers || [];
  const [adding, setAdding] = useState(false);
  const [newLayer, setNewLayer] = useState({ name: "", command: "", gating: "blocking", repo: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  async function handleRemoveLayer(idx) {
    const updated = layers.filter((_, i) => i !== idx);
    setSaving(true); setError(null);
    try {
      await updateProject(project.id, { test_layers: updated });
      onUpdated();
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  }

  async function handleAddLayer(e) {
    if (e) e.preventDefault();
    const { name, command, gating, repo } = newLayer;
    if (!name.trim() || !command.trim()) return;
    const layer = {
      name: name.trim(),
      command: command.trim(),
      gating,
      runner: "local",
      timeout: 300,
      depends_on: [],
    };
    if (repo.trim()) layer.repo = repo.trim();
    const updated = [...layers, layer];
    setSaving(true); setError(null);
    try {
      await updateProject(project.id, { test_layers: updated });
      setNewLayer({ name: "", command: "", gating: "blocking", repo: "" });
      setAdding(false);
      onUpdated();
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  }

  return (
    <div style={{ marginTop: "0.75rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.25rem" }}>
        <span style={{ fontWeight: 600, fontSize: "0.85rem" }}>Test Plan</span>
        {!adding && (
          <button className="btn btn-sendback btn-sm" onClick={() => setAdding(true)}>+ Add layer</button>
        )}
      </div>
      {error && <div className="new-task-error" style={{ margin: "0.25rem 0" }}>{error}</div>}
      {layers.length === 0 && !adding && (
        <div style={{ fontSize: "0.8rem", color: "var(--fg-dim)" }}>
          No test layers configured. The orchestrator will use the profile's test command.
        </div>
      )}
      {layers.map((l, idx) => (
        <div key={idx} className="project-repo-row" style={{ fontSize: "0.82rem" }}>
          <span style={{ fontWeight: 600 }}>{l.name}</span>
          <code style={{ fontSize: "0.75rem", color: "var(--fg-dim)", flex: 1, marginLeft: "0.5rem", overflow: "hidden", textOverflow: "ellipsis" }}>
            {l.command}
          </code>
          <span className={`memory-tag ${l.gating === "blocking" ? "" : "advisory"}`} style={{ fontSize: "0.65rem" }}>
            {l.gating}
          </span>
          {l.repo && (
            <span style={{ fontSize: "0.7rem", color: "var(--fg-dim)" }} title={l.repo}>
              ↗ {l.repo.split("/").pop()}
            </span>
          )}
          <button className="project-repo-action danger" disabled={saving} onClick={() => handleRemoveLayer(idx)} title="Remove layer">✕</button>
        </div>
      ))}
      {adding && (
        <form onSubmit={handleAddLayer} style={{ display: "flex", flexDirection: "column", gap: "0.3rem", marginTop: "0.4rem", padding: "0.4rem", border: "1px solid var(--border)", borderRadius: "4px", background: "var(--surface-1)" }}>
          <div style={{ display: "flex", gap: "0.3rem" }}>
            <input className="new-task-input" style={{ flex: 1 }} placeholder="Layer name (e.g. unit, integration)" value={newLayer.name}
              onChange={(e) => setNewLayer({ ...newLayer, name: e.target.value })} autoFocus />
            <select className="new-task-input" style={{ width: 120 }} value={newLayer.gating}
              onChange={(e) => setNewLayer({ ...newLayer, gating: e.target.value })}>
              {GATING_OPTIONS.map(g => <option key={g} value={g}>{g}</option>)}
            </select>
          </div>
          <input className="new-task-input" placeholder="Test command (e.g. uv run pytest -q)" value={newLayer.command}
            onChange={(e) => setNewLayer({ ...newLayer, command: e.target.value })} />
          <input className="new-task-input" placeholder="Cross-repo path (optional, e.g. ~/git/tests-repo)" value={newLayer.repo}
            onChange={(e) => setNewLayer({ ...newLayer, repo: e.target.value })} />
          <div style={{ display: "flex", gap: "0.3rem", justifyContent: "flex-end" }}>
            <button type="button" className="btn btn-sendback btn-sm" onClick={() => setAdding(false)}>Cancel</button>
            <button type="submit" className="btn btn-approve btn-sm" disabled={!newLayer.name.trim() || !newLayer.command.trim() || saving}>
              {saving ? "…" : "Add Layer"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

function AddProjectModal({ onClose, onSaved }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [scanRoot, setScanRoot] = useState("~/git");
  const [detected, setDetected] = useState([]);
  const [selectedRepos, setSelectedRepos] = useState(new Set());
  const [scanning, setScanning] = useState(false);

  async function handleScan() {
    setScanning(true);
    setError(null);
    try {
      const res = await detectRepos(scanRoot);
      setDetected(res.repos || []);
    } catch (e) {
      setError(e.message);
    } finally {
      setScanning(false);
    }
  }

  function toggleRepo(path) {
    setSelectedRepos((s) => {
      const n = new Set(s);
      n.has(path) ? n.delete(path) : n.add(path);
      return n;
    });
  }

  async function handleCreate(e) {
    if (e) e.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const repoPaths = [...selectedRepos];
      for (const rp of repoPaths) {
        try { await onboardRepo(rp); } catch { /* already profiled or best-effort */ }
      }
      await createProject({ name: name.trim(), repo_paths: repoPaths });
      onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sendback-overlay" onClick={onClose}>
      <div className="new-task-modal" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
        <div className="sendback-label">New Project</div>
        <form onSubmit={handleCreate}>
          <input
            className="new-task-input"
            placeholder="Project name (required)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
          <div style={{ fontSize: '0.8rem', color: 'var(--fg-dim)', margin: '0.5rem 0 0.25rem' }}>
            Scan for repos to add:
          </div>
          <div className="new-task-row">
            <PathInputSettings value={scanRoot} onChange={setScanRoot} placeholder="Scan root, e.g. ~/git" />
            <button type="button" className="btn btn-sendback" disabled={scanning} onClick={handleScan}>
              {scanning ? <><span className="grill-spinner" style={{ width: 14, height: 14 }} /> Scanning…</> : 'Scan'}
            </button>
          </div>
          {detected.length > 0 && (
            <div className="ob-repolist" style={{ maxHeight: '180px', margin: '0.5rem 0' }}>
              {detected.map((r) => (
                <label key={r.path} className={`ob-repo${selectedRepos.has(r.path) ? " sel" : ""}`} style={{ padding: '0.2rem 0.4rem' }}>
                  <input type="checkbox" checked={selectedRepos.has(r.path)} onChange={() => toggleRepo(r.path)} />
                  <span className="ob-repo-name">{r.name}</span>
                  {r.ecosystem && <span className="ob-tag">{r.ecosystem}</span>}
                </label>
              ))}
            </div>
          )}
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-approve" disabled={!name.trim() || busy}>
              {busy ? <><span className="grill-spinner" style={{ width: 14, height: 14 }} /> Creating…</> : 'Create Project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

/* ── TRACKER Boards management (Phase 3b) ────────────────────────────────────── */

function TrackerBoardsPanel() {
  const [boards, setBoards] = useState([]);
  const [loading, setLoading] = useState(true);
  const [newBoard, setNewBoard] = useState("");
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchTrackerSettings()
      .then((r) => setBoards(r.boards || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  async function addBoard() {
    const key = newBoard.trim();
    if (!key || boards.includes(key)) return;
    const next = [...boards, key].sort();
    try {
      await updateTrackerBoards(next);
      setBoards(next); setNewBoard(""); setError(null);
    } catch (e) { setError(e.message); }
  }

  async function removeBoard(key) {
    const next = boards.filter((b) => b !== key);
    try {
      await updateTrackerBoards(next);
      setBoards(next); setError(null);
    } catch (e) { setError(e.message); }
  }

  if (loading) return <div className="settings-empty">Loading…</div>;

  return (
    <div>
      <div className="config-hint" style={{ marginBottom: '0.5rem' }}>
        TRACKER boards to poll for tasks. Board keys like <code>SPRINT-42</code> or team assignment groups.
      </div>
      {error && <div className="settings-error" style={{ marginBottom: '0.5rem' }}>{error}</div>}
      {boards.length === 0 ? (
        <div className="settings-empty">No boards configured</div>
      ) : (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
          {boards.map((b) => (
            <li key={b} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0.4rem 0', borderBottom: '1px solid var(--border)' }}>
              <code style={{ fontSize: '0.85rem' }}>{b}</code>
              <button className="btn btn-sendback" style={{ fontSize: '0.7rem', padding: '0.15rem 0.5rem' }}
                onClick={() => removeBoard(b)}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem' }}>
        <input
          style={{ flex: 1, padding: '0.3rem 0.5rem', fontSize: '0.85rem', border: '1px solid var(--border)', borderRadius: 4, background: 'var(--surface-1)', color: 'var(--text)' }}
          placeholder="Board key, e.g. SPRINT-42"
          value={newBoard}
          onChange={(e) => setNewBoard(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && addBoard()}
        />
        <button className="btn btn-approve" style={{ fontSize: '0.8rem', padding: '0.3rem 0.8rem' }}
          disabled={!newBoard.trim()} onClick={addBoard}>Add</button>
      </div>
    </div>
  );
}

/* ── Config viewer ───────────────────────────────────────────────────────── */

function ConfigPanel() {
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchConfig()
      .then((data) => { setConfig(data); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="settings-empty">Loading…</div>;
  if (error) return <div className="settings-error">{error}</div>;

  return (
    <div className="config-panel">
      <div className="config-hint">
        Read-only view of <code>~/.no_human/config.yaml</code>. Edit via CLI: <code>nh config edit</code>
      </div>
      <pre className="config-json">{JSON.stringify(config, null, 2)}</pre>
    </div>
  );
}
