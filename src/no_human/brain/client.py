"""HTTP to the control plane. One base URL, ``httpx``, nothing else.

``httpx`` is ALREADY a declared dependency of this product
(``pyproject.toml``); the team-brain client adds no package at all, which is
invariant L1 and is asserted by a frozen-literal test over the parsed dependency
list. There is no AWS SDK here, no request signing, no credential chain: the
service is reached the way any HTTPS API is reached, and everything that makes
it an AWS service is on the other side of the URL.

Two disciplines are load-bearing and both are tested:

  * **TLS is verified.** No ``verify=`` argument is passed anywhere in this
    file. Recorded because the tree contains a counter-example —
    ``vcs/pr_watcher.py`` constructs an ``httpx`` client with ``verify=False``.
    That pattern must not be copied here, and a test greps this package for it.

  * **No Claude credential can leave this machine.** Every outbound request
    passes ``_guard_outbound``, which refuses on anything shaped like an OAuth
    subscription token or an Anthropic API key, in a header or a body. The
    bearer comes from exactly one function in ``credentials.py`` and the first
    increment sends no bodies at all.
"""

from __future__ import annotations

import re
from typing import Any

from .settings import BrainConfig

#: Every request is a small JSON GET on a cold path a human typed. Short enough
#: that a wedged service is a fast, obvious failure rather than a hang.
TIMEOUT_SECONDS = 15.0

#: Shapes that must NEVER appear in an outbound request. Deliberately a closed
#: list of MARKER SHAPES rather than an entropy heuristic — the repo has learned
#: that regex-over-free-text detectors take many adversarial rounds, and this one
#: only has to recognise the two credential formats this product actually holds.
_FORBIDDEN_OUTBOUND = (
    re.compile(r"sk-ant-", re.IGNORECASE),
    re.compile(r"\bCLAUDE_CODE_OAUTH_TOKEN\b"),
    re.compile(r"\bANTHROPIC_(API_KEY|AUTH_TOKEN)\b"),
    # The Claude OAuth token's own prefix.
    re.compile(r"sk-ant-oat", re.IGNORECASE),
)


class BrainHTTPError(RuntimeError):
    """A request failed. ``status`` is None for a transport failure."""

    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class BrainCredentialLeak(RuntimeError):
    """A request was refused because it carried Claude credential material.

    This is a bug-catcher, not a runtime condition: nothing in this package can
    reach a Claude credential. It exists so that if something ever does, the
    request fails loudly here instead of succeeding quietly at the service.
    """


def _guard_outbound(headers: dict, content: str | bytes | None) -> None:
    text = "\n".join(f"{k}: {v}" for k, v in (headers or {}).items())
    if content:
        text += "\n" + (content.decode("utf-8", "replace")
                        if isinstance(content, bytes) else str(content))
    for pattern in _FORBIDDEN_OUTBOUND:
        if pattern.search(text):
            raise BrainCredentialLeak(
                "refusing to send a request carrying Claude credential material"
            )


#: The only hosts the plaintext-HTTP exception covers. Compared against the
#: PARSED host, never against a string prefix: `"http://localhost.evil.com"
#: .startswith("http://localhost")` is True, so the prefix form let a remote
#: host be reached over plaintext HTTP — the exception is about the host being
#: loopback, so it has to be decided on the host.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _base(cfg: BrainConfig) -> str:
    from urllib.parse import urlsplit

    url = cfg.control_plane_url
    if not url:
        raise BrainHTTPError("team_brain.control_plane_url is not set")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError as exc:
        raise BrainHTTPError(
            f"team_brain.control_plane_url is not a usable URL: {exc}") from exc
    # `hostname` drops the port AND any userinfo, so `http://127.0.0.1@evil.com`
    # resolves to `evil.com` here, which is the host the request would go to.
    if parts.scheme != "https" and not (
            parts.scheme == "http" and host in _LOOPBACK_HOSTS):
        raise BrainHTTPError(
            "team_brain.control_plane_url must be https:// (loopback excepted "
            "for tests)"
        )
    return url


def _request(method: str, url: str, *, headers: dict | None = None,
             params: dict | None = None, data: dict | None = None,
             transport: Any = None) -> tuple[int, Any]:
    import httpx

    headers = dict(headers or {})
    headers.setdefault("accept", "application/json")
    body = None
    if data is not None:
        # urlencoded because the only POST in this increment is an OAuth token
        # exchange, whose spec says so.
        import urllib.parse
        body = urllib.parse.urlencode(data)
        headers["content-type"] = "application/x-www-form-urlencoded"
    _guard_outbound(headers, body)

    kwargs: dict[str, Any] = {"timeout": TIMEOUT_SECONDS}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        # NO `verify=` — httpx verifies by default and that default is the
        # security property. See the module docstring.
        with httpx.Client(**kwargs) as http:
            response = http.request(method, url, headers=headers,
                                    params=params, content=body)
    except Exception as exc:  # noqa: BLE001 — every transport failure is one answer
        raise BrainHTTPError(f"{method} {url} failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


# --- unauthenticated -------------------------------------------------------


def health(cfg: BrainConfig, *, transport: Any = None) -> bool:
    status, _ = _request("GET", f"{_base(cfg)}/v1/health", transport=transport)
    return status == 200


def auth_config(cfg: BrainConfig, *, transport: Any = None) -> dict:
    """The control plane's own OIDC endpoints — the discovery document.

    This is the second server route the first increment needs, and the design
    document did not anticipate it. It asks for one only because the alternative
    is worse: the deployed sign-in domain embeds a cloud region and an account
    id, so hard-coding it (or putting it in ``config.yaml``) would put AWS
    surface into the local product and break invariant L3, which is the one
    invariant the whole client exists to keep.
    """
    status, payload = _request("GET", f"{_base(cfg)}/v1/auth/config",
                               transport=transport)
    if status != 200 or not isinstance(payload, dict):
        raise BrainHTTPError(
            f"the control plane did not return sign-in configuration "
            f"(HTTP {status}). This build needs the /v1/auth/config route.",
            status, payload)
    return payload


def _token_endpoint(auth: dict) -> tuple[str, str]:
    """The token endpoint and client id from the DISCOVERY DOCUMENT, checked.

    ONE helper, because there are two callers and only one of them used to
    check. ``exchange_code`` validated the scheme; ``refresh_tokens`` did not,
    and it is the worse of the two to leave open: the code exchange sends a
    one-time authorization code, while the refresh sends the LONG-LIVED refresh
    token, on every sync. `token_endpoint` is server-authored — it comes from
    `/v1/auth/config`, which is the one thing this client asks an unauthenticated
    route for — so a control plane that had been taken over could answer with
    `http://attacker/token` and the refresh token left the machine in cleartext,
    to a host of the attacker's choosing, with no error. Demonstrated verbatim:

        SENT IN CLEARTEXT -> http://attacker.example/token
           body: grant_type=refresh_token&client_id=abc&refresh_token=...

    The scheme is checked on the PARSED URL rather than with `startswith`, for
    the reason `_base` already records: `"http://localhost.evil.com"` starts
    with a loopback prefix and is not loopback. No loopback exception here at
    all — an identity provider is never on this machine.
    """
    from urllib.parse import urlsplit

    endpoint = str(auth.get("token_endpoint") or "")
    client_id = str(auth.get("client_id") or "")
    try:
        parts = urlsplit(endpoint)
        scheme, host = parts.scheme, (parts.hostname or "")
    except ValueError:
        scheme, host = "", ""
    # The HOST is required as well as the scheme. `urlsplit("https://").scheme`
    # is "https", so a scheme-only check passed a URL with no host at all and
    # the request was still attempted — found by the red-green for this fix,
    # not reasoned about afterwards.
    if scheme != "https" or not host or not client_id:
        raise BrainHTTPError("the control plane returned no usable token endpoint")
    return endpoint, client_id


def exchange_code(auth: dict, code: str, verifier: str, redirect_uri: str,
                  *, transport: Any = None) -> dict:
    endpoint, client_id = _token_endpoint(auth)
    status, payload = _request("POST", endpoint, data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }, transport=transport)
    if status != 200 or not isinstance(payload, dict) or "id_token" not in payload:
        raise BrainHTTPError(f"token exchange failed (HTTP {status})", status, payload)
    return payload


def refresh_tokens(auth: dict, refresh_token: str, *, transport: Any = None) -> dict:
    endpoint, client_id = _token_endpoint(auth)
    status, payload = _request("POST", endpoint, data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }, transport=transport)
    if status != 200 or not isinstance(payload, dict) or "id_token" not in payload:
        raise BrainHTTPError(f"token refresh failed (HTTP {status})", status, payload)
    return payload


# --- authenticated ----------------------------------------------------------


def _auth_headers(id_token: str) -> dict:
    if not id_token:
        raise BrainHTTPError("no brain credential; run `nh brain login`")
    return {"authorization": f"Bearer {id_token}"}


def sync_delta(cfg: BrainConfig, id_token: str, since: int,
               *, transport: Any = None) -> dict:
    status, payload = _request(
        "GET", f"{_base(cfg)}/v1/brain/sync",
        headers=_auth_headers(id_token), params={"since": int(since)},
        transport=transport)
    if status == 409:
        raise BrainHTTPError("resync_required", 409, payload)
    if status != 200 or not isinstance(payload, dict):
        raise BrainHTTPError(f"sync failed (HTTP {status})", status, payload)
    return payload


def manifest(cfg: BrainConfig, id_token: str, version: int,
             *, transport: Any = None) -> dict | None:
    """One signed manifest envelope, or None when that version has none.

    A version with no manifest is NORMAL and is not a trust failure: the
    control plane allocates a version to every row that must reach the delta
    stream, which includes filed proposals and the re-stamped OLD row of a
    supersede — and it writes exactly one manifest per ADMISSION. The caller
    treats an absent manifest as "this row is unverified", which means it is
    never injected. That is fail-closed for the row without quarantining the
    team.
    """
    status, payload = _request(
        "GET", f"{_base(cfg)}/v1/brain/manifests/{int(version)}",
        headers=_auth_headers(id_token), transport=transport)
    if status == 404:
        return None
    if status != 200 or not isinstance(payload, dict):
        raise BrainHTTPError(f"manifest {version} failed (HTTP {status})",
                             status, payload)
    return payload
