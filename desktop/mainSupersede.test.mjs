// A discarded navigation must NEVER spawn a server.
//
// Removing the `if (!current())` guards in _loadBoardOrError leaves the suite
// green, yet a superseded nav then boots a SECOND nh racing for the port on the
// OLD token — the defect those guards exist to prevent.
//
// TIMING, measured rather than assumed. NH_ORIGIN is non-routable, so every
// probe costs its full 1500ms timeout. Two navs are queued back-to-back and
// navScheduler claims its generation AT ENQUEUE, so the first is discarded when
// it runs. Measured spawn counts:
//        t=  5s  25s  30s  40s
//   clean:   1    1    1    1     (nav A returns at the guard; only nav B boots)
//   guards
//   removed: 1    1    2    2     (nav A boots too, then B boots after its
//                                  restartOwnServer wait)
// Sampling at 40s therefore has ~10s of margin on both sides. An earlier sample
// does NOT work: the two timelines differ by only ~1.3s around t=3-4s, which is
// a knife-edge that would flake under parallel load.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

register("./testing/electronLoader.mjs", import.meta.url);

const HERE = fileURLToPath(new URL(".", import.meta.url));
const MARK = `nhSup${process.pid}`;
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-sup-"));
fs.mkdirSync(path.join(home, ".no_human"));
const spawnLog = path.join(home, "spawns.log");
const fakeNh = path.join(home, "nh");
fs.writeFileSync(fakeNh, `#!/usr/bin/env node
process.title = ${JSON.stringify(MARK)};
require("node:fs").appendFileSync(${JSON.stringify(spawnLog)}, Date.now() + "\\n");
setInterval(() => {}, 1000);
`);
fs.chmodSync(fakeNh, 0o755);
process.env.HOME = home;
process.env.NH_BIN = fakeNh;                       // never resolve the real nh
process.env.NH_ORIGIN = `http://10.255.255.1:${19900 + (process.pid % 80)}`;

// No token yet, so startup takes the setup path and spawns nothing.
const stub = await import("./testing/electronStub.mjs");
await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 3000));

test.after(() => {
  try { execSync(`/usr/bin/pkill -9 -f ${MARK}`); } catch { /* already gone */ }
  fs.rmSync(home, { recursive: true, force: true });
});

const spawnCount = () => (fs.existsSync(spawnLog)
  ? fs.readFileSync(spawnLog, "utf8").trim().split("\n").filter(Boolean).length : 0);

test("a discarded navigation never spawns a server", async () => {
  assert.equal(spawnCount(), 0,
    "precondition: no token at startup, so nothing should have been spawned");
  fs.writeFileSync(path.join(home, ".no_human", ".env"),
    "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-supersede\n");

  const onNavigate = stub.calls.nav.get("will-navigate");
  const dismiss = stub.calls.ipc.get("nh:dismiss");
  assert.ok(onNavigate && dismiss, "main.mjs must register both");

  onNavigate({ preventDefault() {} }, "nh://retry");                  // nav A
  await new Promise((r) => setTimeout(r, 50));
  dismiss({ senderFrame: { url: pathToFileURL(path.join(HERE, "token.html")).href } });

  await new Promise((r) => setTimeout(r, 40000));  // see the measured table above

  assert.equal(spawnCount(), 1,
    "the discarded navigation spawned an nh of its own; it races the live one "
    + "for the port and serves the OLD token");
});
