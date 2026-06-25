import { useEffect, useState, useCallback } from "react";
import {
  addRule, addSkill, confirmLearning, fetchConfig, fetchLearnings,
  fetchRules, fetchSkills, rejectLearning, removeRule, removeSkill,
} from "./api.js";

const TABS = [
  { key: "rules",     label: "Rules" },
  { key: "skills",    label: "Skills" },
  { key: "learnings", label: "Learnings" },
  { key: "config",    label: "Config" },
];

export default function Settings() {
  const [tab, setTab] = useState("rules");

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
        {tab === "rules"     && <MemoryList kind="rules" fetchFn={fetchRules} addFn={addRule} removeFn={removeRule} />}
        {tab === "skills"    && <MemoryList kind="skills" fetchFn={fetchSkills} addFn={addSkill} removeFn={removeSkill} />}
        {tab === "learnings" && <LearningsPanel />}
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
