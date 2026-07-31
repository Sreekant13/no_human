// What the Settings > Updates panel says, as a pure function.
//
// The board runs in two places — inside the desktop shell, where
// window.nhDesktop exists and updates are actionable, and in a plain browser,
// where the page is served by a locally installed `nh` and the only meaningful
// upgrade path is pip. Those are genuinely different products to the user, and
// conflating them produces a "Download update" button that cannot work.
//
// This is out of the JSX for the same reason drainChip.js is: the interesting
// part is the decision, and a decision buried in JSX can only be tested by
// regexing the source — which this project has already paid for once.
//
// It NEVER fabricates a version. Unknown is rendered as unknown.

/** Closed set of tones, mirroring the rest of the board's status vocabulary. */
export const TONES = ["ok", "info", "warn", "error"];

/**
 * @param {object}  s
 * @param {boolean} s.inShell     running inside the desktop shell
 * @param {string}  s.current     the running version, or null/undefined
 * @param {object}  s.update      the last payload from the shell, if any
 * @returns {{title,detail,tone,actions:string[],version:string}}
 */
export function updateNotice({ inShell = false, current = null, update = null } = {}) {
  const version = current || "unknown";

  if (!inShell) {
    return {
      title: "Updates",
      detail: `You are running no_human ${version} in a browser. Upgrade the`
        + " command line with: pip install --upgrade no-human",
      tone: "info",
      actions: [],
      version,
    };
  }

  const mode = update?.mode ?? null;

  if (mode === "downloading") {
    const pct = Number.isFinite(update?.percent) ? update.percent : 0;
    return {
      title: `Downloading ${update?.latest ?? "update"}… ${pct}%`,
      detail: "You can keep working. The update installs when you choose to restart.",
      tone: "info",
      actions: [],
      version,
    };
  }

  if (mode === "downloaded") {
    return {
      title: `no_human ${update?.latest ?? ""} is ready to install`.trim(),
      detail: "Restarting takes a few seconds. Running tasks are not interrupted"
        + " — the server keeps going.",
      tone: "ok",
      actions: ["install", "later"],
      version,
    };
  }

  if (mode === "unavailable") {
    // The honest, legible unsigned case. It still TELLS the user an update
    // exists — hiding it would be the silent failure the design forbids.
    return {
      title: `no_human ${update?.latest ?? ""} is available`.trim(),
      detail: update?.message
        || "This build is not code-signed, so it cannot install updates itself."
           + " Download the new version manually.",
      tone: "warn",
      actions: ["download-page"],
      version,
    };
  }

  if (mode === "available") {
    return {
      title: `no_human ${update?.latest ?? ""} is available`.trim(),
      detail: `You have ${version}. Nothing downloads until you choose to.`,
      tone: "info",
      actions: ["download", "later"],
      version,
    };
  }

  if (mode === "failed") {
    return {
      title: "Could not check for updates",
      detail: update?.error || "The update service could not be reached.",
      tone: "error",
      actions: ["check"],
      version,
    };
  }

  if (mode === "up-to-date") {
    return {
      title: `no_human ${version} is up to date`,
      detail: "Checked just now.",
      tone: "ok",
      actions: ["check"],
      version,
    };
  }

  // Nothing has been reported yet this session.
  return {
    title: `no_human ${version}`,
    detail: "Updates are checked once a day. You are told when one is"
      + " available and choose when to install it.",
    tone: "info",
    actions: ["check"],
    version,
  };
}
