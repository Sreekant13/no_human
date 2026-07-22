// Which renderer is allowed to drive the credential IPC.
//
// The setup screen shares ONE BrowserWindow (and one preload) with the board,
// so `window.nhSetup` is reachable from every page the server renders. Only the
// local setup file may save a token or dismiss the screen — otherwise injected
// board content could overwrite the operator's credential.
//
// Pure and electron-free so it is unit-testable; main.mjs supplies the URL.
import { fileURLToPath } from "node:url";

/** True only for the exact local setup file. Fails closed on anything odd. */
export function isSetupUrl(url, setupFile) {
  if (typeof url !== "string" || !url.startsWith("file://")) return false;
  try {
    return fileURLToPath(url) === setupFile;
  } catch {
    return false;
  }
}
