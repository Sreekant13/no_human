"""The board HTML document is served with a strict Content-Security-Policy.

CSP is defense-in-depth behind react-markdown's output encoding (see
web/src/markdownSafety.test.mjs): even if injected markup reached the DOM,
`script-src 'self'` means no injected <script> or inline handler can execute.
Board XSS is same-origin, so it would otherwise pass every Origin/Host check and
drive the whole loopback API; this closes the script-execution half. The
local_boundary middleware applies it to text/html responses only.
"""
import pytest

from no_human.api.local_boundary import (
    _BOARD_CSP,
    _BOARD_CSP_REPORT_ONLY,
    install_local_boundary,
)


def _script_src(csp: str) -> str:
    # the token span after "script-src" up to the next ";"
    return csp.split("script-src", 1)[1].split(";", 1)[0]


def test_csp_policy_is_strict():
    csp = _BOARD_CSP
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    ss = _script_src(csp)
    assert "*" not in ss
    assert "'unsafe-inline'" not in ss
    assert "'unsafe-eval'" not in ss
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert "require-trusted-types-for 'script'" in _BOARD_CSP_REPORT_ONLY


@pytest.mark.asyncio
async def test_middleware_adds_csp_to_html_responses_only(tmp_path):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from httpx import AsyncClient, ASGITransport

    from no_human.config import load_config

    app = FastAPI()
    app.state.config = load_config(tmp_path / "config.yaml")
    install_local_boundary(app)

    @app.get("/doc")
    async def _doc():
        return HTMLResponse("<!doctype html><p>board</p>")

    @app.get("/api/x")
    async def _apix():
        return JSONResponse({"ok": True})

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://localhost") as c:
        html = await c.get("/doc", headers={"Host": "127.0.0.1:8420"})
        api = await c.get("/api/x", headers={"Host": "127.0.0.1:8420"})

    # The HTML document carries the strict CSP + Trusted Types report-only.
    assert html.headers.get("content-security-policy") == _BOARD_CSP
    assert "require-trusted-types-for" in html.headers.get(
        "content-security-policy-report-only", "")
    # A JSON API response does NOT carry the document CSP.
    assert api.headers.get("content-security-policy") is None
