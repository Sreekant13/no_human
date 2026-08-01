import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// A resume checkpoint the orchestrator could not read is a LOSS of work that
// the run continues through — the attempt branches from base instead, and used
// to look exactly like one that resumed correctly. The backend now records it
// on the attempt row; this pins the rest of the chain to the board, because a
// column nothing renders is the same silence in a different place.
//
// Every link is read from ITS OWN SOURCE rather than restated here: a test that
// hand-copies the field name passes happily while a rename breaks the drawer.

const SRC = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(SRC, p), "utf8");

const FIELD = "resume_checkpoint_lost";

const slideOver = read("SlideOver.jsx");
const css = read("styles.css");
const models = read("../../src/no_human/api/models.py");
const orchestrator = read("../../src/no_human/core/orchestrator.py");
const db = read("../../src/no_human/core/db.py");

test("the backend actually writes the field this UI reads", () => {
  assert.ok(
    new RegExp(`"${FIELD}":\\s*"TEXT"`).test(db),
    `${FIELD} must be a real attempts column (db.py att_wanted)`,
  );
  assert.ok(
    orchestrator.includes(`attempt_id, ${FIELD}=`),
    `the orchestrator must record ${FIELD} on the attempt`,
  );
});

test("the field is on the wire shape the drawer receives", () => {
  // AttemptOut both declares it and copies it out of the row — declaring it
  // without the from_row line yields a field that is always null.
  assert.ok(models.includes(`${FIELD}: str | None = None`), "AttemptOut must declare it");
  assert.ok(models.includes(`${FIELD}=row.get("${FIELD}")`), "from_row must populate it");
});

test("the attempt row renders it, distinctly from the failure line", () => {
  assert.ok(slideOver.includes(`a.${FIELD} &&`), "the attempt row must render it");
  assert.ok(
    slideOver.includes('className="attempt-resume-lost"'),
    "it needs its own class — the attempt is NOT failed and must not read as failed",
  );
  assert.ok(!/attempt-resume-lost[^"]*test-result-fail-msg/.test(slideOver));
  // Defined, or it renders unstyled — the .attempt-* family is hand-written CSS
  // and deadCss.test.mjs only guards the .blocker-* family.
  assert.ok(/\.attempt-resume-lost\s*\{/.test(css), "styles.css must define the class");
});

test("the orchestrator's loss event has a human label on the timeline", () => {
  // The emit and the label map must agree; an unlabelled kind renders as the
  // raw enum, which is how the timeline stops being read.
  assert.ok(
    orchestrator.includes('self.emit("resume_checkpoint_lost"'),
    "the orchestrator must emit the loss event",
  );
  assert.ok(
    /resume_checkpoint_lost:\s*"[^"]+"/.test(slideOver),
    "SlideOver's event label map must name the kind",
  );
});
