import { useState, useRef, useEffect } from "react";
import SlideOver from "./SlideOver.jsx";
import { groupFailedByTitle } from "./boardGroups.js";
import { LANES, routeTask } from "./boardLanes.js";

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
        ) : lane.key === "failed" ? (
          // U4: same-title failures collapse to the newest + a count — one
          // stubborn task retried five ways must not bury the board.
          groupFailedByTitle(tasks).map(({ task, collapsedCount }) => (
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
          tasks.map((task) => (
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

  let cardCls = "task-card";
  if (isAwaiting) cardCls += " awaiting";
  if (isStale) cardCls += " stale";
  if (isActive) cardCls += " active-working";

  return (
    <div
      className={cardCls}
      style={{ "--lane-accent": accent }}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onClick(e)}
    >
      {isActive && <div className="card-active-pulse" title="agent is working on this" />}
      <div className="card-id">{task.id.slice(0, 8)}</div>
      <div className="card-title">{task.title}</div>
      {task.live_status && isActive && (
        <div className="card-live-status">{task.live_status}</div>
      )}
      {task.subtask_progress && (
        <div className="card-subtask-progress">sub-tasks {task.subtask_progress}</div>
      )}
      {task.description_short && !task.live_status && (
        <div className="card-description">{task.description_short}</div>
      )}
      {task.blocker_question && isAwaiting && (
        <div className="card-blocker-q">{task.blocker_question}</div>
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
            att {task.attempt_count}{task.last_turns != null ? ` · ${task.last_turns}t` : ""}{task.total_tokens > 0 ? ` · ${task.total_tokens >= 1000000 ? `${(task.total_tokens/1000000).toFixed(1)}M` : task.total_tokens >= 1000 ? `${(task.total_tokens/1000).toFixed(0)}k` : task.total_tokens} tok` : ""}
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
