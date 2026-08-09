// What the "AI history" step is allowed to say happened.
//
// THE DEFECT: the result callout read its counts from the EXTRACT response and
// its item count from the ANALYZE response, and printed them in one sentence as
// though they described one pass:
//
//     Scanned 0 conversations → 16 items to review (incl. 16 skills).
//
// Two things are wrong there. The arithmetic reads as nonsense — zero
// conversations cannot yield sixteen findings — and it is only unreadable
// because the sentence never says the sixteen came from somewhere else
// entirely (the skills catalog, which is scanned whether or not any transcript
// exists). And "16 skills" was the number DISCOVERED on disk, while the items
// actually queued are `skills_added` from the analyze response: a re-scan
// dedupes, so the same screen would go on claiming sixteen next to "0 items to
// review".
//
// Both are fixed by describing ONE pass — the analyze response, which reports
// what it actually ingested — and by letting the zero case name its own source
// instead of printing a zero beside a non-zero total.
//
// Pure and string-returning so it can be tested: this project's `node --test`
// harness has no React renderer, so a sentence assembled inside JSX can only
// ever be checked by grepping for its own source text.

/** The result line for a finished scan.
 *
 * @param transcripts conversations the analyze pass actually read
 * @param messages    messages inside them
 * @param skills      skills it QUEUED (not the number found on disk)
 * @param proposals   items now waiting on the rules step — the total, skills
 *                    included
 * @param claudeCode  how many of the conversations came from Claude Code
 *                    (`sources.claude_code`); named only when it is not all of
 *                    them, so the common case does not repeat itself
 */
export function scanSummary({ transcripts = 0, messages = 0, skills = 0, proposals = 0,
                              claudeCode = 0 } = {}) {
  const items = `${proposals} item${proposals === 1 ? "" : "s"} to review`;
  const skillPart = `${skills} skill${skills === 1 ? "" : "s"}`;
  if (transcripts === 0) {
    // Nothing was read, so do not print "Scanned 0 conversations" next to a
    // count that did not come from conversations.
    return skills > 0
      ? `No past conversations were readable, but your ${skillPart} were cataloged → ${items}.`
      : `No past conversations were readable → ${items}.`;
  }
  const convos = `${transcripts} conversation${transcripts === 1 ? "" : "s"}`;
  const detail = [
    messages ? `${messages.toLocaleString()} messages` : "",
    claudeCode && claudeCode < transcripts ? `${claudeCode} from Claude Code` : "",
  ].filter(Boolean).join(", ");
  const withMessages = detail ? `${convos} (${detail})` : convos;
  return `Scanned ${withMessages} → ${items}`
    + (skills > 0 ? `, including ${skillPart} cataloged from this machine.` : ".");
}
