"""Single-owner OAuth 2.1 authorization server + bearer token verifier.

Implements what claude.ai custom connectors need (MCP authorization spec 2025-11-25):
- RFC 8414 authorization server metadata, advertising CIMD and DCR
- Client ID Metadata Documents (client_id is an https URL on an allowlisted host)
- Dynamic Client Registration (RFC 7591), stateless: the client_id is a signed blob
- Authorization code + PKCE (S256 only), login page for the single owner
  (password + optional TOTP), RFC 9207 `iss` in the redirect
- Refresh tokens with rotation and reuse detection
- Access tokens bound to this server's /mcp URL (RFC 8707 audience)

Tokens are HMAC-signed JWTs, so restarts don't sign the owner out. One-time-use
state (used codes, rotated refresh tokens, TOTP replay) lives in memory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from string import Template
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt
import pyotp
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from finance_mcp.config import Settings
from finance_mcp.http import get_only_client

log = logging.getLogger(__name__)

SCOPE = "finance:read"
SUPPORTED_SCOPES = (SCOPE, "offline_access")
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")
OWNER = "owner"
MAX_FORM_BYTES = 16 * 1024

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PAGE_HEADERS = {
    **NO_STORE,
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400, redirect_to: str | None = None) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status
        self.redirect_to = redirect_to  # set when the error must go back to the client's redirect_uri


# ---------------------------------------------------------------- signing


class Signer:
    """HS256 JWTs with a separate derived key per token purpose."""

    def __init__(self, key: str, issuer: str, now: Callable[[], float]) -> None:
        self._root = key.encode()
        self._issuer = issuer
        self._now = now

    def _key(self, purpose: str) -> bytes:
        return hmac.new(self._root, f"finance-mcp/{purpose}".encode(), hashlib.sha256).digest()

    def encode(self, purpose: str, claims: dict[str, Any], ttl: int) -> str:
        now = int(self._now())
        payload = {**claims, "iss": self._issuer, "iat": now, "exp": now + ttl, "typ": purpose}
        return jwt.encode(payload, self._key(purpose), algorithm="HS256")

    def decode(self, purpose: str, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self._key(purpose),
                algorithms=["HS256"],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "typ"], "verify_aud": False},
                leeway=0,
            )
        except jwt.PyJWTError as exc:
            raise ValueError(f"invalid {purpose}: {type(exc).__name__}") from None
        if claims.get("typ") != purpose:
            raise ValueError(f"invalid {purpose}: wrong type")
        return claims


# ---------------------------------------------------------------- clients


@dataclass(frozen=True)
class Client:
    client_id: str
    name: str
    redirect_uris: tuple[str, ...]


class _Expiring:
    """Set of keys with expiry (used codes, rotated refresh tokens, revoked families)."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._data: dict[str, float] = {}

    def add(self, key: str, ttl: float) -> None:
        self._prune()
        self._data[key] = self._now() + ttl

    def get(self, key: str) -> float | None:
        exp = self._data.get(key)
        return exp if exp and exp > self._now() else None

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def _prune(self) -> None:
        if len(self._data) > 10_000:
            now = self._now()
            self._data = {k: v for k, v in self._data.items() if v > now}


class LoginThrottle:
    """Per-IP and global failed-login limits (global is the real defence behind a proxy)."""

    def __init__(self, now: Callable[[], float], per_ip: int = 5, global_: int = 20, window: float = 900) -> None:
        self._now = now
        self._per_ip, self._global, self._window = per_ip, global_, window
        self._fails: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _trim(self, q: deque[float]) -> None:
        cutoff = self._now() - self._window
        while q and q[0] < cutoff:
            q.popleft()

    def blocked(self, ip: str) -> bool:
        self._trim(self._all)
        q = self._fails.get(ip, deque())
        self._trim(q)
        return len(q) >= self._per_ip or len(self._all) >= self._global

    def fail(self, ip: str) -> None:
        self._fails.setdefault(ip, deque()).append(self._now())
        self._all.append(self._now())

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)


def _canon(url: str) -> str:
    return url.rstrip("/")


def _is_loopback(uri: str) -> bool:
    p = urlparse(uri)
    return p.scheme == "http" and (p.hostname or "") in ("localhost", "127.0.0.1", "::1")


def _loopback_key(uri: str) -> tuple[str, str, str]:
    p = urlparse(uri)
    return (p.scheme, p.hostname or "", p.path)


def _pkce_s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


# ---------------------------------------------------------------- server


class OwnerAuthServer:
    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], float] = time.time,
        cimd_transport: Any = None,
    ) -> None:
        self.s = settings
        self.issuer = settings.public_base_url
        self.resource = settings.mcp_url
        self._now = now
        self.signer = Signer(settings.oauth_signing_key.get_secret_value(), self.issuer, now)
        self._used_codes = _Expiring(now)
        self._used_refresh = _Expiring(now)
        self._revoked_families = _Expiring(now)
        self._throttle = LoginThrottle(now)
        self._totp = pyotp.TOTP(settings.owner_totp_secret.get_secret_value()) if settings.owner_totp_secret else None
        self._last_totp_step = -1
        self._cimd_cache: dict[str, tuple[float, Client]] = {}
        self._cimd_transport = cimd_transport
        self._template = Template(resources.files("finance_mcp").joinpath("templates/login.html").read_text())

    # ---- metadata ----

    def metadata(self) -> dict[str, Any]:
        b = self.issuer
        return {
            "issuer": b,
            "authorization_endpoint": f"{b}/authorize",
            "token_endpoint": f"{b}/token",
            "registration_endpoint": f"{b}/register",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "scopes_supported": list(SUPPORTED_SCOPES),
            "authorization_response_iss_parameter_supported": True,
        }

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "Personal finance (read-only)",
        }

    # ---- redirect + client validation ----

    def _redirect_globally_allowed(self, uri: str) -> bool:
        if uri == CLAUDE_CALLBACK:
            return True
        if self.s.allow_loopback_redirects and _is_loopback(uri):
            p = urlparse(uri)
            return not p.query and not p.fragment and p.path == "/callback"
        return False

    def _redirect_matches(self, uri: str, client: Client) -> bool:
        if not self._redirect_globally_allowed(uri):
            return False
        if uri in client.redirect_uris:
            return True
        # RFC 8252 7.3: loopback redirects match regardless of port.
        return _is_loopback(uri) and any(
            _is_loopback(r) and _loopback_key(r) == _loopback_key(uri) for r in client.redirect_uris
        )

    async def resolve_client(self, client_id: str) -> Client:
        if not client_id or len(client_id) > 4096:
            raise OAuthError("invalid_client", "Unknown client.", 401)
        if client_id.startswith("https://"):
            return await self._cimd_client(client_id)
        if client_id.startswith("dcr_"):
            try:
                claims = self.signer.decode("client", client_id[4:])
            except ValueError:
                raise OAuthError("invalid_client", "Unknown client.", 401) from None
            return Client(client_id, str(claims.get("n") or "MCP client"), tuple(claims.get("r") or ()))
        raise OAuthError("invalid_client", "Unknown client.", 401)

    async def _cimd_client(self, url: str) -> Client:
        cached = self._cimd_cache.get(url)
        if cached and cached[0] > self._now():
            return cached[1]
        host = (urlparse(url).hostname or "").lower()
        if host not in self.s.cimd_hosts:
            raise OAuthError("invalid_client", "Client metadata host is not allowed.", 401)
        try:
            async with get_only_client(timeout=5.0, transport=self._cimd_transport) as http:
                resp = await http.get(url, headers={"Accept": "application/json"})
            if resp.status_code != 200 or len(resp.content) > 64 * 1024:
                raise ValueError(f"status {resp.status_code}")
            doc = resp.json()
        except Exception as exc:  # noqa: BLE001 - any failure means an unusable client
            log.warning("CIMD fetch failed for %s: %s", url, type(exc).__name__)
            raise OAuthError("invalid_client", "Could not load client metadata.", 401) from None
        if not isinstance(doc, dict) or doc.get("client_id") != url:
            raise OAuthError("invalid_client", "Client metadata does not match client_id.", 401)
        uris = doc.get("redirect_uris")
        if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
            raise OAuthError("invalid_client", "Client metadata has no redirect_uris.", 401)
        client = Client(url, str(doc.get("client_name") or host)[:100], tuple(uris))
        self._cimd_cache[url] = (self._now() + 3600, client)
        return client

    # ---- endpoints ----

    async def register(self, request: Request) -> Response:
        try:
            body = await _json_body(request)
            uris = body.get("redirect_uris")
            if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
                raise OAuthError("invalid_redirect_uri", "redirect_uris must be a non-empty list.")
            if len(uris) > 10 or not all(self._redirect_globally_allowed(u) for u in uris):
                raise OAuthError("invalid_redirect_uri", "Only Claude's callback URLs are accepted by this server.")
            grants = body.get("grant_types") or ["authorization_code", "refresh_token"]
            if not set(grants) <= {"authorization_code", "refresh_token"}:
                raise OAuthError("invalid_client_metadata", "Unsupported grant_types.")
            name = str(body.get("client_name") or "MCP client")[:100]
        except OAuthError as e:
            return _oauth_json_error(e)
        blob = self.signer.encode("client", {"r": uris, "n": name}, ttl=10 * 365 * 86400)
        client_id = f"dcr_{blob}"
        return JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": int(self._now()),
                "client_name": name,
                "redirect_uris": uris,
                "grant_types": grants,
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": " ".join(SUPPORTED_SCOPES),
            },
            status_code=201,
            headers=NO_STORE,
        )

    async def _validate_authorize(self, params: dict[str, str]) -> tuple[Client, str, dict[str, Any]]:
        """Return (client, redirect_uri, request claims). Raises OAuthError; errors with
        `redirect_to` set go back to the client, the rest are shown on an error page."""
        client = await self.resolve_client(params.get("client_id", ""))
        redirect_uri = params.get("redirect_uri")
        explicit = bool(redirect_uri)
        if not redirect_uri:
            if len(client.redirect_uris) != 1:
                raise OAuthError("invalid_request", "redirect_uri is required.")
            redirect_uri = client.redirect_uris[0]
        if not self._redirect_matches(redirect_uri, client):
            raise OAuthError("invalid_request", "redirect_uri is not registered for this client.")

        def redirect_error(error: str, desc: str) -> OAuthError:
            return OAuthError(error, desc, redirect_to=redirect_uri)

        if params.get("response_type") != "code":
            raise redirect_error("unsupported_response_type", "Only response_type=code is supported.")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not (43 <= len(challenge) <= 128):
            raise redirect_error("invalid_request", "PKCE with code_challenge_method=S256 is required.")
        resource = params.get("resource")
        if resource and _canon(resource) != _canon(self.resource):
            raise redirect_error("invalid_target", "Unknown resource.")
        scopes = (params.get("scope") or SCOPE).split()
        if not set(scopes) <= set(SUPPORTED_SCOPES):
            raise redirect_error("invalid_scope", "Unsupported scope.")
        if SCOPE not in scopes:
            scopes.append(SCOPE)
        claims = {
            "cid": client.client_id,
            "ru": redirect_uri,
            "rx": explicit,
            "cc": challenge,
            "sc": " ".join(scopes),
            "st": params.get("state"),
        }
        return client, redirect_uri, claims

    def _redirect(self, uri: str, params: dict[str, Any]) -> RedirectResponse:
        query = urlencode({k: v for k, v in {**params, "iss": self.issuer}.items() if v is not None})
        sep = "&" if urlparse(uri).query else "?"
        return RedirectResponse(f"{uri}{sep}{query}", status_code=303, headers=NO_STORE)

    async def authorize_get(self, request: Request) -> Response:
        params = dict(request.query_params)
        try:
            client, redirect_uri, claims = await self._validate_authorize(params)
        except OAuthError as e:
            if e.redirect_to:
                return self._redirect(
                    e.redirect_to, {"error": e.error, "error_description": e.description, "state": params.get("state")}
                )
            return self._page(error_page=e.description, status=400)
        req = self.signer.encode("authreq", claims, ttl=600)
        return self._page(req=req, client=client, redirect_uri=redirect_uri)

    async def authorize_post(self, request: Request) -> Response:
        ip = _client_ip(request)
        try:
            form = await _form_body(request)
            claims = self.signer.decode("authreq", form.get("req", ""))
            client = await self.resolve_client(claims["cid"])
        except ValueError:
            return self._page(
                error_page="This sign-in link expired. Start the connection again from Claude.", status=400
            )
        except OAuthError as e:
            return self._page(error_page=e.description, status=e.status)
        if self._throttle.blocked(ip):
            log.warning("login throttled")
            return self._page(
                req=form.get("req", ""),
                client=client,
                redirect_uri=claims["ru"],
                error="Too many failed attempts. Wait 15 minutes and try again.",
                status=429,
            )
        if not self._check_credentials(form.get("password", ""), form.get("totp", "")):
            self._throttle.fail(ip)
            log.warning("failed owner login")
            return self._page(
                req=form.get("req", ""),
                client=client,
                redirect_uri=claims["ru"],
                error="Incorrect password or code.",
                status=401,
            )
        self._throttle.reset(ip)
        log.info("owner signed in; issuing code to %s", client.name)
        code = self.signer.encode(
            "code",
            {
                "cid": claims["cid"],
                "ru": claims["ru"],
                "rx": claims["rx"],
                "cc": claims["cc"],
                "sc": claims["sc"],
                "jti": secrets.token_urlsafe(16),
            },
            ttl=120,
        )
        return self._redirect(claims["ru"], {"code": code, "state": claims.get("st")})

    def _check_credentials(self, password: str, totp: str) -> bool:
        expected = hashlib.sha256(self.s.owner_password.get_secret_value().encode()).digest()
        ok = hmac.compare_digest(hashlib.sha256(password.encode()).digest(), expected)
        if self._totp is None:
            return ok
        code = "".join(ch for ch in totp if ch.isdigit())
        step = int(self._now() // 30)
        totp_ok = False
        for offset in (-1, 0, 1):
            candidate = step + offset
            if hmac.compare_digest(self._totp.at(candidate * 30), code) and candidate > self._last_totp_step:
                totp_ok = True
                if ok:
                    self._last_totp_step = candidate  # prevent replay of this code
                break
        return ok and totp_ok

    async def token(self, request: Request) -> Response:
        try:
            ctype = request.headers.get("content-type", "")
            if not ctype.startswith("application/x-www-form-urlencoded"):
                raise OAuthError("invalid_request", "Use application/x-www-form-urlencoded.")
            form = await _form_body(request)
            grant = form.get("grant_type")
            if grant == "authorization_code":
                return self._token_response(*self._exchange_code(form))
            if grant == "refresh_token":
                return self._token_response(*self._exchange_refresh(form))
            raise OAuthError("unsupported_grant_type", "Unsupported grant_type.")
        except OAuthError as e:
            return _oauth_json_error(e)

    def _exchange_code(self, form: dict[str, str]) -> tuple[str, str, str | None]:
        try:
            c = self.signer.decode("code", form.get("code", ""))
        except ValueError:
            raise OAuthError("invalid_grant", "Invalid or expired authorization code.") from None
        if c["jti"] in self._used_codes:
            raise OAuthError("invalid_grant", "Authorization code already used.")
        if form.get("client_id") != c["cid"]:
            raise OAuthError("invalid_grant", "Code was issued to another client.")
        if (c["rx"] or form.get("redirect_uri")) and form.get("redirect_uri") != c["ru"]:
            raise OAuthError("invalid_grant", "redirect_uri mismatch.")
        verifier = form.get("code_verifier", "")
        if not (43 <= len(verifier) <= 128) or not hmac.compare_digest(_pkce_s256(verifier), c["cc"]):
            raise OAuthError("invalid_grant", "PKCE verification failed.")
        resource = form.get("resource")
        if resource and _canon(resource) != _canon(self.resource):
            raise OAuthError("invalid_target", "Unknown resource.")
        self._used_codes.add(c["jti"], 300)
        return c["cid"], c["sc"], None

    def _exchange_refresh(self, form: dict[str, str]) -> tuple[str, str, str | None]:
        try:
            r = self.signer.decode("refresh", form.get("refresh_token", ""))
        except ValueError:
            raise OAuthError("invalid_grant", "Invalid or expired refresh token.") from None
        if form.get("client_id") and form.get("client_id") != r["cid"]:
            raise OAuthError("invalid_grant", "Refresh token was issued to another client.")
        if r["fam"] in self._revoked_families:
            raise OAuthError("invalid_grant", "Refresh token revoked.")
        first_use = self._used_refresh.get(r["jti"])
        if first_use is not None:
            used_at = first_use - self.s.refresh_token_ttl_seconds
            if self._now() - used_at > 30:  # not a near-simultaneous retry: treat as theft
                self._revoked_families.add(r["fam"], self.s.refresh_token_ttl_seconds)
                log.warning("refresh token reuse detected; revoking token family")
            raise OAuthError("invalid_grant", "Refresh token already used.")
        scopes = r["sc"]
        if form.get("scope"):
            requested = form["scope"].split()
            if not set(requested) <= set(scopes.split()):
                raise OAuthError("invalid_scope", "Cannot widen scope on refresh.")
            scopes = " ".join(requested)
        self._used_refresh.add(r["jti"], self.s.refresh_token_ttl_seconds)
        return r["cid"], scopes, r["fam"]

    def _token_response(self, client_id: str, scopes: str, family: str | None) -> Response:
        family = family or secrets.token_urlsafe(12)
        access = self.signer.encode(
            "access",
            {
                "sub": OWNER,
                "cid": client_id,
                "sc": scopes,
                "aud": self.resource,
                "fam": family,
                "jti": secrets.token_urlsafe(12),
            },
            ttl=self.s.access_token_ttl_seconds,
        )
        refresh = self.signer.encode(
            "refresh",
            {"cid": client_id, "sc": scopes, "fam": family, "jti": secrets.token_urlsafe(16)},
            ttl=self.s.refresh_token_ttl_seconds,
        )
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": self.s.access_token_ttl_seconds,
                "refresh_token": refresh,
                "scope": scopes,
            },
            headers=NO_STORE,
        )

    # ---- token verification (resource server side) ----

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            c = self.signer.decode("access", token)
        except ValueError:
            return None
        if c.get("sub") != OWNER or _canon(str(c.get("aud", ""))) != _canon(self.resource):
            return None
        if c.get("fam") in self._revoked_families:
            return None
        return AccessToken(
            token=token,
            client_id=c["cid"],
            scopes=str(c.get("sc", "")).split(),
            expires_at=int(c["exp"]),
            resource=self.resource,
            subject=OWNER,
        )

    # ---- pages ----

    def _page(
        self,
        *,
        req: str = "",
        client: Client | None = None,
        redirect_uri: str = "",
        error: str = "",
        error_page: str = "",
        status: int = 200,
    ) -> HTMLResponse:
        host = urlparse(redirect_uri).netloc if redirect_uri else ""
        body = self._template.substitute(
            mode="error" if error_page else "login",
            heading="Can’t continue sign-in" if error_page else "Sign in to allow access",
            client_name=html.escape(client.name if client else ""),
            redirect_host=html.escape(host),
            req=html.escape(req, quote=True),
            error=html.escape(error or error_page),
            error_hidden="" if (error or error_page) else "hidden",
            totp_hidden="" if self._totp else "hidden",
            totp_required="required" if self._totp else "",
        )
        return HTMLResponse(body, status_code=status, headers=PAGE_HEADERS)

    def routes(self) -> list[Route]:
        async def as_metadata(_: Request) -> Response:
            return JSONResponse(self.metadata(), headers={"Cache-Control": "public, max-age=300"})

        async def pr_metadata(_: Request) -> Response:
            return JSONResponse(self.protected_resource_metadata(), headers={"Cache-Control": "public, max-age=300"})

        return [
            Route("/.well-known/oauth-authorization-server", as_metadata, methods=["GET"]),
            Route("/.well-known/openid-configuration", as_metadata, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource", pr_metadata, methods=["GET"]),
            Route("/register", self.register, methods=["POST"]),
            Route("/authorize", self.authorize_get, methods=["GET"]),
            Route("/authorize", self.authorize_post, methods=["POST"]),
            Route("/token", self.token, methods=["POST"]),
        ]


def _oauth_json_error(e: OAuthError) -> JSONResponse:
    return JSONResponse(
        {"error": e.error, "error_description": e.description}, status_code=e.status or 400, headers=NO_STORE
    )


async def _read_limited(request: Request) -> bytes:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise OAuthError("invalid_request", "Request body too large.", 413)
    return body


async def _form_body(request: Request) -> dict[str, str]:
    from urllib.parse import parse_qsl

    raw = await _read_limited(request)
    return dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))


async def _json_body(request: Request) -> dict[str, Any]:
    import json

    raw = await _read_limited(request)
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise OAuthError("invalid_client_metadata", "Body must be JSON.") from None
    if not isinstance(data, dict):
        raise OAuthError("invalid_client_metadata", "Body must be a JSON object.")
    return data
