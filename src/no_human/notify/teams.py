"""Microsoft Teams notifications via a Power Automate Workflows webhook.

Same role and same contract as :mod:`no_human.notify.slack`: a **write-only**
notify-OUT channel for "a human is needed". It never reads. It is entirely
separate from ``context/teams.py``, which is a read-only CONTEXT source over
the Microsoft Graph ``/search/query`` API — different direction, different
credential, different failure mode. Nothing here reads a Graph token and
nothing in ``context/teams.py`` posts.

WHY POWER AUTOMATE AND NOT THE CLASSIC "INCOMING WEBHOOK" CONNECTOR
------------------------------------------------------------------
Office 365 / Microsoft 365 connectors — the classic Teams *Incoming Webhook*
whose URL looked like ``https://outlook.office.com/webhook/…`` (and, after the
Jan-2025 URL migration, ``https://<tenant>.webhook.office.com/webhookb2/…``) —
have been **retired**. Microsoft's final schedule (Microsoft 365 Developer Blog,
"Retirement of Office 365 connectors within Microsoft Teams", update dated
2026-04-14) says the disable rollout began 2026-05-18 and completed 2026-05-22,
and that "after these dates, Office 365 Connectors will no longer function".
Building onto that path today would ship a notifier that cannot deliver.

The supported successor is a **Power Automate "Workflows" webhook**: in Teams,
the Workflows app → "Post to a channel when a webhook request is received" →
pick team + channel → copy the generated URL. Documented properties we rely on
(Microsoft Teams connector reference, ``learn.microsoft.com/connectors/teams``):

* **POST only**, ``Content-Type: application/json``.
* The URL is issued on a Logic Apps runtime host (documented example
  ``https://xxxxx.logic.azure.com:443/…``) and carries its SAS credential in
  the query string (``sp``/``sv``/``sig``). **The whole URL is a secret** — it
  is never logged here, not even on failure.
* We deliberately do NOT allowlist a host. The documented pattern is
  ``*.logic.azure.com``, but Microsoft has issued newer flow endpoints on other
  hosts and there is no published guarantee, so pinning one host would break
  working installs. What we *do* reject is the small, closed set of hosts that
  are known-retired (below) — a denylist of dead things is safe in a way an
  allowlist of live things is not.

PAYLOAD
-------
An **Adaptive Card**, not the legacy MessageCard. Workflows accepts both, but
Microsoft documents that MessageCard "button rendering is not supported" — a
MessageCard with an action would post and render without its button, which is
exactly the silent half-delivery this module must not produce. The envelope
(``type: "message"`` + one ``application/vnd.microsoft.card.adaptive``
attachment with an explicit ``contentUrl: null``) is the documented
``AdaptiveCardItemSchema``. The button itself is the operator's
``notifications.board_url``, wired through ``notify.build_notifier``; with it
unset there is simply no ``actions`` block.

Card schema is pinned to **1.2**. Teams supports up to 1.5 on desktop, but the
cards reference states the Teams *mobile* app supports Adaptive Cards only up
to 1.2 and that later versions "might not render correctly". A human-attention
alert is read on a phone, so 1.2 is the correct ceiling; ``Action.OpenUrl`` has
existed since 1.0, so nothing is lost.

SUCCESS
-------
Any **2xx**. The stock Workflows template has no Response action, so the Logic
Apps Request trigger returns ``202 Accepted`` with an empty body; asserting
``== 200`` would report a real delivery as a failure. The retired connector's
``200`` + literal body ``1`` contract no longer applies to anything.

OPERATIONAL HAZARD, SURFACED NOT SWALLOWED
------------------------------------------
Power Automate documents that "a cloud flow that isn't triggered within a 90
day period might be turned off". A quiet alert channel can therefore die on its
own. That is precisely why a failed POST logs at WARNING and returns ``False``
rather than being silently dropped: the operator must be able to see that the
channel stopped delivering.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("no_human.notify")

# Adaptive Card schema pinned for Teams mobile (see module docstring).
CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
CARD_VERSION = "1.2"

# Hosts that served the RETIRED Office 365 connector webhooks. Both the
# original (`outlook.office.com/webhook/...`) and the post-Jan-2025 migrated
# (`<tenant>.webhook.office.com/webhookb2/...`) shapes are dead as of the
# 2026-05-18..22 disable rollout. Matching is on the host only, so the URL's
# secret query string is never inspected or echoed.
_RETIRED_CONNECTOR_HOSTS = ("outlook.office.com", "outlook.office365.com")
_RETIRED_CONNECTOR_HOST_SUFFIX = ".webhook.office.com"

RETIRED_CONNECTOR_MESSAGE = (
    "this is a retired Office 365 connector webhook URL. Microsoft disabled "
    "Office 365 connectors between 2026-05-18 and 2026-05-22 and they no longer "
    "function. Replace it with a Power Automate Workflows webhook: in Teams, "
    "open the Workflows app, choose 'Post to a channel when a webhook request "
    "is received', pick the team and channel, and copy the generated URL into "
    "notifications.teams_webhook_url."
)


def is_retired_connector_url(url: str | None) -> bool:
    """True if ``url`` is a known-dead Office 365 connector webhook.

    Host-only check — the query string holds the credential and is never read.
    """
    if not url:
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return host in _RETIRED_CONNECTOR_HOSTS or host.endswith(_RETIRED_CONNECTOR_HOST_SUFFIX)


def build_card(event: str, message: str, *, url: str | None = None) -> dict:
    """The documented Workflows envelope around a v1.2 Adaptive Card.

    Pure — no network, no credential. ``url``, when given, becomes an
    ``Action.OpenUrl`` button (the reason this is an Adaptive Card and not a
    MessageCard, whose buttons Microsoft documents as not rendering).
    """
    body: list[dict] = [
        {"type": "TextBlock", "text": f"no_human · {event}",
         "weight": "bolder", "size": "medium", "wrap": True},
        {"type": "TextBlock", "text": message, "wrap": True},
    ]
    content: dict = {
        "$schema": CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": CARD_VERSION,
        "body": body,
    }
    if url:
        content["actions"] = [
            {"type": "Action.OpenUrl", "title": "Open in no_human", "url": url}
        ]
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": content,
        }],
    }


class TeamsNotifier:
    """Write-only Teams notify-OUT channel.

    Interface-compatible with :class:`~no_human.notify.slack.SlackNotifier`
    (``enabled`` / ``notify``) so both can sit behind the same fan-out.
    """

    def __init__(self, webhook_url: str | None, *, board_url: str | None = None):
        self.webhook_url = webhook_url
        # Optional deep link rendered as the card's button.
        self.board_url = board_url

    @property
    def configured(self) -> bool:
        """The operator asked for this channel (a URL is set).

        True even for a retired connector URL — that is the whole point: the
        fan-out must be able to tell "Teams was never set up" from "Teams was
        set up and cannot deliver", and report only the second as a failure.
        """
        return bool(self.webhook_url)

    @property
    def retired(self) -> bool:
        """A configured-but-dead classic connector URL."""
        return is_retired_connector_url(self.webhook_url)

    @property
    def enabled(self) -> bool:
        """A URL that could actually deliver.

        A retired connector URL is NOT enabled: it is configuration that can
        never post, and reporting it as an active channel would be a lie the
        operator only discovers when an alert fails to arrive.
        """
        return bool(self.webhook_url) and not self.retired

    def notify(self, event: str, message: str) -> bool:
        """Post an Adaptive Card. True only if Teams accepted it (2xx).

        Never raises — a notification problem must not fail a task — but never
        reports a delivery it did not make either.
        """
        line = f"[{event}] {message}"
        if self.retired:
            # Loud, actionable, and once per attempt: the operator has a URL
            # that will never work again. Never echo the URL (it is a secret).
            log.error("teams notify skipped: %s", RETIRED_CONNECTOR_MESSAGE)
            return False
        if not self.enabled:
            log.info("teams(disabled): %s", line)
            return False
        payload = build_card(event, message, url=self.board_url)
        try:
            resp = httpx.post(self.webhook_url, json=payload, timeout=10)
        except Exception as exc:  # noqa: BLE001 — notifications are best-effort
            # Type name only: an httpx exception's str() can contain the
            # request URL, which carries the webhook's SAS credential.
            log.warning("teams notify failed: %s", type(exc).__name__)
            return False
        if 200 <= resp.status_code < 300:
            return True
        # A Workflows webhook answers 202 on success; anything else (401 on a
        # tampered SAS, 404 on a deleted flow, 429 throttling, 400 on a
        # rejected body) is a real non-delivery and is reported as one.
        log.warning("teams notify failed: HTTP %s", resp.status_code)
        return False
