// Pure view-model for the Settings "Account" panel: given the auth-status
// payload (see api/app.py's auth-status endpoint), decide which controls
// render. Two billing modes exist:
//  - "subscription" (default, and the fallback for an absent/unrecognized
//    auth_mode from an older server) — OAuth profile/token switcher, the
//    metered-key alarm, and token-present status all apply.
//  - "api_key" — the operator's own ANTHROPIC_API_KEY (from ~/.no_human/.env)
//    IS the chosen billing path, so the metered-key alarm would be wrong (it
//    warns about exactly the key that is supposed to be set), the OAuth
//    profile/token controls do not apply, and token_present=false is normal
//    rather than a status to display.
// Read-only derivation, no I/O — safe to call on every render.
export function authPanelView(status) {
  const mode = status?.auth_mode === "api_key" ? "api_key" : "subscription";
  if (mode === "api_key") {
    return {
      mode,
      showMeteredAlarm: false,
      showOAuthForm: false,
      showTokenStatus: false,
      showRestartBanner: !!status?.restart_required,
      apiKeyPresent: !!status?.api_key_present,
    };
  }
  return {
    mode,
    showMeteredAlarm: !!status?.metered_key_present,
    showOAuthForm: true,
    showTokenStatus: true,
    showRestartBanner: !!status?.restart_required,
    apiKeyPresent: !!status?.api_key_present,
  };
}
