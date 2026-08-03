"""Notify-OUT channels and the fan-out that drives them.

Two write-only channels exist: Slack (``notifications.slack_webhook_url``) and
Microsoft Teams (``notifications.teams_webhook_url``). Both are OPTIONAL and
both are OFF by default; with neither configured, notifications are logged, so
a fresh install runs with nothing set up.

:func:`build_notifier` is the one place that decides which channels are live.
Call sites (``cli/commands.py``, ``api/app.py``) construct the notifier from
config through it instead of naming a single channel, so enabling Teams is
purely a config change.

DELIVERY SEMANTICS — deliberately strict, because a notification that silently
did not arrive is worse than no notification. :meth:`MultiNotifier.notify`
returns ``True`` only when **every enabled channel** accepted the message. One
channel down turns the result ``False`` and logs which one failed by name. It
never raises: a notification problem must not fail a task.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger("no_human.notify")


class Notifier(Protocol):
    @property
    def enabled(self) -> bool: ...

    def notify(self, event: str, message: str) -> bool: ...


class MultiNotifier:
    """Fan a notification out to every configured channel.

    ``channels`` is a list of ``(name, notifier)`` pairs. Only channels
    reporting ``enabled`` are posted to; a configured-but-unusable channel
    (e.g. a retired Teams connector URL) reports ``enabled=False`` and is
    still asked to ``notify`` once so it can log its own reason — see
    :meth:`notify`.
    """

    def __init__(self, channels: list[tuple[str, Notifier]] | None = None):
        self.channels = list(channels or [])

    @property
    def enabled(self) -> bool:
        """True if at least one channel could actually deliver."""
        return any(n.enabled for _, n in self.channels)

    def notify_detail(self, event: str, message: str) -> dict[str, bool]:
        """Per-channel delivery results, keyed by channel name.

        A channel the operator never set up is skipped and does not appear —
        that is not a failure. A channel that IS set up but cannot deliver (a
        retired Teams connector URL) is reported as ``False`` and is asked to
        notify anyway so it logs its own reason. Without that distinction a
        working Slack would mask a dead Teams channel completely: the alert
        the operator configured would never arrive and nothing would say so.
        """
        results: dict[str, bool] = {}
        for name, notifier in self.channels:
            usable = notifier.enabled
            if not usable and not getattr(notifier, "configured", False):
                continue
            try:
                sent = bool(notifier.notify(event, message))
            except Exception as exc:  # noqa: BLE001 — one bad channel must not
                # stop the others, and must never propagate into a task.
                log.warning("%s notify raised: %s", name, type(exc).__name__)
                sent = False
            results[name] = sent if usable else False
        return results

    def notify(self, event: str, message: str) -> bool:
        """True only if EVERY channel the operator configured accepted it.

        One path, no special case: :meth:`notify_detail` already decides which
        channels count. No configured channel at all -> log instead of send
        (the documented "runs without Slack/Teams set up" behaviour).
        """
        line = f"[{event}] {message}"
        results = self.notify_detail(event, message)
        if not results:
            log.info("notify(disabled): %s", line)
            return False
        failed = sorted(n for n, ok in results.items() if not ok)
        if failed:
            log.warning("notify not delivered to %s: %s", ", ".join(failed), line)
            return False
        return True


def build_notifier(config: dict[str, Any] | None) -> MultiNotifier:
    """Build the fan-out from ``notifications.*``.

    Tolerates a null ``notifications`` section (the config deep-merge
    shadowing trap). Channels with no URL are still constructed — they report
    ``enabled=False`` and log instead of sending, which is the documented
    "runs without Slack/Teams set up" behaviour.

    ``integrations.teams.enabled`` is a MUTE SWITCH over the Teams channel:
    False builds the notifier with no URL, so it logs instead of sending and
    the operator keeps the webhook they pasted. It defaults to True (and to
    True when the section is missing or shadowed to null), so an install that
    predates the key behaves byte-for-byte as before.
    """
    from .slack import SlackNotifier
    from .teams import TeamsNotifier

    notifications = (config or {}).get("notifications") or {}
    integrations = (config or {}).get("integrations") or {}
    teams_cfg = integrations.get("teams") or {}
    teams_url = notifications.get("teams_webhook_url")
    # `is False`, not falsy: only an explicit `false` mutes. A missing key and
    # a null one (`teams:` with nothing under it — the deep-merge shadowing
    # trap) both leave the channel alone, because the failure mode of guessing
    # wrong here is an alert that silently never arrives.
    if teams_cfg.get("enabled", True) is False:
        teams_url = None
    return MultiNotifier([
        ("slack", SlackNotifier(notifications.get("slack_webhook_url"))),
        # `board_url` is what turns the Adaptive Card's Action.OpenUrl button
        # on. Nothing passed it before, so the button this card format was
        # CHOSEN for (MessageCard's buttons are documented as not rendering)
        # could never render. Still None by default — an operator whose board
        # is not reachable from a phone leaves it unset and the payload is
        # byte-for-byte what it was.
        ("teams", TeamsNotifier(teams_url,
                                board_url=notifications.get("board_url"))),
    ])


__all__ = ["Notifier", "MultiNotifier", "build_notifier"]
