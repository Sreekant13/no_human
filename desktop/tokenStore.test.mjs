// Unit tests for credential storage. Pure logic over a real temp HOME — no
// mocks, and never the operator's real ~/.no_human/.env.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  TOKEN_KEY, configuredProfile, envPath, hasToken, parseEnv, tokenVarFor,
  validateToken, writeToken,
} from "./tokenStore.mjs";

const home = () => mkdtempSync(join(tmpdir(), "nhhome-"));

test("hasToken: the .env file wins, then process env, else false", () => {
  const h = home();
  assert.equal(hasToken({}, h), false, "empty home has no token");
  assert.equal(hasToken({ [TOKEN_KEY]: "sk-ant-oat-x" }, h), true);
  assert.equal(hasToken({ [TOKEN_KEY]: "   " }, h), false, "blank is not a token");
  writeToken("sk-ant-oat-fromfile", h);
  assert.equal(hasToken({}, h), true, "reads the .env file");
});

test("writeToken: preserves other secrets, one token line after rewrite", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h),
    "# comment\nJIRA_API_TOKEN=keepme\nCIRCLECI_TOKEN=keeptoo\n");
  writeToken("sk-ant-oat-first", h);
  let text = fs.readFileSync(envPath(h), "utf8");
  assert.match(text, /JIRA_API_TOKEN=keepme/, "other secrets survive");
  assert.match(text, /CIRCLECI_TOKEN=keeptoo/);
  assert.match(text, /# comment/, "comments survive");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-first");

  // Rewriting must REPLACE, not append a second line.
  writeToken("sk-ant-oat-second", h);
  text = fs.readFileSync(envPath(h), "utf8");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-second");
  const occurrences = text.split("\n").filter((l) => l.startsWith(TOKEN_KEY));
  assert.equal(occurrences.length, 1, "exactly one token line");
  assert.match(text, /JIRA_API_TOKEN=keepme/, "still preserved on rewrite");
});

test("writeToken: creates the file 0600", () => {
  const h = home();
  const p = writeToken("sk-ant-oat-x", h);
  assert.equal(fs.statSync(p).mode & 0o777, 0o600);
});

test("writeToken: removes DUPLICATE keys — dotenv is later-wins", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h),
    `${TOKEN_KEY}=OLD1\nFOO=b\n${TOKEN_KEY}=OLD2\n`);
  writeToken("sk-ant-oat-new", h);
  const text = fs.readFileSync(envPath(h), "utf8");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-new",
    "the effective (last) value must be the new one");
  assert.equal(text.split("\n").filter((l) => l.startsWith(TOKEN_KEY)).length, 1);
  assert.match(text, /FOO=b/);
});

test("writeToken: hardens an existing 0644 .env to 0600", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h), "FOO=b\n", { mode: 0o644 });
  fs.chmodSync(envPath(h), 0o644);
  writeToken("sk-ant-oat-x", h);
  assert.equal(fs.statSync(envPath(h)).mode & 0o777, 0o600);
});

test("writeToken: always ends with a newline", () => {
  const h = home();
  const p = writeToken("sk-ant-oat-x", h);
  assert.ok(fs.readFileSync(p, "utf8").endsWith("\n"));
});

test("profile-aware: a named auth_profile uses its own key", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\n  auth_profile: personal\n");
  assert.equal(configuredProfile(h), "personal");
  assert.equal(tokenVarFor("personal"), `${TOKEN_KEY}_PERSONAL`);
  writeToken("sk-ant-oat-profile", h);
  const env = parseEnv(fs.readFileSync(envPath(h), "utf8"));
  assert.equal(env[`${TOKEN_KEY}_PERSONAL`], "sk-ant-oat-profile");
  assert.equal(env[TOKEN_KEY], undefined, "must not write the bare key");
  assert.equal(hasToken({}, h), true);
});

test("hasToken: a bare token does NOT satisfy a named profile", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\n  auth_profile: personal\n");
  fs.writeFileSync(envPath(h), `${TOKEN_KEY}=bare-only\n`);
  assert.equal(hasToken({}, h), false);
});

test("validateToken: rejects empty, whitespace and API keys", () => {
  assert.notEqual(validateToken(""), "");
  assert.notEqual(validateToken("   "), "");
  assert.match(validateToken("sk-ant-api03-abc"), /API key/);
  assert.match(validateToken("SK-ANT-API03-ABC"), /API key/, "case-insensitive");
  assert.notEqual(validateToken("has space"), "");
  assert.equal(validateToken("sk-ant-oat-valid"), "", "a real token passes");
});

test("writeToken: refuses to persist an invalid value", () => {
  const h = home();
  assert.throws(() => writeToken("", h));
  assert.throws(() => writeToken("sk-ant-api03-nope", h));
  assert.equal(fs.existsSync(envPath(h)), false, "nothing written on reject");
});
