import test from "node:test";
import assert from "node:assert/strict";
import { cardTitle } from "./cardTitle.js";

test("prefers title_short, falls back to title", () => {
  assert.equal(cardTitle({ title: "Long title here", title_short: "Long title" }), "Long title");
  assert.equal(cardTitle({ title: "Only title" }), "Only title");
  assert.equal(cardTitle({ title: "T", title_short: "" }), "T");
});
