export function cardTitle(task) {
  return (task && task.title_short) || (task && task.title) || "";
}
