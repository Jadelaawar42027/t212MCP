"""Create (or renew) the Revolut consent through Enable Banking.

This is the only code in the repo that sends POST requests (/auth and /sessions), and it
runs on your machine, not in the server. The server itself stays GET-only.

Steps:
  1. Reads Revolut's maximum consent validity from GET /aspsps and asks for the longest.
  2. POST /auth, then prints the Revolut login URL.
  3. You approve in Revolut and land on <redirect>/callback?code=...&state=... The page may
     show an error; that's fine. Copy the full URL from the address bar and paste it here.
  4. POST /sessions with the code, then prints the session ID, consent expiry and every
     account that came back.

Usage:
    uv run python scripts/link_revolut.py [--redirect-url URL] [--country ES] [--days N]

Reads ENABLE_BANKING_APP_ID and ENABLE_BANKING_KEY_PATH (or ENABLE_BANKING_PRIVATE_KEY_B64)
from the environment or .env. Never prints the private key or the JWT.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from finance_mcp.providers.revolut.keys import JwtSigner, KeyLoadError, load_private_key

API = "https://api.enablebanking.com"
DEFAULT_REDIRECT = "https://finance-mcp-production.up.railway.app/callback"


def die(msg: str) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def load_env_file(path: Path = Path(".env")) -> None:
    """Minimal .env reader so the script works without exporting variables."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def check(response: httpx.Response, what: str) -> dict:
    if response.is_success:
        return response.json()
    try:
        body = response.json()
        detail = f"{body.get('error') or ''} {body.get('message') or ''} {body.get('detail') or ''}".strip()
    except ValueError:
        detail = response.text[:300]
    die(f"{what} failed: HTTP {response.status_code} {detail}")
    raise AssertionError  # unreachable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--redirect-url", default=DEFAULT_REDIRECT)
    parser.add_argument("--country", default="ES")
    parser.add_argument("--bank", default="Revolut")
    parser.add_argument("--psu-type", default="personal", choices=["personal", "business"])
    parser.add_argument("--days", type=int, help="Consent length in days (default: the bank's maximum)")
    args = parser.parse_args()

    load_env_file()
    app_id = os.environ.get("ENABLE_BANKING_APP_ID", "").strip()
    if not app_id:
        die("Set ENABLE_BANKING_APP_ID (in .env or the environment).")
    try:
        key = load_private_key(
            key_path=os.environ.get("ENABLE_BANKING_KEY_PATH") or None,
            key_b64=os.environ.get("ENABLE_BANKING_PRIVATE_KEY_B64") or None,
        )
    except KeyLoadError as exc:
        die(str(exc))
    signer = JwtSigner(app_id, key)

    with httpx.Client(base_url=API, timeout=30) as http:
        auth = {"Authorization": f"Bearer {signer.token()}"}

        # 1. Which Revolut entry, and how long can consent last?
        aspsps = check(
            http.get("/aspsps", params={"country": args.country, "psu_type": args.psu_type}, headers=auth),
            "GET /aspsps",
        ).get("aspsps", [])
        bank = next((a for a in aspsps if a.get("name", "").lower() == args.bank.lower()), None)
        if bank is None:
            similar = [a.get("name") for a in aspsps if args.bank.lower()[:3] in a.get("name", "").lower()]
            die(f"{args.bank} not found for {args.country}. Similar: {similar or 'none'}")
        max_seconds = int(bank.get("maximum_consent_validity") or 90 * 86400)
        max_days = max_seconds // 86400
        days = min(args.days or max_days, max_days)
        print(f"• {bank['name']} ({bank['country']}): maximum consent validity {max_days} days. Requesting {days}.")
        if bank.get("beta"):
            print("  Note: Enable Banking marks this bank as beta.")
        valid_until = datetime.now(UTC) + timedelta(seconds=min(days * 86400, max_seconds) - 120)

        # 2. Start authorization.
        state = secrets.token_urlsafe(24)
        started = check(
            http.post(
                "/auth",
                headers=auth,
                json={
                    "access": {"valid_until": valid_until.isoformat()},
                    "aspsp": {"name": bank["name"], "country": bank["country"]},
                    "state": state,
                    "redirect_url": args.redirect_url,
                    "psu_type": args.psu_type,
                },
            ),
            "POST /auth",
        )
        print("\n1. Open this URL, log in to Revolut and approve access:\n")
        print(f"   {started['url']}\n")
        print(f"2. You'll land on {args.redirect_url}?code=... (an error page there is fine).")
        pasted = input("3. Paste the full URL from the address bar here: ").strip()

        query = parse_qs(urlparse(pasted).query)
        if "error" in query:
            die(f"Bank returned an error: {query.get('error')} {query.get('error_description', '')}")
        if query.get("state", [None])[0] != state:
            die("The 'state' in the pasted URL doesn't match this run. Start again and use the newest link.")
        code = query.get("code", [None])[0]
        if not code:
            die("No 'code' parameter in that URL.")

        # 3. Exchange the code for a session. Refresh the JWT in case the login took a while.
        auth = {"Authorization": f"Bearer {signer.token()}"}
        session = check(http.post("/sessions", headers=auth, json={"code": code}), "POST /sessions")

    accounts = session.get("accounts") or []
    expires = (session.get("access") or {}).get("valid_until")
    print("\n✓ Consent created.\n")
    print(f"  Session ID:    {session.get('session_id')}")
    print(f"  Valid until:   {expires}")
    print(f"  Accounts:      {len(accounts)}\n")
    for i, acc in enumerate(accounts, 1):
        ident = acc.get("account_id") or {}
        print(f"  [{i}] {acc.get('name') or '(no name)'}")
        for field in ("product", "details", "currency", "cash_account_type", "usage"):
            if acc.get(field):
                print(f"      {field:<18} {acc[field]}")
        if ident:
            print(f"      {'account_id':<18} {json.dumps(ident)}")
        for other in acc.get("all_account_ids") or []:
            print(f"      {'other id':<18} {json.dumps(other)}")
        print(f"      {'uid':<18} {acc.get('uid')}")
        print(f"      {'identification_hash':<18} {acc.get('identification_hash')}")
    print("\nFull account objects as returned:")
    print(json.dumps(accounts, indent=2, ensure_ascii=False))

    # identification_hash stays the same across re-consents; uid changes every time.
    labels = {
        a.get("identification_hash") or a.get("uid"): a.get("details") or a.get("product") or a.get("currency") or ""
        for a in accounts
    }
    print("\nNext:")
    print(f"  ENABLE_BANKING_SESSION_ID={session.get('session_id')}")
    print(f"  ACCOUNT_LABELS='{json.dumps(labels, ensure_ascii=False)}'   # edit the labels as you like")


if __name__ == "__main__":
    main()
