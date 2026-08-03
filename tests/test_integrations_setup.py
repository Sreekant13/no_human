"""The onboarding "Connect your tools" setup surface.

Two things were broken and are covered here:

1. ``teams`` had no ``DEFAULT_CONFIG["integrations"]`` block at all, so no
   config-driven UI could offer it and the only way to reach it was to
   hand-edit YAML.
2. The wizard step configured NOTHING — it listed statuses read-only.

The gate this file exists to hold is the CREDENTIAL one: onboarding writes
``config.yaml`` and only ``config.yaml``, so a field that would put a token in
that world-readable file must be refused. `test_no_discovered_field_is_a
_credential` and `test_apply_setup_refuses_every_credential_shaped_field`
DISCOVER the fields from the defaults rather than listing them, so a NEW
integration block is covered the day it is added — the case a hand-written
list can never catch.

CRITICAL SAFETY: every test monkeypatches no_human.config.CONFIG_PATH and
ENV_PATH onto tmp_path — the real ~/.no_human is never read or written.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import no_human.config as nh_config
import no_human.integrations as reg
from no_human.api.app import app
from no_human.core.db import Store
from no_human.notify import build_notifier

# Field names that are unambiguously credentials. Used as ATTACK input, never
# as the guard's own definition — the guard derives its answer from FIELD_SPECS
# plus a name pattern, so these are an independent oracle.
CREDENTIAL_ATTEMPTS = [
    "api_token", "api_key", "token", "secret", "password", "webhook_url",
    "access_key", "private_key", "oauth_token", "bot_token", "app_token",
    "signature", "session_cookie", "credentials", "pat",
]


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    for var in ("JIRA_API_TOKEN", "LINEAR_API_KEY", "CIRCLECI_TOKEN",
                "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# --------------------------------------------------------------------------- #
# Gap 1: Teams exists in the default config                                    #
# --------------------------------------------------------------------------- #

def test_teams_has_a_default_config_block():
    blocks = nh_config.DEFAULT_CONFIG["integrations"]
    assert "teams" in blocks, (
        "notify/teams.py is wired into the dispatcher and tested, but with no "
        "config block nothing can offer it to a user"
    )
    assert blocks["teams"]["enabled"] is True, (
        "default must be ON: a False default would silently stop delivery for "
        "every install that already has notifications.teams_webhook_url set"
    )


def test_teams_block_does_not_duplicate_the_webhook_url():
    """One source of truth per setting. The URL stays where build_notifier
    already reads it, and it is a secret besides."""
    assert "webhook_url" not in nh_config.DEFAULT_CONFIG["integrations"]["teams"]
    assert "teams_webhook_url" in nh_config.DEFAULT_CONFIG["notifications"]


def test_every_registry_notification_integration_now_has_a_block():
    """slack and teams are both notify-side registry entries; before this
    change only slack had a config block."""
    blocks = nh_config.DEFAULT_CONFIG["integrations"]
    assert {"jira", "linear", "circleci", "slack", "teams"} <= set(blocks)


@pytest.mark.parametrize("enabled,expect", [(True, True), (False, False), (None, True)])
def test_teams_enabled_is_a_mute_switch_honoured_by_the_dispatcher(enabled, expect):
    """`integrations.teams.enabled` is observed, not decoration. None = the
    key is absent entirely (a config that predates it), which must behave
    exactly as before."""
    cfg = {"notifications": {"teams_webhook_url":
                             "https://x.logic.azure.com/workflows/a?sig=s"}}
    if enabled is not None:
        cfg["integrations"] = {"teams": {"enabled": enabled}}
    teams = dict(build_notifier(cfg).channels)["teams"]
    assert teams.enabled is expect


def test_teams_mute_switch_survives_a_null_integrations_section():
    """Deep-merge shadowing trap: `integrations:` set to null must not crash
    the notifier and must not mute the channel."""
    n = build_notifier({"integrations": None,
                        "notifications": {"teams_webhook_url": "https://x/y?sig=s"}})
    assert dict(n.channels)["teams"].enabled is True
    # `teams:` present but null, and `enabled:` present but null, are the same
    # trap one level down — neither may silently mute a configured channel.
    for shadowed in ({"teams": None}, {"teams": {"enabled": None}}):
        n = build_notifier({"integrations": shadowed,
                            "notifications": {"teams_webhook_url": "https://x/y?sig=s"}})
        assert dict(n.channels)["teams"].enabled is True, shadowed


# --------------------------------------------------------------------------- #
# Discovery: the wizard is generated from the defaults, not from a name list   #
# --------------------------------------------------------------------------- #

def test_setup_specs_cover_exactly_the_default_config_blocks():
    names = [s["name"] for s in reg.setup_specs({})]
    assert names == list(nh_config.DEFAULT_CONFIG["integrations"])


def test_a_new_integration_block_appears_with_no_ui_change(monkeypatch):
    """The requirement in one test: a SIXTH integration must show up in the
    wizard without anyone editing the UI or this module."""
    blocks = dict(nh_config.DEFAULT_CONFIG["integrations"])
    blocks["pagerduty"] = {"enabled": False, "service_key_id": "", "routing": ""}
    merged = dict(nh_config.DEFAULT_CONFIG)
    merged["integrations"] = blocks
    monkeypatch.setattr(nh_config, "DEFAULT_CONFIG", merged)

    specs = {s["name"]: s for s in reg.setup_specs({})}
    assert "pagerduty" in specs
    new = specs["pagerduty"]
    assert new["enable_field"] == "enabled"
    assert [f["name"] for f in new["fields"]] == ["enabled", "service_key_id", "routing"]
    assert [f["label"] for f in new["fields"]] == ["Enabled", "Service key id", "Routing"]


def test_labels_agree_with_the_settings_panel_where_both_describe_a_field():
    """The wizard and Settings → Integrations must not name the same setting
    two different ways ("Site URL" vs "Site"). FIELD_SPECS' label wins when it
    has one; a generated label is the fallback, not the default."""
    jira = {s["name"]: s for s in reg.setup_specs({})}["jira"]
    labels = {f["name"]: f["label"] for f in jira["fields"]}
    assert labels["site"] == "Site URL"
    assert labels["jql"] == "JQL filter"
    assert labels["project_key"] == "Project key"
    # No FieldSpec describes `write_back`, so it falls back to generated.
    assert labels["write_back"] == "Write back"


def test_setup_spec_names_the_env_var_but_never_a_value(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_SUPERSECRET")
    (tmp_path / ".env").write_text("LINEAR_API_KEY=lin_api_SUPERSECRET\n")
    specs = {s["name"]: s for s in reg.setup_specs({})}
    linear = specs["linear"]
    assert [s["env_var"] for s in linear["secrets"]] == ["LINEAR_API_KEY"]
    assert linear["secrets"][0]["set"] is True
    assert "SUPERSECRET" not in str(specs)


def test_teams_secret_note_points_at_settings_not_at_a_config_field():
    """Teams' webhook is a secret that the code reads from config.yaml. The
    wizard must NOT collect it — it must say where it goes instead."""
    teams = {s["name"]: s for s in reg.setup_specs({})}["teams"]
    assert teams["secrets"] == []
    assert "Settings" in teams["secret_note"]
    assert [f["name"] for f in teams["fields"]] == ["enabled"]


# --------------------------------------------------------------------------- #
# The credential gate                                                          #
# --------------------------------------------------------------------------- #

def test_no_discovered_field_is_a_credential():
    """Every field the wizard offers, for every integration that exists —
    discovered, so a new block is covered the day it lands."""
    secret_paths = {
        s.config_path
        for specs in reg.FIELD_SPECS.values() for s in specs
        if s.secret and s.config_path
    }
    for spec in reg.setup_specs({}):
        for f in spec["fields"]:
            dotted = f"integrations.{spec['name']}.{f['name']}"
            assert not reg.is_credential_name(f["name"]), (
                f"{dotted} would persist a credential into config.yaml")
            assert dotted not in secret_paths, (
                f"{dotted} is declared secret by FIELD_SPECS")


@pytest.mark.parametrize("field", CREDENTIAL_ATTEMPTS)
def test_apply_setup_refuses_every_credential_shaped_field(field):
    """Cross-product: every integration that exists × every credential-shaped
    field name. Nothing may be written and config.yaml must not appear."""
    for name in nh_config.DEFAULT_CONFIG["integrations"]:
        # Refused BY THE CREDENTIAL RULE — not incidentally, by the
        # "unknown setting" check that would also reject it. See
        # assert_config_safe_field's note on why that ordering matters.
        with pytest.raises(ValueError, match="credential"):
            reg.apply_setup(name, {field: "SUPERSECRET-VALUE"})
    assert not nh_config.CONFIG_PATH.exists()


def test_apply_setup_refusal_leaves_an_existing_config_byte_identical():
    reg.apply_setup("linear", {"enabled": True, "team_key": "ENG"})
    before = nh_config.CONFIG_PATH.read_bytes()
    with pytest.raises(ValueError):
        reg.apply_setup("linear", {"team_key": "ENG2", "api_key": "lin_api_LEAK"})
    assert nh_config.CONFIG_PATH.read_bytes() == before, (
        "validation must happen before ANY write — a refused field left the "
        "file half-updated")
    assert b"LEAK" not in before


def test_setting_names_that_are_not_credentials_still_pass():
    """The guard must not be so broad it blocks real settings — `team_key`
    and `project_key` end in `_key` and are not credentials."""
    assert reg.is_credential_name("team_key") is False
    assert reg.is_credential_name("project_key") is False
    assert reg.is_credential_name("default_repo") is False
    reg.assert_config_safe_field("linear", "team_key")
    reg.assert_config_safe_field("jira", "project_key")


def test_a_new_block_that_ships_a_credential_key_is_refused_not_offered(monkeypatch):
    """The case the credential guard actually exists for, and the one a
    hand-written list of field names can never catch: someone adds a NEW
    integration block and puts a token key in it. The wizard must neither
    render it nor write it — even though it IS a legitimate key of that
    block, so the "unknown field" check does not fire."""
    blocks = dict(nh_config.DEFAULT_CONFIG["integrations"])
    blocks["pagerduty"] = {
        "enabled": False,
        "service": "",
        "api_token": "",        # the mistake
        "routing_key": "",      # ...and a subtler one
    }
    merged = dict(nh_config.DEFAULT_CONFIG)
    merged["integrations"] = blocks
    monkeypatch.setattr(nh_config, "DEFAULT_CONFIG", merged)

    offered = {f["name"] for f in
               {s["name"]: s for s in reg.setup_specs({})}["pagerduty"]["fields"]}
    assert offered == {"enabled", "service"}, (
        "a credential key of a new block must never be rendered as a field")

    for field in ("api_token", "routing_key"):
        with pytest.raises(ValueError, match="credential"):
            reg.apply_setup("pagerduty", {field: "SUPERSECRET-VALUE"})
    assert not nh_config.CONFIG_PATH.exists()

    # ...while the non-credential keys of the same block still work.
    reg.apply_setup("pagerduty", {"enabled": True, "service": "svc"})
    on_disk = nh_config.CONFIG_PATH.read_text()
    assert "svc" in on_disk
    assert "SUPERSECRET" not in on_disk


def test_apply_setup_refuses_an_unknown_field():
    with pytest.raises(ValueError):
        reg.apply_setup("linear", {"not_a_setting": "x"})


# --------------------------------------------------------------------------- #
# What the operator actually gets: an ENABLED integration and no secret        #
# --------------------------------------------------------------------------- #

def test_wizard_result_is_an_enabled_working_linear_config_with_no_secret():
    reg.apply_setup("linear", {"enabled": True, "team_key": "ENG", "label": "bug"})

    text = nh_config.CONFIG_PATH.read_text()
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    lin = cfg["integrations"]["linear"]

    # 1. It is ON, by the EXACT predicate `nh serve` gates the poller on
    #    (cli/commands.py: `linear_cfg.get("enabled")`).
    assert ((cfg.get("integrations") or {}).get("linear") or {}).get("enabled") is True
    # 2. It carries the settings the poller needs.
    assert lin["team_key"] == "ENG"
    assert lin["label"] == "bug"
    # 3. And no credential landed in the world-readable file.
    assert "LINEAR_API_KEY" not in text
    assert "api_key" not in text
    assert "lin_api" not in text


def test_wizard_result_turns_jira_on_too():
    reg.apply_setup("jira", {"enabled": True, "site": "https://acme.atlassian.net",
                             "project_key": "PROJ", "email": "me@x.com"})
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["jira"]["enabled"] is True
    assert cfg["integrations"]["jira"]["project_key"] == "PROJ"
    assert "JIRA_API_TOKEN" not in nh_config.CONFIG_PATH.read_text()


def test_apply_setup_does_not_write_the_env_file_at_all():
    reg.apply_setup("linear", {"enabled": True, "team_key": "ENG"})
    assert not nh_config.ENV_PATH.exists(), (
        "the onboarding route is config-only; it must never touch the "
        "credential store")


def test_apply_setup_preserves_unrelated_config_keys():
    nh_config.CONFIG_PATH.write_text("llm:\n  auth_mode: subscription\n")
    reg.apply_setup("linear", {"enabled": True, "team_key": "ENG"})
    import yaml
    on_disk = yaml.safe_load(nh_config.CONFIG_PATH.read_text())
    assert on_disk["llm"]["auth_mode"] == "subscription"
    assert on_disk["integrations"]["linear"]["team_key"] == "ENG"


def test_list_field_accepts_a_comma_string_and_a_list():
    reg.apply_setup("linear", {"state_types": "triage, backlog"})
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["state_types"] == ["triage", "backlog"]
    reg.apply_setup("linear", {"state_types": ["unstarted"]})
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["state_types"] == ["unstarted"]


def test_multiline_text_value_is_refused():
    with pytest.raises(ValueError):
        reg.apply_setup("linear", {"team_key": "ENG\nother: value"})
    assert not nh_config.CONFIG_PATH.exists()


# --------------------------------------------------------------------------- #
# Honest enabled/disabled state (Settings and the wizard agree with reality)   #
# --------------------------------------------------------------------------- #

def test_status_reports_configured_but_off_honestly():
    """The "I don't have Linear" report: every setting filled in, poller never
    starts, panel said "Configured". `enabled` now says so."""
    cfg = {"integrations": {"linear": {"team_key": "ENG", "enabled": False}}}
    st = {s.name: s for s in reg.list_integrations(cfg)}
    assert st["linear"].configured is True
    assert st["linear"].enabled is False


def test_status_enabled_is_none_for_integrations_with_no_switch():
    st = {s.name: s for s in reg.list_integrations({})}
    for name in ("github", "gitlab", "jenkins"):
        assert st[name].enabled is None, "these are views over ci.*, not own blocks"
    assert st["teams"].enabled is True     # default-on mute switch
    assert st["slack"].enabled is False    # `intake`, default off
    assert st["jira"].enabled is False


def test_enable_field_is_discovered_not_named():
    assert reg.enable_field("jira") == "enabled"
    assert reg.enable_field("linear") == "enabled"
    assert reg.enable_field("teams") == "enabled"
    # slack's switch is its single boolean, `intake` — found, not hardcoded.
    assert reg.enable_field("slack") == "intake"
    assert reg.enable_field("github") is None


# --------------------------------------------------------------------------- #
# API surface                                                                  #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store):
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        yield c


@pytest.mark.asyncio
async def test_get_setup_lists_every_default_block(client):
    r = await client.get("/api/integrations/setup")
    assert r.status_code == 200, r.text
    names = [s["name"] for s in r.json()["integrations"]]
    assert names == list(nh_config.DEFAULT_CONFIG["integrations"])
    assert "teams" in names


@pytest.mark.asyncio
async def test_put_setup_enables_linear_and_writes_no_secret(client):
    r = await client.put("/api/integrations/linear/setup",
                         json={"values": {"enabled": True, "team_key": "ENG"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "linear"
    assert body["enabled"] is True
    assert body["configured"] is True

    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["enabled"] is True
    assert "LINEAR_API_KEY" not in nh_config.CONFIG_PATH.read_text()
    # And app.state was refreshed, so the next GET agrees.
    r2 = await client.get("/api/integrations/setup")
    linear = {s["name"]: s for s in r2.json()["integrations"]}["linear"]
    assert linear["enabled"] is True
    assert {f["name"]: f["value"] for f in linear["fields"]}["team_key"] == "ENG"


@pytest.mark.asyncio
async def test_put_setup_rejects_a_credential_with_422(client):
    r = await client.put("/api/integrations/linear/setup",
                         json={"values": {"api_key": "lin_api_LEAKME"}})
    assert r.status_code == 422, r.text
    assert "LEAKME" not in r.text
    # load_config materialises a default config.yaml, so the assertion is that
    # nothing from the refused call reached it — not that the file is absent.
    assert "LEAKME" not in nh_config.CONFIG_PATH.read_text()
    assert "api_key" not in nh_config.CONFIG_PATH.read_text()


@pytest.mark.asyncio
async def test_put_setup_unknown_integration_422(client):
    r = await client.put("/api/integrations/mystery/setup",
                         json={"values": {"enabled": True}})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_setup_requires_a_local_origin(client):
    r = await client.put("/api/integrations/linear/setup",
                         json={"values": {"enabled": True}},
                         headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403, r.text
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["enabled"] is False


@pytest.mark.asyncio
async def test_list_integrations_endpoint_reports_enabled(client):
    await client.put("/api/integrations/linear/setup",
                     json={"values": {"enabled": True, "team_key": "ENG"}})
    r = await client.get("/api/integrations")
    by_name = {s["name"]: s for s in r.json()["integrations"]}
    assert by_name["linear"]["enabled"] is True
    assert by_name["github"]["enabled"] is None
