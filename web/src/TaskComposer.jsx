import { useState, useEffect, useRef } from "react";
import { fetchProjects, fetchConfig, fetchIntegrations, searchJiraIssues, fetchJiraIssue } from "./api.js";
import { COMPOSER_KINDS, kindByValue, needsPrUrl } from "./composerKinds.js";
import { splitPrompt } from "./promptSplit.js";
import { promptFromIssue, jiraStatusChipStyle, externalIdFromIssue, importedChip } from "./jiraImport.js";
import { jiraResultHeader, jiraEmptyMessage, formatIssueUpdated } from "./jiraImport.js";
import { greetingName } from "./greeting.js";
import { hasPrRef } from "./prRefs.js";
import { keepFocusInDialog } from "./keepFocusInDialog.js";
import { formatBytes } from "./formatBytes.js";
import { pluralize } from "./pluralize.js";
import { useEscapeKey } from "./useEscapeKey.js";

// The new-task composer (Task 5A) — one prompt, kind chips, inline controls.
//
// FIRST screen migrated to Tailwind (Task 5.0 bridge): utilities resolve to the
// same CSS variables the plain-CSS screens use, so light/dark come from the
// existing [data-theme] blocks.
//
// It owns the composed spec and hands it to the parent, which runs the intake
// grill exactly as the old form branch did. It never creates the task itself.
// The parent re-seeds it via `initial` so a failed grill never loses the prompt.

// SCRUM-3: the display limit for the "Showing first N open tickets" header —
// must match api.js's searchJiraIssues default so truncation is never a lie.
const JIRA_LIMIT = 50;

// ONE control system. Two Preflight-off hazards are handled here, once:
//   1. `border` alone sets border-WIDTH. Preflight normally supplies
//      `border-style: solid`; without it the computed style stays `none`, which
//      forces the width back to 0 — the border vanishes on a <div>, and a <button>
//      falls back to the UA `outset` bevel. Every bordered control states
//      `border-solid` explicitly. (Measured in Chromium: 0px/none without it.)
//   2. A control that states no background keeps the native `buttonface` grey.
// Surface rule: a control sits on `bg-panel` (the input surface) or on `bg-card`
// (the dialog) and takes the OTHER token as its fill, so its edge always reads.
// (Never use an /opacity modifier on a bridged colour: the tokens are hex, not
// channel triplets, so `bg-accent/30` would not resolve.)
const CTL =
  "inline-flex h-10 shrink-0 cursor-pointer items-center justify-center rounded-full " +
  "border border-solid font-ui text-sm transition-colors";
const ON_PANEL = `${CTL} border-line bg-card text-text-muted hover:bg-hover hover:text-text`;
const ON_CARD = `${CTL} border-line bg-panel text-text-muted hover:bg-hover hover:text-text`;
// `text-base` is the --base token: dark ink on the light-blue dark-theme accent,
// near-white on the strong light-theme accent — the trick .btn-approve already
// used. Plain `text-white` reads at 2.85:1 on the dark accent, a WCAG AA failure
// on the primary action.
const ACCENT = `${CTL} border-accent bg-accent text-base hover:border-accent-600 hover:bg-accent-600`;
const GHOST = `${CTL} border-transparent bg-transparent text-text-muted hover:text-text`;

const SELECT =
  "w-full cursor-pointer appearance-none border-0 bg-transparent p-0 pr-6 font-ui " +
  "text-sm text-text outline-none";

const TEXT_FIELD =
  "h-10 w-full rounded-full border border-solid border-line bg-panel px-5 font-mono " +
  "text-sm text-text outline-none transition-colors placeholder:text-text-muted " +
  "focus:border-accent";

// A <select> dressed as a control: the native caret is stripped and redrawn in a
// token colour. `outline-none` on the <select> would leave a keyboard user with no
// focus indication at all, so the wrapper carries the focus state instead.
function SelectPill({ children, onPanel = false, grow = false, ...rest }) {
  return (
    <div
      className={
        `${onPanel ? ON_PANEL : ON_CARD} relative px-5 focus-within:border-accent ` +
        (grow ? "min-w-0 flex-1 justify-start" : "")
      }
    >
      <select className={SELECT} {...rest}>
        {children}
      </select>
      <span
        aria-hidden="true"
        className="pointer-events-none absolute right-4 font-ui text-xs text-text-muted"
      >
        ▾
      </span>
    </div>
  );
}

export default function TaskComposer({ busy, error, initial, onStart, onClose }) {
  // Seeded from `initial`: the parent unmounts this component for the duration of
  // the grill, so a grill that FAILS would otherwise drop the operator back into an
  // empty composer — prompt, attachments, kind and PR URL all gone.
  const [prompt, setPrompt] = useState(initial?.prompt ?? "");
  const [kind, setKind] = useState(initial?.kind ?? "feature");
  const [prUrl, setPrUrl] = useState(initial?.prUrl ?? "");
  const [priority, setPriority] = useState(initial?.priority ?? "medium");
  const [files, setFiles] = useState(initial?.files ?? []);
  // undefined = fetch in flight · [] = genuinely none. The initial-[]
  // ambiguity showed 'No projects yet' during the load window and then
  // REFLOWED the repo control under the operator (same loading≠empty
  // class as the Stats est-loader; probed live: false for ~1-4s).
  const [projects, setProjects] = useState(undefined);
  const [selectedProjectId, setSelectedProjectId] = useState(initial?.projectId ?? "");
  const [repoPath, setRepoPath] = useState(initial?.repoPath ?? "");
  const [customRepo, setCustomRepo] = useState(Boolean(initial?.customRepo));
  const [config, setConfig] = useState(null);
  // Task 1.6 — Import from Jira. `source` rides along invisibly (never a
  // control the operator sees) so a task created from a picked ticket carries
  // Task.source = "jira"; everything else runs the SAME grill flow untouched.
  const [source, setSource] = useState(initial?.source ?? "board");
  // SCRUM-33: the dedup key for a picked Jira ticket — seeded from `initial`
  // (like `source`) so a grill-fail re-seed doesn't drop it; null for every
  // typed/board task since only handleJiraPick below ever sets it.
  const [externalId, setExternalId] = useState(initial?.externalId ?? null);
  const [jiraConfigured, setJiraConfigured] = useState(false);
  const [jiraOpen, setJiraOpen] = useState(false);
  const [jiraQuery, setJiraQuery] = useState("");
  const [jiraResults, setJiraResults] = useState(undefined); // undefined = no search run yet
  // The query the MOUNTED results were fetched with (review finding: the
  // header/empty copy must describe the rows on screen, not the keystrokes
  // typed since — pairing the live query with stale rows misstates both).
  const [jiraShownQuery, setJiraShownQuery] = useState("");
  // SCRUM-3: replaces the old list-teardown-driving flag — a refresh in flight no longer tears
  // down the visible list, so this now drives only the in-flight hint + the
  // "nothing to show yet" skeleton gate, not the list's visibility.
  const [jiraRefreshing, setJiraRefreshing] = useState(false);
  const [jiraError, setJiraError] = useState(null);
  const [jiraNonce, setJiraNonce] = useState(0); // bumped by "Try again" to re-run the same search
  const fileInputRef = useRef(null);
  const dialogRef = useRef(null);
  // M2 — picking a ticket unmounts the clicked result button (the panel
  // closes), dropping focus to <body>. Send it to the prompt textarea
  // instead: the operator's next move is reading/editing the prefilled
  // prompt, same as after typing one by hand. The textarea itself never
  // unmounts when the Jira panel closes, so a direct .focus() call works
  // immediately — no effect/rAF needed (same imperative idiom as fileInputRef
  // above; optional chaining guards the pre-mount null case).
  const promptRef = useRef(null);

  // Escape closes the composer — suppressed while a submit is in flight so it
  // cannot discard a task that is already being created.
  // Live view of the typed repo path for the projects-resolve effect (state
  // captured at mount would be stale by resolve time).
  const repoPathRef = useRef("");
  useEffect(() => { repoPathRef.current = repoPath; });

  // Escape closes the Jira panel first (progressive disclosure — the same
  // "narrowest thing first" rule Integrations.jsx's Configure form uses),
  // and only falls through to closing the whole composer once it's shut.
  useEscapeKey(() => (jiraOpen ? setJiraOpen(false) : onClose()), !busy);

  // Jira configured? Fetched once — hidden entirely (not just disabled) when
  // it isn't, so an operator with no Jira integration never sees the affordance.
  useEffect(() => {
    fetchIntegrations()
      .then((r) => {
        const jira = (r.integrations || []).find((i) => i.name === "jira");
        setJiraConfigured(Boolean(jira?.configured));
      })
      .catch(() => {}); // best-effort, like the greeting's fetchConfig below
  }, []);

  // Debounced free-text search while the panel is open. 300ms so each
  // keystroke isn't a request; `ignore` + the cleanup timer discard a stale
  // response that lands after a newer query has already superseded it.
  useEffect(() => {
    if (!jiraOpen) return;
    const q = jiraQuery.trim();
    let ignore = false;
    // SCRUM-3: do NOT clear jiraResults here — every keystroke used to tear
    // the visible list down into skeletons before the 300ms debounce even
    // fired. The previous results stay mounted; jiraRefreshing only drives
    // the subtle "· Updating…" hint (and the skeleton gate, for the case
    // where there is nothing to show yet).
    setJiraRefreshing(true);
    setJiraError(null);
    // An empty query is valid and is the default: it browses ALL of the
    // configured project's open tickets so the picker lists everything to
    // choose from. Only typing needs the 300ms debounce; the initial (empty)
    // browse loads immediately.
    const h = setTimeout(() => {
      searchJiraIssues(q, JIRA_LIMIT)
        .then((issues) => {
          if (ignore) return;
          setJiraResults(issues);
          setJiraShownQuery(q);
          setJiraRefreshing(false);
        })
        .catch((err) => {
          if (ignore) return;
          setJiraError(err.message);
          setJiraResults(undefined);
          setJiraRefreshing(false);
        });
    }, q ? 300 : 0);
    return () => { ignore = true; clearTimeout(h); };
  }, [jiraQuery, jiraOpen, jiraNonce]);

  useEffect(() => {
    fetchProjects().then((p) => {
      setProjects(p || []);
      // A path TYPED during the load window must survive the picker's
      // arrival: without this, resolve swapped the control and submit
      // silently shipped the auto-defaulted project instead of the typed
      // repo (PR #116 review, medium — probe-proven). Non-empty free text
      // pins custom mode; the existing toggle proves the preserve path.
      setCustomRepo((cur) => cur || Boolean(repoPathRef.current?.trim()));
      // Only default the project when none is chosen — a re-seeded composer must
      // keep the operator's pick.
      setSelectedProjectId((cur) => cur || (p && p.length > 0 ? p[0].id : ""));
    });
    // The greeting is best-effort: no config, no name, no error.
    fetchConfig().then(setConfig).catch(() => {});
  }, []);

  // `role="dialog" aria-modal` promises the rest of the page is inert, but no
  // browser enforces that for Tab. Keep focus inside, and hand it back on close.
  useEffect(() => {
    const previous = document.activeElement;
    function onKeyDown(e) {
      if (e.key !== "Tab" || !dialogRef.current) return;
      const items = [...dialogRef.current.querySelectorAll("button, select, textarea, input")]
        .filter((el) => !el.disabled && el.type !== "file" && el.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previous && typeof previous.focus === "function") previous.focus();
    };
  }, []);

  const { title, description } = splitPrompt(prompt);
  const name = greetingName(config);
  const project = (projects ?? []).find((p) => p.id === selectedProjectId);
  const selectedChip = kindByValue(kind);

  // A code_review task is failed by the backend unless a PR/MR ref reaches it in
  // the title or description, so submit stays closed until one does. The prompt
  // counts too — pasting the URL there must never be blocked by the URL field.
  const prRefMissing = needsPrUrl(kind) && !hasPrRef(prompt) && !hasPrRef(prUrl);
  const hasRepo = customRepo ? repoPath.trim() : selectedProjectId || repoPath.trim();
  const canSubmit = Boolean(title) && Boolean(hasRepo) && !prRefMissing && !busy;
  // With no projects, the free-text path IS the only input — a toggle there would
  // be a no-op button whose only effect is wiping what was typed.
  const loaded = projects !== undefined;
  const showRepoToggle = loaded && projects.length > 0;
  // While loading, keep the free-text field (no swap-under-cursor when the
  // picker arrives is unavoidable, but the false claim below is).
  const freeTextRepo = customRepo || !loaded || projects.length === 0;

  // Picking an issue prefills the SAME prompt textarea the typed path uses —
  // the operator proceeds exactly as with a typed task from here on; the
  // grill flow below never has to know an import happened.
  async function handleJiraPick(issue) {
    setJiraOpen(false);
    setJiraQuery("");
    setJiraResults(undefined);
    // Prefill SYNCHRONOUSLY from the list brief so the operator sees their
    // pick instantly and can start editing (review 2026-07-25: awaiting the
    // detail fetch first let a slow Jira clobber typed text seconds later,
    // and two rapid picks could resolve out of order).
    const briefPrompt = promptFromIssue(issue);
    setPrompt(briefPrompt);
    setSource("jira");
    setExternalId(externalIdFromIssue(issue));
    promptRef.current?.focus();
    // SCRUM-9: the browse list truncates description to 2000 chars; upgrade
    // to the full issue best-effort — but only if the prompt is still exactly
    // the brief-derived text (untouched, and not superseded by a later pick).
    try {
      const full = await fetchJiraIssue(issue.key);
      setPrompt((current) =>
        current === briefPrompt ? promptFromIssue(full) : current);
    } catch {
      // the brief already in hand stands
    }
  }

  function handleSubmit(e) {
    if (e) e.preventDefault();
    if (!canSubmit) return;
    // The PR URL rides in the description — that is where parse_pr_refs looks.
    const fullDescription =
      [description, needsPrUrl(kind) ? prUrl.trim() : ""].filter(Boolean).join("\n\n") || null;
    onStart({
      title,
      description: fullDescription,
      kind,
      priority,
      files,
      repoPath: freeTextRepo ? repoPath.trim() || null : repoPath || null,
      projectId: !customRepo && selectedProjectId ? selectedProjectId : null,
      // Echoed back as `initial` if the grill fails, so nothing typed is lost.
      prompt,
      prUrl,
      customRepo,
      source,
      externalId,
    });
  }

  return (
    <div
      // data-nested-modal: any dialog that can sit above the task drawer must claim Escape,
      // or the drawer's own handler closes IT too and the typed prompt is gone.
      data-nested-modal
      // No backdrop-click close: a friend testing the app lost a long typed
      // prompt to a stray click outside the box. Escape, the × above and Cancel
      // are the deliberate ways out; none of them is reachable by accident.
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm sm:items-center sm:p-8"
      onMouseDown={keepFocusInDialog}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="New task"
        className="relative max-h-[90vh] w-full max-w-5xl overflow-y-auto rounded-3xl border border-solid border-line bg-card px-6 py-10 shadow-2xl sm:px-14 sm:py-12"
       
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className={`${GHOST} absolute right-5 top-5 w-10 text-lg hover:bg-hover`}
        >
          ×
        </button>

        {/* Import from Jira — hidden entirely when unconfigured (graceful degrade).
            An inline disclosure, not a nested modal: it opens in place, right where
            the affordance sits, and never blocks the rest of the composer. */}
        {jiraConfigured && (
          <div className="mb-4 flex justify-center">
            <button
              type="button"
              onClick={() => setJiraOpen((v) => !v)}
              aria-expanded={jiraOpen}
              aria-controls="jira-import-panel"
              className={`${GHOST} gap-2 rounded-full border border-solid border-line px-4 hover:bg-hover hover:text-text`}
            >
              <span aria-hidden="true">🎫</span> Import from Jira
            </button>
          </div>
        )}

        {jiraConfigured && jiraOpen && (
          <div
            id="jira-import-panel"
            className="jira-panel-enter mb-4 rounded-2xl border border-solid border-line bg-panel p-4"
          >
            <input
              autoFocus
              className={TEXT_FIELD}
              placeholder="Filter your Jira tickets — blank shows all open…"
              value={jiraQuery}
              onChange={(e) => setJiraQuery(e.target.value)}
              aria-label="Search Jira tickets"
            />
            <div className="mt-3 flex flex-col gap-2" aria-live="polite">
              {!jiraError && jiraResults && jiraResults.length > 0 && (
                <p className="mb-2 font-ui text-xs text-text-muted">
                  {jiraResultHeader(jiraShownQuery, jiraResults.length, JIRA_LIMIT)}
                  {jiraRefreshing ? " · Updating…" : ""}
                </p>
              )}
              {(!jiraResults || jiraResults.length === 0) && jiraRefreshing && (
                <>
                  <div className="skeleton h-14 w-full rounded-xl" aria-hidden="true" />
                  <div className="skeleton h-14 w-full rounded-xl" aria-hidden="true" />
                </>
              )}
              {!jiraRefreshing && jiraError && (
                <div
                  role="alert"
                  className="flex items-center justify-between gap-3 rounded-xl px-4 py-3 font-ui text-sm"
                  style={{ color: "var(--red)", background: "var(--red-dim)" }}
                >
                  <span>{jiraError}</span>
                  <button
                    type="button"
                    onClick={() => setJiraNonce((n) => n + 1)}
                    className={`${GHOST} shrink-0 px-3`}
                  >
                    Try again
                  </button>
                </div>
              )}
              {!jiraRefreshing && !jiraError && jiraResults && jiraResults.length === 0 && (
                <p className="px-2 py-3 text-center font-ui text-sm text-text-muted">
                  {jiraEmptyMessage(jiraShownQuery)}
                </p>
              )}
              {!jiraError && jiraResults && jiraResults.length > 0 && jiraResults.map((issue, i) => {
                // M1 — colour encodes STATE: the category derived from the
                // status name (jiraImport.js), never the raw enum. `null`
                // (unrecognised status) keeps the neutral className below —
                // no colour claim the normalizer can't back up.
                const chipStyle = jiraStatusChipStyle(issue.status);
                // SCRUM-18 — accidental re-import trap: when this ticket already
                // has a board task, show its status next to the Jira status chip.
                // The row stays clickable either way — import is always possible,
                // just no longer accidental.
                const imp = importedChip(issue.imported);
                // SCRUM-3: null (missing/malformed updated) omits the date
                // segment entirely — never renders a JS date-parse-failure string.
                const updatedText = formatIssueUpdated(issue.updated);
                return (
                  <button
                    key={issue.key}
                    type="button"
                    onClick={() => handleJiraPick(issue)}
                    style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}
                    className="jira-result-enter flex cursor-pointer flex-col gap-1 rounded-xl border border-solid border-line bg-card px-4 py-3 text-left transition-colors hover:border-accent hover:bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-ui text-sm font-medium text-text">
                        {issue.key}: {issue.summary}
                      </span>
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
                            className={
                              "shrink-0 rounded-full border border-solid px-2.5 py-0.5 font-ui text-xs " + imp.className
                            }
                            style={imp.style || undefined}
                          >
                            {imp.label}
                          </span>
                        )}
                      </span>
                    </div>
                    <span className="font-ui text-xs text-text-dim">
                      {updatedText}
                      {issue.assignee ? `${updatedText ? " · " : ""}${issue.assignee}` : ""}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        <div className="mb-9 text-center">
          <h2 className="font-display text-4xl font-semibold tracking-tight text-text-hi sm:text-5xl">
            {name ? `Hey there, ${name}` : "Hey there"}
          </h2>
          <p className="mt-3 font-ui text-base text-text-muted">What should I work on?</p>
        </div>

        <form onSubmit={handleSubmit} className={busy ? "pointer-events-none opacity-60" : ""}>
          {/* One large input surface: the prompt and the controls that qualify it. */}
          <div className="rounded-2xl border border-solid border-line bg-panel p-4 transition-colors focus-within:border-accent sm:p-5">
            <textarea
              ref={promptRef}
              className="min-h-[180px] w-full resize-none border-0 bg-transparent px-2 py-1 font-ui text-lg leading-relaxed text-text outline-none placeholder:text-text-muted sm:min-h-[200px]"
              autoFocus
              placeholder="Describe the task. The first line becomes its title."
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => {
                // Cmd/Ctrl+Enter submits, the way every chat composer does.
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") handleSubmit(e);
              }}
            />

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={(e) => setFiles(Array.from(e.target.files || []))}
              />
              <button
                type="button"
                className={`${ON_PANEL} w-10 text-lg`}
                onClick={() => fileInputRef.current?.click()}
                title="Attach screenshots or documents — the agent reads them"
                aria-label="Attach files"
              >
                +
              </button>

              <SelectPill
                onPanel
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                aria-label="Priority"
              >
                <option value="high">high priority</option>
                <option value="medium">medium priority</option>
                <option value="low">low priority</option>
              </SelectPill>

              <div className="ml-auto flex items-center gap-2">
                <button type="button" className={`${GHOST} px-5`} onClick={onClose}>
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={!canSubmit}
                  className={`${ACCENT} px-6 font-medium disabled:cursor-not-allowed disabled:opacity-40`}
                >
                  {busy ? "Exploring repo…" : "Next →"}
                </button>
              </div>
            </div>
          </div>

          {/* What shape of work this is. One choice out of many → radios, not toggles.
              N5: the two most consequential groups (kind, repo) sat unlabeled
              outside the card while priority (least consequential) sat inside —
              visible eyebrows give the hierarchy back without moving anything. */}
          <p className="mt-4 px-1 font-ui text-xs uppercase tracking-wide text-text-dim" id="kind-eyebrow">Kind</p>
          <div role="radiogroup" aria-labelledby="kind-eyebrow" className="mt-1.5 flex flex-wrap gap-2">
            {COMPOSER_KINDS.map((chip) => {
              const selected = chip.kind === kind;
              return (
                <button
                  key={chip.kind}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => setKind(chip.kind)}
                  className={`${selected ? ACCENT : ON_CARD} px-5`}
                >
                  {chip.label}
                </button>
              );
            })}
          </div>

          {/* The hint lived only in a `title` tooltip — invisible to keyboard and
              touch users. Show the selected kind's meaning instead. */}
          {selectedChip && (
            <p className="mt-2 px-5 font-ui text-sm text-text-muted">{selectedChip.hint}</p>
          )}

          {needsPrUrl(kind) && (
            <div className="mt-3">
              <input
                className={TEXT_FIELD}
                placeholder="https://github.com/owner/repo/pull/123"
                value={prUrl}
                onChange={(e) => setPrUrl(e.target.value)}
                aria-label="PR or MR URL"
                aria-describedby="pr-url-hint"
              />
              <p
                id="pr-url-hint"
                role={prRefMissing ? "alert" : undefined}
                className={`mt-2 px-5 font-ui text-sm ${prRefMissing ? "" : "text-text-muted"}`}
                style={prRefMissing ? { color: "var(--red)" } : undefined}
              >
                {prRefMissing
                  ? "Paste the PR/MR to review — a full URL, or a “host/owner/repo PR #123” reference."
                  : "The agent fetches this diff, reviews it, and drafts comments for your approval."}
              </p>
            </div>
          )}

          {/* Where the work happens. */}
          <p className="mt-3 px-1 font-ui text-xs uppercase tracking-wide text-text-dim">Repository</p>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            {!freeTextRepo ? (
              <SelectPill
                grow
                value={selectedProjectId}
                onChange={(e) => {
                  setSelectedProjectId(e.target.value);
                  // Otherwise a repo picked inside the OLD project rides along.
                  setRepoPath("");
                }}
                aria-label="Project"
              >
                {(projects ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} ({p.repo_paths.length} {pluralize(p.repo_paths.length, "repo")})
                  </option>
                ))}
              </SelectPill>
            ) : (
              <input
                className={`${TEXT_FIELD} min-w-0 flex-1`}
                placeholder="~/git/my-project"
                value={repoPath}
                onChange={(e) => setRepoPath(e.target.value)}
                aria-label="Repository path"
              />
            )}

            {!freeTextRepo && project && project.repo_paths.length > 1 && (
              <SelectPill
                value={repoPath || project.primary_repo || project.repo_paths[0]}
                onChange={(e) => setRepoPath(e.target.value)}
                aria-label="Repository"
              >
                {project.repo_paths.map((rp) => (
                  <option key={rp} value={rp}>
                    {rp.split("/").pop()}
                    {rp === project.primary_repo ? " (primary)" : ""}
                  </option>
                ))}
              </SelectPill>
            )}

            {showRepoToggle && (
              <button
                type="button"
                className={`${ON_CARD} px-5`}
                onClick={() => {
                  const next = !customRepo;
                  setCustomRepo(next);
                  // Leaving custom mode clears the free text and restores a project;
                  // ENTERING it keeps whatever was already typed.
                  if (!next) {
                    setRepoPath("");
                    setSelectedProjectId(projects[0]?.id || "");
                  }
                }}
              >
                {customRepo ? "back to projects" : "custom path"}
              </button>
            )}
          </div>

          {loaded && projects.length === 0 && (
            <p className="mt-2 px-5 font-ui text-sm text-text-muted">
              No projects yet — give the path of the repo to work in.
            </p>
          )}

          {files.length > 0 && (
            <div className="mt-3 px-5 font-ui text-sm text-text-muted">
              {files.map((f) => `${f.name} (${formatBytes(f.size)})`).join(", ")}
            </div>
          )}

          {error && (
            <div
              className="mt-4 rounded-2xl px-5 py-4 font-ui text-sm"
              style={{ color: "var(--red)", background: "var(--red-dim)" }}
            >
              {error}
            </div>
          )}
        </form>
      </div>
    </div>
  );
}
