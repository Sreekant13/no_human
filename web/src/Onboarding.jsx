import React, { useEffect, useRef, useState } from "react";
import {
  detectRepos, onboardRepo, extractHistory, analyzeHistory,
  confirmRules, completeOnboarding, suggestPaths, createProject,
  generateDocs, fetchIntegrations,
} from "./api.js";
import { LegionLogo } from "./Logo.jsx";
import { statusChip, KIND_LABEL, NAME_LABEL } from "./integrationChip.js";
import { IntegrationIcon } from "./integrationIcons.jsx";

// "Building your validator agent" — a T-800-style head that assembles itself
// while the (slow) history scan runs: the chrome skull draws in, plates snap on,
// a scan line sweeps, and the iconic red eye powers up. Pure SVG + CSS.
function ValidatorAvatar() {
  return (
    <svg className="validator-avatar" viewBox="0 0 120 130" role="img"
         aria-label="Building validator agent">
      <defs>
        <linearGradient id="va-steel-g" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#DDE2E9" />
          <stop offset=".5" stopColor="#B4BCC8" />
          <stop offset="1" stopColor="#8C95A4" />
        </linearGradient>
        <radialGradient id="va-red-g" cx="0.5" cy="0.45" r="0.6">
          <stop offset="0" stopColor="#FF8A8A" />
          <stop offset=".5" stopColor="#E5202A" />
          <stop offset="1" stopColor="#8E0000" />
        </radialGradient>
        <clipPath id="va-clip">
          <path d="M60 14 C40 14 30 28 30 47 C30 55 28 58 27 63 C26 68 30 71 34 72 C35 80 40 87 47 91 L49 99 C52 104 56 106 60 106 C64 106 68 104 71 99 L73 91 C80 87 85 80 86 72 C90 71 94 68 93 63 C92 58 90 55 90 47 C90 28 80 14 60 14 Z" />
        </clipPath>
      </defs>
      <g className="va-skull">
        <path fill="url(#va-steel-g)" stroke="#67717F" strokeWidth="1.5" d="M60 14 C40 14 30 28 30 47 C30 55 28 58 27 63 C26 68 30 71 34 72 C35 80 40 87 47 91 L49 99 C52 104 56 106 60 106 C64 106 68 104 71 99 L73 91 C80 87 85 80 86 72 C90 71 94 68 93 63 C92 58 90 55 90 47 C90 28 80 14 60 14 Z" />
        <path fill="#ECEFF3" opacity=".55" d="M60 18 C45 18 36 28 35 44 C46 35 74 35 85 44 C84 28 75 18 60 18 Z" />
        <path fill="#7B8493" d="M33 52 C44 47 76 47 87 52 L85 58 C75 53 45 53 35 58 Z" />
        <ellipse cx="46" cy="63" rx="10.5" ry="8" fill="#363D4B" />
        <ellipse cx="74" cy="63" rx="10.5" ry="8" fill="#363D4B" />
        <circle cx="46" cy="63" r="3.8" fill="#9AA6B5" />
        <path fill="#363D4B" d="M60 69 L56 79 L64 79 Z" />
        <path stroke="#67717F" fill="none" strokeWidth="1.2" d="M32 70 L40 74 M88 70 L80 74" />
        <rect x="45" y="84" width="30" height="12" rx="2.5" fill="#C7CDD8" stroke="#67717F" strokeWidth="1.2" />
        <g stroke="#67717F" strokeWidth="1.1">
          <line x1="45" y1="90" x2="75" y2="90" />
          <line x1="52" y1="84" x2="52" y2="96" />
          <line x1="60" y1="84" x2="60" y2="96" />
          <line x1="68" y1="84" x2="68" y2="96" />
        </g>
        {/* the iconic red eye — powers on after the skull forms */}
        <circle className="va-eye-red" cx="74" cy="63" r="4.6" fill="url(#va-red-g)" />
        {/* scan line, clipped to the skull so it reads as an internal sweep */}
        <rect className="va-scan" clipPath="url(#va-clip)" x="26" y="12" width="68" height="3" rx="1.5" />
      </g>
    </svg>
  );
}

// Input with live directory autocomplete (via /api/fs/suggest). As you type a
// path, matching sub-directories are offered through a native <datalist>.
function PathInput({ value, onChange, placeholder, autoFocus }) {
  const [opts, setOpts] = useState([]);
  const listId = "pathlist-" + (placeholder || "p").replace(/\W/g, "");
  useEffect(() => {
    let live = true;
    const t = setTimeout(async () => {
      const res = await suggestPaths(value);
      if (live) setOpts(res.suggestions || []);
    }, 120);
    return () => { live = false; clearTimeout(t); };
  }, [value]);
  return (
    <>
      <input
        className="ob-input" list={listId} value={value} placeholder={placeholder}
        autoFocus={autoFocus} spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={listId}>
        {opts.map((o) => (
          <option key={o.path} value={o.path}>
            {o.is_repo ? "git repo" : "folder"}
          </option>
        ))}
      </datalist>
    </>
  );
}

// The first-run wizard. Warm-editorial, dark-first (see docs/DESIGN_SYSTEM.md).
// Every step wires to real /api/onboarding/* endpoints — no fake data.

const STEPS = [
  { key: "welcome",  title: "Welcome" },
  { key: "team",     title: "You" },
  { key: "repos",    title: "Repositories" },
  { key: "projects", title: "Projects" },
  { key: "docs",     title: "Docs" },
  { key: "integrations", title: "Integrations" },
  { key: "history",  title: "AI history" },
  { key: "rules",    title: "Rules review" },
  { key: "summary",  title: "Launch" },
];

export default function Onboarding({ onComplete }) {
  const [i, setI] = useState(0);
  const [team, setTeam] = useState("");
  const [root, setRoot] = useState("~/git");
  const [detected, setDetected] = useState([]);
  const [selectedRepos, setSelectedRepos] = useState(new Set());
  const [onboarded, setOnboarded] = useState({});   // path -> {ecosystem,test_cmd,...} | "busy"
  const [projectDefs, setProjectDefs] = useState([]);   // [{name, repos: Set}]
  const [newProjName, setNewProjName] = useState("");
  const [docs, setDocs] = useState([""]);
  const [history, setHistory] = useState(null);     // {available, transcripts}
  const [scanPhase, setScanPhase] = useState("idle"); // idle|extracting|analyzing|done|unavailable
  const [proposals, setProposals] = useState([]);
  const [chosenRules, setChosenRules] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [wikiGen, setWikiGen] = useState({});  // repoPath -> "busy"|{ok,files}|{error}
  const [integrations, setIntegrations] = useState(null);   // null=unloaded

  const step = STEPS[i];
  const next = () => setI((n) => Math.min(n + 1, STEPS.length - 1));
  const back = () => setI((n) => Math.max(n - 1, 0));

  // Advancing a step swaps the whole card underneath the user, and nothing moved focus
  // with it. Measured on the pre-change build, not assumed: the Continue button lives in
  // .ob-nav, a SIBLING of the card, so it is the same DOM node before and after and it
  // KEEPS focus — across the eight advances, focus stayed on that button 6 times, sat on
  // a step's own autofocused input once, and fell to <body> once (entering the repos
  // step, which is the one that kicks off an async guard() — consistent with the button
  // being disabled mid-flight, though that cause is inferred, not measured).
  // So the defect was never "focus is lost". It is that focus stays parked on a control
  // that sits AFTER the card in DOM order, so the next Tab walks out of the wizard
  // instead of into the new step's fields — and a screen reader is told nothing at all
  // while the card's entire contents change. Moving focus into the card, labelled
  // "Step N of 9: <title>", announces the change once and puts the next Tab inside it.
  const cardRef = useRef(null);
  // Compare the previous index rather than a "have we mounted" flag: <StrictMode>
  // (main.jsx) double-invokes effects on mount with refs preserved, so a boolean reads
  // as already-mounted on the second pass and steals focus on first paint in dev.
  const prevStep = useRef(i);
  useEffect(() => {
    if (prevStep.current === i) return;
    prevStep.current = i;
    const card = cardRef.current;
    // A step that autoFocuses its own input has already placed focus better than we can;
    // stealing it back to the container would undo that.
    if (card && !card.contains(document.activeElement)) card.focus();
  }, [i]);

  async function guard(fn) {
    setBusy(true); setErr(null);
    try { await fn(); } catch (e) { setErr(e.message); } finally { setBusy(false); }
  }

  // Phase 3a: preload history scan on mount — kick off _gather_history early
  // so results are ready by the time the user reaches the history step.
  const historyPromiseRef = useRef(null);
  useEffect(() => {
    historyPromiseRef.current = extractHistory().catch(() => null);
  }, []);

  // Auto-detect repos when entering the repos step the first time.
  useEffect(() => {
    if (step.key === "repos" && detected.length === 0) {
      guard(async () => {
        const res = await detectRepos(root);
        setDetected(res.repos || []);
      });
    }
    if (step.key === "integrations" && integrations === null) {
      fetchIntegrations().then((r) => setIntegrations(r.integrations || [])).catch(() => setIntegrations([]));
    }
    // deps intentionally partial (was: eslint-disable react-hooks/exhaustive-deps — plugin never loaded here)
  }, [step.key]);

  function toggleRepo(path) {
    setSelectedRepos((s) => {
      const n = new Set(s);
      n.has(path) ? n.delete(path) : n.add(path);
      return n;
    });
  }

  async function onboardSelected() {
    await guard(async () => {
      for (const path of selectedRepos) {
        setOnboarded((o) => ({ ...o, [path]: "busy" }));
        const res = await onboardRepo(path);
        setOnboarded((o) => ({ ...o, [path]: res }));
      }
    });
  }

  async function scanHistory() {
    setErr(null);
    try {
      setScanPhase("extracting");
      // Phase 3a: use preloaded result if available, else fresh fetch.
      const ext = (historyPromiseRef.current && await historyPromiseRef.current) || await extractHistory();
      historyPromiseRef.current = null;  // consumed
      setHistory(ext);
      if (ext.available && (ext.transcripts > 0 || ext.skills > 0)) {
        setScanPhase("analyzing");
        const an = await analyzeHistory(30);
        setProposals(an.proposals || []);
        setChosenRules(new Set((an.proposals || []).map((p) => p.id)));
        setScanPhase("done");
      } else {
        setScanPhase("unavailable");
      }
    } catch (e) {
      setErr(e.message);
      setScanPhase("idle");
    }
  }
  const scanning = scanPhase === "extracting" || scanPhase === "analyzing";

  function toggleRule(id) {
    setChosenRules((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  }

  function addProject() {
    const name = newProjName.trim();
    if (!name || projectDefs.some((p) => p.name === name)) return;
    setProjectDefs([...projectDefs, { name, repos: new Set() }]);
    setNewProjName("");
  }

  function toggleProjectRepo(projIdx, repoPath) {
    setProjectDefs(projectDefs.map((p, i) => {
      if (i !== projIdx) return p;
      const next = new Set(p.repos);
      next.has(repoPath) ? next.delete(repoPath) : next.add(repoPath);
      return { ...p, repos: next };
    }));
  }

  function removeProject(projIdx) {
    setProjectDefs(projectDefs.filter((_, i) => i !== projIdx));
  }

  async function finish() {
    await guard(async () => {
      if (chosenRules.size) await confirmRules([...chosenRules]);
      // Create projects via API.
      for (const pd of projectDefs) {
        const repoPaths = [...pd.repos];
        if (repoPaths.length > 0) {
          try {
            await createProject({ name: pd.name, repo_paths: repoPaths });
          } catch (e) {
            // 409 = already exists, skip.
            if (!e.message.includes("already exists")) throw e;
          }
        }
      }
      await completeOnboarding({
        team: team.trim() || null,
        repos: [...selectedRepos],
        docs: docs.map((d) => d.trim()).filter(Boolean),
      });
      onComplete();
    });
  }

  return (
    <div className="ob-shell">
      <div className="ob-stage">
        {/* The wizard replaces the whole app shell (App.jsx returns it before the shell's
            own h1 element), so without this the document had no h1 at all on eight of the nine
            steps — the step headings are h2. Visually hidden: the visible headline is the
            step's own. */}
        <h1 className="sr-only">Set up no_human</h1>
        <div className="ob-stepper" role="list" aria-label="Setup progress">
          {STEPS.map((s, idx) => (
            <div
              key={s.key}
              role="listitem"
              // Position and state were carried only by colour and opacity, which say
              // nothing to a screen reader. aria-current marks where you are; the
              // hidden suffix says whether a step is finished or still ahead.
              aria-current={idx === i ? "step" : undefined}
              className={`ob-step${idx === i ? " current" : ""}${idx < i ? " done" : ""}`}
            >
              <span className="ob-step-dot" />
              <span className="ob-step-label">{s.title}</span>
              {/* Only the states aria-current does NOT already convey. Saying
                  "(current step)" here too would make the current item announce its
                  state twice. */}
              {idx !== i && (
                <span className="sr-only">{idx < i ? " (completed)" : " (not started)"}</span>
              )}
            </div>
          ))}
        </div>

        <div
          className="ob-card"
          key={step.key}
          ref={cardRef}
          // Focus target for the step change above. tabIndex -1 keeps it out of the tab
          // order — it is a landing spot, not a control.
          tabIndex={-1}
          role="group"
          aria-label={`Step ${i + 1} of ${STEPS.length}: ${step.title}`}
        >
          {step.key === "welcome" && (
            <Stagger>
              <div className="ob-brand"><LegionLogo size={44} /><span>no_human</span></div>
              {/* h2, not h1: the wizard's h1 is the hidden one on .ob-stage, so every step
                  now reads h1 → h2 instead of this step alone owning the h1 and the other
                  eight starting at h2. Renders identically — verified pixel-identical —
                  but NOT merely because `.ob-h1` is a class-only selector: Tailwind
                  preflight is off here, so UA margins would otherwise differ (h1 0.67em
                  vs h2 0.83em). What actually makes the swap safe is the universal
                  `*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }`
                  reset near the top of styles.css. If that reset is ever narrowed, this
                  headline needs an explicit margin-top. */}
              <h2 className="ob-h1">Stop hand-holding one chat. Ship 10× in parallel.</h2>
              <p className="ob-lede">
                no_human fields an <em>entire legion of agents at once</em> — many tasks,
                across many repos, simultaneously. Each one is carried from intake
                to an open PR on its own: no babysitting, no re-explaining, no
                steering every step. You just review and approve.
              </p>
              <div className="ob-agents">
                <div className="ob-agent ob-agent-worker">
                  <div className="ob-agent-role">Worker</div>
                  <p>Builds the change, writes the tests, and runs them.</p>
                </div>
                <div className="ob-agent ob-agent-supervisor">
                  <div className="ob-agent-role">Supervisor</div>
                  <p>Watches in real time — enforces your rules, blocks guesses.</p>
                </div>
                <div className="ob-agent ob-agent-reviewer">
                  <div className="ob-agent-role">Reviewer</div>
                  <p>Fresh eyes, told to refute “done.” Pass/fail with evidence.</p>
                </div>
              </div>
              <div className="ob-flow">
                <span>intake</span>
                <span>spec</span>
                <span>plan</span>
                <span>build</span>
                <span>review</span>
                <span className="ob-flow-pr">open PR</span>
                <span className="ob-flow-you">you approve</span>
              </div>
              <p className="ob-note">Let's teach them how <em>you</em> work — about five minutes.</p>
            </Stagger>
          )}

          {step.key === "team" && (
            <Stagger>
              <h2 className="ob-h2">Who are you with?</h2>
              <p className="ob-sub">Used to scope rules to your team.</p>
              <label className="ob-field">
                <span>Team</span>
                <input className="ob-input" value={team} placeholder="e.g. METRICSDB, ANALYTICS-EXPORT"
                       onChange={(e) => setTeam(e.target.value)} autoFocus />
              </label>
            </Stagger>
          )}

          {step.key === "repos" && (
            <Stagger>
              <h2 className="ob-h2">Which repositories do you work on?</h2>
              <div className="ob-row">
                <PathInput value={root} onChange={setRoot} placeholder="scan root, e.g. ~/git" />
                <button className="ob-btn-ghost" disabled={busy}
                        onClick={() => guard(async () => setDetected((await detectRepos(root)).repos || []))}>
                  Re-scan
                </button>
              </div>
              <div className="ob-repolist">
                {detected.length === 0 && <div className="ob-empty">{busy ? <><span className="grill-spinner" style={{ width: 16, height: 16, verticalAlign: 'middle', marginRight: 8 }} />Scanning for git repos…</> : "No git repos found under that path."}</div>}
                {detected.map((r) => {
                  const st = onboarded[r.path];
                  return (
                    <label key={r.path} className={`ob-repo${selectedRepos.has(r.path) ? " sel" : ""}`}>
                      <input type="checkbox" checked={selectedRepos.has(r.path)} onChange={() => toggleRepo(r.path)} />
                      <span className="ob-repo-name">{r.name}</span>
                      {r.ecosystem && <span className="ob-tag">{r.ecosystem}</span>}
                      {st === "busy" && <span className="ob-repo-status"><span className="grill-spinner" style={{ width: 12, height: 12, verticalAlign: 'middle', marginRight: 4 }} />profiling…</span>}
                      {st && st !== "busy" && (
                        <span className="ob-repo-status ok">
                          {st.ecosystem || "?"}{st.test_cmd ? ` · ${st.test_cmd}` : ""} · unproven
                        </span>
                      )}
                    </label>
                  );
                })}
              </div>
              {selectedRepos.size > 0 && (
                <button className="ob-btn" disabled={busy} onClick={onboardSelected}>
                  {busy ? "Profiling…" : `Profile ${selectedRepos.size} repo${selectedRepos.size > 1 ? "s" : ""}`}
                </button>
              )}
              <p className="ob-note">Profiling derives install/test/lint from each repo's own declarations. Proving (running the tests) happens later via <code>nh onboard</code> — never trusted until proven.</p>
            </Stagger>
          )}

          {step.key === "projects" && (
            <Stagger>
              <h2 className="ob-h2">Group repos into projects</h2>
              <p className="ob-sub">A project is a named unit of work — it can span multiple repos. When you create a task, you pick a project.</p>
              <div className="ob-row" style={{ marginBottom: '0.75rem' }}>
                <input className="ob-input" value={newProjName} placeholder="Project name, e.g. metrics-core" onChange={(e) => setNewProjName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addProject()} />
                <button className="ob-btn-ghost" onClick={addProject} disabled={!newProjName.trim()}>+ Add project</button>
              </div>
              {projectDefs.length === 0 && <div className="ob-empty">No projects defined yet. Add at least one to continue.</div>}
              {projectDefs.map((pd, pi) => (
                <div key={pd.name} className="ob-project-card">
                  <div className="ob-project-head">
                    <strong>{pd.name}</strong>
                    <span className="ob-faint">{pd.repos.size} repo{pd.repos.size !== 1 ? "s" : ""}</span>
                    <button className="ob-btn-ghost" style={{ marginLeft: 'auto', fontSize: '0.75rem' }} aria-label={`Remove project ${pd.name}`} onClick={() => removeProject(pi)}>✕</button>
                  </div>
                  <div className="ob-repolist" style={{ maxHeight: '140px' }}>
                    {[...selectedRepos].map((rp) => (
                      <label key={rp} className={`ob-repo${pd.repos.has(rp) ? " sel" : ""}`} style={{ padding: '0.25rem 0.5rem' }}>
                        <input type="checkbox" checked={pd.repos.has(rp)} onChange={() => toggleProjectRepo(pi, rp)} />
                        <span className="ob-repo-name">{rp.split("/").pop()}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ))}
              <p className="ob-note">You can add or edit projects later in Settings. Repos not assigned to any project can still be used via the "other" option when creating a task.</p>
            </Stagger>
          )}

          {step.key === "docs" && (
            <Stagger>
              <h2 className="ob-h2">Any docs the agents should read?</h2>
              <p className="ob-sub">READMEs, ADRs, team handbooks — paths or URLs. Optional.</p>
              <ListEditor items={docs} setItems={setDocs} placeholder="~/git/team/docs or https://…" />
              {selectedRepos.size > 0 && (
                <div style={{ marginTop: "1rem" }}>
                  <h3 className="ob-h2" style={{ fontSize: "0.95rem" }}>Generate docs for your repos</h3>
                  <p className="ob-sub">Auto-generate architecture, modules, and conventions wiki for each repo.</p>
                  {[...selectedRepos].map((rp) => {
                    const st = wikiGen[rp];
                    return (
                      <div key={rp} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                        <code style={{ fontSize: "0.8rem", flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{rp.split("/").slice(-2).join("/")}</code>
                        {st === "busy" ? (
                          <span style={{ fontSize: "0.75rem", color: "var(--fg-dim)" }}>generating…</span>
                        ) : st && st.ok ? (
                          <span style={{ fontSize: "0.75rem", color: "var(--success)" }}>✓ {st.files?.length || 0} files</span>
                        ) : st && st.error ? (
                          <span style={{ fontSize: "0.75rem", color: "var(--danger)" }}>✗ {st.error}</span>
                        ) : (
                          <button className="ob-btn" style={{ padding: "0.2rem 0.6rem", fontSize: "0.75rem" }}
                            disabled={busy}
                            onClick={async () => {
                              setWikiGen((g) => ({ ...g, [rp]: "busy" }));
                              try {
                                const res = await generateDocs(rp);
                                setWikiGen((g) => ({ ...g, [rp]: res }));
                              } catch (e) {
                                setWikiGen((g) => ({ ...g, [rp]: { error: e.message } }));
                              }
                            }}>Generate</button>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Stagger>
          )}

          {step.key === "integrations" && (
            <Stagger>
              <h2 className="ob-h2">Connect your tools <span className="ob-faint">(optional)</span></h2>
              <p className="ob-sub">no_human works with your issue tracker, CI, and notifications. Here's what it detects now — set any of these up later in Settings → Integrations. You can skip this and keep going.</p>
              {integrations === null ? (
                <div className="ob-empty">Checking integrations…</div>
              ) : (
                <div className="ob-integrations">
                  {integrations.map((it) => {
                    const chip = statusChip(it);
                    return (
                      <div key={it.name} className="ob-integration">
                        <span className="ob-integration-mark"><IntegrationIcon name={it.name} size={20} /></span>
                        <span className="ob-integration-name">{NAME_LABEL[it.name] || it.name}</span>
                        <span className="ob-integration-kind">{KIND_LABEL[it.kind] || it.kind}</span>
                        <span className={`integration-chip tone-${chip.tone}`}>{chip.label}</span>
                      </div>
                    );
                  })}
                </div>
              )}
              <p className="ob-note">Configure these anytime in Settings → Integrations — set values in config.yaml and tokens in ~/.no_human/.env.</p>
            </Stagger>
          )}

          {step.key === "history" && (
            <Stagger>
              <h2 className="ob-h2">Learn from your AI history</h2>
              <p className="ob-sub">
                no_human reads your past <strong>Claude Code</strong> and ide-agent/agent-a
                conversations, catalogs your <strong>skills</strong>, and distills the
                corrections you keep repeating — so the Supervisor enforces them for you.
              </p>
              <button className="ob-btn" disabled={scanning} onClick={scanHistory}>
                {scanPhase === "extracting" ? "Reading conversations…"
                  : scanPhase === "analyzing" ? "Distilling rules…"
                  : scanPhase === "done" ? "Re-scan"
                  : "Scan my AI history"}
              </button>

              {/* While the (slow) scan runs, build the validator agent on screen
                  so it never looks stuck. */}
              {scanning && (
                <div className="va-build">
                  <ValidatorAvatar />
                  <div className="va-build-text">
                    <div className="va-build-title">Claude is building your validator agent…</div>
                    <div className="va-build-sub">
                      {scanPhase === "extracting"
                        ? "Booting up — reading your Claude Code & ide-agent conversations…"
                        : <>Generalizing the lessons from <strong>{(history?.messages || 0).toLocaleString()}</strong> messages
                           across <strong>{history?.transcripts || 0}</strong> conversations. This can take up to a minute.</>}
                    </div>
                  </div>
                </div>
              )}

              {scanPhase === "unavailable" && history && (
                <div className="ob-callout">No running ide-agent/agent-a IDE detected — you can skip this and add rules later. <span className="ob-faint">({history.detail})</span></div>
              )}
              {scanPhase === "done" && history && (
                <div className="ob-callout ok">
                  Scanned <strong>{history.transcripts}</strong> conversations
                  {history.messages ? <> (<strong>{history.messages.toLocaleString()}</strong> messages)</> : null}
                  {history.sources?.claude_code ? <> · <strong>{history.sources.claude_code}</strong> from Claude Code</> : null} →{" "}
                  <strong>{proposals.length}</strong> item{proposals.length === 1 ? "" : "s"} to review
                  {history.skills ? <> (incl. <strong>{history.skills}</strong> skill{history.skills === 1 ? "" : "s"})</> : null}.
                  <div className="ob-faint" style={{ marginTop: 6 }}>
                    {proposals.length === 0
                      ? "No durable rules stood out in these conversations — you can add rules anytime in Settings."
                      : "Rules are generalized from your corrections (not copied verbatim) and labelled by importance. Refine or add more anytime in Settings."}
                  </div>
                </div>
              )}
            </Stagger>
          )}

          {step.key === "rules" && (
            <Stagger>
              <h2 className="ob-h2">Review the rules we found</h2>
              <p className="ob-sub">Confirmed rules become standing guidance the Supervisor enforces. Nothing is active until you confirm it.</p>
              {proposals.length === 0 ? (
                <div className="ob-empty">No proposals — nothing extracted, or you skipped the scan. You can add rules anytime in Settings.</div>
              ) : (
                <div className="ob-rules">
                  {proposals.map((p) => (
                    <label key={p.id} className={`ob-rule${chosenRules.has(p.id) ? " sel" : ""}`}>
                      <input type="checkbox" checked={chosenRules.has(p.id)} onChange={() => toggleRule(p.id)} />
                      <div>
                        <div className="ob-rule-head">
                          <span className={`ob-rule-cat cat-${p.category}`}>{p.category}</span>
                          {p.importance && <span className={`ob-rule-imp imp-${p.importance}`}>{p.importance}</span>}
                        </div>
                        <div className="ob-rule-text">{p.content}</div>
                      </div>
                    </label>
                  ))}
                </div>
              )}
            </Stagger>
          )}

          {step.key === "summary" && (
            <Stagger>
              <h2 className="ob-h2">Ready.</h2>
              <ul className="ob-summary">
                <li><span>Team</span><b>{team || "—"}</b></li>
                <li><span>Projects</span><b>{projectDefs.length}</b></li>
                <li><span>Repos</span><b>{selectedRepos.size}</b></li>
                <li><span>Docs</span><b>{docs.filter((d) => d.trim()).length}</b></li>
                <li><span>Rules confirmed</span><b>{chosenRules.size}</b></li>
              </ul>
              <p className="ob-note">You can change any of this later in Settings.</p>
            </Stagger>
          )}

          {err && <div className="ob-error">{err}</div>}
        </div>

        <div className="ob-nav">
          <button className="ob-btn-ghost" onClick={back} disabled={i === 0 || busy}>Back</button>
          <div className="ob-nav-spacer" />
          {i < STEPS.length - 1 ? (
            <button className="ob-btn" onClick={next} disabled={busy}>Continue</button>
          ) : (
            <button className="ob-btn ob-btn-go" onClick={finish} disabled={busy}>
              {busy ? "Launching…" : "Enter no_human"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Stagger({ children }) {
  return (
    <div className="ob-stagger">
      {Array.isArray(children)
        ? children.map((c, idx) => <div key={idx} className="ob-rise" style={{ animationDelay: `${idx * 60}ms` }}>{c}</div>)
        : children}
    </div>
  );
}

function ListEditor({ items, setItems, placeholder }) {
  return (
    <div className="ob-listedit">
      {items.map((v, idx) => (
        <div className="ob-row" key={idx}>
          <input
            className="ob-input"
            value={v}
            placeholder={placeholder}
            onChange={(e) => setItems(items.map((x, k) => (k === idx ? e.target.value : x)))}
          />
          {items.length > 1 && (
            <button className="ob-btn-ghost" aria-label={v.trim() ? `Remove ${v.trim()}` : "Remove this entry"} onClick={() => setItems(items.filter((_, k) => k !== idx))}>✕</button>
          )}
        </div>
      ))}
      <button className="ob-btn-ghost ob-add" onClick={() => setItems([...items, ""])}>+ add another</button>
    </div>
  );
}
