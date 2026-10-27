"""Enable Banking application key and JWT handling.

The private key is read from a file path first (local dev), then from base64 of the
PEM in an env var (Railway). Neither the key nor any JWT is ever logged or put in an
error message.

JWT format (https://enablebanking.com/docs/api/reference/): RS256, header `kid` = app ID,
claims iss="enablebanking.com", aud="api.enablebanking.com", iat, exp (max 24h).
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

JWT_ISSUER = "enablebanking.com"
JWT_AUDIENCE = "api.enablebanking.com"
JWT_LIFETIME = 3600  # well under the 24h maximum
JWT_RENEW_BEFORE = 300


class KeyLoadError(ValueError):
    """Key could not be loaded. The message never contains key material."""


def load_private_key(*, key_path: str | Path | None = None, key_b64: str | None = None) -> RSAPrivateKey:
    """Load the RSA private key. A path wins over base64 when both are set."""
    if key_path:
        path = Path(key_path).expanduser()
        try:
            pem = path.read_bytes()
        except OSError:
            raise KeyLoadError(f"Cannot read ENABLE_BANKING_KEY_PATH ({path}).") from None
        source = "ENABLE_BANKING_KEY_PATH"
    elif key_b64:
        try:
            pem = base64.b64decode("".join(key_b64.split()), validate=True)
        except (binascii.Error, ValueError):
            raise KeyLoadError("ENABLE_BANKING_PRIVATE_KEY_B64 is not valid base64.") from None
        source = "ENABLE_BANKING_PRIVATE_KEY_B64"
    else:
        raise KeyLoadError("Set ENABLE_BANKING_KEY_PATH or ENABLE_BANKING_PRIVATE_KEY_B64.")
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        raise KeyLoadError(f"{source} does not contain an unencrypted PEM private key.") from None
    if not isinstance(key, RSAPrivateKey):
        raise KeyLoadError(f"{source} must be an RSA private key (Enable Banking uses RS256).")
    return key


class JwtSigner:
    """Mints short-lived application JWTs and reuses them until close to expiry."""

    def __init__(self, app_id: str, key: RSAPrivateKey, *, clock: Callable[[], float] = time.time) -> None:
        if not app_id:
            raise ValueError("ENABLE_BANKING_APP_ID is required")
        self.app_id = app_id
        self._key = key
        self._clock = clock
        self._token: str | None = None
        self._exp = 0.0

    def __repr__(self) -> str:  # never show key material
        return f"JwtSigner(app_id={self.app_id!r})"

    def token(self) -> str:
        now = self._clock()
        if self._token is None or now >= self._exp - JWT_RENEW_BEFORE:
            iat = int(now)
            self._exp = iat + JWT_LIFETIME
            self._token = jwt.encode(
                {"iss": JWT_ISSUER, "aud": JWT_AUDIENCE, "iat": iat, "exp": int(self._exp)},
                self._key,
                algorithm="RS256",
                headers={"kid": self.app_id, "typ": "JWT"},
            )
        return self._token
