// Cap the DISPLAYED length of an untrusted, server-supplied auth label (a profile
// name, or the CLAUDE_CODE_OAUTH_TOKEN_<P> variable derived from it) so a
// credential-shaped value can never be rendered in full. The server enforces
// MAX_PROFILE_NAME_LEN=32 on profile names and refuses sk-ant-/sk_ant_ prefixes
// (config.py), so a *valid* value is always at or under these caps and is never
// truncated — only a pre-guard or hand-edited value is clipped. React already
// escapes the text; this guards LENGTH, the one thing escaping does not.
//
// Caps tie to the backend contract: PROFILE_CAP == MAX_PROFILE_NAME_LEN, and
// TOKENVAR_CAP == len("CLAUDE_CODE_OAUTH_TOKEN_") + MAX_PROFILE_NAME_LEN.
export const PROFILE_CAP = 32;
export const TOKENVAR_CAP = 56;

export function capName(s, max) {
  if (typeof s !== "string" || s.length <= max) return s;
  return s.slice(0, max) + "…";
}
