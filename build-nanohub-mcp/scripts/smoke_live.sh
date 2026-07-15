#!/bin/sh
# Live, mostly non-mutating verification for a nanoHUB MCP deployment.
#
# Usage:
#   HUB=https://nanohub.org TOOL=yourtool ./smoke_live.sh
#   HUB=... TOOL=... TOKEN=<bearer> ./smoke_live.sh
#   RUN_DCR=1 ...       # explicitly opt into creating a throwaway OAuth client
#   TASK_TOOL=run_simulation TASK_ARGS_JSON='{"run_handle":"..."}' ...
#
# DCR is opt-in because registration mutates the hub. Each check prints
# PASS/FAIL; the exit code is the number of failures.

HUB="${HUB:-https://nanohub.org}"
TOOL="${TOOL:?set TOOL=yourtoolname}"
FAILURES=0
MCP="$HUB/api/mcp/$TOOL/mcp"
TMP_INIT_HEADERS=$(mktemp)
TMP_INIT_BODY=$(mktemp)
trap 'rm -f "$TMP_INIT_HEADERS" "$TMP_INIT_BODY"' EXIT HUP INT TERM

say()  { printf '%s\n' "$*"; }
pass() { say "PASS   $*"; }
fail() { say "FAIL   $*"; FAILURES=$((FAILURES + 1)); }

say "== 1. 401 challenge + protected-resource metadata =="
HDRS=$(curl -sS -m 20 -D - -o /dev/null "$MCP")
echo "$HDRS" | grep -qi '^HTTP.* 401' \
  && pass "unauthenticated GET returns 401" \
  || fail "expected 401 on unauthenticated GET (clients discover OAuth on 401)"
echo "$HDRS" | grep -qi 'WWW-Authenticate:.*resource_metadata=' \
  && pass "WWW-Authenticate carries resource_metadata" \
  || fail "WWW-Authenticate missing resource_metadata"

PRM=$(curl -sS -m 20 "$MCP/.well-known/oauth-protected-resource")
AS=$(printf '%s' "$PRM" | python3 -c \
  'import sys,json; print(json.load(sys.stdin)["authorization_servers"][0])' 2>/dev/null)
[ -n "$AS" ] && pass "protected-resource metadata parses (AS: $AS)" \
             || fail "protected-resource metadata missing/unparseable"

say "== 2. Authorization-server metadata =="
ASMETA=$(curl -sS -m 20 "$AS/.well-known/oauth-authorization-server" 2>/dev/null)
ISSUER=$(printf '%s' "$ASMETA" | python3 -c \
  'import sys,json; print(json.load(sys.stdin)["issuer"])' 2>/dev/null)
if [ -n "$ISSUER" ]; then
  [ "$ISSUER" = "$AS" ] \
    && pass "issuer byte-matches authorization_servers entry" \
    || fail "issuer '$ISSUER' != advertised AS '$AS'"
  printf '%s' "$ASMETA" | grep -q '"none"' \
    && pass "token_endpoint_auth_methods includes none" \
    || fail "metadata does not advertise none"
else
  fail "AS metadata not served at $AS/.well-known/oauth-authorization-server"
fi

say "== 3. CORS: preflight vs actual =="
PRE=$(curl -sS -m 20 -X OPTIONS -H 'Origin: https://example.com' \
      -H 'Access-Control-Request-Method: POST' -D - -o /dev/null "$MCP")
ACT=$(curl -sS -m 20 -X POST -H 'Origin: https://example.com' \
      -H 'Content-Type: application/json' -d '{}' -D - -o /dev/null "$MCP")
echo "$PRE" | grep -qi 'Access-Control-Allow-Origin' \
  && pass "preflight has Access-Control-Allow-Origin" \
  || fail "preflight missing Access-Control-Allow-Origin"
echo "$ACT" | grep -qi 'Access-Control-Allow-Origin' \
  && pass "actual response has Access-Control-Allow-Origin" \
  || fail "actual response missing Access-Control-Allow-Origin"
echo "$ACT" | grep -qi 'Access-Control-Expose-Headers:.*WWW-Authenticate' \
  && pass "WWW-Authenticate is exposed to browser JS" \
  || fail "Access-Control-Expose-Headers missing WWW-Authenticate"
echo "$ACT" | grep -qi 'Access-Control-Expose-Headers:.*Mcp-Session-Id' \
  && pass "Mcp-Session-Id is exposed to browser JS" \
  || fail "Access-Control-Expose-Headers missing Mcp-Session-Id"
if echo "$PRE$ACT" | grep -qi 'Allow-Credentials: *true'; then
  echo "$PRE$ACT" | grep -qi 'Allow-Origin: *\\*' \
    && fail "invalid wildcard origin with Allow-Credentials" \
    || pass "credentials header present without wildcard origin"
else
  pass "no Allow-Credentials header (correct for bearer auth)"
fi

if [ "${RUN_DCR:-}" = "1" ]; then
  say "== 4. DCR + public-client token exchange (opt-in) =="
  REG=$(curl -sS -m 20 -X POST "$HUB/authorize/register" \
        -H 'Content-Type: application/json' \
        -d '{"client_name":"smoke-test","redirect_uris":["http://127.0.0.1:33418/callback"],"token_endpoint_auth_method":"none"}')
  CID=$(printf '%s' "$REG" | python3 -c \
    'import sys,json; print(json.load(sys.stdin).get("client_id",""))' 2>/dev/null)
  if [ -n "$CID" ]; then
    pass "DCR issued public client $CID (delete it afterwards)"
    EX=$(curl -sS -m 20 -X POST "$HUB/developer/oauth/token" \
         -d "grant_type=authorization_code&client_id=$CID&code=bogus&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback")
    printf '%s' "$EX" | grep -q 'invalid_grant' \
      && pass "secret-less exchange reached code validation" \
      || fail "unexpected token response: $EX"
  else
    fail "DCR registration failed: $REG"
  fi
else
  say "== 4. DCR skipped (set RUN_DCR=1 intentionally) =="
fi

if [ -n "${TOKEN:-}" ]; then
  say "== 5. MCP session, Resources, and optional Tasks =="
  curl -sS -m 30 -X POST "$MCP" -D "$TMP_INIT_HEADERS" -o "$TMP_INIT_BODY" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{"extensions":{"io.modelcontextprotocol/tasks":{},"io.modelcontextprotocol/ui":{"mimeTypes":["text/html;profile=mcp-app"]}}},"clientInfo":{"name":"nanohub-smoke","version":"1"}}}'
  INIT=$(cat "$TMP_INIT_BODY")
  SESSION_ID=$(awk 'tolower($1) == "mcp-session-id:" {print $2}' "$TMP_INIT_HEADERS" | tr -d '\r' | tail -n 1)
  printf '%s' "$INIT" | grep -q '"result"' \
    && pass "initialize succeeded" \
    || fail "initialize failed: $(printf '%.200s' "$INIT")"
  if [ -z "$SESSION_ID" ]; then
    fail "initialize did not return Mcp-Session-Id; session-bound checks cannot run"
  fi

  if [ -n "$SESSION_ID" ]; then
    INIT_STATUS=$(curl -sS -m 20 -o /dev/null -w '%{http_code}' -X POST "$MCP" \
      -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SESSION_ID" \
      -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","method":"initialized"}')
    case "$INIT_STATUS" in
      200|202) pass "initialized notification accepted ($INIT_STATUS)" ;;
      *) fail "initialized notification returned HTTP $INIT_STATUS" ;;
    esac

    TOOLS=$(curl -sS -m 30 -X POST "$MCP" \
      -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SESSION_ID" \
      -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
    N=$(printf '%s' "$TOOLS" | python3 -c \
      'import sys,json; print(len(json.load(sys.stdin)["result"]["tools"]))' 2>/dev/null)
    [ -n "$N" ] && [ "$N" -gt 0 ] \
      && pass "tools/list returned $N tools" \
      || fail "tools/list failed or empty: $(printf '%.200s' "$TOOLS")"

    RESOURCES=$(curl -sS -m 30 -X POST "$MCP" \
      -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SESSION_ID" \
      -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"resources/list","params":{}}')
    URI=$(printf '%s' "$RESOURCES" | python3 -c \
      'import sys,json; print((json.load(sys.stdin)["result"].get("resources") or [{}])[0].get("uri",""))' 2>/dev/null)
    if [ -n "$URI" ]; then
      READ=$(python3 -c 'import json,sys; print(json.dumps({"jsonrpc":"2.0","id":4,"method":"resources/read","params":{"uri":sys.argv[1]}}))' "$URI")
      RESOURCE_RESULT=$(curl -sS -m 30 -X POST "$MCP" \
        -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SESSION_ID" \
        -H 'Content-Type: application/json' -d "$READ")
      printf '%s' "$RESOURCE_RESULT" | grep -q '"result"' \
        && pass "resources/read succeeded for $URI" \
        || fail "resources/read failed for $URI: $(printf '%.200s' "$RESOURCE_RESULT")"
    else
      say "INFO   resources/list returned no resources"
    fi

    if [ -n "${TASK_TOOL:-}" ]; then
      if [ -z "${TASK_ARGS_JSON:-}" ]; then TASK_ARGS_JSON='{}'; fi
      CALL=$(TASK_ARGS_JSON="$TASK_ARGS_JSON" TASK_TOOL="$TASK_TOOL" python3 - <<'PY'
import json, os
print(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": os.environ["TASK_TOOL"],
                             "arguments": json.loads(os.environ["TASK_ARGS_JSON"])}}))
PY
)
      TASK_RESPONSE=$(curl -sS -m 30 -X POST "$MCP" \
        -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SESSION_ID" \
        -H 'Content-Type: application/json' -d "$CALL")
      TASK_ID=$(printf '%s' "$TASK_RESPONSE" | python3 -c \
        'import sys,json; print(json.load(sys.stdin).get("result",{}).get("taskId",""))' 2>/dev/null)
      if [ -z "$TASK_ID" ]; then
        fail "async tool did not return a task: $(printf '%.200s' "$TASK_RESPONSE")"
      else
        pass "async tool returned task $TASK_ID"
        n=0
        while [ "$n" -lt 30 ]; do
          n=$((n + 1))
          sleep 1
          TASK_GET=$(python3 -c 'import json,sys; print(json.dumps({"jsonrpc":"2.0","id":6,"method":"tasks/get","params":{"taskId":sys.argv[1]}}))' "$TASK_ID")
          TASK_STATUS=$(curl -sS -m 30 -X POST "$MCP" \
            -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SESSION_ID" \
            -H 'Content-Type: application/json' -d "$TASK_GET")
          STATUS=$(printf '%s' "$TASK_STATUS" | python3 -c \
            'import sys,json; print(json.load(sys.stdin).get("result",{}).get("status",""))' 2>/dev/null)
          case "$STATUS" in
            completed) pass "task completed"; break ;;
            failed|cancelled) fail "task ended $STATUS: $(printf '%.200s' "$TASK_STATUS")"; break ;;
          esac
          if [ "$n" -eq 30 ]; then fail "task did not finish within 30 polls"; fi
        done
      fi
    fi
  fi
else
  say "== 5. authenticated MCP checks skipped (set TOKEN=...) =="
fi

say ""
say "$FAILURES failure(s)"
exit "$FAILURES"
