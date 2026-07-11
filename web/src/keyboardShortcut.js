// A global 'n' opens New Task, but only when the user isn't typing and no modal
// is already up. Decision kept pure so it's node --test'd without a DOM.
const EDITABLE_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);

export function shouldTriggerNewTask(event, { modalOpen } = {}) {
  if (!event || event.key !== "n") return false;
  if (event.ctrlKey || event.metaKey || event.altKey) return false;
  if (modalOpen) return false;
  const t = event.target;
  if (t) {
    if (EDITABLE_TAGS.has(t.tagName)) return false;
    if (t.isContentEditable) return false;
  }
  return true;
}
