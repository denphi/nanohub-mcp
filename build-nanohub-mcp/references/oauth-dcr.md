# OAuth + Dynamic Client Registration on nanoHUB

**As a tool author you configure none of this** — the hub provides it, and
Claude/ChatGPT/Claude Code connect on their own. Read this to understand what
happens, and especially to decode auth errors (table below) before concluding
"OAuth is broken".

Three components cooperate. Only the first two are hub-side code; HubZero
core is a fixed external dependency.

| Component | Role |
|---|---|
| **com_mcp** (API gateway) | 401 challenge with `WWW-Authenticate: Bearer resource_metadata="…"`, serves RFC 9728 protected-resource metadata at `/api/mcp/{tool}/mcp/.well-known/oauth-protected-resource` |
| **com_authorize** | RFC 8414 / OIDC discovery for issuer `https://{host}/authorize`, RFC 7591 DCR at `/authorize/register` |
| **HubZero core** (com_developer + bshaffer oauth2-server-php 1.11.1) | the real `/developer/oauth/authorize` and `/developer/oauth/token` endpoints |

## The discovery chain (why each piece exists)

1. Client hits `/api/mcp/{tool}/mcp` without a token → **401** (must be 401,
   not 403 — MCP clients only trigger discovery on 401) with
   `WWW-Authenticate: Bearer resource_metadata="…"`.
2. Protected-resource metadata lists
   `authorization_servers: ["https://{host}/authorize"]`. HubZero can't route
   root `/.well-known/*` to a component, so the issuer carries a **path**;
   spec-compliant clients fall through to path-appended well-known URLs.
3. com_authorize serves the AS metadata at
   `/authorize/.well-known/openid-configuration` and
   `…/oauth-authorization-server`. The `issuer` string must **byte-match** the
   `authorization_servers` entry (no trailing slash) — clients validate this.
4. Metadata advertises `token_endpoint_auth_methods_supported:
   ["client_secret_post", "none"]` and `code_challenge_methods_supported:
   ["S256"]`, plus `registration_endpoint`.

## DCR: hybrid admission (supported, but gated)

`POST /authorize/register` (RFC 7591), two tiers:

- **Anonymous** — allowed only when *every* `redirect_uris` host is on the
  vendor allowlist (component param; claude.ai, claude.com, chatgpt.com,
  openai.com) or is a loopback host (`localhost`, `127.0.0.1`, `::1` — how
  Claude Code registers). Rate-limited per IP/hour and globally/day.
- **Member** — any other redirect URI requires `Authorization: Bearer` from a
  member of com_mcp's `allowed_groups` (RFC 7591 "initial access token"
  pattern). 401 without a token, 403 for non-members, capped apps per member.

Registration is **not** the security boundary: a client row is inert until a
group member authenticates at the consent screen, and the gateway 403s
non-members on every call. Redirect URIs must be absolute https, or http only
on loopback, no fragments. Client names are sanitized (consent screen prints
them) and suffixed "(auto-registered)".

For `token_endpoint_auth_method: "none"` the response simply omits
`client_secret`; a random secret is still stored (never derive it from the
client_id — HubZero's automatic hook would use `sha1(client_id)`, i.e. public
knowledge).

## HubZero AS facts you must accept (not fix)

- Every published, non-deleted client is a **public client**
  (`isPublicClient()` == published && state != 2), so secret-less exchange
  completes. That's why advertising `"none"` is truthful.
- **PKCE is advertised but not enforced** — the token endpoint ignores
  `code_verifier`. Clients require S256 in metadata to proceed; flows
  complete. Server-side enforcement is a HubZero-core follow-up. Don't claim
  PKCE security you don't have.
- Grant types for DCR clients: `authorization_code`, `refresh_token` only
  (never `password`/`client_credentials`).

## Decoding token-endpoint errors (saves hours)

| Error you see | What it actually means |
|---|---|
| `invalid_client` — "This client is invalid or must authenticate using a client secret" | **client_id not found / not published.** NOT "public clients rejected". |
| `invalid_client` — "client credentials are required" | public clients genuinely disabled (`allow_public_clients` false) — not nanoHUB's config |
| `invalid_client` — "The client credentials are invalid" | a secret was sent and it's wrong |
| `invalid_grant` — "Authorization code doesn't exist or is invalid for the client" | **client auth PASSED**; the code is bad/expired/foreign. This is the success signal in smoke tests. |

Definitive smoke test: register a throwaway client with
`token_endpoint_auth_method: "none"` and a loopback redirect, then exchange a
bogus code with no secret. `invalid_grant` ⇒ the whole public-client path
works. See verification.md for the exact curl commands.

## 401 vs 403 discipline

- **401** = "authenticate" → clients run OAuth discovery. Use for missing or
  invalid tokens, always with `WWW-Authenticate`.
- **403** = "you're authenticated but not allowed" → use for group-gate
  denials, with a JSON body naming `required_groups`. Returning 401 here sends
  clients into an infinite re-auth loop.
