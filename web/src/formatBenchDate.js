// Short human bench date ("Jul 20, 2026") from an ISO string. Pure; node --test'd.
export function formatBenchDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso; // fallback to raw, never "Invalid Date"
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
