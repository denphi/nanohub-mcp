# The com_mcp gateway: PHP realities, CORS, SSE, sessions

**Audience: hub platform maintainers.** Tool authors don't touch this layer,
but if your clients fail with browser/CORS/session symptoms, this is the
vocabulary for the bug report.

The gateway (`com_mcp/api/controllers/mcpv1_0.php`) sits between MCP clients
and the tool-session server. Endpoints:

```
GET  /api/mcp/{tool}              session bootstrap (returns endpoints)
GET  /api/mcp/{tool}/mcp          streamable HTTP / SSE endpoint event
POST /api/mcp/{tool}/mcp          JSON-RPC proxy (the main path)
GET  /api/mcp/{tool}/mcp/.well-known/oauth-protected-resource
GET  /api/mcp/{tool}/openapi.json
```

## HubZero/PHP framework traps

These drive almost every design decision in the controller:

1. **The framework buffers `$this->response`** — headers and body are emitted
   only after the controller returns. Streaming (SSE) and immediate-header
   cases must use native `header()` + `echo` + `exit`. The framework path also
   emits `Content-Length: 0` for SSE bodies set via `setContent()` — clients
   receive an empty event stream and never learn the endpoint.

2. **The exception handler rebuilds the response object**, so headers set on
   `$this->response` do NOT survive `throw`/`App::abort`. Any response that
   must carry specific headers on an error path (401 challenge, 403 group
   denial) has to be raw `header()`/`exit`.

3. **Consequence: every raw-emit path must re-emit CORS headers itself.**
   Setting `Access-Control-Allow-Origin` once in `execute()` covers only
   framework-sent responses (e.g. the OPTIONS preflight) — which is exactly
   how you end up with "CORS present on preflight, missing on actual
   responses", the single most reported browser-host failure.

## CORS rules for MCP over browsers

- `Access-Control-Allow-Origin: *` on **every** response: preflight, 200 raw
  passthrough, SSE, 401 challenge, 403 denial.
- Never combine `*` with `Access-Control-Allow-Credentials: true` — the pair
  is spec-invalid and browsers reject it. Bearer-token auth needs no cookies.
- `Access-Control-Expose-Headers: WWW-Authenticate, Mcp-Session-Id` on actual
  responses — without it, browser JS cannot read the OAuth challenge (so
  discovery never starts) nor the session id (so async tasks break).
- Preflight `Access-Control-Allow-Headers` must include `Authorization,
  Content-Type, Mcp-Session-Id, MCP-Protocol-Version, Last-Event-ID` —
  otherwise the browser refuses to *send* the MCP session header.
- The 403 group-denial body must be CORS-readable too, or browser users see
  an opaque error instead of "you need group X".

## Session forwarding (async tools depend on it)

- Forward the client's `Mcp-Session-Id` request header to the MCP server —
  it is the spec's session channel. Dropping it breaks every task-capable
  async tool ("Task-capable async tool calls require an MCP session").
- Echo a server-minted `Mcp-Session-Id` response header back to the client so
  task ownership and polling survive across calls.

## Proxy behaviors that keep strict clients happy

- **Raw byte passthrough**: when nothing was rewritten, echo the MCP server's
  response bytes verbatim. A decode→re-encode round trip can't tell `{}` from
  `[]` and corrupts empty arrays (CSP domain lists!), which claude.ai rejects
  against the tool's output schema.
- **Notifications** (JSON-RPC without `id`) are acknowledged with HTTP 202.
- **`ping`** results are normalized to `{}` (some backends return `[]`).
- **SSE without real streaming**: PHP/nginx can't reliably hold streams, so
  the GET endpoint returns one complete `event: endpoint` SSE body (raw-emit)
  pointing clients at the POST endpoint — spec-compliant and buffer-proof.
  For true streaming paths, disable buffering (`zlib.output_compression=0`,
  `X-Accel-Buffering: no`, flush `ob_` levels) and send headers natively.

## The chat host is also a host

com_mcp ships its own browser chat (`site/assets/js/`). It must declare its
capabilities at `initialize` like any other host — Tasks
(`io.modelcontextprotocol/tasks`) *and* MCP Apps
(`io.modelcontextprotocol/ui` with
`mimeTypes: ["text/html;profile=mcp-app"]`) — because servers gate behavior
on that declaration. Forgetting the declaration breaks your own host first.

Gateway URI quirk: the gateway prepends its origin to resource URIs
(`https://nanohub.org/ui://tool/app`), so hosts must canonicalize before
comparing against advertised `ui://` URIs (keep from the last non-http scheme
marker onward).
