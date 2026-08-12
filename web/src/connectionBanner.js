// Pure view-model for the stale-data banner (incident 2026-08-12): a socket
// that is open but hasn't yet delivered a fresh snapshot is exactly as stale
// as one that is closed, so the banner is driven by the reconnector's phase,
// not by a raw boolean "connected". `role: "status"`, never `alert(` — the
// incident was a SILENT staleness, not something that needs a modal.
export function connectionBanner(phase) {
  switch (phase) {
    case "live":
      return null;
    case "connecting":
    case "disconnected":
      return {
        text: "Disconnected — data may be stale",
        className: "nh-stale-banner",
        role: "status",
      };
    case "resyncing":
      return {
        text: "Reconnected — resyncing board…",
        className: "nh-stale-banner nh-stale-banner-sync",
        role: "status",
      };
    case "sync-failed":
      return {
        text: "Connection restored but data sync failed — retrying",
        className: "nh-stale-banner nh-stale-banner-error",
        role: "status",
      };
    default:
      return null;
  }
}
