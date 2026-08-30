// Task 7: the composer seed for "Follow up" on a done/failed task — the
// drawer's action bar button opens the New Task composer with this as
// `initial`. `followsId` is not user-editable; it rides through
// TaskComposer's onStart -> App.jsx's fields -> the create-task POST body
// untouched (see TaskComposer.jsx's handleSubmit and App.jsx's NewTaskModal
// handleSubmit).
export function followUpSeed(task) {
  const lines = [`Follow-up to ${task.id.slice(0, 8)}: ${task.title}`];
  if (task.pr_url) lines.push(`Previous PR: ${task.pr_url}`);
  lines.push("", "What to change now:", "");
  return {
    prompt: lines.join("\n"),
    kind: task.kind || "feature",
    repoPath: task.repo_path,
    followsId: task.id,
  };
}
