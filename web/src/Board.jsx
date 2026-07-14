import { useState, useRef, useEffect } from "react";
import { fmtTokens, totalBurn } from "./cost.js";
import SlideOver from "./SlideOver.jsx";
import { groupFailedByTitle } from "./boardGroups.js";
import { LANES, routeTask, isWaiting, isRealFailure } from "./boardLanes.js";
import { taskProgress } from "./taskProgress.js";
import { topPrioritised } from "./laneView.js";

// 5B: how many cards a collapsible lane shows before the expand arrow. 4 keeps
// every lane visible without vertical scroll on a typical viewport; the count
// badge still shows the true total, so nothing is hidden from awareness.
const LANE_TOP_N = 4;

export default function Board({ tasks }) {
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
        {LANES.map((lane) => (
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

function Lane({ lane, tasks, onSelect }) {
  const [expanded, setExpanded] = useState(false);
  // Human-gate lanes (Needs Answer, Review PR) NEVER collapse — every task there
  // needs action; hiding one behind an arrow defeats the board. Only the
  // in-flight/outcome lanes (Working, Failed, Done), which grow unbounded, do.
  const collapsible = !lane.needsYou;
  const isFailed = lane.key === "failed";
  // U4: the failed lane collapses same-title dupes first; top-N composes with it
  // by operating on the GROUPED rows (not raw tasks), so counts never double.
  const rows = isFailed ? groupFailedByTitle(tasks) : tasks;

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
    const tsOf = isFailed
      ? (g) => g.task.last_activity || g.task.updated_at || g.task.created_at || ""
      : undefined;
    // A cancelled task also ends in `failed` status, so a run of cancels would push the
    // one REAL failure — the only row that wants attention — behind "Show N more".
    const isPriority = isFailed ? (g) => isRealFailure(g.task) : null;
    const r = topPrioritised(rows, expanded ? rows.length : LANE_TOP_N, tsOf, isPriority);
    visible = r.visible;
    hiddenCount = r.hiddenCount;
  }
  const showToggle =
    collapsible && (hiddenCount > 0 || (expanded && rows.length > LANE_TOP_N));

  return (
    <div className={`lane lane-${lane.key}${lane.loud ? " lane-loud" : ""}${tasks.length > 0 ? " lane-has-tasks" : ""}`}>
      <div className="lane-header">
        <div className="lane-dot" style={{ background: lane.accent }} />
        <div className="lane-title">{lane.label}</div>
        {tasks.length > 0 && <div className="lane-count">{tasks.length}</div>}
      </div>
      <div className="lane-body">
        {tasks.length === 0 ? (
          <div className={`lane-empty${lane.needsYou ? " lane-empty-clear" : ""}`}>
            <span className="lane-empty-icon" aria-hidden="true">{lane.emptyIcon || "·"}</span>
            <span className="lane-empty-text">{lane.emptyHint || ""}</span>
          </div>
        ) : isFailed ? (
          // U4: same-title failures collapse to the newest + a count — one
          // stubborn task retried five ways must not bury the board.
          visible.map(({ task, collapsedCount }) => (
            <div key={task.id} className="failed-group">
              <TaskCard
                task={task}
                accent={lane.accent}
                isAwaiting={!!lane.needsYou}
                showSubStatus={lane.showSubStatus}
                onClick={(e) => onSelect(task.id, e.currentTarget)}
              />
              {collapsedCount > 0 && (
                <div className="failed-group-count" title="Older failed runs with this title — open the card to see attempts">
                  +{collapsedCount} older with this title
                </div>
              )}
            </div>
          ))
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
const ACTIVE_STATUSES = new Set(["context", "planning", "implementing", "reviewing", "testing"]);

// Human-readable action hint for "Needs You" tasks
function actionHint(task) {
  if (task.status === "awaiting_approval") return "review & approve PR";
  if (task.status === "awaiting_input") return "answer question";
  if (task.status === "escalated") return "advise or split task";
  if (task.status === "blocked") return "answer question";
  return null;
}

function TaskCard({ task, accent, isAwaiting, showSubStatus, onClick }) {
  const activityTs = task.last_activity || task.updated_at || task.created_at;
  const ageMs = Date.now() - new Date(activityTs).getTime();
  const ageSec = ageMs / 1000;
  const age = relativeTime(activityTs);
  const isStale = STALE_STATUSES.has(task.status) && ageSec > STALE_THRESHOLD_S;
  const priority = task.priority ?? "medium";

  const isActive = ACTIVE_STATUSES.has(task.status);
  const waiting = isWaiting(task);

  let cardCls = "task-card";
  if (isAwaiting) cardCls += " awaiting";
  if (isStale) cardCls += " stale";
  if (isActive) cardCls += " active-working";
  if (waiting) cardCls += " waiting-parked";

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
      {isActive && <div className="card-active-pulse" title="agent is working on this" />}
      {waiting && (
        <div className="card-waiting-tag" title={task.blocker_wake_condition || "will resume on its own"}>
          ◷ {task.status === "paused_quota" ? "waits for quota" : "waits for its own signal"}
        </div>
      )}
      <div className="card-id">{task.id.slice(0, 8)}</div>
      <div className="card-title">{task.title}</div>
      {task.live_status && isActive && (
        <div className="card-live-status">{task.live_status}</div>
      )}
      {task.subtask_progress && (
        <div className="card-subtask-progress">sub-tasks {task.subtask_progress}</div>
      )}
      {isActive && taskProgress(task.status) != null && (
        <div
          className="card-progress"
          title={`~${taskProgress(task.status)}% through the pipeline (${task.status})`}
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
        <div className="card-blocker-q">
          {/* The clamp lives on an inner span: `overflow:hidden` clips at the PADDING
              box, so a padded clamped box lets the next line show through its bottom
              padding as a sliced sliver of text. */}
          <span>{task.blocker_question}</span>
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
        {task.attempt_count > 0 && (
          <span className="card-attempts">
            att {task.attempt_count}{task.last_turns != null ? ` · ${task.last_turns}t` : ""}{totalBurn(task.total_tokens, task.total_cache_read) > 0 ? ` · ${fmtTokens(totalBurn(task.total_tokens, task.total_cache_read))} tok` : ""}
          </span>
        )}
        {priority === "high" && <span className="card-priority card-priority-high">HI</span>}
        {priority === "low"  && <span className="card-priority card-priority-low">LO</span>}
        {task.pr_url && (
          task.pr_url.startsWith("http") ? (
            <a
              className="card-pr-badge"
              href={task.pr_url}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
            >PR</a>
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
