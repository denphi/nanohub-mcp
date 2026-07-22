# Live verification (do this before trusting any bug report — including your own)

## Contents

- Local checks
- OAuth and DCR
- CORS
- Session-aware MCP handshake
- Scientific sanity and offline suites

**Prime directive: the code deployed on the hub is not your working tree.**
Header values, DCR admission rules, and controller revisions on the live site
have all been observed to differ from local checkouts. Verify behavior with
curl against the live host first; read code second.

Automated versions of the checks below ship with this skill:

```sh
python scripts/validate_server.py bin/yourtool.py --render-apps   # pre-deploy, offline
HUB=https://nanohub.org TOOL=yourtool scripts/smoke_live.sh        # post-deploy, live (OAuth/CORS/session)
python scripts/check_conformance.py http://localhost:8000          # core + extension conformance, live
```

`validate_server.py` (offline) and `check_conformance.py` (live) share one rule
set (`scripts/mcp_conformance.py`) so a check can't drift between them. Together
they cover the core protocol and every extension nanohub-mcp implements —
**MCP Apps** (`io.modelcontextprotocol/ui`: ui:// apps present ⇔ capability
advertised, and each app's `ui/initialize` handshake is shape-conformant),
**MCP Tasks** (`io.modelcontextprotocol/tasks`), and **elicitation**. Run
`check_conformance.py` against a local `start_mcp` first, then the hub URL with
`--token` (a hub deployment is not your working tree).

For manual runs, set `HUB=https://nanohub.org` (or your hub) and
`TOOL=yourtoolname`.

## 0. Local first

```sh
start_mcp --app bin/yourtool.py --port 8000
curl -s localhost:8000 -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python3 -m json.tool
```

Anything broken locally is broken deployed — settle schemas, defaults, and
tool behavior here where iteration is seconds, not tool-publish cycles.

## 1. OAuth discovery chain

```sh
# Protected-resource metadata + 401 challenge
curl -sD - -o /dev/null "$HUB/api/mcp/$TOOL/mcp" | grep -iE '^HTTP|www-auth'
#   → 401 with WWW-Authenticate: Bearer resource_metadata="…"

curl -s "$HUB/api/mcp/$TOOL/mcp/.well-known/oauth-protected-resource" | python3 -m json.tool
curl -s "$HUB/authorize/.well-known/oauth-authorization-server" | python3 -m json.tool
#   issuer must byte-match the authorization_servers entry (no trailing slash)
```

## 2. DCR + public-client token exchange (the definitive OAuth test)

```sh
# Register a throwaway public client (loopback = anonymous tier)
curl -s -X POST "$HUB/authorize/register" -H 'Content-Type: application/json' -d '{
  "client_name": "smoke-test",
  "redirect_uris": ["http://127.0.0.1:33418/callback"],
  "token_endpoint_auth_method": "none"}'
# → 201 with client_id, NO client_secret

# Exchange a bogus code with no secret
curl -s -X POST "$HUB/developer/oauth/token" \
  -d "grant_type=authorization_code&client_id=$CID&code=bogus&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"
```

Interpret via the error table in oauth-dcr.md. In short:
`invalid_grant` = **PASS** (client auth worked, only the fake code failed);
`invalid_client "…client secret"` = that client_id is unknown to the AS.
Clean up smoke-test client rows in the developer dashboard afterwards.

Run this mutating check only when needed (`RUN_DCR=1` in `smoke_live.sh`), and
delete the throwaway client afterwards.

## 3. CORS — compare preflight vs actual

```sh
curl -s -X OPTIONS -H 'Origin: https://example.com' \
  -H 'Access-Control-Request-Method: POST' -D - -o /dev/null \
  "$HUB/api/mcp/$TOOL/mcp" | grep -iE '^HTTP|access-control|www-auth'
curl -s -X POST -H 'Origin: https://example.com' \
  -H 'Content-Type: application/json' -d '{}' -D - -o /dev/null \
  "$HUB/api/mcp/$TOOL/mcp" | grep -iE '^HTTP|access-control|www-auth'
curl -s -H 'Origin: https://example.com' -D - -o /dev/null \
  "$HUB/api/mcp/$TOOL/mcp" | grep -iE '^HTTP|access-control|www-auth'
```

PASS requires on **actual** (non-OPTIONS) responses: `Access-Control-Allow-Origin`
present, `Access-Control-Expose-Headers` includes `WWW-Authenticate` and
`Mcp-Session-Id`, and no `Allow-Credentials: true` anywhere alongside `*`.
Headers that appear only on OPTIONS = the raw-emit-path bug (gateway-cors.md).

## 4. MCP handshake through the gateway

```sh
MCP="$HUB/api/mcp/$TOOL/mcp"
curl -s -D initialize.headers -o initialize.json -X POST "$MCP" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-06-18",
        "capabilities":{"extensions":{"io.modelcontextprotocol/ui":{"mimeTypes":["text/html;profile=mcp-app"]}}},
        "clientInfo":{"name":"smoke","version":"0"}}}'
SESSION_ID=$(awk 'tolower($1)=="mcp-session-id:" {print $2}' initialize.headers | tr -d '\r')
# The nanohub-mcp revision documented here names this notification
# `initialized`; verify newer framework revisions before changing it.
curl -s -X POST "$MCP" -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"initialized"}'
curl -s -X POST "$MCP" -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Reuse that exact session header for `resources/list`, `resources/read`,
`tools/call`, `tasks/get`, and `tasks/cancel`. Also verify: notification (no
`id`) → HTTP 202; a tool whose result contains
an empty array survives as `[]`, not `{}` (raw passthrough intact); response
carries `Mcp-Session-Id` when the server minted one.

## 5. Simulation sanity (defaults are the test)

Run each `create_*` tool with **no arguments** and check the generated
deck/output for physical sense (e.g. the PN diode default deck must contain
`elect=1` — anode sweep — and a forward IV that actually turns on, ideality
≈ 1, not ~1e-17 A leakage).

## 6. Local suites

```sh
python3 -m pytest tests/ -q -r a --junitxml=test-results.xml
python3 scripts/check_ci.py test-results.xml
python3 scripts/validate_server.py bin/yourtool.py
```

Watch for silently-skipped suites: padremcp's offline tests skip **all** tests
when an import fails (e.g. numpy missing from the venv) and report green-ish
"28 skipped". `0 passed` is a failure.

## When a report contradicts you

Reproduce the reporter's exact inputs (their redirect URI, their client_id)
before rebutting. In one real case the drafted reply claimed a localhost
redirect could never get tokens — live testing with the reporter's exact URI
proved the opposite in two curl calls, and the corrected reply asked for
their client_id instead. Empirics first, then the email.
