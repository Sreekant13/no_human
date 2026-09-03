"""The board document is served with a strict Content-Security-Policy.

CSP is defense-in-depth behind react-markdown's output encoding (see
web/src/markdownSafety.test.mjs): even if injected markup reached the DOM,
`script-src 'self'` means no injected <script> or inline handler can execute.
Board XSS is same-origin, so it would otherwise pass every Origin/Host check and
drive the whole loopback API; this closes the script-execution half.

The policy is `api/app.py`'s `_CSP`, computed once per app start by
`_build_csp` and applied by the `_csp_header` middleware to EVERY response.
These tests run against the real app - a policy asserted on a bare FastAPI
would say nothing about what the board actually receives.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from no_human.api.app import _CSP, _build_csp, app
from no_human.config import load_config
from no_human.core.db import Store


def _directive(csp: str, name: str) -> str:
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith(name + " ") or part == name:
            return part
    return ""


def test_csp_policy_is_strict():
    for name, value in (
        ("default-src", "default-src 'self'"),
        ("script-src", "script-src 'self'"),
        ("object-src", "object-src 'none'"),
        ("base-uri", "base-uri 'self'"),
        ("frame-ancestors", "frame-ancestors 'none'"),
        ("form-action", "form-action 'self'"),
    ):
        assert _directive(_CSP, name) == value, (name, _directive(_CSP, name))
    script = _directive(_CSP, "script-src")
    for token in ("*", "'unsafe-inline'", "'unsafe-eval'", "http:", "https:"):
        assert token not in script, script
    # the board's WebSocket and SSE are same-origin ws/http: keep them reachable
    assert "ws:" in _directive(_CSP, "connect-src") and "wss:" in _directive(_CSP, "connect-src")
    # telemetry off (the default) leaves the policy byte-identical
    assert _build_csp({}) == _CSP


@pytest.mark.asyncio
async def test_the_board_document_and_the_api_carry_the_policy(tmp_path):
    store = await Store(tmp_path / "csp.db").connect()
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state.csp = _build_csp(app.state.config.data)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost") as c:
            doc = await c.get("/", headers={"Host": "127.0.0.1:8420"})
            api = await c.get("/api/tasks", headers={"Host": "127.0.0.1:8420"})
    finally:
        await store.close()
    # `/` is the board document when web/dist is present (200, text/html) and
    # a 503 explaining the missing build otherwise; the header is on both, and
    # it is exactly the policy the app computed for ITS config (telemetry on
    # adds the PostHog hosts to script-src/connect-src, nothing else).
    expected = app.state.csp
    assert doc.status_code in (200, 503), doc.text[:120]
    assert doc.headers.get("content-security-policy") == expected
    assert api.status_code == 200
    assert api.headers.get("content-security-policy") == expected
    # the served header itself, not the constant: these directives must be
    # present verbatim in what the browser receives
    served = doc.headers["content-security-policy"]
    for name, value in (("default-src", "default-src 'self'"), ("object-src", "object-src 'none'"),
                        ("base-uri", "base-uri 'self'"), ("frame-ancestors", "frame-ancestors 'none'"),
                        ("form-action", "form-action 'self'")):
        assert _directive(served, name) == value, name
    assert _directive(served, "script-src").startswith("script-src 'self'")
