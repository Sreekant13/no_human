"""Every field of every integration must carry where-to-find-it help and a real
URL — a new field that lands without one fails the build here (the "cyclic
process is a test" from spec §4 C1). Help text is never inspected for prose;
only that it is non-empty and the URL is an https:// one."""
from no_human.integrations import FIELD_SPECS, setup_specs
from no_human.integrations.help import help_for
from no_human.config import DEFAULT_CONFIG


def test_every_field_of_every_integration_has_help_and_url():
    missing = []
    for name, specs in FIELD_SPECS.items():
        for s in specs:
            text, url = help_for(name, s.name)
            if not text or not url.startswith("https://"):
                missing.append((name, s.name))
    for spec in setup_specs({"integrations": DEFAULT_CONFIG["integrations"]}):
        for f in spec["fields"]:
            if not f.get("help") or not f.get("help_url", "").startswith("https://"):
                missing.append((spec["name"], f["name"]))
    assert missing == [], f"fields without help: {missing}"


def test_nine_integrations_present():
    assert set(FIELD_SPECS) >= {"github", "gitlab", "jira", "monday", "linear",
                                "slack", "teams", "jenkins", "circleci"}
