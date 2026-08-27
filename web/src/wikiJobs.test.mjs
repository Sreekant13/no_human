import test from "node:test";
import assert from "node:assert/strict";

import { nextJobState, shouldPoll } from "./wikiJobs.js";

test("queued/running keep polling; done/failed stop", () => {
  assert.equal(shouldPoll({ status: "running" }), true);
  assert.equal(shouldPoll({ status: "queued" }), true);
  assert.equal(shouldPoll({ status: "done" }), false);
  assert.equal(shouldPoll({ status: "failed" }), false);
  assert.equal(shouldPoll(undefined), false);
});

test("a failed job exposes its error verbatim", () => {
  const s = nextJobState({}, "/r", { job_id: "j", status: "failed", error: "failed to parse wiki JSON from agent output: I could not" });
  assert.match(s["/r"].error, /I could not/);
  assert.equal(s["/r"].jobId, "j");
});

test("a queued POST response has no status and reads as queued, then a poll updates it", () => {
  let s = nextJobState({}, "/r", { job_id: "j" });
  assert.equal(s["/r"].status, "queued");
  assert.equal(shouldPoll(s["/r"]), true);
  s = nextJobState(s, "/r", { id: "j", status: "done", files: '["a.md","b.md"]' });
  assert.equal(shouldPoll(s["/r"]), false);
  assert.equal(s["/r"].files, '["a.md","b.md"]');
  assert.equal(s["/r"].jobId, "j");   // carried forward, not erased by the poll
});
