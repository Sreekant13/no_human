// A blocker's answer options, in every shape the API can hand us.
//
// Options used to be plain strings. They are objects now — `{label, action}` —
// so that "raise the limit for this task" can carry the change that makes it
// true. Three shapes coexist and always will:
//
//   - objects, from any blocker raised after the change;
//   - bare strings, from rows written before it, and from every agent-raised
//     blocker (the agent proposes labels; it is never allowed to attach an
//     action, or it could resolve a blocker by weakening its own gate);
//   - junk, because this crosses a JSON boundary.
//
// Rendering an object where React expects a string throws and blanks the panel,
// which is precisely the screen a human reaches for when a task is stuck.

export function normalizeOption(option) {
  if (option && typeof option === "object") {
    return {
      label: String(option.label ?? ""),
      action: option.action ?? null,
    };
  }
  return { label: String(option ?? ""), action: null };
}

export function normalizeOptions(options) {
  return Array.isArray(options) ? options.map(normalizeOption) : [];
}

// An option with an action changes task state when chosen; one without just
// submits its label as the free-text answer. The board should say which.
export function hasAction(option) {
  return Boolean(normalizeOption(option).action);
}
