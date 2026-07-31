// The build must never hand back something that LOOKS shippable and isn't.
//
// These assert the observable outputs a consumer acts on — the electron-builder
// `identity`/`notarize` values, the artifact filename tag, and the
// auto-update permission — not the internal shape of the branch that produced
// them. Breaking the wiring (returning `null` identity when a cert is present,
// dropping the "-UNSIGNED" tag, letting an unsigned build auto-update) turns
// these red while every helper stays individually correct.
import assert from "node:assert/strict";
import test from "node:test";
import {
  NOTARY_CREDENTIAL_SETS, SIGNED, SIGNED_NOT_NOTARIZED, UNSIGNED,
  notaryCredentialSet, signingBanner, signingPlan,
} from "./signing.cjs";

const NOTARY = { APPLE_API_KEY: "k", APPLE_API_KEY_ID: "id", APPLE_API_ISSUER: "iss" };
const CERT = { CSC_LINK: "file:///cert.p12" };

test("no credentials at all: unsigned, tagged, and barred from auto-update", () => {
  const p = signingPlan({});
  assert.equal(p.mode, UNSIGNED);
  // `null` is electron-builder's explicit "do not sign". `undefined` would mean
  // "auto-discover", which on a machine that happens to hold a cert in its
  // keychain would sign a build that must not be signed.
  assert.equal(p.identity, null, "unsigned builds must pin identity to null");
  assert.equal(p.notarize, false);
  assert.equal(p.artifactTag, "-UNSIGNED",
    "an unsigned artifact must be named so it cannot be shipped by accident");
  assert.equal(p.canAutoUpdate, false,
    "Squirrel.Mac cannot install into an unsigned bundle");
  assert.equal(p.fatal, false, "an unsigned build is still allowed to succeed");
});

test("cert but no notarization credentials: refuses the clean name", () => {
  const p = signingPlan({ ...CERT });
  assert.equal(p.mode, SIGNED_NOT_NOTARIZED);
  assert.equal(p.artifactTag, "-UNNOTARIZED");
  assert.equal(p.notarize, false, "cannot notarize without credentials");
  assert.equal(p.canAutoUpdate, false,
    "Gatekeeper rejects an un-notarized download, so an update offer would lie");
  assert.match(p.reason, /notarization credentials/i);
});

test("cert + notarization credentials: the only combination that ships clean", () => {
  const p = signingPlan({ ...CERT, ...NOTARY });
  assert.equal(p.mode, SIGNED);
  assert.equal(p.artifactTag, "",
    "only a signed+notarized build may carry the plain release filename");
  assert.equal(p.notarize, true);
  assert.equal(p.canAutoUpdate, true);
  assert.equal(p.identity, undefined,
    "identity must be auto-discovered, never a hardcoded certificate name");
});

test("CSC_NAME is accepted as an identity source as well as CSC_LINK", () => {
  const p = signingPlan({ CSC_NAME: "Developer ID Application: X (Y)", ...NOTARY });
  assert.equal(p.mode, SIGNED);
});

test("all three of Apple's credential sets are honoured", () => {
  // Guard the guard: if this list is silently trimmed, builds that could be
  // notarized would be tagged unshippable and nobody would know why.
  assert.equal(NOTARY_CREDENTIAL_SETS.length, 3);
  const sets = [
    { APPLE_API_KEY: "a", APPLE_API_KEY_ID: "b", APPLE_API_ISSUER: "c" },
    { APPLE_ID: "a", APPLE_APP_SPECIFIC_PASSWORD: "b", APPLE_TEAM_ID: "c" },
    { APPLE_KEYCHAIN: "a", APPLE_KEYCHAIN_PROFILE: "b" },
  ];
  for (const env of sets) {
    assert.ok(notaryCredentialSet(env), `unrecognised set: ${Object.keys(env)}`);
    assert.equal(signingPlan({ ...CERT, ...env }).mode, SIGNED);
  }
});

test("a partially-filled credential set does not count as credentials", () => {
  // The failure this prevents: two of three vars set in CI, notarization
  // silently skipped, artifact still named as a release.
  const p = signingPlan({ ...CERT, APPLE_ID: "a", APPLE_TEAM_ID: "c" });
  assert.equal(p.mode, SIGNED_NOT_NOTARIZED);
  assert.equal(notaryCredentialSet({ APPLE_ID: "a", APPLE_TEAM_ID: "c" }), null);
});

test("empty and whitespace-only vars are absent, not present", () => {
  // CI systems export unset secrets as the empty string. Treating "" as set is
  // how you get `codesign --sign ""`.
  assert.equal(signingPlan({ CSC_LINK: "" }).mode, UNSIGNED);
  assert.equal(signingPlan({ CSC_LINK: "   " }).mode, UNSIGNED);
  assert.equal(signingPlan({ ...CERT, APPLE_API_KEY: "", APPLE_API_KEY_ID: "b",
                             APPLE_API_ISSUER: "c" }).mode, SIGNED_NOT_NOTARIZED);
});

test("NH_REQUIRE_SIGNED turns an unshippable build into a hard failure", () => {
  assert.equal(signingPlan({ NH_REQUIRE_SIGNED: "1" }).fatal, true);
  assert.equal(signingPlan({ ...CERT, NH_REQUIRE_SIGNED: "1" }).fatal, true,
    "signed-but-not-notarized must also fail a release build");
  assert.equal(signingPlan({ ...CERT, ...NOTARY, NH_REQUIRE_SIGNED: "1" }).fatal,
    false, "a real release build must pass its own gate");
  assert.equal(signingPlan({ NH_REQUIRE_SIGNED: "0" }).fatal, false,
    "an explicit 0 must not enable the gate");
});

test("the banner names the mode and never renders empty", () => {
  // The operator's stated failure mode is a SILENT unsigned build. The banner
  // is the thing that makes it not silent, so its content is asserted.
  for (const env of [{}, CERT, { ...CERT, ...NOTARY }]) {
    const plan = signingPlan(env);
    const banner = signingBanner(plan);
    assert.ok(banner.includes(plan.reason), "the banner must carry the reason");
    assert.ok(banner.length > 40);
  }
  assert.match(signingBanner(signingPlan({})), /UNSIGNED — NOT SHIPPABLE/);
  assert.match(signingBanner(signingPlan({ ...CERT, ...NOTARY })),
    /release build \(signed \+ notarized\)/);
  assert.match(signingBanner(signingPlan({ NH_REQUIRE_SIGNED: "1" })),
    /refusing to continue/);
});
