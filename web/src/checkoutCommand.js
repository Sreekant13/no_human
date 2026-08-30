// The one-line fetch+switch a human runs to get a task's branch onto their
// own machine — the drawer's "Copy checkout command" button copies exactly
// this, so there is one place that knows the shape of the command.

/**
 * @param {string} branch the attempt's branch name (falsy => not checked out)
 * @param {string} [remote] defaults to "origin"
 * @returns {string} `git fetch <remote> <branch> && git switch <branch>`, or
 *   "" when there is no branch to check out.
 */
export function checkoutCommand(branch, remote = "origin") {
  if (!branch) return "";
  return `git fetch ${remote} ${branch} && git switch ${branch}`;
}
