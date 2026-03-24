# OAuth 2.1 + Remote Access

## Goal

Expose BluePopcorn MCP server to the internet so it works from **claude.ai web/mobile** — the only clients that can't set custom auth headers. Avery has DDNS already configured.

## Why OAuth 2.1

| Client | Local? | Bearer token? | Needs OAuth? |
|---|---|---|---|
| Claude Code (stdio) | spawns process | N/A | No |
| Claude Desktop (stdio) | spawns process | N/A | No |
| Claude Code (HTTP) | localhost | Yes | No |
| Claude Desktop (HTTP) | localhost | Yes | No |
| Cursor / VS Code | localhost | Yes | No |
| **claude.ai web/mobile** | **no — needs public URL** | **no** | **Yes** |

Bearer token auth covers every local client. OAuth is only needed because claude.ai web uses the OAuth 2.1 + PKCE flow to authenticate — it can't set arbitrary headers.

## Reference Implementation

fetchaller-mcp (`Averyy/fetchaller-mcp`) has a proven OAuth 2.1 implementation across 4 files:

- `src/fetchaller/http/oauth.py` — `OAuthManager` class: client registration store, auth code generation, JWT token creation/verification via `PyJWT` + `cryptography`
- `src/fetchaller/http/routes.py` — OAuth endpoints: `.well-known/oauth-protected-resource`, `.well-known/oauth-authorization-server`, `/register`, `/authorize` (GET/POST), `/token`
- `src/fetchaller/http/templates.py` — HTML login page (user pastes their `MCP_API_KEY` to authorize)
- `src/fetchaller/http/middleware.py` — `verify_bearer_auth()` accepts both raw API key and OAuth JWT

Flow: claude.ai discovers OAuth endpoints via `.well-known` → dynamic client registration → redirects user to `/authorize` login page → user enters their `MCP_API_KEY` → server issues JWT → claude.ai uses JWT as Bearer token on subsequent requests.

## Implementation Plan

### Phase 1: Rate Limiting

Add before exposing to the internet. Port from fetchaller.

- [ ] `RateLimiter` class in `mcp/http/middleware.py` (per-IP, bounded memory, stale entry cleanup)
- [ ] Default 100 req/min, configurable via `MCP_RATE_LIMIT` env var
- [ ] Apply to `/mcp` endpoint

### Phase 2: OAuth 2.1

Port from fetchaller, adapt to BluePopcorn naming.

- [ ] Add dependencies: `PyJWT`, `cryptography`
- [ ] `mcp/http/oauth.py` — `OAuthManager` with in-memory client store, PKCE verification, JWT signing
- [ ] OAuth discovery endpoints (`.well-known/oauth-protected-resource`, `.well-known/oauth-authorization-server`)
- [ ] `/register` — dynamic client registration (MCP spec requirement)
- [ ] `/authorize` GET/POST — HTML login page, user enters `MCP_API_KEY`
- [ ] `/token` — exchange auth code + PKCE verifier for JWT
- [ ] Update `verify_bearer_auth()` to accept raw API key OR valid JWT
- [ ] `mcp/http/templates.py` — login page HTML (BluePopcorn branded)
- [ ] Tests for OAuth flow (registration → authorize → token → authenticated request)

### Phase 3: Remote Access

Expose to the internet via DDNS.

- [ ] Bind to `0.0.0.0` when `--host 0.0.0.0` or `HTTP_HOST=0.0.0.0` (already supported)
- [ ] Document reverse proxy setup (nginx/Caddy) with TLS termination
- [ ] Document DDNS + port forwarding or Cloudflare Tunnel as alternatives
- [ ] Add `MCP_BASE_URL` env var for OAuth redirect URIs (must match public URL)
- [ ] Update README with remote setup guide

### Phase 4: Polish

- [ ] Docker Compose with Caddy for automatic HTTPS
- [ ] Health check endpoint already exists (`/health`)
- [ ] Logging for OAuth events (registration, auth attempts, token refresh)
- [ ] Document in README: local (Bearer) vs remote (OAuth) auth

## Env Vars (New)

| Variable | Default | Description |
|---|---|---|
| `MCP_RATE_LIMIT` | `100` | Requests per minute per IP |
| `MCP_BASE_URL` | `http://localhost:{port}` | Public URL for OAuth redirects |
| `MCP_JWT_SECRET` | (auto-generated) | JWT signing key. Auto-generated if not set, but set explicitly for persistence across restarts |

## Not Planned

- **Multi-user / multi-Seerr** — single-user tool, one Seerr instance
- **PyPI / ClawHub publishing** — revisit after OAuth ships
- **Persistent OAuth store** — in-memory is fine for single-user; clients re-auth on restart
