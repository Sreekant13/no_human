import { useEffect, useState, useCallback, useRef } from "react";
import {
  addRule, addSkill, confirmLearning, fetchLearnings,
  fetchRules, fetchSkills, rejectLearning, removeRule, removeSkill,
  fetchProjects, createProject, updateProject, deleteProject,
  fetchProfiles, detectRepos, onboardRepo, suggestPaths,
} from "./api.js";
import { groupLearningsByProject } from "./learningGroups.js";
import { useEscapeKey } from "./useEscapeKey.js";
import { pluralize } from "./pluralize.js";
import IntegrationsPanel from "./Integrations.jsx";

const SECTIONS = [
  { key: "projects",  label: "Projects" },
  { key: "rules",     label: "Rules" },
  { key: "skills",    label: "Skills" },
  { key: "learnings", label: "Learnings" },
  { key: "integrations", label: "Integrations" },
];

// Settings, modeled on the Claude macOS desktop app: an overlay dialog with a
// left section list and the section's panel content on the right — not a
// routed page. Mirrors the SlideOver focus/Escape pattern (SlideOver.jsx),
// so it behaves like the app's other modal surfaces.
export default function SettingsOverlay({ onClose }) {
  const [section, setSection] = useState("projects");
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const triggerRef = useRef(null);

  // Capture the element that had focus when the overlay opened (the trigger,
  // e.g. the sidenav "Settings" button) and restore focus to it on close —
  // whether that's Escape, the × button, or the backdrop, they all tear down
  // this component the same way, so a single mount/unmount effect covers all
  // three. Guard against the trigger having been unmounted in the meantime.
  useEffect(() => {
    triggerRef.current = document.activeElement;
    return () => {
      const trigger = triggerRef.current;
      if (trigger && trigger !== document.body && document.contains(trigger) && typeof trigger.focus === "function") {
        trigger.focus();
      }
    };
  }, []);

  useEffect(() => {
    closeRef.current?.focus();

    function onKeyDown(e) {
      if (e.key === "Escape") {
        // A modal ABOVE the overlay (add-rule/add-skill/add-project) owns the
        // key — closing both would discard whatever the operator just typed.
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

  const activeLabel = SECTIONS.find((s) => s.key === section)?.label || "Settings";

  return (
    <>
      <div className="settings-overlay-backdrop" onClick={onClose} />
      <div
        className="settings-overlay"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-overlay-title"
        ref={dialogRef}
      >
        <div className="settings-overlay-left">
          <div className="settings-overlay-group-header">Settings</div>
          <nav className="settings-overlay-nav">
            {SECTIONS.map((s) => (
              <button
                key={s.key}
                className={`settings-overlay-navitem${section === s.key ? " active" : ""}`}
                aria-current={section === s.key ? "true" : undefined}
                onClick={() => setSection(s.key)}
              >
                {s.label}
              </button>
            ))}
          </nav>
        </div>
        <div className="settings-overlay-right">
          <div className="settings-overlay-header">
            <h2 className="settings-overlay-title" id="settings-overlay-title">{activeLabel}</h2>
            <button className="settings-overlay-close" onClick={onClose} ref={closeRef} aria-label="Close settings">✕</button>
          </div>
          <div className="settings-overlay-body">
            {section === "projects"  && <ProjectsPanel />}
            {section === "rules"     && <MemoryList kind="rules" fetchFn={fetchRules} addFn={addRule} removeFn={removeRule} />}
            {section === "skills"    && <MemoryList kind="skills" fetchFn={fetchSkills} addFn={addSkill} removeFn={removeSkill} />}
            {section === "learnings" && <LearningsPanel />}
            {section === "integrations" && <IntegrationsPanel />}
          </div>
        </div>
      </div>
    </>
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
        {/* The overlay header (Settings.jsx) already shows the active section
            label — this h3's own title text would be a near-duplicate heading,
            so it's scoped-hidden under .settings-overlay-body (styles.css)
            while staying intact for any non-overlay reuse of this panel. */}
        <h3 className="memory-title">
          <span className="panel-title-text">{kind === "rules" ? "Confirmed Rules" : "Confirmed Skills"}</span>
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
  useEscapeKey(onClose, !busy);

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
    <div className="sendback-overlay" data-nested-modal onClick={onClose}>
      <div className="new-task-modal" onClick={(e) => e.stopPropagation()}>
        <div className="sendback-label">Add {kind.slice(0, -1)}</div>
        <form onSubmit={handleSubmit}>
          <div className="ntm-field">
            <label className="ntm-label">Title</label>
            <input
              className="new-task-input"
              placeholder="Short, descriptive title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              autoFocus
            />
          </div>
          <div className="ntm-field">
            <label className="ntm-label">Content</label>
            <textarea
              className="sendback-textarea"
              placeholder="The full rule or skill description"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={4}
            />
          </div>
          <div className="ntm-field">
            <label className="ntm-label">Tags</label>
            <input
              className="new-task-input"
              placeholder="Comma-separated, e.g. python, testing (optional)"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
            />
          </div>
          {error && <div className="new-task-error">{error}</div>}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-approve" disabled={!title.trim() || !content.trim() || busy}>
              {busy ? "\u2026" : "Add"}
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
        {/* See MemoryList: redundant with the overlay header's own title. */}
        <h3 className="memory-title">
          <span className="panel-title-text">Learning Queue</span>
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
        <div className="learning-groups">
          {groupLearningsByProject(items).map((group) => (
            <details key={group.key} className="learning-group" open>
              <summary className="learning-group-header">
                <span className="learning-group-label">{group.label}</span>
                <span className="memory-count">{group.count}</span>
              </summary>
              <div className="memory-list">
                {group.items.map((item) => (
                  <LearningCard
                    key={item.id}
                    item={item}
                    isPending={view === "pending"}
                    onAction={handleAction}
                  />
                ))}
              </div>
            </details>
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
        {/* See MemoryList: redundant with the overlay header's own title. */}
        <h3 className="memory-title">
          <span className="panel-title-text">Projects</span>
          {!loading && <span className="memory-count">{projects.length}</span>}
        </h3>
        <button className="btn btn-new-task" onClick={() => setShowAdd(true)}>
          + New Project
        </button>
      </div>
      <div className="config-hint">
        A project groups multiple repos into a single unit of work. When creating a task, you pick a project.
      </div>
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
    <div className={`memory-card project-card${expanded ? " expanded" : ""}`}>
      {/* A <div> with an onClick was keyboard-inert, and expanding a project is the ONLY route
          to its repo list and test-plan editor — the whole panel was mouse-only. */}
      <div
        className="memory-card-header"
        style={{ cursor: 'pointer' }}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
      >
        <span className="memory-card-title" style={{ flex: 1, marginBottom: 0 }}>{project.name}</span>
        <span className="memory-card-type">{project.repo_paths.length} {pluralize(project.repo_paths.length, "repo")}</span>
        <span className="project-expand-icon">{expanded ? '\u25BE' : '\u25B8'}</span>
        <button className="memory-card-remove" onClick={(e) => { e.stopPropagation(); onDelete(); }} title="Delete project">{'\u2715'}</button>
      </div>
      {expanded && (
        <div className="project-expanded-body">
          {project.repo_paths.length === 0 ? (
            <div className="ntm-hint">No repos in this project yet.</div>
          ) : (
            <div className="project-repos">
              {project.repo_paths.map((rp) => (
                <div key={rp} className="project-repo-row">
                  <span className="project-repo-name">{rp.split('/').pop()}</span>
                  <span className="project-repo-path">{rp}</span>
                  {rp === project.primary_repo ? (
                    <span className="project-repo-primary">primary</span>
                  ) : (
                    <button className="project-repo-action" onClick={() => handleSetPrimary(rp)} title="Set as primary">{'\u2605'}</button>
                  )}
                  <button className="project-repo-action danger" onClick={() => handleRemoveRepo(rp)} title="Remove repo">{'\u2715'}</button>
                </div>
              ))}
            </div>
          )}
          {error && <div className="new-task-error">{error}</div>}
          {addingRepo ? (
            <div className="project-add-repo">
              <PathInputSettings value={newRepoPath} onChange={setNewRepoPath} placeholder="Repo path, e.g. ~/git/my-repo" />
              <button className="btn btn-approve btn-sm" disabled={!newRepoPath.trim() || profiling} onClick={handleAddRepo}>
                {profiling ? <><span className="grill-spinner" /> Profiling…</> : 'Add'}
              </button>
              <button className="btn btn-sendback btn-sm" onClick={() => { setAddingRepo(false); setNewRepoPath(""); }}>Cancel</button>
            </div>
          ) : (
            <button className="btn btn-sendback btn-sm" style={{ marginTop: '10px' }} onClick={() => setAddingRepo(true)}>+ Add repo</button>
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
const RUNNER_OPTIONS = ["local", "ci"];
const CI_BACKEND_OPTIONS = ["gitlab", "jenkins"];

function TestPlanEditor({ project, onUpdated }) {
  const layers = project.test_layers || [];
  const [adding, setAdding] = useState(false);
  const [newLayer, setNewLayer] = useState({ name: "", command: "", gating: "blocking", repo: "", runner: "local", ciBackend: "gitlab", ciProject: "", ciBranch: "", ciVars: "" });
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
    const { name, command, gating, repo, runner, ciBackend, ciProject, ciBranch, ciVars } = newLayer;
    if (!name.trim()) return;
    if (runner === "local" && !command.trim()) return;
    if (runner === "ci" && !ciProject.trim()) return;
    const layer = {
      name: name.trim(),
      command: command.trim(),
      gating,
      runner,
      timeout: runner === "ci" ? 3600 : 300,
      depends_on: [],
    };
    if (repo.trim()) layer.repo = repo.trim();
    if (runner === "ci") {
      const variables = {};
      ciVars.split("\n").forEach(line => {
        const [k, ...rest] = line.split(":");
        if (k && rest.length) variables[k.trim()] = rest.join(":").trim();
      });
      layer.ci = {
        backend: ciBackend,
        [ciBackend === "jenkins" ? "job" : "project"]: ciProject.trim(),
        ...(ciBranch.trim() ? { branch: ciBranch.trim() } : {}),
        ...(Object.keys(variables).length ? { variables } : {}),
        timeout_minutes: 60,
      };
      if (ciBackend === "jenkins") layer.ci.mode = "human_gated";
    }
    const updated = [...layers, layer];
    setSaving(true); setError(null);
    try {
      await updateProject(project.id, { test_layers: updated });
      setNewLayer({ name: "", command: "", gating: "blocking", repo: "", runner: "local", ciBackend: "gitlab", ciProject: "", ciBranch: "", ciVars: "" });
      setAdding(false);
      onUpdated();
    } catch (e) { setError(e.message); }
    finally { setSaving(false); }
  }

  return (
    <div className="project-expanded-body">
      <div className="memory-header" style={{ marginBottom: '8px' }}>
        <span className="ntm-label" style={{ marginBottom: 0 }}>Test Plan</span>
        {!adding && (
          <button className="btn btn-sendback btn-sm" onClick={() => setAdding(true)}>+ Add layer</button>
        )}
      </div>
      {error && <div className="new-task-error">{error}</div>}
      {layers.length === 0 && !adding && (
        <div className="ntm-hint">
          No test layers configured. The orchestrator will use the profile&apos;s test command.
        </div>
      )}
      {layers.map((l, idx) => (
        <div key={idx} className="project-repo-row">
          <span className="project-repo-name">{l.name}</span>
          {l.runner === "ci" ? (
            <code className="project-repo-path" style={{ color: 'var(--accent)' }}
              title={l.ci ? JSON.stringify(l.ci, null, 2) : ""}>
              ci:{l.ci?.backend || "?"} {"\u2192"} {l.ci?.project || l.ci?.job || "?"}
            </code>
          ) : (
            <code className="project-repo-path">{l.command}</code>
          )}
          <span className={`memory-tag${l.gating !== "blocking" ? " advisory" : ""}`}>
            {l.gating}
          </span>
          {l.repo && (
            <span className="project-repo-path" style={{ flex: 'none' }} title={l.repo}>
              {"\u2197"} {l.repo.split("/").pop()}
            </span>
          )}
          <button className="project-repo-action danger" disabled={saving} onClick={() => handleRemoveLayer(idx)} title="Remove layer">{"\u2715"}</button>
        </div>
      ))}
      {adding && (
        <form onSubmit={handleAddLayer} className="test-layer-form">
          <div className="new-task-row">
            <input className="new-task-input" style={{ flex: 1, marginBottom: 0 }} placeholder="Layer name (e.g. unit, integration, ci_gate-e2e)" value={newLayer.name}
              onChange={(e) => setNewLayer({ ...newLayer, name: e.target.value })} autoFocus />
            <select className="new-task-select" style={{ flex: 'none', width: 80 }} value={newLayer.runner}
              onChange={(e) => setNewLayer({ ...newLayer, runner: e.target.value })}>
              {RUNNER_OPTIONS.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            <select className="new-task-select" style={{ flex: 'none', width: 120 }} value={newLayer.gating}
              onChange={(e) => setNewLayer({ ...newLayer, gating: e.target.value })}>
              {GATING_OPTIONS.map(g => <option key={g} value={g}>{g}</option>)}
            </select>
          </div>
          {newLayer.runner === "local" ? (
            <>
              <input className="new-task-input" style={{ marginBottom: '6px' }} placeholder="Test command (e.g. uv run pytest -q)" value={newLayer.command}
                onChange={(e) => setNewLayer({ ...newLayer, command: e.target.value })} />
              <input className="new-task-input" style={{ marginBottom: '6px' }} placeholder="Cross-repo path (optional, e.g. ~/git/tests-repo)" value={newLayer.repo}
                onChange={(e) => setNewLayer({ ...newLayer, repo: e.target.value })} />
            </>
          ) : (
            <>
              <div className="new-task-row" style={{ marginTop: '6px' }}>
                <select className="new-task-select" style={{ flex: 'none', width: 100 }} value={newLayer.ciBackend}
                  onChange={(e) => setNewLayer({ ...newLayer, ciBackend: e.target.value })}>
                  {CI_BACKEND_OPTIONS.map(b => <option key={b} value={b}>{b}</option>)}
                </select>
                <input className="new-task-input" style={{ flex: 1, marginBottom: 0 }}
                  placeholder={newLayer.ciBackend === "jenkins" ? "Jenkins job path" : "GitLab project (e.g. ci_gate-analytics-export-report-generator)"}
                  value={newLayer.ciProject}
                  onChange={(e) => setNewLayer({ ...newLayer, ciProject: e.target.value })} />
              </div>
              <input className="new-task-input" style={{ marginTop: '6px', marginBottom: '6px' }} placeholder="CI branch (default: main)" value={newLayer.ciBranch}
                onChange={(e) => setNewLayer({ ...newLayer, ciBranch: e.target.value })} />
              <textarea className="new-task-input" rows={3} style={{ fontFamily: 'var(--font-mono)', fontSize: '12px' }}
                placeholder={"Pipeline variables (one per line, KEY:VALUE)\ne.g. METHOD:create\nDESTROY:true"}
                value={newLayer.ciVars}
                onChange={(e) => setNewLayer({ ...newLayer, ciVars: e.target.value })} />
            </>
          )}
          <div className="sendback-actions">
            <button type="button" className="btn btn-sendback btn-sm" onClick={() => setAdding(false)}>Cancel</button>
            <button type="submit" className="btn btn-approve btn-sm" disabled={
              !newLayer.name.trim() || saving ||
              (newLayer.runner === "local" && !newLayer.command.trim()) ||
              (newLayer.runner === "ci" && !newLayer.ciProject.trim())
            }>
              {saving ? "\u2026" : "Add Layer"}
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
    <div className="sendback-overlay" data-nested-modal onClick={onClose}>
      <div className="new-task-modal" style={{ maxWidth: 520 }} onClick={(e) => e.stopPropagation()}>
        <div className="sendback-label">New Project</div>
        <form onSubmit={handleCreate}>
          <div className="ntm-field">
            <label className="ntm-label">Project Name</label>
            <input
              className="new-task-input ntm-title"
              placeholder="e.g. my-service"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </div>
          <hr className="ntm-section-divider" />
          <div className="ntm-field">
            <label className="ntm-label">Scan for repos</label>
            <div className="new-task-row">
              <PathInputSettings value={scanRoot} onChange={setScanRoot} placeholder="Scan root, e.g. ~/git" />
              <button type="button" className="btn btn-sendback btn-sm" disabled={scanning} onClick={handleScan}>
                {scanning ? <><span className="grill-spinner" /> Scanning…</> : 'Scan'}
              </button>
            </div>
          </div>
          {detected.length > 0 && (
            <div className="ob-repolist" style={{ maxHeight: '180px', margin: '8px 0' }}>
              {detected.map((r) => (
                <label key={r.path} className={`ob-repo${selectedRepos.has(r.path) ? " sel" : ""}`} style={{ padding: '4px 8px' }}>
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
              {busy ? <><span className="grill-spinner" /> Creating…</> : 'Create Project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

