# finance-mcp

A personal, **read-only** remote MCP server for your Trading 212 account, built to be added to
claude.ai as a custom connector. It runs on Railway, and only you can sign in, via OAuth 2.1.
It's laid out for more providers (Revolut is next).

- **Read-only, enforced in code.** The only outbound HTTP client is wrapped in a transport that
  refuses anything other than GET, and tests fail if a POST/PUT/PATCH/DELETE becomes possible.
  The tools are also marked `readOnlyHint` in MCP.
- **Private.** `/mcp` needs a bearer token that only the sign-in flow issues. Signing in needs your
  password plus an authenticator code (recommended). `/health` returns 200 with an empty body,
  and every other path returns 404.
- **Gentle on the API.** Per-endpoint throttling uses the Trading 212 spec's limits. A 429 makes it
  back off until `x-ratelimit-reset`. An in-memory cache keeps instruments for 24h and the portfolio
  for 30s.

## Tools

| Tool | What it returns |
| --- | --- |
| `get_account_summary` | Account currency, total value, cash, cost basis, unrealized/realized P&L |
| `get_positions` | All open positions with value, P&L and weight (`sort_by`, `raw`) |
| `get_position(ticker)` | One position. Accepts `AAPL_US_EQ`, `AAPL` or an ISIN |
| `get_order_history(from_date?, to_date?, ticker?)` | Orders newest first, with fills, net value, realized P&L and fees |
| `get_dividends(from_date?, to_date?, ticker?)` | Dividend payments and their total |
| `get_transactions(from_date?, to_date?, type?)` | Deposits, withdrawals, fees, transfers and interest, with totals by type |
| `get_pies` / `get_pie(id)` | Pies and their holdings (Trading 212 has deprecated the Pies API) |
| `search_instruments(query)` | Search by ticker, symbol, ISIN or name, from metadata cached for 24h |
| `portfolio_overview` | Weights, P&L, allocation by type/currency/exchange, top/bottom performers |
| `dividend_report(period)` | By month and by ticker, trailing-12-month income (`ttm`, `ytd`, `all`, `2025`) |
| `net_deposits_and_returns` | Deposited vs current value, simple return, annualized money-weighted return (XIRR) |

Notes:
- The history endpoints have no date filters upstream. Dates are filtered on the server, and
  paging stops once a page is older than `from_date`.
- Paging is capped at `TRADING212_MAX_PAGES`, 50 rows per page. When the cap is hit, responses
  include `truncated_history: true`.

## Auth: what was chosen and why

claude.ai custom connectors support three options: OAuth (with Client ID Metadata Documents or
Dynamic Client Registration), a fixed request header (beta, only for some organizations), or no
auth. This server uses **OAuth 2.1, run by the server itself, for a single owner**:

- It serves RFC 8414 and RFC 9728 metadata, and answers unauthenticated `/mcp` calls with a 401
  and a `resource_metadata` pointer.
- It supports both CIMD (claude.ai's preferred method; client metadata is only fetched from
  `claude.ai`) and DCR, using signed client IDs so no database is needed.
- PKCE with S256 only. Redirects are only accepted to `https://claude.ai/api/mcp/auth_callback`
  and to Claude Code's `http://localhost:<any port>/callback`.
- Login is your password plus an optional TOTP code. A code can't be reused, and failed logins
  are limited per IP and globally.
- Access tokens last 1h and only work for this server's `/mcp` URL. Refresh tokens last 30 days
  and rotate on every use. Reusing an old refresh token revokes the whole chain.
- Tokens are HMAC-signed with `OAUTH_SIGNING_KEY`, so you stay signed in across restarts.
  **Rotating that key signs out every client.**

Limits worth knowing:
- One-time-use bookkeeping (used codes, rotated refresh tokens, TOTP replay) lives in memory.
  A restart forgets it, so an already-used refresh token from before the restart could be
  replayed until it expires.
- Keep `numReplicas` at 1. It's set in `railway.json`.

## Run locally

```bash
uv sync
cp .env.example .env        # fill in values; PUBLIC_BASE_URL=http://localhost:8000
uv run finance-mcp          # serves http://localhost:8000
uv run pytest               # unit tests (no network)
```

**Smoke test against your demo account.** Use a demo key, from Trading 212 switched to Practice
mode, then Settings > API:

```bash
TRADING212_ENV=demo TRADING212_API_KEY=... TRADING212_API_SECRET=... uv run python scripts/smoke.py
# --show prints each tool's JSON. The script refuses TRADING212_ENV=live unless you pass --allow-live.
```

The script:
1. Starts the real server.
2. Checks `/health`, the 404s and the `/mcp` 401.
3. Mints a short-lived token. The browser sign-in itself is covered by `tests/test_auth.py`.
4. Calls every tool through the official MCP client.

## Deploy to Railway

These steps use the Railway CLI. Run them from the repo root.

```bash
# 1. Create the project and an empty service
railway login
railway init --name finance-mcp
railway add --service finance-mcp
railway link                                  # pick the finance-mcp project and service if asked

# 2. Generate a public domain. Note the URL it prints, e.g. https://finance-mcp-production.up.railway.app
railway domain --service finance-mcp

# 3. Set variables. Secrets go in via stdin so they stay out of shell history.
railway variable set TRADING212_ENV=live PUBLIC_BASE_URL=https://<your-domain> --service finance-mcp --skip-deploys
printf %s '<t212 api key>'    | railway variable set TRADING212_API_KEY    --stdin --service finance-mcp --skip-deploys
printf %s '<t212 api secret>' | railway variable set TRADING212_API_SECRET --stdin --service finance-mcp --skip-deploys
printf %s '<long password>'   | railway variable set OWNER_PASSWORD        --stdin --service finance-mcp --skip-deploys
uv run python -c "import pyotp; print(pyotp.random_base32())"     # → add to your authenticator app
printf %s '<that base32 secret>' | railway variable set OWNER_TOTP_SECRET --stdin --service finance-mcp --skip-deploys
openssl rand -base64 48 | tr -d '\n' | railway variable set OAUTH_SIGNING_KEY --stdin --service finance-mcp --skip-deploys

# 4. Deploy (builds the Dockerfile; healthcheck is /health)
railway up --service finance-mcp --detach
railway logs --service finance-mcp

# 5. Check it
curl -i https://<your-domain>/health                                       # 200, empty body
curl -s https://<your-domain>/.well-known/oauth-authorization-server       # metadata JSON
curl -i -X POST https://<your-domain>/mcp                                  # 401 + WWW-Authenticate
```

To add your TOTP secret to an authenticator app, enter it as a manual key with account
`finance-mcp`. Alternatively, generate a QR code locally with
`uv run python -c "import pyotp; print(pyotp.TOTP('<secret>').provisioning_uri('owner', issuer_name='finance-mcp'))"`
and any offline QR tool.

You can also deploy from GitHub instead of `railway up`: in the dashboard, go to New Project,
choose Deploy from GitHub repo, pick this repo, then set the same variables and generate a domain.
Railway finds `railway.json` and the `Dockerfile` automatically.

## Add it to claude.ai

1. Open **claude.ai → Customize → Connectors** (https://claude.ai/customize/connectors). Click
   **Add custom connector**. On Team/Enterprise plans, an Owner adds it under
   **Organization settings → Connectors** instead.
2. Name: `Finance`. URL: `https://<your-domain>/mcp`, including `/mcp`.
3. Authentication: **Sign in now**. OAuth client: **Use Claude's published identity** (CIMD),
   or **Register automatically** (DCR). Both work. Leave request headers empty.
4. Click **Add**, then **Connect**. A green "Read-only, by design." page opens: enter your
   password and authenticator code, then click **Allow read-only access**. You'll be sent back
   to claude.ai.
5. Test it in a new chat: open the **+** menu, then **Connectors**, and make sure Finance is on.
   Then ask *"Give me a portfolio overview"* or *"What dividend income did I get in the last 12
   months?"*
6. Optional: under **Customize → Connectors → Finance**, set tools you won't use to **Blocked**.

**Claude Code works too:** `claude mcp add --transport http finance https://<your-domain>/mcp`,
then run `/mcp` to sign in.

## Optional hardening

- **Restrict `/mcp` to Anthropic's IP range.** Set `ALLOWED_CIDRS=160.79.104.0/21`.
  - This blocks Claude Code running from your laptop. The OAuth pages stay reachable.
  - It relies on the `X-Real-IP` header from Railway's edge, so treat it as an extra layer, not
    the main control.
- **IP-restrict your Trading 212 key.** If you lock the API key to specific IPs in Trading 212's
  API settings, the server needs a fixed outbound address.
  - Railway **Static Outbound IPs** are **Pro plan only**, IPv4 only, and may be shared with
    other customers.
  - Enable them with `railway outbound-network static-ip enable --service finance-mcp`, redeploy,
    then copy the shown IP into Trading 212.
  - The IPs change if you move region.
- **Give the Trading 212 key only read permissions:** account, portfolio, history (orders,
  dividends, transactions), metadata and pies read. If a permission is missing, the tool says
  which one.

## Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `TRADING212_ENV` | yes | `live` or `demo` (default `demo`) |
| `TRADING212_API_KEY` | yes | From Trading 212 → Settings → API |
| `TRADING212_API_SECRET` | yes | Shown once when you create the key |
| `PUBLIC_BASE_URL` | yes | `https://<your-domain>`, no path, no trailing slash |
| `OWNER_PASSWORD` | yes | 12+ characters. Use a long random one |
| `OWNER_TOTP_SECRET` | recommended | Base32 secret for your authenticator app |
| `OAUTH_SIGNING_KEY` | yes | 32+ random characters (`openssl rand -base64 48`) |
| `ALLOWED_CIDRS` | no | e.g. `160.79.104.0/21` |
| `TRADING212_MAX_PAGES` | no | History page cap (default 20 × 50 rows) |
| `TRADING212_TIMEOUT_SECONDS` | no | Default 15 |
| `ACCESS_TOKEN_TTL_SECONDS` / `REFRESH_TOKEN_TTL_SECONDS` | no | Defaults 3600 / 2592000 |
| `PORT` | set by Railway | Default 8000 |

## Layout

```
src/finance_mcp/
  server.py            app factory: MCP at /mcp, OAuth routes, /health, 404 elsewhere
  auth.py              single-owner OAuth 2.1 server + token verifier
  http.py              the only outbound HTTP client factory (GET-only transport)
  config.py            env settings
  templates/login.html sign-in page (no JS, strict CSP)
  providers/
    __init__.py        Provider protocol + registry
    trading212/        client.py (GET-only, throttle, cache, paging), tools.py, models.py, analytics.py
    revolut/           placeholder
tests/                 respx-mocked unit tests (client, tools, GET-only guarantee, auth)
scripts/smoke.py       end-to-end check against the demo account
```

**Adding Revolut later:**
1. Create `providers/revolut/` with a `from_env()` that returns an object with `name`,
   `register_tools(mcp)` and `aclose()`.
2. Add it to `load_providers()`.
3. Give its tools distinct names, e.g. `revolut_get_accounts`.
4. Make outbound calls only through `finance_mcp.http.get_only_client`.
