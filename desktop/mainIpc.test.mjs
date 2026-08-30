// The credential IPC gate, tested at its CALL SITE.
//
// preload.cjs exposes window.nhSetup to EVERY page the server renders, so the
// board shares it with the setup screen. main.mjs gates each handler on
// fromSetupScreen(). setupGate.test.mjs pins isSetupUrl() itself beautifully —
// but nothing pinned that main.mjs still CALLS it, so all three ways of deleting
// the gate (open the predicate, or drop either guard line) kept the suite green.
// A one-line deletion would let board content rewrite ~/.no_human/.env.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

register("./testing/electronLoader.mjs", import.meta.url);

const PORT = 19500 + (process.pid % 150);
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-ipc-"));
fs.mkdirSync(path.join(home, ".no_human"));
fs.writeFileSync(path.join(home, ".no_human", ".env"),
  "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-original\n");
process.env.HOME = home;
// os.homedir() reads USERPROFILE on Windows, not HOME — without this the run
// WRITES THROUGH to the operator's real ~/.no_human (observed: a fixture token
// landed in the real .env and flipped the real config.yaml to api_key mode).
process.env.USERPROFILE = home;
process.env.NH_ORIGIN = `http://127.0.0.1:${PORT}`;
delete process.env.NH_TEST_LOG;      // must not divert the routing under test

// A live server so main.mjs attaches instead of spawning anything.
let probes = 0;
// nh:open-path's tests below swap this in to make one path "known" — every
// other test never touches /api/repos, so its default of no known repos is
// the one they run against.
let knownRepos = "[]";
const server = http.createServer((q, s) => {
  if (q.url === "/api/tasks") probes += 1;   // one per navigation run
  if (q.url === "/api/repos") { s.end(knownRepos); return; }
  s.end("[]");
});
await new Promise((r) => server.listen(PORT, "127.0.0.1", r));

const stub = await import("./testing/electronStub.mjs");
await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 800));

test.after(() => { server.close(); fs.rmSync(home, { recursive: true, force: true }); });

// import.meta.dirname does not exist on node 20, which is what runs here.
const HERE = fileURLToPath(new URL(".", import.meta.url));
const SETUP_URL = pathToFileURL(path.join(HERE, "token.html")).href;
const from = (url) => ({ senderFrame: { url } });
const envText = () => fs.readFileSync(path.join(home, ".no_human", ".env"), "utf8");

const BOARD_PAGES = [
  `http://127.0.0.1:${PORT}/`,                 // the board itself
  `http://127.0.0.1:${PORT}/token.html`,       // a board page named alike
  pathToFileURL(path.join(HERE, "error.html")).href,
  `${SETUP_URL}.evil`,                          // strict-prefix lookalike
];

test("nh:save-token refuses any sender that is not the setup screen", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  assert.ok(handler, "main.mjs must register nh:save-token");
  for (const url of BOARD_PAGES) {
    const res = await handler(from(url), "sk-ant-oat-injected");
    assert.equal(res.ok, false, `accepted a token from ${url}`);
    assert.match(res.error, /not permitted/i);
  }
  assert.match(envText(), /sk-ant-oat-original/,
    "the operator's credential was overwritten from board content");
  assert.doesNotMatch(envText(), /injected/, "an injected token reached disk");
});

test("nh:quit and nh:dismiss refuse a non-setup sender, and obey a real one", async () => {
  const quit = stub.calls.ipc.get("nh:quit");
  const dismiss = stub.calls.ipc.get("nh:dismiss");
  assert.ok(quit && dismiss, "both handlers must be registered");

  const before = stub.calls.quit;
  for (const url of BOARD_PAGES) {
    assert.equal(await quit(from(url)), false, `board content quit the app via ${url}`);
    assert.equal(await dismiss(from(url)), false, `board content drove dismiss via ${url}`);
  }
  assert.equal(stub.calls.quit, before, "the app was quit by a page that is not the setup screen");

  // The gate must not be closed to everyone either, or the real screen is dead.
  assert.equal(await quit(from(SETUP_URL)), true, "the real setup screen cannot quit");
  assert.ok(stub.calls.quit > before, "a genuine quit did not reach app.quit()");
});

test("a missing or malformed senderFrame fails CLOSED", async () => {
  const quit = stub.calls.ipc.get("nh:quit");
  for (const evt of [{}, { senderFrame: null }, { senderFrame: { url: "" } }]) {
    assert.equal(await quit(evt), false, `fell open on ${JSON.stringify(evt)}`);
  }
});

test("the window is revealed on first paint — it must never stay hidden", () => {
  // createWindow uses show:false to kill the pre-paint flash and relies on
  // ready-to-show to reveal. Lose that and the app runs with an invisible
  // window: no board, no error page, nothing to click.
  const win = stub.BrowserWindow.last;
  assert.ok(win.onceHandlers["ready-to-show"],
    "main.mjs must subscribe to ready-to-show, or the window never appears");

  const before = win.shown;
  win.onceHandlers["ready-to-show"]();
  assert.ok(win.shown > before, "ready-to-show fired but the window was not shown");
});

test("Retry is rate-limited: a second click while one is in flight is declined", async () => {
  // Retry is a plain link that can be clicked repeatedly, and each accepted
  // click is a full ensureServer AND another spawned nh. Dropping the idle gate
  // made every extra click leak a live server.
  const onNavigate = stub.calls.nav.get("will-navigate");
  const win = stub.BrowserWindow.last;
  win.loaded.length = 0;

  // Count PROBES, not loads: with a plain schedule() the second nav supersedes
  // the first, so exactly one page is loaded either way — but two navigations
  // RAN, and on a real first run each of those is another ensureServer spawn.
  const before = probes;
  // Fire both WITHOUT awaiting, so the second lands while the first is pending.
  const a = onNavigate({ preventDefault() {} }, "nh://retry");
  const b = onNavigate({ preventDefault() {} }, "nh://retry");
  await Promise.all([a, b]);

  assert.equal(probes - before, 1,
    `a concurrent Retry was accepted (${probes - before} navigations ran); on a `
    + "real first run each one spawns another nh that nothing reclaims");
});

// --- the save contract, not just the gate --------------------------------- //
// mainIpc previously tested only WHO may call nh:save-token. Every behaviour of
// what it RETURNS was unpinned, including the round-14 fix — so "the app lies
// about success", this branch's signature defect, could be reintroduced silently
// for a third time. These run against a live foreign server with nothing owned.

test("a token that cannot be written is reported as a failure, never as success", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  const before = envText();

  const res = await handler(from(SETUP_URL), "sk-ant-api-this-is-a-metered-key");

  assert.equal(res.ok, false,
    "reporting ok here paints 'Connected. Opening no_human…' while NOTHING was written");
  assert.match(res.error, /api key/i, "the reason must name the actual problem");
  assert.equal(envText(), before, "a rejected value still reached disk");
});

test("a blank token is rejected without touching disk", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  const before = envText();
  const res = await handler(from(SETUP_URL), "   ");
  assert.equal(res.ok, false);
  assert.equal(envText(), before);
});

test("saving against someone else's live server says so instead of claiming success", async () => {
  // Nothing of ours is running and the port answers: that server is not ours to
  // restart, and it still holds the OLD token. Reporting ok would send the user
  // back to a board where every task keeps failing on the old credential.
  const handler = stub.calls.ipc.get("nh:save-token");

  const res = await handler(from(SETUP_URL), "sk-ant-oat-brand-new-value");

  assert.equal(res.ok, false, "claimed success over a server holding the old token");
  assert.equal(res.needsRestart, true, "the setup screen needs this to keep the field alive");
  assert.match(res.error, /already running|restart/i);
  assert.match(envText(), /sk-ant-oat-brand-new-value/,
    "the token should still be SAVED — only the running server is stale");
});

test("a superseded navigation must not paint over the screen the user moved to",
  async () => {
    // Every `if (!current())` guard in _loadBoardOrError exists because a
    // discarded navigation went on to publish state and paint. The worst case is
    // the credential screen: the user opens it, an older nav completes, and the
    // board is slammed back over the top of it.
    const onNavigate = stub.calls.nav.get("will-navigate");
    const win = stub.BrowserWindow.last;
    win.loaded.length = 0;
    stub.BrowserWindow.slowLoadUrlMs = 400;      // hold the board load open

    const board = onNavigate({ preventDefault() {} }, "nh://retry");
    await new Promise((r) => setTimeout(r, 30));  // let it get past the probe
    await onNavigate({ preventDefault() {} }, "nh://token");   // supersedes it
    await board;
    stub.BrowserWindow.slowLoadUrlMs = 0;

    const last = win.loaded[win.loaded.length - 1];
    assert.ok(String(last).startsWith("file:token.html"),
      `the credential screen was painted over by a discarded navigation; `
      + `sequence was ${JSON.stringify(win.loaded)}`);
  });

test("the renderer stays sandboxed from node", () => {
  // This shell is shipped in a DMG and renders text it did not author (task
  // titles, PR bodies, CI output). Both switches flipped with the whole suite
  // green and no test mentioned webPreferences at all.
  const { opts } = stub.BrowserWindow.last;
  assert.ok(opts.webPreferences, "the window was created without webPreferences");
  assert.equal(opts.webPreferences.contextIsolation, true,
    "contextIsolation off lets page script reach the preload's scope");
  assert.equal(opts.webPreferences.nodeIntegration, false,
    "nodeIntegration on gives page script require() — full node in the renderer");
  assert.ok(String(opts.webPreferences.preload || "").endsWith("preload.cjs"),
    "the bridge must come from the preload, not the page");
});

test("nh:save-token in api_key mode writes the key and flips config.yaml", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");
  // Disk effects are the contract here; the returned action (ok / needsRestart)
  // depends on server state the other tests already pin.
  await handler(from(SETUP_URL), "sk-ant-api03-e2test", "api_key");
  assert.match(envText(), /ANTHROPIC_API_KEY=sk-ant-api03-e2test/,
    "the key must land in .env under ANTHROPIC_API_KEY");
  const cfg = fs.readFileSync(path.join(home, ".no_human", "config.yaml"), "utf8");
  assert.match(cfg, /auth_mode: api_key/, "the MODE must land in config.yaml");

  // A subscription token pasted while api_key is selected is refused pre-disk.
  const bad = await handler(from(SETUP_URL), "sk-ant-oat-wrong", "api_key");
  assert.equal(bad.ok, false);
  assert.doesNotMatch(envText(), /ANTHROPIC_API_KEY=sk-ant-oat-wrong/);

  // An omitted mode (stale renderer) keeps today's subscription behaviour.
  await handler(from(SETUP_URL), "sk-ant-oat-back-to-sub");
  assert.match(envText(), /CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-back-to-sub/);
  assert.match(fs.readFileSync(path.join(home, ".no_human", "config.yaml"), "utf8"),
    /auth_mode: subscription/, "saving a token must configure subscription mode");
});

test("nh:save-token writes the OPTIONAL OpenAI key only when one is supplied (#6b)", async () => {
  const handler = stub.calls.ipc.get("nh:save-token");

  // A subscription-codex / skipped save omits the 4th arg → NOTHING for OpenAI.
  await handler(from(SETUP_URL), "sk-ant-oat-nocodex");
  assert.doesNotMatch(envText(), /OPENAI_API_KEY/,
    "codex subscription/skip must write no OpenAI credential (constraint #6b)");

  // An empty codex field (api_key chosen but nothing typed) also writes nothing.
  await handler(from(SETUP_URL), "sk-ant-oat-emptycodex", "subscription", "   ");
  assert.doesNotMatch(envText(), /OPENAI_API_KEY/, "an empty OpenAI field writes nothing");

  // api_key codex WITH a value writes OPENAI_API_KEY alongside the Claude token.
  await handler(from(SETUP_URL), "sk-ant-oat-withcodex", "subscription", "sk-proj-openaireal");
  assert.match(envText(), /OPENAI_API_KEY=sk-proj-openaireal/,
    "a supplied OpenAI key must land in .env under OPENAI_API_KEY");
  assert.match(envText(), /CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-withcodex/,
    "the required Claude token is written and untouched by the OpenAI write");

  // An Anthropic key in the OpenAI field is refused BEFORE anything is written.
  const before = envText();
  const bad = await handler(from(SETUP_URL), "sk-ant-oat-x", "subscription", "sk-ant-api03-wrong");
  assert.equal(bad.ok, false, "an Anthropic key in the OpenAI field must be rejected");
  assert.match(bad.error, /Anthropic key/);
  assert.equal(envText(), before,
    "a rejected optional key must leave both credentials on disk untouched");
});

// --- nh:requirements (Task 5: first-run requirements check) --------------- //

test("nh:requirements refuses any sender that is not the setup screen", async () => {
  const handler = stub.calls.ipc.get("nh:requirements");
  assert.ok(handler, "main.mjs must register nh:requirements");
  for (const url of BOARD_PAGES) {
    const res = await handler(from(url));
    assert.deepEqual(res, { ok: false, error: "not permitted" },
      `accepted a requirements probe from ${url}`);
  }
});

test("nh:requirements reports resolved claude/node the same shape the setup screen expects", async () => {
  const handler = stub.calls.ipc.get("nh:requirements");
  const res = await handler(from(SETUP_URL));
  // No "ok"/"error" envelope on the success path — the setup screen renders
  // res.claude/res.node directly, and wrapping it would silently break that.
  assert.equal(res.ok, undefined);
  assert.equal(typeof res.claude.ok, "boolean");
  assert.equal(typeof res.claude.path, "string");
  assert.equal(typeof res.claude.version, "string");
  assert.equal(typeof res.node.ok, "boolean");
  assert.equal(typeof res.node.path, "string");
  // node's shape carries no version field — the setup screen's row for node
  // must not expect one.
  assert.equal("version" in res.node, false);
  if (!res.claude.ok) assert.equal(res.claude.version, "",
    "a missing claude must not report a version");
});

// --- nh:open-path ("Open repo", Task 6) ------------------------------------ //

test("nh:open-path refuses a path the server does not know about", async () => {
  const handler = stub.calls.ipc.get("nh:open-path");
  assert.ok(handler, "main.mjs must register nh:open-path");
  knownRepos = "[]";                      // the server knows no repos
  stub.calls.openedPaths.length = 0;
  const res = await handler(from(SETUP_URL), "/etc/passwd");
  assert.deepEqual(res, { ok: false, error: "not a registered repo" });
  assert.deepEqual(stub.calls.openedPaths, [],
    "shell.openPath must never see a path that failed the allow-list check");
});

test("nh:open-path rejects a missing/blank path without calling the server", async () => {
  const handler = stub.calls.ipc.get("nh:open-path");
  for (const bad of [undefined, "", null, 42]) {
    const res = await handler(from(SETUP_URL), bad);
    assert.equal(res.ok, false);
  }
});

test("nh:open-path opens a path the server reports as a known repo", async () => {
  const handler = stub.calls.ipc.get("nh:open-path");
  knownRepos = JSON.stringify([{ repo_path: "/tmp/known-repo", name: "known-repo" }]);
  stub.calls.openedPaths.length = 0;
  const res = await handler(from(SETUP_URL), "/tmp/known-repo");
  assert.deepEqual(res, { ok: true });
  assert.deepEqual(stub.calls.openedPaths, ["/tmp/known-repo"]);
  knownRepos = "[]";                      // restore the default for later tests
});
