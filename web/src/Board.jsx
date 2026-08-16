import { useState, useRef, useEffect } from "react";
import { fmtTokens, taskBurn } from "./cost.js";
import SlideOver from "./SlideOver.jsx";
import { BOARD_LANES, routeTask, isWaiting, cardActivity } from "./boardLanes.js";
import { taskProgress } from "./taskProgress.js";
import { topPrioritised } from "./laneView.js";
import { partitionAnswerLane, shouldResetStaleOpen } from "./answerLane.js";
import { showConflictBadge, conflictRoundLabel } from "./conflictStatus.js";
import { cardBlockerLine } from "./cardBlockerLine.js";
import { escalationLatencyLine } from "./escalationLatency.js";
import { httpPrUrl } from "./prUrl.js";

// 5B: how many cards a collapsible lane shows before the expand arrow. 4 keeps
// every lane visible without vertical scroll on a typical viewport; the count
// badge still shows the true total, so nothing is hidden from awareness.
const LANE_TOP_N = 4;

export default function Board({ tasks, pendingOpenId, onPendingOpenHandled }) {
  const [selectedId, setSelectedId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const triggerRef = useRef(null);
  const prevUpdatedAtRef = useRef(null);

  // Re-fetch the SlideOver whenever the selected task's updated_at changes via WS
  useEffect(() => {
    if (!selectedId) return;
    const selected = tasks.find((t) => t.id === selectedId);
    const stamp = selected?.updated_at ?? null;
    if (stamp && stamp !== prevUpdatedAtRef.current) {
      prevUpdatedAtRef.current = stamp;
      setRefreshKey((k) => k + 1);
    }
  }, [tasks, selectedId]);

  function openTask(id, domNode) {
    triggerRef.current = domNode;
    prevUpdatedAtRef.current = null; // reset so first open always fetches
    setSelectedId(id);
  }

  // A clicked notification lands here even when Board wasn't mounted at
  // click time: App parks the task id in state; this opens it on mount (or
  // immediately when already mounted) and hands the id back as consumed.
  useEffect(() => {
    if (pendingOpenId) {
      openTask(pendingOpenId, null);
      onPendingOpenHandled?.();
    }
  }, [pendingOpenId]);

  function closeTask() {
    setSelectedId(null);
    prevUpdatedAtRef.current = null;
    // Restore focus to the card that triggered the panel
    triggerRef.current?.focus();
    triggerRef.current = null;
  }

  return (
    <>
      <div className="nh-board">
        {/* 5D: only the GATE lanes. Done and Failed are outcomes — they live behind the two
            buttons above the connection indicator, which open the task table. */}
        {BOARD_LANES.map((lane) => (
          <Lane
            key={lane.key}
            lane={lane}
            tasks={tasks.filter((t) => routeTask(t) === lane.key)}
            onSelect={openTask}
          />
        ))}
      </div>
      {selectedId && (
        <SlideOver
          // Remount on a task switch. Without this the drawer keeps the PREVIOUS task's
          // `task`/`diff` state while already bound to the new id, so "Next review →" showed
          // task A's header, id and diff with an enabled Approve that posted against B.
          // (A key change only fires on selectedId — a refreshKey bump still re-renders in
          // place, so a WS update never flashes an empty drawer.)
          key={selectedId}
          taskId={selectedId}
          onClose={closeTask}
          refreshKey={refreshKey}
          reviewQueue={tasks
            .filter((t) => t.status === "awaiting_approval")
            .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""))
            .map((t) => t.id)}
          onJump={(id) => {
            prevUpdatedAtRef.current = null;
            setSelectedId(id);
          }}
        />
      )}
    </>
  );
}

// SCRUM-15: the Working lane header must agree with the card treatment below
// it — "N live · M queued" only ever comes from cardActivity's own mode, so
// the header cannot drift from what the cards actually render.
function workingBreakdown(tasks) {
  let running = 0;
  let queued = 0;
  for (const task of tasks) {
    const mode = cardActivity(task).mode;
    if (mode === "running") running += 1;
    else if (mode === "queued") queued += 1;
  }
  if (queued === 0) return String(tasks.length);
  // Review 2026-07-25: waiting tasks (paused_quota etc.) are neither running
  // nor queued — omitting them made the header count fewer tasks than the
  // cards below it whenever a queue formed.
  const waiting = tasks.length - running - queued;
  const base = `${running} live · ${queued} queued`;
  return waiting > 0 ? `${base} · ${waiting} waiting` : base;
}

function Lane({ lane, tasks, onSelect }) {
  const [expanded, setExpanded] = useState(false);
  // SCRUM-19: the Needs-Answer lane's OWN collapse — stale escalations (>24h,
  // by escalation recency) sink behind an expandable divider instead of
  // burying tonight's real ones. Independent of `collapsible` below: this
  // lane still shows every fresh card, never hides one behind a top-N arrow.
  const [staleOpen, setStaleOpen] = useState(false);
  // Human-gate lanes (Needs Answer, Review PR) NEVER collapse — every task there
  // needs action; hiding one behind an arrow defeats the board. Only the
  // in-flight/outcome lanes (Working, Failed, Done), which grow unbounded, do.
  const collapsible = !lane.needsYou;
  // 5D: the Failed lane left the board, and with it the same-title collapse — that graveyard
  // problem now lives on the Failed OUTCOME table, which is where the failures are.
  const rows = tasks;

  // Drop a stale expand once the lane fits within top-N: the Working lane churns
  // (tasks drain then a new batch arrives), and a lingering expanded=true would
  // re-inflate the re-grown lane the user never expanded. Resetting while it fits
  // is invisible (all rows already show) and re-growth then starts collapsed.
  useEffect(() => {
    if (expanded && rows.length <= LANE_TOP_N) setExpanded(false);
  }, [expanded, rows.length]);

  let visible = rows;
  let hiddenCount = 0;
  if (collapsible) {
    const r = topPrioritised(rows, expanded ? rows.length : LANE_TOP_N);
    visible = r.visible;
    hiddenCount = r.hiddenCount;
  }
  const showToggle =
    collapsible && (hiddenCount > 0 || (expanded && rows.length > LANE_TOP_N));

  // Never dismisses or filters anything — fresh.length + stale.length === rows.length,
  // same truthful lane-count badge below (tasks.length, unchanged).
  const { fresh, stale } = lane.staleCollapse
    ? partitionAnswerLane(rows, Date.now())
    : { fresh: rows, stale: [] };

  // Reset the stale-divider expansion once the stale group empties: a lingering
  // staleOpen=true would render the NEXT stale card pre-expanded, an expansion the
  // user never performed. Resetting only at length 0 is invisible (nothing shown).
  useEffect(() => {
    if (shouldResetStaleOpen(staleOpen, stale.length)) setStaleOpen(false);
  }, [staleOpen, stale.length]);

  return (
    <div className={`lane lane-${lane.key}${lane.loud ? " lane-loud" : ""}${tasks.length > 0 ? " lane-has-tasks" : ""}`}>
      <div className="lane-header">
        <div className="lane-dot" style={{ background: lane.accent }} />
        <div className="lane-title">{lane.label}</div>
        {tasks.length > 0 && (
          <div className="lane-count">
            {lane.key === "working" ? workingBreakdown(tasks) : tasks.length}
          </div>
        )}
      </div>
      <div className="lane-body">
        {tasks.length === 0 ? (
          <div className={`lane-empty${lane.needsYou ? " lane-empty-clear" : ""}`}>
            <span className="lane-empty-icon" aria-hidden="true">{lane.emptyIcon || "·"}</span>
            <span className="lane-empty-text">{lane.emptyHint || ""}</span>
          </div>
        ) : lane.staleCollapse ? (
          <>
            {fresh.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                accent={lane.accent}
                isAwaiting={!!lane.needsYou}
                showSubStatus={lane.showSubStatus}
                onClick={(e) => onSelect(task.id, e.currentTarget)}
              />
            ))}
            {stale.length > 0 && (
              <button
                type="button"
                className={`lane-stale-divider${staleOpen ? " lane-more-open" : ""}`}
                aria-expanded={staleOpen}
                aria-label={staleOpen ? `Hide ${stale.length} older need answer${stale.length > 1 ? "s" : ""}` : `Show ${stale.length} older need answer${stale.length > 1 ? "s" : ""}`}
                onClick={() => setStaleOpen((v) => !v)}
              >
                <span className="lane-more-text">
                  {stale.length} older need answer{stale.length > 1 ? "s" : ""}
                </span>
                <span className="lane-more-arrow" aria-hidden="true">▾</span>
              </button>
            )}
            {staleOpen && stale.map((task) => (
              <TaskCard
                key={task.id}
                task={task}
                accent={lane.accent}
                isAwaiting={!!lane.needsYou}
                showSubStatus={lane.showSubStatus}
                staleAnswer
                onClick={(e) => onSelect(task.id, e.currentTarget)}
              />
            ))}
          </>
        ) : (
          visible.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              accent={lane.accent}
              isAwaiting={!!lane.needsYou}
              showSubStatus={lane.showSubStatus}
              onClick={(e) => onSelect(task.id, e.currentTarget)}
            />
          ))
        )}
        {showToggle && (
          <button
            type="button"
            className={`lane-more${expanded ? " lane-more-open" : ""}`}
            aria-expanded={expanded}
            aria-label={expanded ? `Show fewer in ${lane.label}` : `Show ${hiddenCount} more in ${lane.label}`}
            onClick={() => setExpanded((v) => !v)}
          >
            <span className="lane-more-text">
              {expanded ? "Show fewer" : `Show ${hiddenCount} more`}
            </span>
            <span className="lane-more-arrow" aria-hidden="true">▾</span>
          </button>
        )}
      </div>
    </div>
  );
}

const STALE_STATUSES = new Set(["context", "planning", "implementing", "reviewing", "testing", "awaiting_approval", "awaiting_input", "blocked"]);
const STALE_THRESHOLD_S = 16 * 3600;

// Human-readable action hint for "Needs You" tasks
function actionHint(task) {
  // B2 #19: approved-but-unmerged is no longer YOUR move — say so instead of
  // asking for a review that already happened.
  if (task.approved_at) return "approved — merge pending";
  // A human who explicitly stopped this parked task already gave their
  // answer — checked before the status branches so it wins regardless of
  // which parked status (awaiting_input/blocked/escalated) the task is in.
  if (task.blocker_human_stopped) return "stopped by you — parked";
  if (task.status === "awaiting_approval") return "review & approve PR";
  if (task.status === "awaiting_input") return "answer question";
  if (task.status === "escalated") return "advise or split task";
  if (task.status === "blocked") return "answer question";
  return null;
}

function TaskCard({ task, accent, isAwaiting, showSubStatus, staleAnswer, onClick }) {
  const activityTs = task.last_activity || task.updated_at || task.created_at;
  const ageMs = Date.now() - new Date(activityTs).getTime();
  const ageSec = ageMs / 1000;
  const age = relativeTime(activityTs);
  const isStale = STALE_STATUSES.has(task.status) && ageSec > STALE_THRESHOLD_S;
  const priority = task.priority ?? "medium";
  const burn = taskBurn(task);

  // SCRUM-15: the live pulse/progress must reflect the scheduler's actual claim,
  // not merely an "active" status — an unclaimed active-status task is queued,
  // and a queued task must never render as "agent is working on this".
  const activity = cardActivity(task);
  const isRunningNow = activity.mode === "running";
  const isQueuedNow = activity.mode === "queued";
  const waiting = isWaiting(task);

  // ph-no-capture: the ENTIRE card is operator content (title, live status,
  // description, blocker question, repo name, PR URL) — masked wholesale
  // from session replay rather than per-field.
  let cardCls = "task-card ph-no-capture";
  if (isAwaiting) cardCls += " awaiting";
  if (isStale) cardCls += " stale";
  if (isRunningNow) cardCls += " active-working";
  if (isQueuedNow) cardCls += " card-queued";
  if (waiting) cardCls += " waiting-parked";
  // Distinct concern from the in-flight `.stale` treatment above (STALE_STATUSES,
  // 16h): this is the escalation-age muting for the collapsed Needs-Answer divider.
  if (staleAnswer) cardCls += " answer-stale";

  return (
    <div
      className={cardCls}
      style={{ "--lane-accent": accent }}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        // Only when the CARD itself is focused: keydown from the PR-link descendant
        // bubbles here, and preventDefault would cancel the anchor's own activation —
        // Enter on a focused PR link would open the drawer instead of the PR.
        if (e.target !== e.currentTarget) return;
        // preventDefault is load-bearing: without it, Enter opens the drawer, the
        // drawer autofocuses its close button in the same event flush, and Enter's
        // default activation then CLICKS that button — the drawer opened and shut
        // within one keypress. (It also stops Space from scrolling the lane.)
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick(e);
        }
      }}
    >
      {isRunningNow && <div className="card-active-pulse" title="agent is working on this" />}
      {isQueuedNow && (
        <div className="card-queued-tag" title="active but not yet picked up by a worker">
          queued
        </div>
      )}
      {waiting && (
        <div className="card-waiting-tag" title={task.blocker_wake_condition || "will resume on its own"}>
          ◷ {task.status === "paused_quota" ? "waits for quota" : "waits for its own signal"}
        </div>
      )}
      <div className="card-title-row">
        <div className="card-title">{task.title}</div>
        <div className="card-id" title="task id — use it with `nh <id>`">{task.id.slice(0, 8)}</div>
      </div>
      {task.live_status && isRunningNow && (
        <div className="card-live-status">{task.live_status}</div>
      )}
      {task.subtask_progress && (
        <div className="card-subtask-progress">sub-tasks {task.subtask_progress}</div>
      )}
      {(isRunningNow || isQueuedNow) && taskProgress(task.status) != null && (
        <div
          className={`card-progress${activity.mutedProgress ? " is-queued" : ""}`}
          title={`~${taskProgress(task.status)}% through the pipeline (${task.status}${isQueuedNow ? ", queued" : ""})`}
          role="progressbar"
          aria-valuenow={taskProgress(task.status)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <div className="card-progress-fill"
               style={{ width: `${taskProgress(task.status)}%` }} />
        </div>
      )}
      {task.description_short && !task.live_status && (
        <div className="card-description">{task.description_short}</div>
      )}
      {task.blocker_question && isAwaiting && (
        <div className="card-blocker-q" title={task.blocker_question}>
          {/* The clamp lives on an inner span: `overflow:hidden` clips at the PADDING
              box, so a padded clamped box lets the next line show through its bottom
              padding as a sliced sliver of text.
              cardBlockerLine leads with the number that differs between two budget
              blockers — clamped to two lines they were otherwise byte-identical
              paragraphs. The untouched sentence stays available on hover and in
              the drawer. */}
          <span>{cardBlockerLine(task.blocker_question)}</span>
        </div>
      )}
      {escalationLatencyLine(task) && (
        <div className="card-escalation-latency" title="how long it took this run to reach a human">
          <span aria-hidden="true">↻</span> {escalationLatencyLine(task)}
        </div>
      )}
      {isAwaiting && actionHint(task) && (
        <div className="card-action-hint">{actionHint(task)}</div>
      )}
      <div className="card-meta">
        {task.repo_name && <span className="card-repo">{task.repo_name}</span>}
        <span className="card-source">{task.source}</span>
        {task.kind && task.kind !== "feature" && (
          <span className={`card-kind kind-${task.kind}`}>{task.kind}</span>
        )}
        {task.has_spec && <span className="card-spec-badge">spec</span>}
        {task.cancelled && <span className="card-cancelled-badge">cancelled</span>}
        {showSubStatus && (
          <span className={`card-substatus substatus-${task.status}`}>{task.status}</span>
        )}
        {showConflictBadge(task) && (
          <span className="card-conflict-badge" title="A sibling PR merged first; the coder is rebasing this branch onto main">
            {conflictRoundLabel(task)}
          </span>
        )}
        {task.attempt_count > 0 && (
          <span className="card-attempts">
            att {task.attempt_count}{task.last_turns != null ? ` · ${task.last_turns}t` : ""}{burn > 0 ? ` · ${fmtTokens(burn)} tok` : ""}
          </span>
        )}
        {priority === "high" && <span className="card-priority card-priority-high">HI</span>}
        {priority === "low"  && <span className="card-priority card-priority-low">LO</span>}
        {task.pr_url && (
          httpPrUrl(task.pr_url) ? (
            <a
              className="card-pr-badge"
              href={task.pr_url}
              target="_blank"
              rel="noreferrer noopener"
              title="open the pull request in a new tab"
              onClick={(e) => e.stopPropagation()}
            >View PR ↗</a>
          ) : (
            <span className="card-pr-badge">PR</span>
          )
        )}
        <span className="card-age">{age}</span>
      </div>
    </div>
  );
}

function relativeTime(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "<1m";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}
