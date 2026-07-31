"""Jenkins CI backend (Phase 6): watch/trigger/human-gated, infra-vs-real
classification, log/test-report parsing — proven against recorded-JSON fixtures
via the _run_cmd seam (no live server). Plus the relatedness triage that decides
ours-vs-pre-existing before the orchestrator burns fix attempts."""

from __future__ import annotations

import json

import pytest

from no_human.ci import ci_from_config
from no_human.ci.base import CIResult, HumanGatedCI, JobResult, PipelineStatus
from no_human.ci.jenkins import JenkinsCI, _console_is_infra
from no_human.core.orchestrator import _ci_failure_unrelated


# --------------------------------------------------------------------------- #
# A fake curl runner that routes by URL (robust to call ordering/poll counts). #
# Each route maps to a list of responses; the last one repeats.               #
# --------------------------------------------------------------------------- #

class FakeJenkins:
    def __init__(self, routes: dict[str, list]):
        self._routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]):
        self.calls.append(cmd)
        url = cmd[-1] if cmd else ""
        # Most-specific route first.
        for key in ("testReport", "consoleText", "buildWithParameters", "api/json"):
            if key in url and key in self._routes:
                seq = self._routes[key]
                if not seq:
                    return None
                return seq[0] if len(seq) == 1 else seq.pop(0)
        return None


def _meta(building=False, result="SUCCESS", number=7):
    return json.dumps({"building": building, "result": result,
                       "number": number, "url": f"https://b/job/x/{number}/"})


def _testreport(failed_cases):
    return json.dumps({
        "failCount": len(failed_cases),
        "suites": [{"cases": [
            {"className": c[0], "name": c[1], "status": "FAILED",
             "errorDetails": c[2] if len(c) > 2 else ""}
            for c in failed_cases
        ]}],
    })


JOB = "job/acme-universe/job/acme-core-test-master/job/PR-042"


# --------------------------------------------------------------------------- #
# watch mode                                                                   #
# --------------------------------------------------------------------------- #

async def test_watch_success():
    fake = FakeJenkins({"api/json": [_meta(result="SUCCESS")]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.passed
    assert not r.infra_failure
    assert r.pipeline_id == "7"


async def test_watch_polls_until_terminal():
    fake = FakeJenkins({"api/json": [
        _meta(building=True), _meta(building=True), _meta(result="SUCCESS"),
    ]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.passed


async def test_watch_real_test_failure_is_not_infra():
    fake = FakeJenkins({
        "api/json": [_meta(result="FAILURE")],
        "testReport": [_testreport([
            ("com.acme.analytics-export.api.tests.analyticsexport.AnalyticsExportE2EIT", "testExport",
             "expected 202 but got 400"),
        ])],
        "consoleText": ["BUILD FAILED\nTests run: 5, Failures: 1"],
    })
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.failed
    assert not r.infra_failure
    assert any("AnalyticsExportE2EIT" in j.name for j in r.jobs)
    assert "testExport" in r.parsed_output
    assert "expected 202" in r.parsed_output


async def test_watch_infra_signature_with_no_test_failures_is_infra():
    fake = FakeJenkins({
        "api/json": [_meta(result="FAILURE")],
        "testReport": [json.dumps({"failCount": 0, "suites": []})],
        "consoleText": ["Cannot contact build-agent-7: hudson.remoting."
                        "ChannelClosedException: channel is already closed"],
    })
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, max_infra_retries=0,
                   _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.failed
    assert r.infra_failure


async def test_watch_aborted_is_infra():
    fake = FakeJenkins({
        "api/json": [_meta(result="ABORTED")],
        "testReport": [None],
        "consoleText": ["Build was aborted"],
    })
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, max_infra_retries=0,
                   _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.infra_failure
    assert r.status == PipelineStatus.CANCELED


async def test_watch_unreachable_status_is_infra_not_green():
    """§3.4: an unreachable build must never read as green."""
    fake = FakeJenkins({"api/json": [None]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, max_infra_retries=0,
                   _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert not r.passed
    assert r.infra_failure


async def test_unstable_with_failures_is_real():
    fake = FakeJenkins({
        "api/json": [_meta(result="UNSTABLE")],
        "testReport": [_testreport([("pkg.FooIT", "testBar", "boom")])],
        "consoleText": ["finished: UNSTABLE"],
    })
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.failed
    assert not r.infra_failure


# --------------------------------------------------------------------------- #
# trigger mode + human-gated mode                                              #
# --------------------------------------------------------------------------- #

async def test_human_gated_mode_parks():
    ci = JenkinsCI(JOB, mode="human_gated", _run_cmd=FakeJenkins({}))
    with pytest.raises(HumanGatedCI):
        await ci.trigger("PR-042")


async def test_trigger_mode_posts_then_polls():
    fake = FakeJenkins({
        "buildWithParameters": [""],          # 201 empty body → success
        "api/json": [_meta(result="SUCCESS")],
    })
    ci = JenkinsCI(JOB, mode="trigger", poll_interval=0, _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.passed
    assert any("buildWithParameters" in (c[-1] if c else "") for c in fake.calls)


async def test_trigger_post_failure_is_infra():
    fake = FakeJenkins({"buildWithParameters": [None]})  # curl -f non-zero
    ci = JenkinsCI(JOB, mode="trigger", poll_interval=0, max_infra_retries=0,
                   _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.infra_failure


async def test_infra_retry_then_success(monkeypatch):
    import no_human.ci.jenkins as jmod
    monkeypatch.setattr(jmod.asyncio, "sleep", _noop_sleep)
    state = {"n": 0}

    def run(cmd):
        url = cmd[-1] if cmd else ""
        if "api/json" in url:
            state["n"] += 1
            return None if state["n"] == 1 else _meta(result="SUCCESS")
        return None

    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, max_infra_retries=2,
                   _run_cmd=run)
    r = await ci.trigger("PR-042")
    assert r.passed


async def _noop_sleep(_):
    return None


# --------------------------------------------------------------------------- #
# auth: credentials reach curl but come from env, never config                 #
# --------------------------------------------------------------------------- #

async def test_credentials_passed_to_curl_when_present():
    fake = FakeJenkins({"api/json": [_meta(result="SUCCESS")]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0,
                   user="svc", token="secrettok", _run_cmd=fake)
    await ci.trigger("PR-042")
    joined = " ".join(" ".join(c) for c in fake.calls)
    assert "svc:secrettok" in joined  # -u user:token reaches curl


def test_console_infra_detector():
    assert _console_is_infra("ERROR: Cannot contact node-3")
    assert _console_is_infra("hudson.remoting.ChannelClosedException")
    assert not _console_is_infra("AssertionError: expected 202 but got 400")


# --------------------------------------------------------------------------- #
# access (401/403) is distinct from infra — and is NOT retried                 #
# --------------------------------------------------------------------------- #

def _with_http(body: str, code: int) -> str:
    # Mimic the real curl -w marker the backend appends.
    from no_human.ci.jenkins import _HTTP_MARKER
    return f"{body}\n{_HTTP_MARKER}{code}"


async def test_403_is_access_not_infra_and_not_retried():
    calls = {"n": 0}

    def run(cmd):
        calls["n"] += 1
        return _with_http("<html>Forbidden</html>", 403)

    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, max_infra_retries=2,
                   user="svc", token="bad", _run_cmd=run)
    r = await ci.trigger("PR-042")
    assert r.access_failure
    assert not r.infra_failure
    assert not r.passed
    # Access failure must NOT be retried (retrying a 403 is futile).
    assert calls["n"] == 1
    assert "JENKINS_API_TOKEN" in r.parsed_output


async def test_401_is_access():
    fake = FakeJenkins({"api/json": [_with_http("auth required", 401)]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, max_infra_retries=0,
                   _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.access_failure


async def test_500_is_infra_not_access():
    fake = FakeJenkins({"api/json": [_with_http("Internal Server Error", 500)]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, max_infra_retries=0,
                   _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.infra_failure
    assert not r.access_failure


async def test_trigger_mode_403_on_post_is_access():
    fake = FakeJenkins({"buildWithParameters": [_with_http("", 403)]})
    ci = JenkinsCI(JOB, mode="trigger", poll_interval=0, max_infra_retries=0,
                   _run_cmd=fake)
    r = await ci.trigger("PR-042")
    assert r.access_failure


def test_interpret_curl_marker_parsing():
    from no_human.ci.jenkins import _interpret_curl
    assert _interpret_curl(None) == (None, "infra")
    assert _interpret_curl('{"ok":1}') == ('{"ok":1}', "ok")  # no marker → ok
    body, kind = _interpret_curl(_with_http('{"building":false}', 200))
    assert kind == "ok" and body == '{"building":false}'
    assert _interpret_curl(_with_http("x", 403))[1] == "access"
    assert _interpret_curl(_with_http("x", 503))[1] == "infra"


# --------------------------------------------------------------------------- #
# factory wiring                                                               #
# --------------------------------------------------------------------------- #

def test_ci_from_config_jenkins():
    ci = ci_from_config({"ci": {
        "enabled": True, "backend": "jenkins", "job": JOB,
        "base_url": "https://build.example.com", "mode": "watch",
    }})
    assert isinstance(ci, JenkinsCI)
    assert ci.mode == "watch"
    assert ci.job == JOB


def test_ci_from_config_jenkins_no_job_is_none():
    assert ci_from_config({"ci": {"enabled": True, "backend": "jenkins"}}) is None


# --------------------------------------------------------------------------- #
# relatedness triage (Phase 6.3)                                               #
# --------------------------------------------------------------------------- #

def _ci_with_failures(names):
    return CIResult(
        pipeline_id="7", pipeline_url="https://b/7", status=PipelineStatus.FAILED,
        jobs=[JobResult(name=n, status="failed") for n in names],
    )


def test_unrelated_when_failing_tests_not_in_diff():
    ci = _ci_with_failures(["com.acme.billing.InvoiceIT.testTotals"])
    changed = ["src/test/java/com/acme/analytics-export/AnalyticsExportE2EIT.java"]
    evidence = _ci_failure_unrelated(ci, changed)
    assert evidence is not None
    assert "InvoiceIT" in evidence


def test_related_when_failing_test_matches_changed_file():
    ci = _ci_with_failures(["com.acme.analytics-export.api.tests.analyticsexport.AnalyticsExportE2EIT.testExport"])
    changed = ["analytics-export-tests/.../AnalyticsExportE2EIT.java"]
    assert _ci_failure_unrelated(ci, changed) is None


def test_attribution_unknown_returns_none():
    # No failing-test names → cannot attribute → fix loop, never skip.
    ci = CIResult(pipeline_id="7", pipeline_url="", status=PipelineStatus.FAILED)
    assert _ci_failure_unrelated(ci, ["a/B.java"]) is None
    # No diff info → cannot attribute.
    assert _ci_failure_unrelated(_ci_with_failures(["x.Y.z"]), []) is None


def test_partial_overlap_routes_to_fix_loop():
    # One related + one unrelated → NOT all-unrelated → fix loop (None).
    ci = _ci_with_failures([
        "com.acme.analytics-export.AnalyticsExportE2EIT.testExport",   # related
        "com.acme.billing.InvoiceIT.testTotals",    # unrelated
    ])
    changed = ["x/AnalyticsExportE2EIT.java"]
    assert _ci_failure_unrelated(ci, changed) is None


# --------------------------------------------------------------------------- #
# cookie auth: session cookie jar + CSRF crumb + refresh-on-access            #
# --------------------------------------------------------------------------- #

_MARK = "\n__NH_HTTP__"


def _ok(body: str = "") -> str:
    return body + _MARK + "200"


def _denied() -> str:
    return "" + _MARK + "403"


class RecordingCurl:
    """Fake _run_cmd that routes by URL substring and records every cmd list.
    Route values are raw curl outputs (body + _HTTP_MARKER + code); the last
    entry in a route repeats."""

    def __init__(self, routes: dict[str, list]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]):
        self.calls.append(cmd)
        url = cmd[-1] if cmd else ""
        for key in ("crumbIssuer", "testReport", "consoleText",
                    "buildWithParameters", "api/json"):
            if key in url and key in self.routes:
                seq = self.routes[key]
                return seq[0] if len(seq) == 1 else seq.pop(0)
        return None

    def calls_to(self, key: str) -> list[list[str]]:
        return [c for c in self.calls if key in (c[-1] if c else "")]


async def test_cookie_auth_sends_cookie_jar():
    rec = RecordingCurl({"api/json": [_ok(_meta(result="SUCCESS"))]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, auth="cookie",
                   cookie_provider=lambda force: {"JSESSIONID.x": "abc"},
                   _run_cmd=rec)
    r = await ci.trigger("PR-1")
    assert r.passed
    api_call = rec.calls_to("api/json")[0]
    assert "-b" in api_call
    assert "JSESSIONID.x=abc" in " ".join(api_call)
    # No basic-auth under cookie mode.
    assert "-u" not in api_call


async def test_cookie_trigger_attaches_crumb():
    rec = RecordingCurl({
        "crumbIssuer": [_ok(json.dumps(
            {"crumbRequestField": "Jenkins-Crumb", "crumb": "CR123"}))],
        "buildWithParameters": [_ok("")],
        "api/json": [_ok(_meta(result="SUCCESS"))],
    })
    ci = JenkinsCI(JOB, mode="trigger", poll_interval=0, auth="cookie",
                   crumb_path="cjoc/crumbIssuer/api/json",
                   cookie_provider=lambda force: {"JSESSIONID.x": "abc"},
                   _run_cmd=rec)
    r = await ci.trigger("PR-1")
    assert r.passed
    post = rec.calls_to("buildWithParameters")[0]
    joined = " ".join(post)
    assert "POST" in post
    assert "Jenkins-Crumb: CR123" in joined
    assert "JSESSIONID.x=abc" in joined
    # The crumb issuer was actually consulted.
    assert rec.calls_to("crumbIssuer")


async def test_cookie_get_does_not_send_crumb():
    rec = RecordingCurl({
        "crumbIssuer": [_ok(json.dumps(
            {"crumbRequestField": "Jenkins-Crumb", "crumb": "CR123"}))],
        "api/json": [_ok(_meta(result="SUCCESS"))],
    })
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, auth="cookie",
                   cookie_provider=lambda force: {"JSESSIONID.x": "abc"},
                   _run_cmd=rec)
    await ci.trigger("PR-1")
    # Read-only GET path must not fetch or attach a crumb.
    assert not rec.calls_to("crumbIssuer")
    assert all("Jenkins-Crumb" not in " ".join(c) for c in rec.calls)


async def test_cookie_refresh_on_access_wall():
    forced: list[bool] = []

    def provider(force):
        forced.append(force)
        return {"JSESSIONID.x": "new"} if force else {"JSESSIONID.x": "old"}

    rec = RecordingCurl({"api/json": [_denied(), _ok(_meta(result="SUCCESS"))]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, auth="cookie",
                   cookie_provider=provider, _run_cmd=rec)
    r = await ci.trigger("PR-1")
    assert r.passed
    assert True in forced  # a forced refresh happened
    api_calls = rec.calls_to("api/json")
    assert any("JSESSIONID.x=new" in " ".join(c) for c in api_calls)


async def test_cookie_access_wall_when_refresh_fails():
    rec = RecordingCurl({"api/json": [_denied(), _denied()]})
    ci = JenkinsCI(JOB, mode="watch", poll_interval=0, max_infra_retries=0,
                   auth="cookie",
                   cookie_provider=lambda force: {} if force else {"s": "old"},
                   _run_cmd=rec)
    r = await ci.trigger("PR-1")
    assert r.access_failure
    assert r.access_env_key == "SSO_PASSWORD"


# --------------------------------------------------------------------------- #
# jenkins_session module                                                       #
# --------------------------------------------------------------------------- #

def test_load_cookies_filters_by_host(tmp_path):
    from no_human.ci.jenkins_session import load_cookies
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"cookies": [
        {"name": "JSESSIONID.x", "value": "v",
         "domain": "build.example.com", "path": "/cjoc"},
        {"name": "other", "value": "z", "domain": "other.example.org", "path": "/"},
    ]}))
    ck = load_cookies(str(state), "https://build.example.com")
    assert ck == {"JSESSIONID.x": "v"}


def test_load_cookies_missing_file_returns_empty():
    from no_human.ci.jenkins_session import load_cookies
    assert load_cookies("/no/such/file.json", "https://build.example.com") == {}


def test_get_session_cookies_uses_existing_without_refresh(tmp_path, monkeypatch):
    from no_human.ci import jenkins_session as js
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"cookies": [
        {"name": "a", "value": "b", "domain": "build.example.com", "path": "/"},
    ]}))

    def _boom(*a, **k):
        raise AssertionError("refresh must not be called when cookies exist")

    monkeypatch.setattr(js, "refresh", _boom)
    assert js.get_session_cookies(
        "https://build.example.com", str(state)) == {"a": "b"}


def test_get_session_cookies_refreshes_when_empty(tmp_path, monkeypatch):
    from no_human.ci import jenkins_session as js
    state = tmp_path / "s.json"  # does not exist yet

    def fake_refresh(base_url, sp, **k):
        open(sp, "w").write(json.dumps({"cookies": [
            {"name": "a", "value": "b",
             "domain": "build.example.com", "path": "/"},
        ]}))
        return True

    monkeypatch.setattr(js, "refresh", fake_refresh)
    ck = js.get_session_cookies("https://build.example.com", str(state),
                                auto_refresh=True)
    assert ck == {"a": "b"}


# --------------------------------------------------------------------------- #
# factory wiring for cookie auth                                               #
# --------------------------------------------------------------------------- #

def test_ci_from_config_jenkins_cookie_auth():
    ci = ci_from_config({"ci": {
        "enabled": True, "backend": "jenkins", "job": JOB,
        "auth": "cookie", "crumb_path": "cjoc/crumbIssuer/api/json",
    }})
    assert isinstance(ci, JenkinsCI)
    assert ci.auth == "cookie"
    assert ci.crumb_path == "cjoc/crumbIssuer/api/json"


def test_ci_from_config_jenkins_defaults_to_token_auth():
    ci = ci_from_config({"ci": {
        "enabled": True, "backend": "jenkins", "job": JOB,
    }})
    assert isinstance(ci, JenkinsCI)
    assert ci.auth == "token"
