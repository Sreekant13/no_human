// FastAPI's error body is not one shape. `HTTPException(detail="…")` sends a
// STRING, but a 422 request-validation failure sends a LIST of error objects:
//   {"detail": [{"type": "missing", "loc": ["body", "repo_path"], "msg": "Field required"}]}
// Every caller in api.js did `new Error(body.detail || fallback)`, which turns
// that list into the literal text "[object Object]" — so the one error class
// that names the offending field was the one class the operator could not read.
//
// Strings pass through untouched. A list is flattened to "field: message" pairs.
// Anything else object-shaped falls back to JSON rather than "[object Object]".
// Nothing usable → the caller's own status-code fallback.

function fieldPath(loc) {
  if (!Array.isArray(loc)) return "";
  // "body" / "query" tell a human nothing about which input was wrong.
  return loc.filter((p) => p !== "body" && p !== "query").join(".");
}

export function detailMessage(body, fallback) {
  const d = body?.detail;
  if (typeof d === "string") return d.trim() || fallback;
  if (Array.isArray(d)) {
    const parts = d
      .map((e) => {
        if (typeof e === "string") return e.trim();
        const where = fieldPath(e?.loc);
        const msg = String(e?.msg ?? "").trim();
        if (where && msg) return `${where}: ${msg}`;
        return where || msg;
      })
      .filter(Boolean);
    return parts.length ? parts.join("; ") : fallback;
  }
  if (d && typeof d === "object") {
    try {
      const s = JSON.stringify(d);
      if (s && s !== "{}" && s !== "[]") return s;
    } catch {
      // Circular or otherwise unserialisable — fall through to the fallback.
    }
  }
  return fallback;
}
