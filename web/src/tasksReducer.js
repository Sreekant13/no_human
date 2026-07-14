// The board's task list, kept in one place so it can be node --test'd.
//
// `sync` used to MERGE the incoming list into the existing one, so a task removed server-side
// (deleted, or filtered out of the board query) lingered on the board until a full reload. The
// WebSocket broadcast always carries the COMPLETE board list (api/app.py: every broadcast sends
// `tasks: [...]` from _board_tasks), so a merge can only ever add — never remove.
export function tasksReducer(state, action) {
  switch (action.type) {
    case "set":
      return action.tasks;
    case "sync":
      // The payload is a full snapshot: adopt it wholesale.
      return action.tasks;
    default:
      return state;
  }
}
