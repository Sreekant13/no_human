"""WS-F: onboarding captures VCS topology + required credentials; a missing
credential escalates a MISSING_ACCESS blocker naming the exact .env key."""

import subprocess

import pytest

from no_human.blockers import BlockerCategory, missing_access
from no_human.blockers.taxonomy import triage
from no_human.config import credential_status
from no_human.ci.base import CIResult, PipelineStatus
from no_human.onboard import (
    OnboardEngine,
    _host_from_remote,
    _strip_remote_credentials,
    derive_required_credentials,
)
from no_human.profile import ProjectProfile
from no_human.core.task import TaskStatus


# --- VCS host parsing ------------------------------------------------------ #

@pytest.mark.parametrize("url,host", [
    ("https://github.com/org/repo.git", "github.com"),
    ("https://code.example.com/org/repo.git", "code.example.com"),
    ("git@github.com:org/repo.git", "github.com"),
    ("git@code.example.com:org/repo.git", "code.example.com"),
    ("https://x-token-auth:secret@gitlab.acme.net/g/p.git", "gitlab.acme.net"),
    ("ssh://git@gitlab.acme.net:22/g/p.git", "gitlab.acme.net"),
])
def test_host_from_remote(url, host):
    assert _host_from_remote(url) == host


def test_strip_remote_credentials():
    assert _strip_remote_credentials("https://user:tok@host/o/r.git") == "https://host/o/r.git"
    # nothing to strip
    assert _strip_remote_credentials("https://host/o/r.git") == "https://host/o/r.git"


# --- required-credential derivation --------------------------------------- #

def test_creds_always_include_oauth_token():
    keys = derive_required_credentials({}, "")
    assert keys == ["CLAUDE_CODE_OAUTH_TOKEN"]


def test_creds_jenkins_backend():
    keys = derive_required_credentials({"backend": "jenkins"}, "code.example.com",
                                       github_hosts=["github.com", "code.example.com"])
    assert "JENKINS_USER" in keys and "JENKINS_API_TOKEN" in keys
    assert "GH_ENTERPRISE_TOKEN" in keys  # GHE host needs a PR token


def test_creds_gitlab_backend():
    keys = derive_required_credentials({"backend": "gitlab"}, "gitlab.acme.net")
    assert "GITLAB_TOKEN" in keys


def test_creds_public_github_needs_no_pr_token():
    keys = derive_required_credentials({"backend": "github_actions"}, "github.com")
    assert "GH_ENTERPRISE_TOKEN" not in keys


def test_creds_jenkinsfile_human_gated_implies_jenkins_keys():
    keys = derive_required_credentials({}, "github.com",
                                       human_gated_steps=["build/CI gated on Jenkins (Jenkinsfile)"])
    assert "JENKINS_USER" in keys and "JENKINS_API_TOKEN" in keys


def test_creds_dedupe_and_stable_order():
    keys = derive_required_credentials({"backend": "jenkins"}, "code.example.com",
                                       github_hosts=["code.example.com"])
    assert keys[0] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert len(keys) == len(set(keys))


# --- credential status (never echoes values) ------------------------------ #

def test_credential_status_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_TEST_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text("MY_TEST_TOKEN=supersecret\nOTHER=\n")
    status = credential_status(["MY_TEST_TOKEN", "OTHER", "ABSENT"], env_path=env)
    assert status == {"MY_TEST_TOKEN": True, "OTHER": False, "ABSENT": False}


def test_credential_status_reads_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROC_TOKEN", "x")
    status = credential_status(["PROC_TOKEN"], env_path=tmp_path / "nope.env")
    assert status["PROC_TOKEN"] is True


# --- missing_access blocker ------------------------------------------------ #

def test_missing_access_names_the_exact_key():
    b = missing_access("JENKINS_API_TOKEN", system="remote CI", goal="ship the fix")
    assert b.category is BlockerCategory.MISSING_ACCESS
    assert "JENKINS_API_TOKEN" in b.question
    assert "JENKINS_API_TOKEN" in b.root_cause_hypothesis
    assert b.goal == "ship the fix"
    # routes to ESCALATED (needs a human), not a silent park
    assert triage(b).target_status is TaskStatus.ESCALATED


def test_missing_access_round_trips():
    from no_human.blockers import Blocker
    b = missing_access("GH_ENTERPRISE_TOKEN")
    b2 = Blocker.from_dict(b.to_dict())
    assert b2.category is BlockerCategory.MISSING_ACCESS
    assert "GH_ENTERPRISE_TOKEN" in b2.question


# --- CIResult carries the exact key --------------------------------------- #

def test_ciresult_access_env_key_field():
    r = CIResult(pipeline_id="", pipeline_url="", status=PipelineStatus.FAILED,
                 access_failure=True, access_env_key="JENKINS_API_TOKEN")
    assert r.access_env_key == "JENKINS_API_TOKEN"


# --- end-to-end: onboard derives vcs + creds for a real git repo ---------- #

def test_onboard_derives_vcs_and_creds(tmp_path):
    import asyncio

    repo = tmp_path
    (repo / "pyproject.toml").write_text("[project]\nname='d'\nversion='0'\n")
    (repo / "test_d.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "git@code.example.com:org/repo.git"], cwd=repo, check=True)

    engine = OnboardEngine(github_hosts=["github.com", "code.example.com"])
    result = asyncio.run(engine.onboard(repo))
    prof = result.profile
    assert prof.vcs_host == "code.example.com"
    assert prof.vcs_remote == "git@code.example.com:org/repo.git"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in prof.required_credentials
    assert "GH_ENTERPRISE_TOKEN" in prof.required_credentials
    # round-trips through the YAML on disk
    prof.save()
    reloaded = ProjectProfile.load(repo)
    assert reloaded.vcs_host == "code.example.com"
    assert reloaded.required_credentials == prof.required_credentials
