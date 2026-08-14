// P1 brain hygiene: the honest footer label for a quarantined-row count.
// Renders a COUNT only — never a row title, a matched term, or any other
// row content. See src/no_human/learning/provenance.py for what sets the
// count this label describes.
import { pluralize } from "./pluralize.js";

export function quarantineFooterLabel(count) {
  const n = Number(count) || 0;
  if (n <= 0) return "";
  return `${n} ${pluralize(n, "row", "rows")} quarantined (hidden)`;
}
