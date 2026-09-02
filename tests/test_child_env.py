"""`agent/child_env.py`: the launcher's ambient secrets never reach a coder
subprocess, by env-var NAME shape, in both the additive (Claude/SDK) and the
full-copy (Codex/subprocess) shapes of a child environment."""
from no_human.agent.child_env import (
    CLAUDE_CHILD_KEEP,
    CODEX_CHILD_KEEP,
    drop_foreign_secrets,
    is_foreign_secret,
    is_secret_env_name,
    scrub_foreign_secrets_into,
)

_AMBIENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/op",
    "GIT_ASKPASS": "/usr/bin/true",            # operational, not a credential
    "GITHUB_TOKEN": "ghp_x",
    "gitlab_token": "glpat_x",                  # lower-case is still a token
    "AWS_SECRET_ACCESS_KEY": "aws_x",
    "AWS_PROFILE": "work",                      # the pointer to a credential file
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
    "GOOGLE_APPLICATION_CREDENTIALS": "/k.json",
    "JIRA_API_TOKEN": "jira_x",                 # what load_env_var exported
    "MY_VENDOR_APIKEY": "v_x",                  # unknown vendor, caught by shape
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth_x",
    "anthropic_api_key": "sk_x",                # lower-case Anthropic name is still Anthropic
    "NO_HUMAN_AGENT_SESSION_KEY": "1",          # hypothetical mark-family name
    "OPENAI_API_KEY": "sk_openai",
}


def test_secret_shape_is_name_based_and_case_insensitive():
    assert is_secret_env_name("GITHUB_TOKEN")
    assert is_secret_env_name("gitlab_token")
    assert is_secret_env_name("AWS_PROFILE")
    assert is_secret_env_name("SSH_AUTH_SOCK")
    assert not is_secret_env_name("PATH")
    assert not is_secret_env_name("GIT_ASKPASS")  # "ASKPASS" is not PASSWORD/PASSWD


def test_keep_list_is_case_insensitive_like_the_shape_test():
    assert not is_foreign_secret("ANTHROPIC_API_KEY", CLAUDE_CHILD_KEEP)
    assert not is_foreign_secret("anthropic_api_key", CLAUDE_CHILD_KEEP)
    assert is_foreign_secret("OPENAI_API_KEY", CLAUDE_CHILD_KEEP)
    assert not is_foreign_secret("OPENAI_API_KEY", CODEX_CHILD_KEEP)
    assert is_foreign_secret("ANTHROPIC_API_KEY", CODEX_CHILD_KEEP)


def test_additive_scrub_overrides_foreign_secrets_to_empty_and_nothing_else():
    env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1", "GITHUB_TOKEN": "deliberate"}
    blanked = scrub_foreign_secrets_into(env, _AMBIENT)
    # Deliberate entries already in `env` win, even when secret-shaped.
    assert env["GITHUB_TOKEN"] == "deliberate"
    assert "GITHUB_TOKEN" not in blanked
    assert sorted(blanked) == sorted([
        "gitlab_token", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "SSH_AUTH_SOCK",
        "GOOGLE_APPLICATION_CREDENTIALS", "JIRA_API_TOKEN", "MY_VENDOR_APIKEY",
        "OPENAI_API_KEY",
    ])
    assert all(env[name] == "" for name in blanked)
    # Kept names and non-secrets are NOT written into the additive mapping:
    # the child inherits their real value from the launcher.
    for untouched in ("PATH", "HOME", "GIT_ASKPASS", "CLAUDE_CODE_OAUTH_TOKEN",
                      "anthropic_api_key", "NO_HUMAN_AGENT_SESSION_KEY"):
        assert untouched not in env, untouched
    # The effective child environment, as the SDK builds it.
    child = {**_AMBIENT, **env}
    assert child["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth_x"
    assert child["AWS_SECRET_ACCESS_KEY"] == ""
    assert child["PATH"] == _AMBIENT["PATH"]


def test_additive_scrub_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("SOME_VENDOR_TOKEN", "x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "keep")
    env: dict[str, str] = {}
    blanked = scrub_foreign_secrets_into(env)
    assert "SOME_VENDOR_TOKEN" in blanked and env["SOME_VENDOR_TOKEN"] == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_full_env_drop_deletes_foreign_secrets_and_keeps_the_rest():
    env = dict(_AMBIENT)
    dropped = drop_foreign_secrets(env)  # Codex keep-list
    assert sorted(dropped) == sorted([
        "GITHUB_TOKEN", "gitlab_token", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
        "SSH_AUTH_SOCK", "GOOGLE_APPLICATION_CREDENTIALS", "JIRA_API_TOKEN",
        "MY_VENDOR_APIKEY", "CLAUDE_CODE_OAUTH_TOKEN", "anthropic_api_key",
    ])
    assert env == {
        "PATH": "/usr/bin:/bin", "HOME": "/home/op", "GIT_ASKPASS": "/usr/bin/true",
        "NO_HUMAN_AGENT_SESSION_KEY": "1", "OPENAI_API_KEY": "sk_openai",
    }


def test_positive_control_a_secret_free_env_is_left_alone():
    env = {"PATH": "/bin"}
    assert scrub_foreign_secrets_into({}, env) == []
    assert drop_foreign_secrets(env) == []
    assert env == {"PATH": "/bin"}
