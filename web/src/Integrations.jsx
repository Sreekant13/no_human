import { useEffect, useState, useCallback } from "react";
import { fetchIntegrations, testIntegration } from "./api.js";
import {
  statusChip, KIND_LABEL, NAME_LABEL, CONFIG_HINT, SECRET_ENV_KEY,
} from "./integrationChip.js";
import { IntegrationIcon } from "./integrationIcons.jsx";
import { useEscapeKey } from "./useEscapeKey.js";

// Settings → Integrations. One card per integration: brand mark, kind, live
// status chip, and a Test-connection check. Secrets are never shown (only that
// they're set); values are configured in config.yaml / .env (there is no
// config-write endpoint yet — see the hint), and verified here.
export default function IntegrationsPanel() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(null);
  const [testing, setTesting] = useState(null);

  const load = useCallback(() => {
    fetchIntegrations()
      .then((r) => setItems(r.integrations || []))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  useEscapeKey(() => setExpanded(null), expanded !== null);

  async function runTest(name) {
    setTesting(name);
    try {
      const status = await testIntegration(name);
      setItems((prev) => prev.map((it) => (it.name === name ? { ...it, ...status } : it)));
    } catch (e) {
      setItems((prev) => prev.map((it) =>
        it.name === name ? { ...it, healthy: false, detail: e.message } : it));
    } finally {
      setTesting(null);
    }
  }

  if (loading) {
    return (
      <div className="settings-loading">
        <span className="grill-spinner" /><span>Loading integrations…</span>
      </div>
    );
  }

  return (
    <div className="integrations-panel">
      <div className="ntm-hint" style={{ marginBottom: "16px" }}>
        Connect no_human to your issue tracker, CI, and notifications. Set values in{" "}
        <code>config.yaml</code> and tokens in <code>~/.no_human/.env</code>, then test the
        connection here.
      </div>
      <div className="integrations-list">
        {items.map((it) => {
          const chip = statusChip(it);
          const isOpen = expanded === it.name;
          return (
            <div key={it.name} className={`integration-card${isOpen ? " open" : ""}`}>
              <button className="integration-head" aria-expanded={isOpen}
                      onClick={() => setExpanded(isOpen ? null : it.name)}>
                <span className="integration-mark" style={{ color: "var(--text-hi)" }}>
                  <IntegrationIcon name={it.name} size={22} />
                </span>
                <span className="integration-name">{NAME_LABEL[it.name] || it.name}</span>
                <span className="integration-kind">{KIND_LABEL[it.kind] || it.kind}</span>
                <span className={`integration-chip tone-${chip.tone}`}>{chip.label}</span>
                <span className="integration-chev" aria-hidden="true">›</span>
              </button>
              {isOpen && (
                <div className="integration-body">
                  <div className="integration-detail">{it.detail || "—"}</div>
                  {SECRET_ENV_KEY[it.name] && (
                    <div className="integration-field">
                      <span className="ntm-label" style={{ marginBottom: 0 }}>Secret</span>
                      <code>{SECRET_ENV_KEY[it.name]}</code>
                      <span className={`integration-secret${it.configured ? " set" : ""}`}>
                        {it.configured ? "●●● set" : "not set"}
                      </span>
                    </div>
                  )}
                  <div className="ntm-hint">Configured in <code>{CONFIG_HINT[it.name]}</code>.</div>
                  <div className="integration-actions">
                    <button className="btn btn-sendback btn-sm" disabled={testing === it.name}
                            onClick={() => runTest(it.name)}>
                      {testing === it.name
                        ? <><span className="grill-spinner" /> Testing…</>
                        : "Test connection"}
                    </button>
                    {testing !== it.name && it.healthy === true && (
                      <span className="integration-result ok">✓ {it.detail}</span>
                    )}
                    {testing !== it.name && it.healthy === false && (
                      <span className="integration-result err">✕ {it.detail}</span>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
