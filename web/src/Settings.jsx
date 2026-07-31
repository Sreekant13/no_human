import { useEffect, useState, useCallback, useRef } from "react";
import keepFocusInDialog from "./keepFocusInDialog.js";
import PathInput from "./PathInput.jsx";
import {
  addRule, addSkill, confirmLearning, fetchLearnings,
  fetchRules, fetchSkills, rejectLearning, removeRule, removeSkill,
  fetchProjects, createProject, updateProject, deleteProject,
  fetchProfiles, detectRepos, onboardRepo,
  fetchAuthStatus, setAuthToken,
} from "./api.js";
import { capName, PROFILE_CAP, TOKENVAR_CAP } from "./capName.js";
import { authPanelView } from "./authPanelView.js";
import { groupLearningsByProject } from "./learningGroups.js";
import { useEscapeKey } from "./useEscapeKey.js";
import { pluralize } from "./pluralize.js";
import { updateNotice } from "./updateNotice.js";
import IntegrationsPanel from "./Integrations.jsx";

const SECTIONS = [
  { key: "projects",  label: "Projects" },
  { key: "rules",     label: "Rules" },
  { key: "skills",    label: "Skills" },
  { key: "learnings", label: "Learnings" },
  { key: "integrations", label: "Integrations" },
  { key: "account",   label: "Account" },
  { key: "updates",   label: "Updates" },
];

// The Updates panel. All of the decision-making lives in updateNotice.js so it
// can be tested without a renderer; this component only renders the result and
// forwards clicks to the desktop shell over the preload bridge.
//
// In a plain browser there is no shell, so there are no actions — the panel
// says so rather than showing a button that cannot work.
function UpdatesPanel() {
  const [update, setUpdate] = useState(null);
  const [busy, setBusy] = useState(false);
  const desktop = typeof window !== "undefined" ? window.nhDesktop : undefined;
  const inShell = Boolean(desktop?.shell);

  useEffect(() => desktop?.onUpdate?.((payload) => setUpdate(payload)), [desktop]);

  const view = updateNotice({ inShell, current: desktop?.version, update });

  const run = useCallback(async (fn) => {
    if (!fn) return;
    setBusy(true);
    try {
      const result = await fn();
      if (result) setUpdate(result);
    } catch (err) {
      setUpdate({ mode: "failed", error: String(err?.message ?? err) });
    } finally {
      setBusy(false);
    }
  }, []);

  const label = {
    check: "Check for updates",
    download: "Download now",
    install: "Restart and install",
    later: "Later",
    "download-page": "Open downloads",
  };

  const onAction = (action) => {
    if (action === "check") return run(() => desktop?.checkForUpdates?.());
    if (action === "download") return run(() => desktop?.downloadUpdate?.());
    if (action === "install") return run(() => desktop?.installUpdate?.());
    if (action === "later") return run(() => desktop?.deferUpdate?.(update?.latest));
    if (action === "download-page") {
      window.open("https://github.com/eyalgolan/no_human/releases", "_blank",
        "noopener,noreferrer");
      return undefined;
    }
    return undefined;
  };

  return (
    <div className="memory-panel">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Updates</span></h3>
      </div>
      <div className={`nh-alarm update-notice update-${view.tone}`}
           role={view.tone === "error" ? "alert" : "status"}>
        <strong>{view.title}</strong>
        <div className="update-detail">{view.detail}</div>
      </div>
      {view.actions.length > 0 && (
        <div className="update-actions">
          {view.actions.map((action) => (
            <button key={action} type="button" disabled={busy}
                    className={action === "later" ? "btn" : "btn btn-approve"}
                    onClick={() => onAction(action)}>
              {label[action] ?? action}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// The Account panel: which OAuth profile pays, whether the running server has
// drifted from the configured one, and a WRITE-ONLY editor for the token. The
// API never returns a token, so there is nothing to reveal and no masked echo —
// the field is cleared the instant it is submitted and never logged.
function AuthPanel() {
  const [status, setStatus] = useState(undefined); // undefined = loading, null = unavailable
  const [profile, setProfile] = useState("");
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetchAuthStatus().then((s) => {
      setStatus(s);
      if (s?.configured_profile) setProfile((p) => p || s.configured_profile);
    });
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!token.trim() || saving) return;
    setSaving(true); setError(null); setSaved(false);
    try {
      const next = await setAuthToken(profile, token);
      setToken("");           // write-only — never keep the token after submit
      setStatus(next);
      setSaved(true);
    } catch (err) {
      setError(err.message);  // the backend's 422 detail, rendered verbatim
    } finally {
      setSaving(false);
    }
  }

  if (status === undefined) return <div className="settings-empty">Loading…</div>;
  if (status === null) {
    return (
      <div className="memory-panel">
        <div className="settings-empty">
          Account status is unavailable — this server build does not expose the
          auth endpoints yet.
        </div>
      </div>
    );
  }

  const profiles = status.profiles || [];
  const view = authPanelView(status);
  return (
    <div className="memory-panel auth-panel">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Account</span></h3>
      </div>

      {view.showMeteredAlarm && (
        <div className="nh-alarm auth-alarm" role="alert">
          An <code>ANTHROPIC_API_KEY</code> is set in no_human&apos;s
          environment while the configured mode is subscription — startup
          scrubs the key so a run bills exactly one path. Unset it where the
          server runs, or switch billing to the key with{" "}
          <code>llm.auth_mode: api_key</code>.
        </div>
      )}
      {view.mode === "subscription" && view.showRestartBanner && (
        <div className="nh-alarm auth-alarm" role="alert">
          Restart required — the running server is still billing
          {" "}&ldquo;{capName(status.active_profile, PROFILE_CAP)}&rdquo;, but
          {" "}&ldquo;{capName(status.configured_profile, PROFILE_CAP)}&rdquo; is now configured.
          Restart no_human to switch.
        </div>
      )}
      {view.mode === "api_key" && view.showRestartBanner && (
        <div className="nh-alarm auth-alarm" role="alert">
          Restart required to switch billing to your API key.
        </div>
      )}

      {view.mode === "api_key" && (
        <dl className="auth-status">
          <div><dt>Billing path</dt><dd>Using your personal Anthropic API key</dd></div>
          <div><dt>Key source</dt><dd><code>~/.no_human/.env</code> (chmod 600)</dd></div>
          <div><dt>API key set</dt><dd>{view.apiKeyPresent ? "yes" : "no"}</dd></div>
        </dl>
      )}

      {view.showOAuthForm && (
        <>
          <dl className="auth-status">
            <div><dt>Configured profile</dt><dd>{capName(status.configured_profile, PROFILE_CAP)}</dd></div>
            <div><dt>Active (billing) profile</dt><dd>{capName(status.active_profile, PROFILE_CAP)}</dd></div>
            <div><dt>Token variable</dt><dd><code>{capName(status.token_var, TOKENVAR_CAP)}</code></dd></div>
            <div><dt>Token set</dt><dd>{status.token_present ? "yes" : "no"}</dd></div>
          </dl>

          <form className="auth-form" onSubmit={handleSubmit} autoComplete="off">
            <label className="auth-label">Profile
              <select className="new-task-select" value={profile} aria-label="Profile"
                      onChange={(e) => setProfile(e.target.value)}>
                {profiles.map((p) => <option key={p.name} value={p.name}>{capName(p.name, PROFILE_CAP)}</option>)}
              </select>
            </label>
            <label className="auth-label">OAuth token
              <input className="new-task-input" type="password" autoComplete="off"
                     spellCheck={false} value={token} aria-label="OAuth token"
                     placeholder="CLAUDE_CODE_OAUTH_TOKEN (subscription or enterprise)"
                     onChange={(e) => { setToken(e.target.value); setSaved(false); setError(null); }} />
            </label>
            <p className="auth-hint">
              A subscription or enterprise OAuth token — not an API key. Stored
              write-only and never shown again.
            </p>
            {error && <div className="settings-error" role="alert">{error}</div>}
            {saved && <div className="auth-saved" role="status">Saved.</div>}
            <button className="btn btn-approve" type="submit" disabled={!token.trim() || saving}>
              {saving ? "Saving…" : "Save token"}
            </button>
          </form>
        </>
      )}
    </div>
  );
}

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

  // Move focus into the overlay when it OPENS — mount-only, for the same reason
  // SlideOver's is: App renders <SettingsOverlay onClose={() => ...} /> with a new
  // arrow every render, so sharing this call with the [onClose] effect below made
  // every background re-render (the 10s worker-status poll) steal focus out of the
  // field the operator was typing into — a token or repo path — and park it on the
  // close button.
  useEffect(() => { closeRef.current?.focus(); }, []);

  // Self-heal ONLY when the focused control was REMOVED — see SlideOver.jsx for
  // why "focus is on <body>" is the wrong question: the operator clicking a
  // heading inside this overlay lands focus on <body> legitimately, and healing
  // from there is the focus theft this whole change exists to remove.
  useEffect(() => {
    const el = dialogRef.current;
    if (!el) return undefined;
    // See SlideOver.jsx: `focusin` fires reliably, the blur on a REMOVED node
    // does not, and a DOM mutation is the only trigger that means "something
    // disappeared" rather than "the component re-rendered".
    //
    // Unlike the drawer, THIS panel contains its nested modals (add rule / add
    // skill / new project) as descendants — measured: 45 nodes under the panel
    // against 167 under its parent. So the scope is the panel itself. An earlier
    // version copied the drawer's comment and its `el.parentNode` verbatim, which
    // watched the whole app shell and matched `[data-nested-modal]`
    // DOCUMENT-WIDE — including the drawer's own overlays and the composer,
    // none of which belong to Settings.
    const scope = el;
    const ours = (n) => Boolean(n && el.contains(n));
    let last = null;
    const onFocusIn = (e) => { if (ours(e.target)) last = e.target; };
    scope.addEventListener("focusin", onFocusIn);
    const obs = new MutationObserver(() => {
      if (!last || document.contains(last)) return;
      // Whatever we remembered is gone. Forget it FIRST, whichever way this
      // goes: leaving it set kept the heal armed against a detached node for the
      // rest of the dialog's life, so an unrelated mutation minutes later stole
      // focus — and retained the detached subtree too.
      last = null;
      const active = document.activeElement;
      if (active && active !== document.body && !el.contains(active)) return;
      closeRef.current?.focus();
    });
    obs.observe(scope, { childList: true, subtree: true });
    return () => { scope.removeEventListener("focusin", onFocusIn); obs.disconnect(); };
  }, []);

  useEffect(() => {
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
      // `first`/`last` must be elements that can ACTUALLY take focus. Two ways this trap
      // has leaked: a focusable type missing from the selector (`summary` — the learnings
      // group headers), and a listed element that cannot be focused, so activeElement can
      // never equal it and the wrap branch never fires (a collapsed <details>'s buttons,
      // or a disabled field). Hence both the wider selector and the visibility filter.
      const focusable = Array.from(
        el.querySelectorAll(
          'button, [href], input, select, summary, textarea, [tabindex]:not([tabindex="-1"])',
        )
      ).filter((n) => {
        if (n.disabled || n.getClientRects().length === 0) return false;
        // Content of a COLLAPSED <details> cannot take focus, but this app's CSS still
        // lays it out — it keeps its client rects and computes contentVisibility:visible,
        // so no style-based test sees it. Ask the <details> instead. Without this, the
        // learnings cards of a collapsed group become `last`, activeElement can never
        // equal them, the wrap never fires, and Tab leaves the modal.
        const d = n.closest("details");
        return !(d && !d.open && n.tagName !== "SUMMARY");
      });
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
      {/* No backdrop close: a stray click here used to discard the whole
          overlay AND any nested modal under it, with everything typed in them. */}
      <div className="settings-overlay-backdrop" onMouseDown={keepFocusInDialog} />
      <div
        className="settings-overlay"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-overlay-title"
        ref={dialogRef}
        // The backdrop above is a SIBLING of this panel, so it never sees a
        // mousedown inside the panel — and most of the panel is headings,
        // labels and padding. Without this, clicking any of them while typing
        // a token or a repo path strands the caret and drops every keystroke.
        onMouseDown={keepFocusInDialog}
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
            {section === "account"     && <AuthPanel />}
            {section === "updates"     && <UpdatesPanel />}
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
    <div className="sendback-overlay" data-nested-modal
         onMouseDown={keepFocusInDialog}>
      <div className="new-task-modal">
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
        {/* Which of the two is showing was carried only by the `active` class, i.e. by
            colour — so a screen reader announced two identical "Pending" / "Active"
            buttons with no way to tell which list was on screen. aria-pressed is the
            toggle-button state; the nav above uses aria-current because it is navigation,
            these switch a view in place. */}
        <div className="learnings-toggle">
          <button
            className={`settings-tab sm${view === "pending" ? " active" : ""}`}
            aria-pressed={view === "pending"}
            onClick={() => setView("pending")}
          >
            Pending
          </button>
          <button
            className={`settings-tab sm${view === "active" ? " active" : ""}`}
            aria-pressed={view === "active"}
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
              <PathInput className="new-task-input" style={{ flex: 1 }} autoFocus listId="settings-addrepo-pathlist" value={newRepoPath} onChange={setNewRepoPath} placeholder="Repo path, e.g. ~/git/my-repo" />
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
            <select className="new-task-select" style={{ flex: 'none', width: 80 }} aria-label="Test runner" value={newLayer.runner}
              onChange={(e) => setNewLayer({ ...newLayer, runner: e.target.value })}>
              {RUNNER_OPTIONS.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
            <select className="new-task-select" style={{ flex: 'none', width: 120 }} aria-label="Gating" value={newLayer.gating}
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
                <select className="new-task-select" style={{ flex: 'none', width: 100 }} aria-label="CI backend" value={newLayer.ciBackend}
                  onChange={(e) => setNewLayer({ ...newLayer, ciBackend: e.target.value })}>
                  {CI_BACKEND_OPTIONS.map(b => <option key={b} value={b}>{b}</option>)}
                </select>
                <input className="new-task-input" style={{ flex: 1, marginBottom: 0 }}
                  placeholder={newLayer.ciBackend === "jenkins" ? "Jenkins job path" : "GitLab project (e.g. group/my-service)"}
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
  // Escape was DEAD here while its sibling AddMemoryModal had it: this modal
  // carries `data-nested-modal`, which makes the overlay's own Escape handler
  // stand down for it, and nothing took over. With backdrop-close removed,
  // Cancel was the only way out — while the code and the composer's spec both
  // state "Escape and Cancel stay as the deliberate exits".
  useEscapeKey(onClose, !busy);
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
    <div className="sendback-overlay" data-nested-modal
         onMouseDown={keepFocusInDialog}>
      <div className="new-task-modal" style={{ maxWidth: 520 }}>
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
              <PathInput className="new-task-input" style={{ flex: 1 }} autoFocus listId="settings-scanroot-pathlist" value={scanRoot} onChange={setScanRoot} placeholder="Scan root, e.g. ~/git" />
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

