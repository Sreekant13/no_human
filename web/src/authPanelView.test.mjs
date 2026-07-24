import { test } from "node:test";
import assert from "node:assert/strict";
import { authPanelView } from "./authPanelView.js";

test("api_key mode suppresses metered alarm, OAuth form, and token status", () => {
  const view = authPanelView({
    auth_mode: "api_key",
    metered_key_present: true,
    api_key_present: true,
  });
  assert.equal(view.mode, "api_key");
  assert.equal(view.showMeteredAlarm, false);
  assert.equal(view.showOAuthForm, false);
  assert.equal(view.showTokenStatus, false);
});

test("api_key mode surfaces api_key_present", () => {
  assert.equal(authPanelView({ auth_mode: "api_key", api_key_present: true }).apiKeyPresent, true);
  assert.equal(authPanelView({ auth_mode: "api_key", api_key_present: false }).apiKeyPresent, false);
});

test("api_key restart banner follows backend restart_required, not token", () => {
  assert.equal(
    authPanelView({ auth_mode: "api_key", restart_required: true, token_present: false }).showRestartBanner,
    true,
  );
  // restart_required absent — no banner, even though token_present=false is normal in api_key mode
  assert.equal(
    authPanelView({ auth_mode: "api_key", token_present: false }).showRestartBanner,
    false,
  );
});

test("subscription mode keeps metered alarm tied to metered_key_present", () => {
  const withKey = authPanelView({ auth_mode: "subscription", metered_key_present: true });
  assert.equal(withKey.showMeteredAlarm, true);
  assert.equal(withKey.showOAuthForm, true);

  const withoutKey = authPanelView({ auth_mode: "subscription", metered_key_present: false });
  assert.equal(withoutKey.showMeteredAlarm, false);
  assert.equal(withoutKey.showOAuthForm, true);
});

test("absent/unknown auth_mode falls back to subscription", () => {
  assert.equal(authPanelView({}).mode, "subscription");
  assert.equal(authPanelView({}).showOAuthForm, true);
  assert.equal(authPanelView({ auth_mode: "weird" }).mode, "subscription");
  assert.equal(authPanelView({ auth_mode: "weird" }).showOAuthForm, true);
});

test("all fields are boolean-coerced so a missing key never renders undefined", () => {
  const sub = authPanelView(undefined);
  assert.equal(sub.mode, "subscription");
  assert.equal(sub.showMeteredAlarm, false);
  assert.equal(sub.showRestartBanner, false);
  assert.equal(sub.apiKeyPresent, false);

  const apiKey = authPanelView({ auth_mode: "api_key" });
  assert.equal(apiKey.showRestartBanner, false);
  assert.equal(apiKey.apiKeyPresent, false);
});
