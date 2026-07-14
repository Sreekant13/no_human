// Minimal preload: contextIsolation is ON and nodeIntegration OFF, so the
// web app runs exactly as it does in a browser. The only bridge is a marker
// letting the UI know it's inside the desktop shell (used by e2e smoke and,
// later, notification dedupe in E3).
const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("nhDesktop", {
  shell: true,
  version: process.env.npm_package_version || "dev",
});
