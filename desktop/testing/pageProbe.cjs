// Measures the shipped pages in a REAL renderer and prints one JSON blob.
// Focus rings, aria wiring and [hidden] overrides are computed-style questions:
// a regex over the stylesheet cannot answer them, so we ask the engine.
// NOTE: windows are deliberately NOT destroyed between loads — destroying one
// and immediately opening another raced and failed the next navigation.
const { app, BrowserWindow } = require("electron");
const path = require("node:path");

const HERE = path.join(__dirname, "..");
const open = async (file, query) => {
  const win = new BrowserWindow({
    show: false, width: 1440, height: 920,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  await win.loadFile(path.join(HERE, file), query ? { query } : {});
  return win;
};

app.whenReady().then(async () => {
  const out = {};
  try {
    const tok = await open("token.html", null);
    // Autofocus must be read BEFORE the Tab below moves it.
    out.autofocused = await tok.webContents.executeJavaScript(
      "document.activeElement && document.activeElement.id");
    // :focus-visible only engages when focus arrived by KEYBOARD, so a
    // programmatic .focus() reports no ring even when the rule is present.
    // Send a real Tab through the input pipeline instead.
    tok.focusOnWebView();
    for (const type of ["keyDown", "char", "keyUp"]) {
      tok.webContents.sendInputEvent({ type, keyCode: "Tab" });
    }
    await new Promise((r) => setTimeout(r, 150));
    out.token = await tok.webContents.executeJavaScript(`(() => {
      const $ = (id) => document.getElementById(id);
      const cs = (el) => getComputedStyle(el);
      const input = $("token"), reveal = $("reveal"), save = $("save");
      const before = { type: input.type, pressed: reveal.getAttribute("aria-pressed") };
      reveal.click();
      const after = { type: input.type, pressed: reveal.getAttribute("aria-pressed") };
      reveal.click();
      const focused = document.activeElement;
      const focusRing = { on: focused && focused.id,
                          matches: focused && focused.matches(":focus-visible"),
                          width: getComputedStyle(focused).outlineWidth,
                          style: getComputedStyle(focused).outlineStyle,
                        };
      // No nhSetup bridge here, so submit takes the error path — the same
      // setMsg() that must flag the field as invalid.
      $("form").dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
      return {
        lang: document.documentElement.lang, before, after, focusRing,
        ariaInvalid: input.getAttribute("aria-invalid"),
        msgRole: $("msg").getAttribute("role"),
        msgText: $("msg").textContent.trim(),
        saveMinHeight: parseFloat(cs(save).minHeight),
        labelFor: document.querySelector('label[for="token"]') ? "token" : null,
        tabOrder: [...document.querySelectorAll("input,button")].map((e) => e.id),
      };
    })()`);

    const errAt = async (query) => {
      const w = await open("error.html", query);
      return w.webContents.executeJavaScript(`(() => {
        const vis = (el) => el && getComputedStyle(el).display !== "none";
        const retry = document.querySelector("a.retry");
        const before = retry ? getComputedStyle(retry).display : "none";
        if (retry) retry.hidden = true;          // the UA rule error.html overrides
        return {
          lang: document.documentElement.lang,
          steps: [...document.querySelectorAll("p.steps")].filter(vis).map((e) => e.id),
          tokenLinkVisible: vis(document.getElementById("token-link")),
          retryDisplayShown: before,
          retryDisplayWhenHidden: retry ? getComputedStyle(retry).display : "no-retry",
        };
      })()`);
    };
    out.errStopFailed = await errAt({ reason: "stop-failed", packaged: "1" });
    out.errNotFound   = await errAt({ reason: "nh-not-found", packaged: "1" });
    out.errPackaged   = await errAt({ reason: "spawn-timeout", packaged: "1" });
    out.errDev        = await errAt({ reason: "spawn-timeout", packaged: "0" });
    process.stdout.write("PROBE_JSON:" + JSON.stringify(out) + "\n");
    app.exit(0);
  } catch (err) {
    process.stdout.write("PROBE_ERROR:" + (err && err.message) + "\n");
    app.exit(1);
  }
});
