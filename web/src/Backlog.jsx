import { useState, useEffect } from "react";
import { fetchIntegrations, searchJiraIssues } from "./api.js";
import {
  initialSelection, toggleKey, selectAll, clearSelection,
  startKeys, startIssues, selectionState, startLabel, multiStartNotice,
} from "./backlogSelection.js";
import {
  jiraStatusChipStyle, importedChip, jiraResultHeader, jiraEmptyMessage, formatIssueUpdated,
} from "./jiraImport.js";

// The Backlog page — the tracker's open tickets, multi-select, start.
//
// This replaces the composer's "Import from Jira" disclosure, which asked the
// operator to find a New Task button first and then discover a ticket picker
// hiding inside it. The backlog IS the work list, so it sits in the sidebar's
// Work group next to Done and Failed and can be read without opening anything.
//
// It never creates a task itself. "Start" hands the picked issues up to App,
// which runs them through the SAME intake flow (five questions) the typed
// composer path runs — see the N>1 note on multiStartNotice in
// backlogSelection.js and the queue in App.jsx. Two creation paths drift; this
// page deliberately has none of its own.
//
// The display limit for the "Showing first N open tickets" header — must match
// api.js's searchJiraIssues default so truncation is never a lie.
const JIRA_LIMIT = 50;

const CTL =
  "inline-flex h-9 shrink-0 cursor-pointer items-center justify-center rounded-full " +
  "border border-solid px-4 font-ui text-sm transition-colors";
// `border-solid` is stated explicitly on every bordered control: Tailwind's
// Preflight is off here, so `border` alone computes to border-style:none and
// paints nothing (and a <button> falls back to the UA bevel).
const GHOST_BTN = `${CTL} border-line bg-panel text-text-muted hover:bg-hover hover:text-text`;
// `text-base` is the --base token — dark ink on the dark theme's light accent,
// near-white on the light theme's strong one. Plain white fails AA on the former.
const ACCENT_BTN =
  `${CTL} h-10 border-accent bg-accent px-6 text-base font-medium ` +
  "hover:border-accent-600 hover:bg-accent-600 disabled:cursor-not-allowed disabled:opacity-50";

export default function Backlog({ onStart, refreshNonce = 0 }) {
  const [query, setQuery] = useState("");
  // undefined = no response yet · [] = genuinely none. Never collapse the two:
  // "no open tickets" during the load window is a claim we cannot back.
  const [issues, setIssues] = useState(undefined);
  // The query the MOUNTED rows were fetched with — the header/empty copy must
  // describe the rows on screen, not the keystrokes typed since.
  const [shownQuery, setShownQuery] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [nonce, setNonce] = useState(0); // bumped by "Try again"
  // undefined = still asking · false = Jira not configured · true = configured.
  const [configured, setConfigured] = useState(undefined);
  // NOTHING is pre-checked, ever. initialSelection() is the only seed.
  const [selected, setSelected] = useState(initialSelection);

  // Is Jira configured at all? One fetch at mount — this decides between the
  // "not connected, here is how" state and a real upstream error, so a missing
  // integration is never reported as "the tracker is down".
  useEffect(() => {
    let ignore = false;
    fetchIntegrations()
      .then((r) => {
        if (ignore) return;
        const jira = (r.integrations || []).find((i) => i.name === "jira");
        setConfigured(Boolean(jira && jira.configured));
      })
      .catch(() => { if (!ignore) setConfigured(false); });
    return () => { ignore = true; };
  }, []);

  // The ticket list. Typing is debounced 300ms; the initial browse-all loads
  // immediately. The visible rows are NOT torn down while a refresh is in
  // flight (that made every keystroke flash a skeleton) — only the error path
  // may clear them, since a stale list beside an error banner is a lie.
  useEffect(() => {
    if (configured !== true) return undefined;
    const q = query.trim();
    setRefreshing(true);
    setError(null);
    let ignore = false;
    const h = setTimeout(() => {
      searchJiraIssues(q, JIRA_LIMIT)
        .then((list) => {
          if (ignore) return;
          setIssues(list);
          setShownQuery(q);
          setRefreshing(false);
        })
        .catch((err) => {
          if (ignore) return;
          setError(err.message);
          setIssues(undefined);
          setRefreshing(false);
        });
    }, q ? 300 : 0);
    return () => { ignore = true; clearTimeout(h); };
  }, [query, configured, nonce, refreshNonce]);

  // Everything on screen is derived from the CURRENT list, so a selection
  // holding a key that is no longer listed (closed ticket, narrowed search)
  // can neither inflate the count nor smuggle a ticket into the start.
  const toStart = startKeys(selected, issues);
  const state = selectionState(selected, issues);
  const notice = multiStartNotice(toStart.length);

  function start(list) {
    if (!list.length) return;
    onStart(list);
    setSelected(clearSelection());
  }

  // ── States that must never be confused with each other ────────────────────

  if (configured === false) {
    return (
      <div className="outcome-page">
        <h2 className="outcome-title">Backlog</h2>
        <div className="outcome-sub">Not connected to a tracker</div>
        <div className="mt-6 max-w-2xl rounded-2xl border border-solid border-line bg-panel p-6 font-ui text-sm text-text-muted">
          <p className="text-text">Jira is not configured.</p>
          <p className="mt-2">
            Open <b>Settings ▸ Integrations</b>, fill in your Jira site URL and project key, and
            put a <code>JIRA_API_TOKEN</code> in <code>~/.no_human/.env</code>. Reload this page
            once it saves and your open tickets will be listed here.
          </p>
          <p className="mt-4 text-text-dim">
            Linear is not connected either — no_human can read Jira today; the Linear side has no
            issue listing yet, so this page does not offer one.
          </p>
        </div>
      </div>
    );
  }

  const header = !error && issues && issues.length > 0
    ? jiraResultHeader(shownQuery, issues.length, JIRA_LIMIT)
    : null;

  return (
    <div className="outcome-page">
      <h2 className="outcome-title">Backlog</h2>
      <div className="outcome-sub">
        {header || (configured === undefined || (refreshing && !issues) ? "Loading your tickets…" : "Open tickets from Jira")}
        {header && refreshing ? " · Updating…" : ""}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <input
          className="h-10 w-full max-w-md rounded-full border border-solid border-line bg-panel px-5 font-mono text-sm text-text outline-none transition-colors placeholder:text-text-muted focus:border-accent"
          placeholder="Filter your tickets — blank shows all open…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Filter backlog tickets"
        />
        <button
          type="button"
          className={GHOST_BTN}
          disabled={state === "empty" || state === "all"}
          onClick={() => setSelected(selectAll(issues))}
        >
          Select all
        </button>
        <button
          type="button"
          className={GHOST_BTN}
          disabled={!toStart.length}
          onClick={() => setSelected(clearSelection())}
        >
          Clear
        </button>
      </div>

      {/* Linear: stated, not offered. There is no Linear issue-listing endpoint
          in this product, so a Linear tab would be an affordance that cannot
          load — the page says where it stands instead of implying a choice. */}
      <p className="mt-2 font-ui text-xs text-text-dim">
        Jira only for now — Linear is not connected.
      </p>

      <div className="mt-4 flex flex-col gap-2" aria-live="polite">
        {!refreshing && error && (
          <div
            role="alert"
            className="flex items-center justify-between gap-3 rounded-xl px-4 py-3 font-ui text-sm"
            style={{ color: "var(--red)", background: "var(--red-dim)" }}
          >
            {/* An upstream failure is NOT an empty backlog. The server's detail
                says which (expired token vs. site/project config); it is shown
                verbatim under a heading that never claims "no tickets". */}
            <span>
              <b>Couldn&apos;t reach Jira.</b> {error} Your backlog is not empty — it could not be read.
            </span>
            <button type="button" className={GHOST_BTN} onClick={() => setNonce((n) => n + 1)}>
              Try again
            </button>
          </div>
        )}

        {(!issues || issues.length === 0) && refreshing && !error && (
          <>
            <div className="skeleton h-16 w-full rounded-xl" aria-hidden="true" />
            <div className="skeleton h-16 w-full rounded-xl" aria-hidden="true" />
            <div className="skeleton h-16 w-full rounded-xl" aria-hidden="true" />
          </>
        )}

        {!refreshing && !error && issues && issues.length === 0 && (
          <p className="px-2 py-8 text-center font-ui text-sm text-text-muted">
            {jiraEmptyMessage(shownQuery)}
          </p>
        )}

        {!error && issues && issues.map((issue, i) => {
          const chipStyle = jiraStatusChipStyle(issue.status);
          // The ticket already has a board task: say so, and take it out of
          // every bulk affordance. Starting it again stays possible, but only
          // via the explicit per-row button below.
          const imp = importedChip(issue.imported);
          const updatedText = formatIssueUpdated(issue.updated);
          const checked = selected.includes(issue.key);
          const disabled = Boolean(issue.imported);
          return (
            <div
              key={issue.key}
              style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}
              className={
                "jira-result-enter flex items-center gap-3 rounded-xl border border-solid bg-card px-4 py-3 transition-colors " +
                (checked ? "border-accent" : "border-line")
              }
            >
              <label className={"flex min-w-0 flex-1 items-center gap-3 " + (disabled ? "cursor-default" : "cursor-pointer")}>
                <input
                  type="checkbox"
                  className="h-4 w-4 shrink-0"
                  style={{ accentColor: "var(--accent)" }}
                  checked={checked}
                  disabled={disabled}
                  onChange={() => setSelected((s) => toggleKey(s, issue.key, issues))}
                  aria-label={`Select ${issue.key}: ${issue.summary}`}
                  title={disabled ? "Already started as a task — use Start again to create a second one" : undefined}
                />
                <span className="flex min-w-0 flex-col gap-1">
                  <span className="truncate font-ui text-sm font-medium text-text">
                    {issue.key}: {issue.summary}
                  </span>
                  <span className="font-ui text-xs text-text-dim">
                    {updatedText}
                    {issue.assignee ? `${updatedText ? " · " : ""}${issue.assignee}` : ""}
                  </span>
                </span>
              </label>
              <span className="flex shrink-0 items-center gap-1.5">
                {issue.status && (
                  <span
                    className={
                      "shrink-0 rounded-full border border-solid px-2.5 py-0.5 font-ui text-xs " +
                      (chipStyle ? "font-medium" : "border-line bg-panel text-text-muted")
                    }
                    style={chipStyle || undefined}
                  >
                    {issue.status}
                  </span>
                )}
                {imp && (
                  <span
                    className={"shrink-0 rounded-full border border-solid px-2.5 py-0.5 font-ui text-xs " + imp.className}
                    style={imp.style || undefined}
                  >
                    {imp.label}
                  </span>
                )}
                {imp && (
                  <button
                    type="button"
                    className={`${GHOST_BTN} h-8 px-3 text-xs`}
                    onClick={() => start([issue])}
                    title={`${issue.key} already has a task (${issue.imported.status}). Starting it again creates a SECOND task for the same ticket.`}
                  >
                    Start again
                  </button>
                )}
              </span>
            </div>
          );
        })}
      </div>

      {/* The action bar. The count is `toStart.length` — the same number that
          gets started — not the raw checkbox count. */}
      <div
        className="sticky bottom-0 mt-4 flex flex-wrap items-center gap-4 border-0 border-t border-solid border-line py-4"
        // --bg is not in the Tailwind token bridge (only the surface tokens
        // are); the bar must be opaque or rows scroll visibly under it.
        style={{ background: "var(--bg)" }}
      >
        <button
          type="button"
          className={ACCENT_BTN}
          disabled={!toStart.length}
          onClick={() => start(startIssues(selected, issues))}
        >
          {startLabel(toStart.length)}
        </button>
        {notice && <span className="font-ui text-xs text-text-muted">{notice}</span>}
      </div>
    </div>
  );
}
