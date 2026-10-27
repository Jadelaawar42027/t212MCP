"""OAuth + HTTP surface tests: rejection paths, the full sign-in flow, CIMD, DCR, refresh rotation."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest
from starlette.testclient import TestClient

from finance_mcp.auth import CLAUDE_CALLBACK, OwnerAuthServer
from finance_mcp.config import Settings
from finance_mcp.providers.trading212 import Trading212Provider
from finance_mcp.providers.trading212.client import Trading212Client
from finance_mcp.server import create_app
from tests.conftest import SUMMARY

BASE = "http://localhost:8000"
PASSWORD = "correct horse battery staple"
TOTP_SECRET = pyotp.random_base32()
CLAUDE_CODE_CIMD = "https://claude.ai/oauth/claude-code-client-metadata"


def make_settings(**overrides) -> Settings:
    values = {
        "public_base_url": BASE,
        "owner_password": PASSWORD,
        "owner_totp_secret": TOTP_SECRET,
        "oauth_signing_key": "k" * 48,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def cimd_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == CLAUDE_CODE_CIMD:
            return httpx.Response(
                200,
                json={
                    "client_id": CLAUDE_CODE_CIMD,
                    "client_name": "Claude Code",
                    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
                    "token_endpoint_auth_method": "none",
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def t212_provider() -> Trading212Provider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v0/equity/account/summary":
            return httpx.Response(200, json=SUMMARY)
        return httpx.Response(404)

    client = Trading212Client(env="demo", api_key="k", api_secret="s", transport=httpx.MockTransport(handler))
    return Trading212Provider(client)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def auth_server(settings) -> OwnerAuthServer:
    return OwnerAuthServer(settings, cimd_transport=cimd_transport())


@pytest.fixture
def http(settings, auth_server):
    app = create_app(settings, providers=[t212_provider()], auth_server=auth_server)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(http, redirect_uris=(CLAUDE_CALLBACK,)) -> str:
    r = http.post("/register", json={"client_name": "Claude", "redirect_uris": list(redirect_uris)})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def authorize_params(client_id: str, challenge: str, redirect_uri: str = CLAUDE_CALLBACK, **extra) -> dict:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st4te",
        "scope": "finance:read offline_access",
        "resource": f"{BASE}/mcp",
        **extra,
    }


def req_token(page: str) -> str:
    return re.search(r'name="req" value="([^"]+)"', page).group(1)


def login(http, client_id: str, challenge: str, redirect_uri: str = CLAUDE_CALLBACK, totp: str | None = None):
    page = http.get("/authorize", params=authorize_params(client_id, challenge, redirect_uri))
    assert page.status_code == 200, page.text
    return http.post(
        "/authorize",
        data={"req": req_token(page.text), "password": PASSWORD, "totp": totp or pyotp.TOTP(TOTP_SECRET).now()},
    )


def full_flow(http, redirect_uri: str = CLAUDE_CALLBACK, client_id: str | None = None) -> dict:
    client_id = client_id or register(http)
    verifier, challenge = pkce()
    r = login(http, client_id, challenge, redirect_uri)
    assert r.status_code == 303, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    tokens = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{BASE}/mcp",
        },
    )
    assert tokens.status_code == 200, tokens.text
    return {**tokens.json(), "client_id": client_id}


def mcp_call(http, token: str | None, method: str = "tools/list", params: dict | None = None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-11-25",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )


# ---------------------------------------------------------------- public surface


def test_health_is_200_and_empty(http):
    r = http.get("/health")
    assert r.status_code == 200 and r.content == b""


@pytest.mark.parametrize("path", ["/", "/admin", "/mcp/extra", "/docs", "/.env", "/api/v0/equity/positions"])
def test_everything_else_is_404(http, path):
    assert http.get(path).status_code == 404


def test_metadata_documents(http):
    asm = http.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == BASE
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["client_id_metadata_document_supported"] is True
    assert "none" in asm["token_endpoint_auth_methods_supported"]
    assert asm["registration_endpoint"] == f"{BASE}/register"
    prm = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"].rstrip("/") == f"{BASE}/mcp"
    assert [s.rstrip("/") for s in prm["authorization_servers"]] == [BASE]


# ---------------------------------------------------------------- rejection


def test_mcp_without_token_is_401_with_discovery_pointer(http):
    r = mcp_call(http, None)
    assert r.status_code == 401
    assert (
        'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"'
        in r.headers["www-authenticate"]
    )


@pytest.mark.parametrize("token", ["garbage", "Bearer", "eyJhbGciOiJub25lIn0.e30."])
def test_mcp_with_bad_token_is_401(http, token):
    assert mcp_call(http, token).status_code == 401


def test_token_signed_with_other_key_rejected(http):
    other = OwnerAuthServer(make_settings(oauth_signing_key="z" * 48))
    forged = other.signer.encode(
        "access", {"sub": "owner", "cid": "x", "sc": "finance:read", "aud": f"{BASE}/mcp", "fam": "f"}, 3600
    )
    assert mcp_call(http, forged).status_code == 401


def test_expired_and_wrong_audience_tokens_rejected(http, auth_server):
    claims = {"sub": "owner", "cid": "x", "sc": "finance:read", "fam": "f"}
    expired = auth_server.signer.encode("access", {**claims, "aud": f"{BASE}/mcp"}, ttl=-10)
    wrong_aud = auth_server.signer.encode("access", {**claims, "aud": "https://other.example/mcp"}, ttl=3600)
    no_scope = auth_server.signer.encode("access", {**claims, "aud": f"{BASE}/mcp", "sc": "other"}, ttl=3600)
    assert mcp_call(http, expired).status_code == 401
    assert mcp_call(http, wrong_aud).status_code == 401
    assert mcp_call(http, no_scope).status_code == 403


def test_refresh_token_cannot_be_used_as_access_token(http):
    tokens = full_flow(http)
    assert mcp_call(http, tokens["refresh_token"]).status_code == 401


def test_register_rejects_foreign_redirects(http):
    for uri in ["https://evil.example/callback", "http://localhost:3000/other", "javascript:alert(1)"]:
        r = http.post("/register", json={"redirect_uris": [uri]})
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


def test_authorize_with_unregistered_redirect_shows_error_page(http):
    client_id = register(http)
    _, challenge = pkce()
    r = http.get("/authorize", params=authorize_params(client_id, challenge, "http://localhost:9999/callback"))
    assert r.status_code == 400 and "location" not in r.headers
    assert "Can’t continue sign-in" in r.text


def test_authorize_unknown_client(http):
    _, challenge = pkce()
    r = http.get("/authorize", params=authorize_params("made-up", challenge))
    assert r.status_code == 400


def test_authorize_requires_s256(http):
    client_id = register(http)
    r = http.get("/authorize", params=authorize_params(client_id, "x" * 43, code_challenge_method="plain"))
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["invalid_request"] and q["state"] == ["st4te"]


def test_authorize_rejects_other_resource(http):
    client_id = register(http)
    _, challenge = pkce()
    r = http.get("/authorize", params=authorize_params(client_id, challenge, resource="https://other/mcp"))
    assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_target"]


def test_wrong_password_and_wrong_totp_rejected(http):
    client_id = register(http)
    _, challenge = pkce()
    page = http.get("/authorize", params=authorize_params(client_id, challenge))
    req = req_token(page.text)
    r = http.post("/authorize", data={"req": req, "password": "nope", "totp": pyotp.TOTP(TOTP_SECRET).now()})
    assert r.status_code == 401 and "Incorrect password or code" in r.text
    r = http.post("/authorize", data={"req": req, "password": PASSWORD, "totp": "000000"})
    assert r.status_code == 401


def test_totp_code_cannot_be_replayed(http):
    client_id = register(http)
    code = pyotp.TOTP(TOTP_SECRET).now()
    _, c1 = pkce()
    assert login(http, client_id, c1, totp=code).status_code == 303
    _, c2 = pkce()
    assert login(http, client_id, c2, totp=code).status_code == 401


def test_login_throttled_after_repeated_failures(http):
    client_id = register(http)
    _, challenge = pkce()
    req = req_token(http.get("/authorize", params=authorize_params(client_id, challenge)).text)
    for _ in range(5):
        http.post("/authorize", data={"req": req, "password": "bad", "totp": "1"})
    r = http.post("/authorize", data={"req": req, "password": PASSWORD, "totp": pyotp.TOTP(TOTP_SECRET).now()})
    assert r.status_code == 429


def test_tampered_login_request_rejected(http):
    r = http.post("/authorize", data={"req": "x.y.z", "password": PASSWORD, "totp": "123456"})
    assert r.status_code == 400 and "expired" in r.text


def test_login_page_security_headers_and_escaping(http):
    client_id = http.post(
        "/register", json={"client_name": "<script>x</script>", "redirect_uris": [CLAUDE_CALLBACK]}
    ).json()["client_id"]
    _, challenge = pkce()
    r = http.get("/authorize", params=authorize_params(client_id, challenge))
    assert "<script>x" not in r.text and "&lt;script&gt;" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert "claude.ai" in r.text  # redirect host is shown


def test_pkce_mismatch_rejected(http):
    client_id = register(http)
    _, challenge = pkce()
    r = login(http, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": secrets.token_urlsafe(48),
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_is_single_use_and_bound_to_client(http):
    client_id = register(http)
    verifier, challenge = pkce()
    r = login(http, client_id, challenge)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_CALLBACK,
        "client_id": "dcr_someone-else",
        "code_verifier": verifier,
    }
    assert http.post("/token", data=data).json()["error"] == "invalid_grant"
    data["client_id"] = client_id
    assert http.post("/token", data=data).status_code == 200
    assert http.post("/token", data=data).json()["error"] == "invalid_grant"


def test_token_endpoint_requires_form_encoding(http):
    r = http.post("/token", json={"grant_type": "authorization_code"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_unsupported_grant(http):
    r = http.post("/token", data={"grant_type": "client_credentials"})
    assert r.json()["error"] == "unsupported_grant_type"


# ---------------------------------------------------------------- happy paths


def test_full_dcr_flow_then_mcp_call(http):
    tokens = full_flow(http)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
    assert "finance:read" in tokens["scope"].split()
    r = mcp_call(http, tokens["access_token"])
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "portfolio_overview" in names
    r = mcp_call(http, tokens["access_token"], "tools/call", {"name": "get_account_summary", "arguments": {}})
    assert r.status_code == 200
    assert "GBP" in r.json()["result"]["content"][0]["text"]


def test_redirect_includes_state_and_iss(http):
    client_id = register(http)
    _, challenge = pkce()
    r = login(http, client_id, challenge)
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CLAUDE_CALLBACK
    q = parse_qs(loc.query)
    assert q["state"] == ["st4te"] and q["iss"] == [BASE]


def test_cimd_client_with_loopback_any_port(http):
    tokens = full_flow(http, redirect_uri="http://localhost:53682/callback", client_id=CLAUDE_CODE_CIMD)
    assert mcp_call(http, tokens["access_token"]).status_code == 200


def test_cimd_host_must_be_allowlisted(http):
    _, challenge = pkce()
    r = http.get("/authorize", params=authorize_params("https://evil.example/client.json", challenge))
    assert r.status_code == 400


def test_loopback_disabled_by_setting(auth_server):
    s = make_settings(allow_loopback_redirects=False)
    app = create_app(s, providers=[], auth_server=OwnerAuthServer(s, cimd_transport=cimd_transport()))
    with TestClient(app, base_url=BASE) as c:
        assert c.post("/register", json={"redirect_uris": ["http://localhost/callback"]}).status_code == 400


def test_refresh_rotation_and_reuse_detection(http, auth_server, monkeypatch):
    tokens = full_flow(http)
    first = http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": tokens["client_id"],
        },
    )
    assert first.status_code == 200
    rotated = first.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert mcp_call(http, rotated["access_token"]).status_code == 200

    # Replay of the old refresh token well after rotation = theft: whole family is revoked.
    real_now = auth_server._now
    monkeypatch.setattr(auth_server, "_now", lambda: real_now() + 60)
    monkeypatch.setattr(auth_server.signer, "_now", lambda: real_now() + 60)
    replay = http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": tokens["client_id"],
        },
    )
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert mcp_call(http, rotated["access_token"]).status_code == 401
    again = http.post("/token", data={"grant_type": "refresh_token", "refresh_token": rotated["refresh_token"]})
    assert again.json()["error"] == "invalid_grant"


def test_refresh_cannot_widen_scope(http):
    tokens = full_flow(http)
    r = http.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "scope": "finance:read admin"},
    )
    assert r.json()["error"] == "invalid_scope"


def test_password_only_mode_without_totp():
    s = make_settings(owner_totp_secret=None)
    app = create_app(s, providers=[], auth_server=OwnerAuthServer(s))
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        client_id = register(c)
        _, challenge = pkce()
        page = c.get("/authorize", params=authorize_params(client_id, challenge))
        assert 'id="totp"' in page.text and "<div hidden>" in page.text
        r = c.post("/authorize", data={"req": req_token(page.text), "password": PASSWORD})
        assert r.status_code == 303


def test_cidr_allowlist_blocks_other_ips():
    s = make_settings(allowed_cidrs="160.79.104.0/21")
    app = create_app(s, providers=[], auth_server=OwnerAuthServer(s))
    with TestClient(app, base_url=BASE) as c:
        assert mcp_call(c, None).status_code == 404  # testclient IP is not in range
        assert c.get("/health").status_code == 200
        r = c.post(
            "/mcp",
            headers={
                "x-real-ip": "160.79.105.10",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={},
        )
        assert r.status_code == 401  # reached the auth layer


def test_settings_validation():
    with pytest.raises(ValueError):
        make_settings(oauth_signing_key="short")
    with pytest.raises(ValueError):
        make_settings(owner_password="short")
    with pytest.raises(ValueError):
        make_settings(public_base_url="http://example.com")
    with pytest.raises(ValueError):
        make_settings(public_base_url="https://example.com/mcp")


def test_login_page_csp_allows_redirect_to_claude(http):
    # Browsers apply form-action to the 303 after the login POST; without the
    # callback origin the redirect to Claude is silently blocked.
    client_id = register(http)
    _, challenge = pkce()
    page = http.get("/authorize", params=authorize_params(client_id, challenge))
    csp = page.headers["content-security-policy"]
    assert "form-action 'self' https://claude.ai;" in csp
