import { useState, useEffect } from "react";
import { fetchIntegrations, searchTrackerIssues } from "./api.js";
import {
  initialSelection, toggleId, selectAll, clearSelection, rowId,
  startIds, startIssues, selectionState, startLabel, multiStartNotice,
} from "./backlogSelection.js";
import {
  configuredTrackers, mergeTrackerResults, sourcesLine, noTrackerMessage,
} from "./backlogSources.js";
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
// which runs them through the SAME intake flow ("Let's scope this") the typed
// composer path runs — see the N>1 note on multiStartNotice in
// backlogSelection.js and the queue in App.jsx. Two creation paths drift; this
// page deliberately has none of its own.
//
// It lists Jira AND Linear. Which trackers it asks, how the answers combine and
// what it may SAY about a tracker it is not showing all live in
// backlogSources.js — read the note there for why that last one is not a
// cosmetic concern.
//
// The display limit for the "Showing first N open tickets" header — must match
// api.js's per-tracker default so truncation is never a lie.
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
  // Per-tracker failures from the LAST list fetch. One tracker being down does
  // not hide the other's tickets — it is reported beside them.
  const [listErrors, setListErrors] = useState([]);
  const [nonce, setNonce] = useState(0); // bumped by "Try again"
  // undefined = still asking · [] = nothing configured · [names] = configured.
  const [trackers, setTrackers] = useState(undefined);
  // Set when the REGISTRY itself could not be read. Distinct from `trackers`
  // being empty — see the note on the branch below.
  const [registryError, setRegistryError] = useState(null);
  // NOTHING is pre-checked, ever. initialSelection() is the only seed.
  const [selected, setSelected] = useState(initialSelection);

  // Which trackers are configured? One fetch at mount. This decides between the
  // "not connected, here is how" state and a real upstream error, so a missing
  // integration is never reported as "the tracker is down" — nor the reverse.
  useEffect(() => {
    let ignore = false;
    fetchIntegrations()
      .then((r) => {
        if (ignore) return;
        setRegistryError(null);
        setTrackers(configuredTrackers(r));
      })
      .catch((err) => {
        if (ignore) return;
        // fetchIntegrations THROWS on a failed request (api.js). It used to
        // swallow every non-ok response into `{integrations: []}`, which is
        // indistinguishable from a healthy server saying "nothing configured" —
        // so a 500 rendered "Jira is not configured" and sent the operator to
        // Settings to fix a token that was never the problem.
        setRegistryError(err.message || "the no_human server did not answer");
        setTrackers(undefined);
      });
    return () => { ignore = true; };
  }, [nonce]);

  // The ticket list, from every configured tracker at once. Typing is debounced
  // 300ms; the initial browse-all loads immediately. The visible rows are NOT
  // torn down while a refresh is in flight (that made every keystroke flash a
  // skeleton) — only a total failure may clear them, since a stale list beside
  // an error banner is a lie.
  useEffect(() => {
    if (!trackers || !trackers.length) return undefined;
    const q = query.trim();
    setRefreshing(true);
    let ignore = false;
    const h = setTimeout(() => {
      Promise.all(trackers.map((t) => searchTrackerIssues(t, q, JIRA_LIMIT)
        .then((list) => ({ tracker: t, issues: list }))
        .catch((err) => ({ tracker: t, error: err.message }))))
        .then((results) => {
          if (ignore) return;
          const { issues: merged, errors } = mergeTrackerResults(results);
          setListErrors(errors);
          // EVERY tracker failed: there is nothing truthful to show, so the
          // rows go rather than sit under a banner contradicting them. One of
          // two failing still lists the other one's tickets.
          setIssues(errors.length === results.length ? undefined : merged);
          setShownQuery(q);
          setRefreshing(false);
        });
    }, q ? 300 : 0);
    return () => { ignore = true; clearTimeout(h); };
  }, [query, trackers, nonce, refreshNonce]);

  // Everything on screen is derived from the CURRENT list, so a selection
  // holding an id that is no longer listed (closed ticket, narrowed search)
  // can neither inflate the count nor smuggle a ticket into the start.
  const toStart = startIds(selected, issues);
  const state = selectionState(selected, issues);
  const notice = multiStartNotice(toStart.length);
  const allFailed = listErrors.length > 0 && !issues;

  function start(list) {
    if (!list.length) return;
    onStart(list);
    setSelected(clearSelection());
  }

  // ── States that must never be confused with each other ────────────────────

  // 1. The server did not answer. NOT "nothing is configured": the difference
  //    between "I could not ask" and "the answer is none" is the difference
  //    between restarting no_human and going to hunt for an API token.
  if (registryError) {
    return (
      <div className="outcome-page">
        <h2 className="outcome-title">Backlog</h2>
        <div className="outcome-sub">Couldn&apos;t reach the no_human server</div>
        <div className="mt-6 max-w-2xl rounded-2xl border border-solid border-line bg-panel p-6 font-ui text-sm text-text-muted">
          <p className="text-text">Your trackers could not be checked.</p>
          <p className="mt-2">{registryError}</p>
          <p className="mt-2">
            This says nothing about your integrations — no_human could not ask. Whether Jira or
            Linear is connected is still unknown.
          </p>
          <button type="button" className={`${GHOST_BTN} mt-4`} onClick={() => setNonce((n) => n + 1)}>
            Try again
          </button>
        </div>
      </div>
    );
  }

  // 2. The server answered, and no tracker is connected.
  if (trackers && trackers.length === 0) {
    return (
      <div className="outcome-page">
        <h2 className="outcome-title">Backlog</h2>
        <div className="outcome-sub">Not connected to a tracker</div>
        <div className="mt-6 max-w-2xl rounded-2xl border border-solid border-line bg-panel p-6 font-ui text-sm text-text-muted">
          <p className="text-text">{noTrackerMessage()}</p>
          <p className="mt-2">
            Open <b>Settings ▸ Integrations</b> and connect either one. Jira needs its site URL
            and project key plus a <code>JIRA_API_TOKEN</code> in <code>~/.no_human/.env</code>;
            Linear needs its team key plus a <code>LINEAR_API_KEY</code> in the same file.
            Reload this page once it saves and your open tickets will be listed here.
          </p>
        </div>
      </div>
    );
  }

  const header = !allFailed && issues && issues.length > 0
    ? jiraResultHeader(shownQuery, issues.length, JIRA_LIMIT)
    : null;

  return (
    <div className="outcome-page">
      <h2 className="outcome-title">Backlog</h2>
      <div className="outcome-sub">
        {header || (trackers === undefined || (refreshing && !issues) ? "Loading your tickets…" : "Open tickets")}
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

      {/* Which trackers these rows came from — DERIVED from what the server
          said is configured, never a hand-written claim about what this product
          can read. The line this replaced said Linear had no issue listing;
          it had one all along. See backlogSources.js. */}
      <p className="mt-2 font-ui text-xs text-text-dim">{sourcesLine(trackers)}</p>

      <div className="mt-4 flex flex-col gap-2" aria-live="polite">
        {!refreshing && listErrors.map((e) => (
          <div
            key={e.tracker}
            role="alert"
            className="flex items-center justify-between gap-3 rounded-xl px-4 py-3 font-ui text-sm"
            style={{ color: "var(--red)", background: "var(--red-dim)" }}
          >
            {/* An upstream failure is NOT an empty backlog. The server's detail
                says which (expired token vs. site/project config); it is shown
                verbatim under a heading that never claims "no tickets". */}
            <span>
              <b>Couldn&apos;t reach {e.label}.</b> {e.message} Your backlog is not empty — it
              could not be read.
            </span>
            <button type="button" className={GHOST_BTN} onClick={() => setNonce((n) => n + 1)}>
              Try again
            </button>
          </div>
        ))}

        {(!issues || issues.length === 0) && refreshing && !allFailed && (
          <>
            <div className="skeleton h-16 w-full rounded-xl" aria-hidden="true" />
            <div className="skeleton h-16 w-full rounded-xl" aria-hidden="true" />
            <div className="skeleton h-16 w-full rounded-xl" aria-hidden="true" />
          </>
        )}

        {!refreshing && !allFailed && issues && issues.length === 0 && (
          <p className="px-2 py-8 text-center font-ui text-sm text-text-muted">
            {jiraEmptyMessage(shownQuery)}
          </p>
        )}

        {issues && issues.map((issue, i) => {
          const chipStyle = jiraStatusChipStyle(issue.status);
          // The ticket already has a board task: say so, and take it out of
          // every bulk affordance. Starting it again stays possible, but only
          // via the explicit per-row button below.
          const imp = importedChip(issue.imported);
          const updatedText = formatIssueUpdated(issue.updated);
          // tracker + key, never the key alone: two trackers mint keys of the
          // same shape and this list holds both (backlogSelection.js: rowId).
          const id = rowId(issue);
          const checked = selected.includes(id);
          const disabled = Boolean(issue.imported);
          return (
            <div
              key={id}
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
                  onChange={() => setSelected((s) => toggleId(s, id, issues))}
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
                {/* Which tracker this row came from. Once two lists are merged
                    the key alone does not say, and both trackers mint keys of
                    the same shape. Only shown when there IS a choice. */}
                {trackers && trackers.length > 1 && (
                  <span className="shrink-0 rounded-full border border-solid border-line bg-panel px-2.5 py-0.5 font-ui text-xs text-text-muted">
                    {issue.tracker === "linear" ? "Linear" : "Jira"}
                  </span>
                )}
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
