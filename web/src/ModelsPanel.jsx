import { useEffect, useState, useCallback } from "react";
import { fetchModels, saveModels } from "./api.js";
import { modelsPanelView, pendingBody, resetBody, applyError } from "./modelsPanelView.js";

// Settings → Models (model picker part 3 of 3). One row per role (coder,
// reviewer, planner, supervisor, utility), fed entirely by GET /api/models —
// every id, price class, default, disabled reason and pinned-role note comes
// from the payload; this component renders it and nothing else. All of the
// decision-making (what's disabled, what a Save/Reset PUT body contains, how
// a 422 reverts) lives in modelsPanelView.js so it's testable without a
// renderer, same split as authPanelView.js/AuthPanel.
export default function ModelsPanel() {
  const [payload, setPayload] = useState(undefined); // undefined = loading, null = unavailable
  const [pending, setPending] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    fetchModels().then((p) => setPayload(p));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (payload === undefined) return <div className="settings-empty">Loading…</div>;

  const view = modelsPanelView(payload);
  if (view.unavailable) {
    return (
      <div className="memory-panel">
        <div className="settings-empty">
          Model settings are unavailable — this server build does not expose
          the models endpoint yet.
        </div>
      </div>
    );
  }

  function selectedFor(row) {
    return pending[row.key] !== undefined ? pending[row.key] : row.current;
  }

  function handleChange(row, value) {
    setPending((p) => ({ ...p, [row.key]: value }));
    setError(null);
  }

  async function commit(body) {
    setSaving(true);
    setError(null);
    try {
      const refreshed = await saveModels(body);
      setPayload(refreshed);
      setPending({});
    } catch (e) {
      const reverted = applyError(pending, e.message);
      setPending(reverted.pending);
      setError(reverted.error);
    } finally {
      setSaving(false);
    }
  }

  const dirty = pendingBody(payload, pending);
  const hasChanges = Object.keys(dirty).length > 0;

  return (
    <div className="memory-panel models-panel">
      <div className="memory-header">
        <h3 className="memory-title"><span className="panel-title-text">Models</span></h3>
      </div>

      {view.showRestartBanner && (
        <div className="nh-alarm auth-alarm" role="alert">
          Restart required — a model change is saved to <code>config.yaml</code>,
          but the running server has not picked it up (it never reloads its
          config mid-run). Restart with <code>nh stop && nh start</code> to
          switch.
        </div>
      )}

      {error && <div className="settings-error" role="alert">{error}</div>}

      <div className="models-rows">
        {view.rows.map((row) => {
          const current = selectedFor(row);
          const selectedOption = row.options.find((o) => o.id === current);
          return (
            <div className="models-row" key={row.role}>
              <label className="auth-label">
                {row.label}
                <select
                  className="new-task-select"
                  aria-label={row.label}
                  value={current}
                  onChange={(e) => handleChange(row, e.target.value)}
                >
                  {row.options.map((o) => (
                    <option key={o.id} value={o.id} disabled={o.disabled} title={o.reason || undefined}>
                      {o.id} ({o.priceLabel})
                    </option>
                  ))}
                </select>
              </label>
              {selectedOption && (
                <span className="integration-chip tone-neutral">{selectedOption.priceLabel}</span>
              )}
              <span className="models-default">
                default: <code>{row.default}</code>
              </span>
              {row.note && <div className="ntm-hint">{row.note}</div>}
              {row.costNote && <div className="ntm-hint">{row.costNote}</div>}
            </div>
          );
        })}
      </div>

      <div className="integration-actions">
        <button
          type="button"
          className="btn btn-approve"
          disabled={!hasChanges || saving}
          onClick={() => commit(dirty)}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="btn btn-sendback"
          disabled={saving}
          onClick={() => commit(resetBody(payload))}
        >
          Reset to defaults
        </button>
      </div>
    </div>
  );
}
