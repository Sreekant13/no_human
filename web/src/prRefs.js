/**
 * Parse PR/MR references out of free text — full URLs OR human shorthand.
 *
 * A MIRROR of `src/no_human/vcs/pr_refs.py`. The backend hard-fails a code_review
 * task whose title+description contains no reference (orchestrator.py), so the
 * composer gates submit on the same grammar. Keep the two in step: a stricter
 * rule here silently blocks a review the backend would have accepted.
 */

// Full URLs already in canonical form (GitHub/GHE …/pull/N, GitLab …/merge_requests/N).
const FULL = /https?:\/\/[^\s()<>]+?\/(?:pull|merge_requests)\/\d+/g;

// Human shorthand: "<host> <owner/group/…/repo> PR #<n>" or "… MR !<n>".
const SHORT =
  /([a-z0-9][a-z0-9.-]*\.[a-z]{2,})[\s/]+([a-z0-9][a-z0-9._/-]*?)\s+(?:(PR)\s*#|(MR)\s*!)(\d+)/gi;

/** Every PR/MR reference in `text` as a full canonical URL, de-duplicated, first-seen order. */
export function parsePrRefs(text) {
  if (!text || typeof text !== "string") return [];
  const seen = new Set();
  const out = [];
  const add = (url) => {
    if (!seen.has(url)) {
      seen.add(url);
      out.push(url);
    }
  };

  for (const m of text.matchAll(FULL)) add(m[0]);
  for (const m of text.matchAll(SHORT)) {
    const [, host, rawPath, , mr, num] = m;
    const path = rawPath.replace(/\/+$/, "");
    add(mr ? `https://${host}/${path}/-/merge_requests/${num}` : `https://${host}/${path}/pull/${num}`);
  }
  return out;
}

/** True when `text` carries at least one PR/MR reference — the composer's submit guard. */
export function hasPrRef(text) {
  return parsePrRefs(text).length > 0;
}
