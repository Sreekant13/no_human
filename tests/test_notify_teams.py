"""Microsoft Teams notify-OUT channel + the Slack/Teams fan-out.

All HTTP is mocked; nothing here needs or reads a real webhook URL. The
webhook URL is a bearer secret (the Power Automate URL carries its SAS
credential in the query string), so several tests assert it never reaches a
log line.
"""

import logging

import httpx
import pytest

from no_human.notify import MultiNotifier, build_notifier
from no_human.notify.slack import SlackNotifier
from no_human.notify.teams import (
    CARD_VERSION,
    TeamsNotifier,
    build_card,
    is_retired_connector_url,
)

LIVE_URL = "https://prod-27.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SECRETSIG"


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        # SlackNotifier uses this; TeamsNotifier inspects status_code directly.
        if not 200 <= self.status_code < 300:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


# --------------------------------------------------------------------------- #
# Retired Office 365 connector detection                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "https://outlook.office.com/webhook/abc-def@ghi/IncomingWebhook/jkl/mno",
    "https://outlook.office365.com/webhook/abc/IncomingWebhook/def/ghi",
    # The post-Jan-2025 migrated connector URL shape.
    "https://acme.webhook.office.com/webhookb2/abc@def/IncomingWebhook/ghi/jkl",
    "https://ACME.WEBHOOK.OFFICE.COM/webhookb2/x",   # host match is case-insensitive
])
def test_retired_connector_urls_are_detected(url):
    assert is_retired_connector_url(url) is True


@pytest.mark.parametrize("url", [
    LIVE_URL,
    "https://prod-00.westus.logic.azure.com:443/workflows/x",
    # A future/undocumented flow host must NOT be rejected — we deny known-dead
    # hosts, we do not allowlist live ones.
    "https://something.powerplatform.com/workflows/x",
    "",
    None,
])
def test_live_and_unknown_hosts_are_not_flagged_retired(url):
    assert is_retired_connector_url(url) is False


def test_a_hostname_merely_containing_the_dead_host_is_not_flagged():
    # Substring matching would wrongly reject a legitimate host. The check is
    # host-suffix/equality, not `in`.
    assert is_retired_connector_url("https://outlook.office.com.evil.test/x") is False
    assert is_retired_connector_url("https://notwebhook.office.company.test/x") is False


def test_retired_url_is_not_enabled_and_never_posts(monkeypatch, caplog):
    posted = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posted.append(a) or _Resp(200))
    n = TeamsNotifier("https://outlook.office.com/webhook/abc/IncomingWebhook/d/e")
    assert n.retired is True
    assert n.enabled is False          # configured but can never deliver
    with caplog.at_level(logging.ERROR, logger="no_human.notify"):
        assert n.notify("stuck", "hello") is False
    assert posted == []                # never posted into a dead endpoint
    text = caplog.text
    assert "Power Automate" in text and "2026-05-22" in text   # actionable
    assert "IncomingWebhook" not in text                        # URL never logged


# --------------------------------------------------------------------------- #
# Card payload                                                                 #
# --------------------------------------------------------------------------- #

def test_card_is_the_documented_adaptive_card_envelope():
    card = build_card("needs_approval", "PR is up", url="https://board.test/t/1")
    assert card["type"] == "message"
    (att,) = card["attachments"]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert "contentUrl" in att and att["contentUrl"] is None   # documented as required
    content = att["content"]
    assert content["type"] == "AdaptiveCard"
    # Pinned at 1.2: the Teams MOBILE app supports only up to 1.2 and later
    # versions "might not render correctly". An alert is read on a phone.
    assert content["version"] == "1.2" == CARD_VERSION
    assert content["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
    assert [b["text"] for b in content["body"]][1] == "PR is up"
    assert content["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open in no_human", "url": "https://board.test/t/1"}
    ]


def test_card_omits_actions_when_there_is_no_url():
    card = build_card("stuck", "no link")
    assert "actions" not in card["attachments"][0]["content"]


def test_card_is_not_the_legacy_messagecard_format():
    # MessageCard still posts to a Workflows webhook but Microsoft documents
    # that its BUTTONS do not render — a silent half-delivery. Guard the shape
    # so nobody "simplifies" the payload back to it.
    card = build_card("stuck", "x", url="https://board.test/t/1")
    assert "@type" not in card and "@context" not in card
    assert "sections" not in card and "potentialAction" not in card


# --------------------------------------------------------------------------- #
# Delivery                                                                     #
# --------------------------------------------------------------------------- #

def test_notify_posts_the_card_and_reports_success(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(url=url, json=json, timeout=timeout)
        return _Resp(202)          # Workflows answers 202, not 200

    monkeypatch.setattr(httpx, "post", fake_post)
    assert TeamsNotifier(LIVE_URL).notify("task_failed", "boom") is True
    assert seen["url"] == LIVE_URL
    assert seen["json"]["attachments"][0]["content"]["body"][1]["text"] == "boom"


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_any_2xx_counts_as_delivered(monkeypatch, status):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(status))
    assert TeamsNotifier(LIVE_URL).notify("stuck", "x") is True


@pytest.mark.parametrize("status", [
    400,   # rejected body
    401,   # tampered/invalid SAS
    404,   # deleted flow
    429,   # throttled
    500,
    302,   # a redirect is NOT a delivery
])
def test_non_2xx_is_reported_as_a_failure_not_a_delivery(monkeypatch, caplog, status):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(status))
    with caplog.at_level(logging.WARNING, logger="no_human.notify"):
        assert TeamsNotifier(LIVE_URL).notify("stuck", "x") is False
    assert f"HTTP {status}" in caplog.text


def test_transport_error_is_swallowed_but_reported_false_without_leaking_the_url(monkeypatch, caplog):
    def boom(*a, **k):
        # httpx exception strings routinely embed the request URL, which is the
        # credential here.
        raise httpx.ConnectError(f"failed to connect to {LIVE_URL}")

    monkeypatch.setattr(httpx, "post", boom)
    with caplog.at_level(logging.WARNING, logger="no_human.notify"):
        assert TeamsNotifier(LIVE_URL).notify("stuck", "x") is False
    assert "ConnectError" in caplog.text
    assert "SECRETSIG" not in caplog.text


def test_unconfigured_is_disabled_and_logs_instead_of_sending(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not post"))
    n = TeamsNotifier(None)
    assert n.enabled is False
    assert n.notify("stuck", "x") is False


# --------------------------------------------------------------------------- #
# Fan-out                                                                      #
# --------------------------------------------------------------------------- #

class _Fake:
    def __init__(self, enabled, result, *, configured=None):
        self.enabled = enabled
        # Defaults to `enabled` — the Slack-shaped case where the two agree.
        self.configured = enabled if configured is None else configured
        self.result = result
        self.calls = []

    def notify(self, event, message):
        self.calls.append((event, message))
        return self.result


def test_fanout_posts_to_every_enabled_channel():
    a, b = _Fake(True, True), _Fake(True, True)
    m = MultiNotifier([("slack", a), ("teams", b)])
    assert m.enabled is True
    assert m.notify("stuck", "x") is True
    assert a.calls == b.calls == [("stuck", "x")]


def test_fanout_skips_channels_that_were_never_set_up():
    # `configured` absent/False == "the operator never asked for this channel".
    # Skipping it is correct and must not drag the result down.
    a, b = _Fake(True, True), _Fake(False, True)
    assert MultiNotifier([("slack", a), ("teams", b)]).notify("stuck", "x") is True
    assert b.calls == []


def test_a_working_slack_does_not_mask_a_dead_teams_channel(monkeypatch, caplog):
    """The failure this whole guard exists for.

    Slack works, Teams is configured with a retired connector URL. Before the
    `configured` split, the fan-out skipped the unusable Teams channel and
    returned True — the operator's Teams alert never arrived and NOTHING said
    so. It must now be reported as a non-delivery and named.
    """
    posted = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posted.append(a) or _Resp(200))
    m = build_notifier({"notifications": {
        "slack_webhook_url": "https://hooks.slack.com/services/X",
        "teams_webhook_url": "https://outlook.office.com/webhook/a/IncomingWebhook/b/c",
    }})
    with caplog.at_level(logging.ERROR, logger="no_human.notify"):
        assert m.notify("needs_approval", "PR is up") is False
    detail = m.notify_detail("needs_approval", "PR is up")
    assert detail == {"slack": True, "teams": False}
    assert "Power Automate" in caplog.text        # actionable reason surfaced
    assert len(posted) == 2                       # Slack still got both sends
    assert all("outlook.office.com" not in str(p) for p in posted)   # never the dead URL


def test_an_unconfigured_teams_does_not_drag_the_result_down(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(200))
    m = build_notifier({"notifications": {
        "slack_webhook_url": "https://hooks.slack.com/services/X"}})
    assert m.notify("stuck", "x") is True
    assert m.notify_detail("stuck", "x") == {"slack": True}


def test_one_failing_channel_makes_the_result_false_and_names_it(caplog):
    a, b = _Fake(True, True), _Fake(True, False)
    m = MultiNotifier([("slack", a), ("teams", b)])
    with caplog.at_level(logging.WARNING, logger="no_human.notify"):
        assert m.notify("stuck", "x") is False       # partial delivery is NOT success
    assert "teams" in caplog.text
    assert a.calls == [("stuck", "x")]               # the healthy channel still got it


def test_a_raising_channel_does_not_stop_the_others_or_propagate(caplog):
    class _Boom:
        enabled = True

        def notify(self, event, message):
            raise RuntimeError("kaboom")

    good = _Fake(True, True)
    m = MultiNotifier([("teams", _Boom()), ("slack", good)])
    with caplog.at_level(logging.WARNING, logger="no_human.notify"):
        assert m.notify("stuck", "x") is False
    assert good.calls == [("stuck", "x")]
    assert "RuntimeError" in caplog.text


def test_a_channel_that_claims_delivery_while_unusable_is_not_believed():
    """Fail closed. A channel reporting `enabled=False` cannot have delivered;
    if its notify() returns True anyway the two statements contradict each
    other, and the fan-out trusts the conservative one."""
    liar = _Fake(False, True, configured=True)
    m = MultiNotifier([("teams", liar)])
    assert m.notify_detail("stuck", "x") == {"teams": False}
    assert m.notify("stuck", "x") is False


def test_notify_detail_reports_per_channel_results():
    a, b = _Fake(True, True), _Fake(True, False)
    assert MultiNotifier([("slack", a), ("teams", b)]).notify_detail("stuck", "x") == {
        "slack": True, "teams": False}


def test_fanout_with_nothing_enabled_logs_and_returns_false():
    m = MultiNotifier([("slack", _Fake(False, True)), ("teams", _Fake(False, True))])
    assert m.enabled is False
    assert m.notify("stuck", "x") is False


def test_a_retired_teams_url_still_gets_to_log_its_reason_when_nothing_is_enabled(monkeypatch, caplog):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not post"))
    m = build_notifier({"notifications": {
        "teams_webhook_url": "https://outlook.office.com/webhook/a/IncomingWebhook/b/c"}})
    with caplog.at_level(logging.ERROR, logger="no_human.notify"):
        assert m.notify("stuck", "x") is False
    assert "Power Automate" in caplog.text


# --------------------------------------------------------------------------- #
# build_notifier                                                               #
# --------------------------------------------------------------------------- #

def test_build_notifier_wires_both_channels_from_config():
    m = build_notifier({"notifications": {
        "slack_webhook_url": "https://hooks.slack.com/services/X",
        "teams_webhook_url": LIVE_URL,
    }})
    by_name = dict(m.channels)
    assert isinstance(by_name["slack"], SlackNotifier)
    assert isinstance(by_name["teams"], TeamsNotifier)
    assert by_name["slack"].enabled and by_name["teams"].enabled
    assert m.enabled is True


def test_build_notifier_tolerates_a_null_notifications_section():
    # The config deep-merge shadowing trap: `notifications:` set to null.
    for cfg in ({"notifications": None}, {}, None):
        m = build_notifier(cfg)
        assert m.enabled is False
        assert sorted(n for n, _ in m.channels) == ["slack", "teams"]


def test_build_notifier_wires_the_card_button_url_so_it_is_not_dead_in_production(monkeypatch):
    """`board_url` was reachable only by constructing TeamsNotifier by hand.

    build_notifier is the only production constructor, and it did not pass one
    — so `notify()` always called `build_card(url=None)` and the Action.OpenUrl
    button never rendered anywhere, while the module docstring justified
    choosing Adaptive Card over MessageCard *on that button*. Wire it, and pin
    both ends: set -> a button, unset -> the same payload as before.
    """
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(json=json)
        return _Resp(202)

    monkeypatch.setattr(httpx, "post", fake_post)
    teams = dict(build_notifier({"notifications": {
        "teams_webhook_url": LIVE_URL,
        "board_url": "https://board.example.test/t/1",
    }}).channels)["teams"]
    assert teams.board_url == "https://board.example.test/t/1"
    assert teams.notify("needs_approval", "PR is up") is True
    assert seen["json"]["attachments"][0]["content"]["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open in no_human",
         "url": "https://board.example.test/t/1"}
    ]

    # Unset (the default) is unchanged: no button, no actions block.
    seen.clear()
    plain = dict(build_notifier({"notifications": {
        "teams_webhook_url": LIVE_URL}}).channels)["teams"]
    assert plain.board_url is None
    assert plain.notify("needs_approval", "PR is up") is True
    assert "actions" not in seen["json"]["attachments"][0]["content"]

    # It is a real config key, defaulting to null; and it is a deep link, not a
    # credential, so unlike the two webhook URLs beside it, it stays readable
    # in /api/config.
    from no_human.api.app import _scrub_secrets
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["notifications"]["board_url"] is None
    out = _scrub_secrets({"notifications": {"board_url": "https://board.example.test"}})
    assert out["notifications"]["board_url"] == "https://board.example.test"


def test_teams_webhook_url_is_a_default_config_key_and_defaults_to_none():
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["notifications"]["teams_webhook_url"] is None


def test_the_teams_webhook_is_scrubbed_from_the_config_endpoint():
    # /api/config scrubs any key matching token|secret|password|webhook|key.
    # A new webhook key must be covered by that, not an exception to it.
    from no_human.api.app import _scrub_secrets
    out = _scrub_secrets({"notifications": {"teams_webhook_url": LIVE_URL}})
    assert "SECRETSIG" not in str(out)
