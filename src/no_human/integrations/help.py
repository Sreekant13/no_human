"""One catalogue of per-field "where do I find this?" help for every
integration field, keyed ``(integration, field) -> (text, url)``.

A real user could not tell what "Linear team key" meant, and a green check
meant "a key was typed", not "the connection works" (spec §4). This file is the
first half of the fix: one place that says, for every field, what it is and
where in the vendor's UI to get it, with a URL that opens that page.

Fields whose meaning is identical across every tracker — the polling switch,
the write-back opt-in, the poll interval, and the "run tasks in repo" target —
are stored once under the wildcard integration ``"*"`` rather than repeated per
tracker. :func:`help_for` checks the exact key first, then the wildcard, so a
tracker can still override a shared field by adding its own entry.

``test_integrations_help.py`` fails the build if any FIELD_SPECS or setup_specs
field has no entry here, so the catalogue cannot silently fall behind a new
field.
"""
from __future__ import annotations

_DOCS = "https://github.com/no-human-ai/no_human/blob/main/docs/integrations.md"

HELP: dict[tuple[str, str], tuple[str, str]] = {
    # ── Jira (id.atlassian.com) ────────────────────────────────────────────
    ("jira", "site"): (
        "Your Atlassian site URL, e.g. https://acme.atlassian.net — the host "
        "you sign in to Jira on.",
        "https://support.atlassian.com/jira-software-cloud/docs/what-it-means-to-be-a-jira-cloud-admin/"),
    ("jira", "project_key"): (
        "The project key — the prefix in issue ids, PROJ in PROJ-123. "
        "Jira → Project settings → Details.",
        "https://support.atlassian.com/jira-software-cloud/docs/what-is-an-issue/"),
    ("jira", "email"): (
        "The Atlassian account email the API token below belongs to.",
        "https://id.atlassian.com/manage-profile/email"),
    ("jira", "jql"): (
        "Optional JQL filter selecting which issues to pull, e.g. "
        "status = 'To Do'. Leave blank to pull the whole project.",
        "https://support.atlassian.com/jira-software-cloud/docs/use-advanced-search-with-jira-query-language-jql/"),
    ("jira", "api_token"): (
        "Atlassian API token for the email above: id.atlassian.com → Security "
        "→ Create and manage API tokens.",
        "https://id.atlassian.com/manage-profile/security/api-tokens"),

    # ── Linear (linear.app) ────────────────────────────────────────────────
    ("linear", "team_key"): (
        "The prefix of your issue ids — ENG in ENG-123. "
        "Linear → Settings → Teams.",
        "https://linear.app/settings/teams"),
    ("linear", "label"): (
        "Optional: pull only issues carrying this label. "
        "Linear → Settings → Labels.",
        "https://linear.app/docs/labels"),
    ("linear", "state_types"): (
        "Which workflow-state TYPES to pull. Linear's seven types are triage, "
        "backlog, unstarted, started, completed, canceled, duplicate.",
        "https://linear.app/docs/configuring-workflows"),
    ("linear", "api_key"): (
        "Personal API key. Linear → Settings → Security & access → "
        "Personal API keys.",
        "https://linear.app/settings/account/security"),

    # ── monday (developer.monday.com) ──────────────────────────────────────
    ("monday", "board_id"): (
        "The numeric board id — the number after /boards/ in the board's URL.",
        "https://developer.monday.com/api-reference/docs/introduction-to-graphql"),
    ("monday", "status_column"): (
        "The status column's id (e.g. bug_status), NOT its title. Discover it "
        "with the query boards { columns { id title type } }.",
        "https://developer.monday.com/api-reference/docs/columns"),
    ("monday", "todo_labels"): (
        "The status labels that mean 'not started yet', e.g. Ready for Dev — "
        "these are the items that get pulled in.",
        "https://developer.monday.com/api-reference/docs/status"),
    ("monday", "in_progress_label"): (
        "Optional: the status label to move an item to when work starts.",
        "https://developer.monday.com/api-reference/docs/status"),
    ("monday", "done_label"): (
        "Optional: the status label to move an item to when the PR is opened.",
        "https://developer.monday.com/api-reference/docs/status"),
    ("monday", "api_token"): (
        "Your monday API token: avatar → Developers → My access tokens.",
        "https://developer.monday.com/api-reference/docs/authentication"),

    # ── CircleCI (app.circleci.com) ────────────────────────────────────────
    ("circleci", "project_slug"): (
        "The CircleCI project slug vcs/org/repo, e.g. gh/no-human-ai/no_human "
        "(gh for GitHub, bb for Bitbucket).",
        "https://circleci.com/docs/api-developers-guide/"),
    ("circleci", "api_token"): (
        "A CircleCI personal API token: User Settings → Personal API Tokens.",
        "https://app.circleci.com/settings/user/tokens"),

    # ── GitHub Actions (github.com) ────────────────────────────────────────
    ("github", "project"): (
        "The owner/repo whose GitHub Actions runs gate your PRs — the two path "
        "segments after github.com/, e.g. no-human-ai/no_human.",
        "https://docs.github.com/en/actions"),

    # ── GitLab CI (gitlab.com) ─────────────────────────────────────────────
    ("gitlab", "project"): (
        "The namespace/project path whose GitLab CI pipelines gate your PRs — "
        "the path after gitlab.com/ in the project URL.",
        "https://docs.gitlab.com/ee/ci/"),

    # ── Jenkins ────────────────────────────────────────────────────────────
    ("jenkins", "job"): (
        "The job path in Jenkins, e.g. folder/my-pipeline — the segments after "
        "/job/ in the build URL.",
        "https://www.jenkins.io/doc/book/using/"),
    ("jenkins", "user"): (
        "Your Jenkins username — the account the API token below belongs to.",
        "https://www.jenkins.io/doc/book/security/managing-security/"),
    ("jenkins", "api_token"): (
        "Jenkins API token: your name (top right) → Configure → API Token → "
        "Add new Token.",
        "https://www.jenkins.io/doc/book/security/managing-security/"),

    # ── Slack (api.slack.com) ──────────────────────────────────────────────
    ("slack", "webhook_url"): (
        "An incoming-webhook URL for the channel to post to: your Slack app → "
        "Incoming Webhooks → Add New Webhook to Workspace.",
        "https://api.slack.com/messaging/webhooks"),
    ("slack", "intake"): (
        "Turns on the Socket-Mode worker that connects to Slack (needs "
        "SLACK_BOT_TOKEN and SLACK_APP_TOKEN in ~/.no_human/.env).",
        "https://api.slack.com/apis/connections/socket"),

    # ── Microsoft Teams (learn.microsoft.com) ──────────────────────────────
    ("teams", "webhook_url"): (
        "A Power Automate 'When a Teams webhook request is received' URL for "
        "the channel — the retired Office 365 connector URLs no longer work.",
        "https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook"),

    # ── Shared across every tracker (wildcard "*") ─────────────────────────
    ("*", "default_repo"): (
        "The repository the coder works in when an issue from this tracker is "
        "pulled into no_human.",
        _DOCS),
    ("*", "enabled"): (
        "Turn polling on or off for this integration — no issues are pulled "
        "while it is off.",
        _DOCS),
    ("*", "write_back"): (
        "Opt-in: comment back on the source issue when its status changes. "
        "Never transitions or closes it.",
        _DOCS),
    ("*", "poll_interval"): (
        "How often to poll for new issues, e.g. 5m. The floor is 60 seconds.",
        _DOCS),
}


def help_for(integration: str, field: str) -> tuple[str, str]:
    """The ``(text, url)`` help for one field, or ``("", "")`` if none.

    The exact ``(integration, field)`` key wins; a ``("*", field)`` wildcard is
    the fallback for fields that mean the same thing on every tracker."""
    entry = HELP.get((integration, field)) or HELP.get(("*", field))
    return entry if entry else ("", "")
