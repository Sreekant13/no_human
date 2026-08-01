"""Local stand-ins for the three SaaS systems the scenario team runs on.

READ THIS BEFORE BELIEVING ANY RESULT THIS MODULE PRODUCES
----------------------------------------------------------
Nothing in here is a live integration and nothing in here should ever be
reported as one. These are in-process HTTP servers on 127.0.0.1 that speak
enough of the real protocol to exercise **our adapter code**. What they prove
and what they cannot prove:

  PROVES      that our request shape, auth scheme, URL construction, response
              parsing, pagination and write-back logic do what we say they do,
              and that a config the docs describe actually reaches the adapter.
  DOES NOT    prove anything about the real vendor: not their auth, not their
  PROVE       rate limits, not their field semantics, not their deprecations,
              and not whether a token with the scopes we ask for can do the
              writes we perform. A green run here is compatible with a totally
              broken live integration.

The harness labels every result from this module ``live=False`` and the friction
log prints the boundary in full. If you find yourself quoting a number from a
fake run as evidence the integration works, that is the failure this docstring
exists to prevent.

GitHub is the exception and is handled elsewhere: the product already ships a
local **bare git repository** backend (``vcs/__init__.py``, the ``local`` kind)
for exactly this offline case, so the harness pushes to a real local bare repo
and gets a real branch and a real ``local-pr://`` marker back. That is a genuine
push through the genuine code path — but it is still not ``gh pr create``, and
the harness says so.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


# --------------------------------------------------------------------------- #
# Fake Jira Cloud
# --------------------------------------------------------------------------- #

def _adf(text: str) -> dict[str, Any]:
    """Wrap text in Atlassian Document Format, the way Jira Cloud really does.

    Deliberately NOT a plain string. The real API returns ADF for descriptions
    and a fake that returns a bare string would silently excuse a parser that
    cannot handle the real thing.
    """
    paras = [p for p in text.split("\n\n") if p.strip()]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]}
            for p in paras
        ],
    }


@dataclass
class JiraState:
    """Everything the fake knows, plus everything it was asked to do."""

    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Writes the product attempted, in order. The harness asserts on these.
    comments: list[tuple[str, str]] = field(default_factory=list)
    transitions_applied: list[tuple[str, str]] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    auth_seen: list[str] = field(default_factory=list)
    unknown_paths: list[str] = field(default_factory=list)


def _issue(key: str, summary: str, description: str, issue_type: str,
           status: str = "To Do", category: str = "To Do",
           labels: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "key": key,
        "id": str(abs(hash(key)) % 100000),
        "fields": {
            "summary": summary,
            "description": _adf(description),
            "status": {"name": status, "statusCategory": {"name": category}},
            "issuetype": {"name": issue_type},
            "labels": list(labels),
        },
    }


_TYPE_FOR_KIND = {
    "feature": "Story",
    "bug": "Bug",
    "refactor": "Task",
    "investigation": "Task",
    "chore": "Task",
    "design": "Task",
}


def state_from_backlog(backlog) -> JiraState:
    """Load the aviation backlog into the fake as real-shaped Jira issues."""
    st = JiraState()
    for t in backlog:
        body = t.description
        if t.criteria:
            body += "\n\nAcceptance criteria:\n" + "\n".join(f"- {c}" for c in t.criteria)
        st.issues[t.key] = _issue(
            t.key, t.title, body, _TYPE_FOR_KIND.get(t.kind, "Task"),
            labels=tuple(t.tags),
        )
    return st


class _JiraHandler(BaseHTTPRequestHandler):
    state: JiraState  # set on the server class

    # Silence the default stderr access log — the harness owns the output.
    def log_message(self, *_a):  # noqa: D102
        pass

    # -- helpers ---------------------------------------------------------- #
    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record_auth(self) -> None:
        self.state.auth_seen.append(self.headers.get("Authorization", "") and "basic-present" or "MISSING")

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # -- routes ----------------------------------------------------------- #
    def do_GET(self):  # noqa: N802
        self._record_auth()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        st = self.state

        if u.path == "/rest/api/3/search/jql":
            jql = (q.get("jql") or [""])[0]
            st.searches.append(jql)
            # Honour the one JQL clause the product's default query uses, so a
            # broken default is visible rather than masked by a fake that
            # returns everything regardless of the query.
            issues = list(st.issues.values())
            if "statusCategory != Done" in jql:
                issues = [i for i in issues
                          if i["fields"]["status"]["statusCategory"]["name"] != "Done"]
            n = int((q.get("maxResults") or ["50"])[0])
            return self._send(200, {"issues": issues[:n], "isLast": True})

        parts = [p for p in u.path.split("/") if p]
        # /rest/api/3/issue/<key>[/transitions]
        if len(parts) >= 5 and parts[:4] == ["rest", "api", "3", "issue"]:
            key = parts[4]
            issue = st.issues.get(key)
            if issue is None:
                return self._send(404, {"errorMessages": [f"Issue does not exist: {key}"]})
            if len(parts) == 6 and parts[5] == "transitions":
                return self._send(200, {"transitions": [
                    {"id": "11", "name": "To Do",
                     "to": {"name": "To Do", "statusCategory": {"name": "To Do"}}},
                    {"id": "21", "name": "In Progress",
                     "to": {"name": "In Progress",
                            "statusCategory": {"name": "In Progress"}}},
                    {"id": "31", "name": "Done",
                     "to": {"name": "Done", "statusCategory": {"name": "Done"}}},
                ]})
            return self._send(200, issue)

        st.unknown_paths.append(u.path)
        return self._send(404, {"errorMessages": ["not found"]})

    def do_POST(self):  # noqa: N802
        self._record_auth()
        u = urlparse(self.path)
        body = self._body()
        st = self.state
        parts = [p for p in u.path.split("/") if p]

        if len(parts) == 6 and parts[:4] == ["rest", "api", "3", "issue"]:
            key = parts[4]
            if parts[5] == "comment":
                # Unwrap ADF back to text so the assertion reads naturally.
                text = json.dumps(body.get("body", ""))
                st.comments.append((key, text))
                return self._send(201, {"id": "1", "body": body.get("body")})
            if parts[5] == "transitions":
                tid = str(((body.get("transition") or {}).get("id")) or "")
                name = {"11": "To Do", "21": "In Progress", "31": "Done"}.get(tid, tid)
                st.transitions_applied.append((key, name))
                issue = st.issues.get(key)
                if issue:
                    issue["fields"]["status"] = {
                        "name": name, "statusCategory": {"name": name}}
                return self._send(204, {})

        st.unknown_paths.append(u.path)
        return self._send(404, {"errorMessages": ["not found"]})


class FakeJira:
    """A 127.0.0.1 Jira Cloud good enough to exercise ``intake/jira.py``."""

    def __init__(self, state: JiraState):
        self.state = state
        handler = type("_H", (_JiraHandler,), {"state": state})
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeJira":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()


# --------------------------------------------------------------------------- #
# Fake Slack incoming webhook
# --------------------------------------------------------------------------- #

@dataclass
class SlackState:
    posts: list[dict[str, Any]] = field(default_factory=list)
    bad_requests: list[str] = field(default_factory=list)


class _SlackHandler(BaseHTTPRequestHandler):
    state: SlackState

    def log_message(self, *_a):  # noqa: D102
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            self.state.bad_requests.append(raw.decode("utf-8", "replace"))
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"invalid_payload")
            return
        self.state.posts.append(payload)
        # The real endpoint answers with the literal body "ok".
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class FakeSlack:
    """A 127.0.0.1 stand-in for a Slack incoming webhook."""

    def __init__(self) -> None:
        self.state = SlackState()
        handler = type("_S", (_SlackHandler,), {"state": self.state})
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def webhook_url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}/services/T000/B000/FAKE"

    def __enter__(self) -> "FakeSlack":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()


# --------------------------------------------------------------------------- #
# Fake Jenkins and fake CircleCI
# --------------------------------------------------------------------------- #
#
# The website claims: "Jenkins & CircleCI — test layers can run on your CI, and
# the results gate the loop." These two fakes exist to hold that sentence to
# account without a credential.
#
# They are more faithful than the Jira fake, because both adapters can be
# pointed at them with almost no seam:
#
#   Jenkins   speaks over `curl` in a subprocess, and its `base_url` is an
#             ordinary config value. Pointing it at 127.0.0.1 exercises the real
#             adapter, the real curl invocation, real HTTP status handling and
#             the real parser. Nothing is stubbed.
#   CircleCI  speaks over httpx to a module-level `_API` constant. Only that one
#             constant is redirected; every other line of the adapter runs.
#
# What they still cannot prove: that the real Jenkins accepts our crumb flow,
# that a real CircleCI token has the scopes we assume, or that either vendor's
# JSON matches these shapes today. Those need a live instance. Everything
# produced from these is labelled `live: false`.


@dataclass
class CIState:
    """What the fake should do, and what it was asked."""

    # "green" | "red" | "running_forever" | "unauthorized" | "server_error"
    outcome: str = "green"
    requests: list[str] = field(default_factory=list)
    auth_seen: list[str] = field(default_factory=list)


class _JenkinsHandler(BaseHTTPRequestHandler):
    state: CIState

    def log_message(self, *_a):  # noqa: D102
        pass

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, body: str) -> None:
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _gate(self) -> bool:
        """Returns False (and answers) when the configured outcome is a wall."""
        self.state.auth_seen.append(
            "basic" if self.headers.get("Authorization") else "none")
        if self.state.outcome == "unauthorized":
            self._text(401, "Unauthorized")
            return False
        if self.state.outcome == "server_error":
            self._text(503, "Service Unavailable")
            return False
        return True

    def do_POST(self):  # noqa: N802
        self.state.requests.append("POST " + self.path)
        if not self._gate():
            return
        self._text(201, "")

    def do_GET(self):  # noqa: N802
        st = self.state
        st.requests.append("GET " + self.path)
        if not self._gate():
            return
        path = urlparse(self.path).path
        if path.endswith("/crumbIssuer/api/json"):
            return self._json(200, {"crumb": "abc", "crumbRequestField": "Jenkins-Crumb"})
        if path.endswith("/api/json"):
            if st.outcome == "running_forever":
                return self._json(200, {"building": True, "result": None,
                                        "number": 7, "url": "http://x/7/"})
            passed = st.outcome == "green"
            return self._json(200, {
                "building": False,
                "result": "SUCCESS" if passed else "FAILURE",
                "number": 7, "url": "http://x/7/"})
        if path.endswith("/testReport/api/json"):
            passed = st.outcome == "green"
            return self._json(200, {
                "passCount": 12 if passed else 9,
                "failCount": 0 if passed else 3,
                "skipCount": 0})
        if path.endswith("/consoleText"):
            return self._text(200,
                              "Tests run: 12, Failures: 0, Errors: 0, Skipped: 0"
                              if st.outcome == "green" else
                              "Tests run: 12, Failures: 3, Errors: 0, Skipped: 0")
        return self._text(404, "not found")


class FakeJenkins:
    """A 127.0.0.1 Jenkins the real `JenkinsCI` adapter can be pointed at."""

    def __init__(self, outcome: str = "green"):
        self.state = CIState(outcome=outcome)
        handler = type("_J", (_JenkinsHandler,), {"state": self.state})
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeJenkins":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()


class _CircleHandler(BaseHTTPRequestHandler):
    state: CIState

    def log_message(self, *_a):  # noqa: D102
        pass

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gate(self) -> bool:
        self.state.auth_seen.append(self.headers.get("Circle-Token") and "token" or "none")
        if self.state.outcome == "unauthorized":
            self._json(401, {"message": "Unauthorized"})
            return False
        if self.state.outcome == "server_error":
            self._json(503, {"message": "unavailable"})
            return False
        return True

    def do_POST(self):  # noqa: N802
        self.state.requests.append("POST " + self.path)
        if not self._gate():
            return
        self._json(201, {"id": "pipe-1"})

    def do_GET(self):  # noqa: N802
        st = self.state
        st.requests.append("GET " + self.path)
        if not self._gate():
            return
        path = urlparse(self.path).path
        if path.endswith("/workflow"):
            status = {"green": "success", "red": "failed",
                      "running_forever": "running"}.get(st.outcome, "success")
            return self._json(200, {"items": [{"name": "build", "status": status}]})
        if path.endswith("/pipeline"):
            return self._json(200, {"items": [{"id": "pipe-1"}]})
        return self._json(404, {"message": "not found"})


class FakeCircleCI:
    """A 127.0.0.1 CircleCI API v2, for the real `CircleCICI` adapter."""

    def __init__(self, outcome: str = "green"):
        self.state = CIState(outcome=outcome)
        handler = type("_C", (_CircleHandler,), {"state": self.state})
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def api_url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}/api/v2"

    def __enter__(self) -> "FakeCircleCI":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()
