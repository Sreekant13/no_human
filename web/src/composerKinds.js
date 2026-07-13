/**
 * The task-kind chips the composer offers.
 *
 * This mirrors the kind `<select>` the modal shipped with. The authoritative enum
 * (`src/no_human/intake/classify.py` TaskKind) also carries `traceability`, a
 * specialised TRACKER/TNTC flow the UI has never offered — it is deliberately absent
 * here rather than silently introduced.
 */

export const COMPOSER_KINDS = [
  { kind: "feature", label: "Feature", hint: "Implement new behaviour" },
  { kind: "bugfix", label: "Bug fix", hint: "Fix a defect or regression" },
  { kind: "ci_fix", label: "CI fix", hint: "Make a red build green" },
  { kind: "test_gap", label: "Test gap", hint: "Add missing coverage" },
  { kind: "investigation", label: "Investigation", hint: "Root-cause report — read-only, no PR" },
  { kind: "design_doc", label: "Design doc", hint: "Write a cited design document" },
  {
    kind: "code_review",
    label: "Code review",
    hint: "Review someone else's PR — needs its URL",
    // The backend extracts the PR/MR ref from title+description and fails the task
    // without one, so the composer collects and validates it up front.
    needsPrUrl: true,
  },
];

/** The chip for `kind`, or undefined if the composer does not offer it. */
export function kindByValue(kind) {
  if (!kind) return undefined;
  return COMPOSER_KINDS.find((c) => c.kind === kind);
}

/** True when `kind` cannot run without a PR/MR reference. */
export function needsPrUrl(kind) {
  return kindByValue(kind)?.needsPrUrl === true;
}
