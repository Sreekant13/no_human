import test from "node:test";
import assert from "node:assert/strict";
import { formatBytes } from "./formatBytes.js";

test("0 bytes has no decimal", () => {
  assert.equal(formatBytes(0), "0 B");
});

test("a plain byte value stays in B", () => {
  assert.equal(formatBytes(512), "512 B");
});

test("1024 crosses the KB boundary", () => {
  assert.equal(formatBytes(1024), "1.0 KB");
});

test("1536 bytes is 1.5 KB", () => {
  assert.equal(formatBytes(1536), "1.5 KB");
});

test("KB values use one decimal", () => {
  assert.equal(formatBytes(2048), "2.0 KB");
});

test("MB values use one decimal", () => {
  assert.equal(formatBytes(2 * 1024 ** 2), "2.0 MB");
});

test("GB values use one decimal", () => {
  assert.equal(formatBytes(3 * 1024 ** 3), "3.0 GB");
});
