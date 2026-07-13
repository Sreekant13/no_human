/**
 * The name the composer greets the operator by — or "" when it cannot know.
 *
 * A wrong name is worse than no name, so this only derives one from an address
 * whose local part clearly reads as a person: letters (with dots or hyphens) in
 * at most two parts, no digits, and no service-account vocabulary in any segment
 * ("svc-account" and "ci-bot" are shaped exactly like "anne-marie"). Anything
 * else yields "" and the UI greets a plain "Hey there".
 */

// "dev" / "anne-marie.dubois" / "eyal" — never "user123" or "a.b.c.d".
const HUMAN_LOCAL = /^[a-z]+(?:-[a-z]+)*(?:\.[a-z]+(?:-[a-z]+)*)?$/i;

// A segment from this set means the mailbox is a role, not a person.
const RESERVED = new Set([
  "svc", "service", "account", "bot", "ci", "cd", "build", "jenkins",
  "noreply", "no", "reply", "mailer", "notifications", "alerts",
  "admin", "root", "support", "info", "team", "dev", "ops",
]);

const titleCase = (part) =>
  part
    .split("-")
    .map((seg) => seg.charAt(0).toUpperCase() + seg.slice(1).toLowerCase())
    .join("-");

export function greetingName(config) {
  const email = config?.notifications?.email_to;
  if (!email || typeof email !== "string" || !email.includes("@")) return "";

  const local = email.split("@")[0];
  if (!HUMAN_LOCAL.test(local)) return "";

  const segments = local.toLowerCase().split(/[.-]/);
  if (segments.some((seg) => RESERVED.has(seg))) return "";

  return titleCase(local.split(".")[0]);
}
