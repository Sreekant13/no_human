/**
 * The name the composer greets the operator by — or "" when it cannot know.
 *
 * A wrong name is worse than no name, so this only derives one from an address
 * whose local part clearly reads as a person: letters (with dots or hyphens) in
 * at most two parts, no digits, and no service-account vocabulary in any segment
 * ("svc-account" and "ci-bot" are shaped exactly like "anne-marie"). Anything
 * else yields "" and the UI greets a plain "Hey there".
 *
 * It reads every place the operator can put their OWN address, in order of how
 * deliberately they had to configure it. `notifications.email_to` used to be the
 * only source, which had two holes: it ships with a placeholder value, so an
 * untouched install looked "configured"; and an operator who set up Jira with
 * their personal account had genuinely told us their name and was still greeted
 * by nobody.
 */

// "sam.rivera" / "anne-marie.dubois" / "sam" — never "user123" or "a.b.c.d".
const HUMAN_LOCAL = /^[a-z]+(?:-[a-z]+)*(?:\.[a-z]+(?:-[a-z]+)*)?$/i;

// A segment from this set means the mailbox is a role, not a person.
const RESERVED = new Set([
  "svc", "service", "account", "bot", "ci", "cd", "build", "jenkins",
  "noreply", "no", "reply", "mailer", "notifications", "alerts",
  "admin", "root", "support", "info", "team", "dev", "ops",
]);

// The values config.py ships in DEFAULTS. An operator who never edited the file
// has configured nothing, and a placeholder must never be greeted by name. Today
// "dev@example.com" is caught anyway because "dev" is in RESERVED — that is luck,
// not a rule, and it would evaporate the moment the default changed.
export const PLACEHOLDER_ADDRESSES = new Set(["dev@example.com"]);

const titleCase = (part) =>
  part
    .split("-")
    .map((seg) => seg.charAt(0).toUpperCase() + seg.slice(1).toLowerCase())
    .join("-");

function nameFrom(email) {
  if (!email || typeof email !== "string" || !email.includes("@")) return "";
  if (PLACEHOLDER_ADDRESSES.has(email.trim().toLowerCase())) return "";

  const local = email.split("@")[0];
  if (!HUMAN_LOCAL.test(local)) return "";

  const segments = local.toLowerCase().split(/[.-]/);
  if (segments.some((seg) => RESERVED.has(seg))) return "";

  return titleCase(local.split(".")[0]);
}

export function greetingName(config) {
  // Ordered by how deliberate the setting is. Both are the operator's own
  // address; neither is a team or agent identity (`git.agent_identity_email` is
  // the AGENT's and is never consulted).
  const sources = [
    config?.notifications?.email_to,
    config?.integrations?.jira?.email,
  ];
  for (const email of sources) {
    const name = nameFrom(email);
    if (name) return name;
  }
  return "";
}
