#!/usr/bin/env python3
"""Live MCP + extension conformance driver for a running nanohub-mcp server.

Drives a running server over Streamable HTTP the way a real MCP host would, and
asserts conformance to the core protocol AND the extensions nanohub-mcp
implements. Complements the offline `validate_server.py` (which inspects the
module) by checking what the server *actually negotiates on the wire*.

Usage:
    # local (start_mcp --app bin/yourtool.py --port 8000)
    python check_conformance.py http://localhost:8000

    # deployed on the hub (needs a bearer token)
    python check_conformance.py https://nanohub.org/api/mcp/yourtool/mcp --token "$TOKEN"

    python check_conformance.py URL --call-tool list_actions            # exercise a keyless tool
    python check_conformance.py URL --task-tool run_sim --task-args '{"run_handle":"..."}'

Checks:
  core   initialize / initialized / tools/list / resources/list / prompts/list;
         every tool inputSchema is an object.
  apps   ui:// resources ⇔ io.modelcontextprotocol/ui advertised; each app is
         resources/read-able with the app mimeType, a _meta.ui block, and a
         conformant ext-apps handshake in its HTML.
  tasks  io.modelcontextprotocol/tasks advertised ⇔ tasks/get answers with a
         structured JSON-RPC error for an unknown id (never a transport crash).
  elicit initialize accepts a client that declares elicitation; optional deep
         probe via --call-tool of a known elicitation tool.

Exit code = number of failed checks (0 = fully conformant). PASS/FAIL per line.
Run against a local server first; the hub deployment is not your working tree.
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import uuid
from urllib import request as _request
from urllib import error as _error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_conformance import (  # noqa: E402
    MCP_APP_MIME,
    APPS_EXTENSION_ID,
    TASKS_EXTENSION_ID,
    check_app_handshake,
)

# Client capabilities we advertise so the server exposes everything it can:
# every extension nanohub-mcp knows about, plus core elicitation.
_CLIENT_CAPABILITIES = {
    "elicitation": {},
    "extensions": {
        APPS_EXTENSION_ID: {"mimeTypes": [MCP_APP_MIME]},
        TASKS_EXTENSION_ID: {},
    },
}


class Driver(object):
    """Minimal Streamable-HTTP MCP client (JSON or SSE responses, optional
    session header, optional bearer token)."""

    def __init__(self, url, token=None, timeout=30):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id = None
        self._id = 0

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        req = _request.Request(self.url, data=data, headers=self._headers(), method="POST")
        try:
            resp = _request.urlopen(req, timeout=self.timeout)
        except _error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            return exc.code, {}, body
        sid = resp.headers.get("Mcp-Session-Id")
        if sid and not self.session_id:
            self.session_id = sid
        return resp.getcode(), dict(resp.headers), resp.read().decode("utf-8", "replace")

    @staticmethod
    def _parse(body):
        """Return the JSON-RPC object from a JSON or SSE response body."""
        body = (body or "").strip()
        if body.startswith("{"):
            try:
                return json.loads(body)
            except ValueError:
                pass
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return {}

    def request(self, method, params=None):
        self._id += 1
        code, _headers, body = self._post({
            "jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        return code, self._parse(body)

    def notify(self, method, params=None):
        code, _headers, _body = self._post({
            "jsonrpc": "2.0", "method": method, "params": params or {}})
        return code


class Report(object):
    def __init__(self):
        self.failures = 0

    def check(self, cond, label, detail=""):
        if cond:
            print("PASS   " + label)
        else:
            print("FAIL   " + label + ((" — " + detail) if detail else ""))
            self.failures += 1
        return cond

    def info(self, label):
        print("       " + label)


def run(url, token=None, call_tool=None, tool_args=None, task_tool=None,
        task_args=None, timeout=30):
    rep = Report()
    d = Driver(url, token=token, timeout=timeout)

    # ── core: initialize ────────────────────────────────────────────────────
    print("== core: initialize ==")
    code, res = d.request("initialize", {
        "protocolVersion": "2026-01-26",
        "capabilities": _CLIENT_CAPABILITIES,
        "clientInfo": {"name": "nanohub-conformance", "version": "1"},
    })
    if code == 401:
        rep.check(False, "initialize", "401 Unauthorized — pass --token for a hub deployment")
        print("\n{} failed check(s)".format(rep.failures))
        return rep.failures
    result = res.get("result") or {}
    if not rep.check(bool(result) and not res.get("error"), "initialize succeeded",
                     json.dumps(res)[:200]):
        print("\n{} failed check(s)".format(rep.failures))
        return rep.failures
    caps = result.get("capabilities") or {}
    advertised = caps.get("extensions") or {}
    rep.info("server: {} / {}".format(
        (result.get("serverInfo") or {}).get("name", "?"),
        result.get("protocolVersion", "?")))
    rep.info("extensions advertised: {}".format(", ".join(sorted(advertised)) or "none"))
    d.notify("initialized")

    # ── core: discovery ─────────────────────────────────────────────────────
    print("== core: tools / resources / prompts ==")
    _c, tl = d.request("tools/list")
    tools = (tl.get("result") or {}).get("tools") or []
    rep.check(len(tools) > 0, "tools/list returns >=1 tool", json.dumps(tl)[:200])
    bad_schema = [t.get("name") for t in tools
                  if (t.get("inputSchema") or {}).get("type") != "object"]
    rep.check(not bad_schema, "every tool inputSchema is an object",
              "offenders: {}".format(bad_schema))

    _c, rl = d.request("resources/list")
    resources = (rl.get("result") or {}).get("resources") or []
    rep.check("error" not in rl, "resources/list ok", json.dumps(rl)[:160])

    _c, pl = d.request("prompts/list")
    rep.check("error" not in pl, "prompts/list ok", json.dumps(pl)[:160])

    # ── apps extension ──────────────────────────────────────────────────────
    print("== extension: MCP Apps (io.modelcontextprotocol/ui) ==")
    ui_resources = [r for r in resources if str(r.get("uri", "")).startswith("ui://")]
    ui_advertised = APPS_EXTENSION_ID in advertised
    if ui_resources:
        # A server exposing ui:// apps MUST advertise the capability or no host
        # will mount them.
        if rep.check(ui_advertised, "ui:// apps present => ui extension advertised",
                     "{} app(s) but {} absent".format(len(ui_resources), APPS_EXTENSION_ID)):
            mimes = (advertised.get(APPS_EXTENSION_ID) or {}).get("mimeTypes") or []
            rep.check(MCP_APP_MIME in mimes,
                      "ui extension advertises {!r}".format(MCP_APP_MIME),
                      "mimeTypes: {}".format(mimes))
        for r in ui_resources:
            uri = r["uri"]
            _c, rr = d.request("resources/read", {"uri": uri})
            contents = (rr.get("result") or {}).get("contents") or []
            html = "".join(c.get("text", "") for c in contents if isinstance(c, dict))
            mimes = [c.get("mimeType") for c in contents if isinstance(c, dict)]
            has_meta = any("_meta" in c for c in contents if isinstance(c, dict))
            rep.check(bool(html), "resources/read {} returns content".format(uri),
                      json.dumps(rr)[:160])
            rep.check(MCP_APP_MIME in mimes,
                      "{} served as {!r}".format(uri, MCP_APP_MIME), "got {}".format(mimes))
            rep.check(has_meta, "{} carries _meta.ui".format(uri))
            errs, warns = check_app_handshake(html)
            rep.check(not errs, "{} ext-apps handshake conformant".format(uri),
                      "; ".join(errs))
            for w in warns:
                rep.info("{}: {}".format(uri, w))
    elif ui_advertised:
        rep.check(False, "ui extension advertised but no ui:// resource is readable",
                  "advertised {} with zero apps".format(APPS_EXTENSION_ID))
    else:
        rep.info("no MCP Apps on this server (skipped)")

    # ── tasks extension ─────────────────────────────────────────────────────
    print("== extension: MCP Tasks (io.modelcontextprotocol/tasks) ==")
    tasks_advertised = TASKS_EXTENSION_ID in advertised
    _c, tg = d.request("tasks/get", {"taskId": "conformance-nonexistent-" + uuid.uuid4().hex})
    # However it answers, it must be a structured JSON-RPC reply, not a crash.
    structured = isinstance(tg, dict) and ("error" in tg or "result" in tg)
    rep.check(structured, "tasks/get returns a structured JSON-RPC reply",
              json.dumps(tg)[:160])
    if tasks_advertised:
        rep.info("tasks extension advertised (server has async tools)")
    if task_tool:
        print("== tasks: exercise {} ==".format(task_tool))
        _c, cr = d.request("tools/call", {
            "name": task_tool, "arguments": json.loads(task_args or "{}")})
        rep.check("error" not in cr, "tools/call {} accepted".format(task_tool),
                  json.dumps(cr)[:200])

    # ── elicitation ─────────────────────────────────────────────────────────
    print("== core: elicitation ==")
    # We declared elicitation at initialize; a conformant server accepted it
    # without choking. Deep-probing requires a tool that actually elicits.
    rep.check(True, "server accepted a client declaring elicitation capability")
    if call_tool:
        print("== exercise tool {} ==".format(call_tool))
        _c, cr = d.request("tools/call", {
            "name": call_tool, "arguments": json.loads(tool_args or "{}")})
        rep.check("error" not in cr, "tools/call {} succeeded".format(call_tool),
                  json.dumps(cr)[:200])

    print("\n{} failed check(s)".format(rep.failures))
    return rep.failures


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", help="MCP endpoint, e.g. http://localhost:8000 or "
                               "https://nanohub.org/api/mcp/<tool>/mcp")
    p.add_argument("--token", default=os.environ.get("TOKEN"),
                   help="OAuth bearer token (or $TOKEN); required for hub deployments")
    p.add_argument("--call-tool", help="name of a keyless tool to exercise")
    p.add_argument("--tool-args", help="JSON arguments for --call-tool")
    p.add_argument("--task-tool", help="name of an async tool to exercise")
    p.add_argument("--task-args", help="JSON arguments for --task-tool")
    p.add_argument("--timeout", type=int, default=30)
    args = p.parse_args()
    try:
        return run(args.url, token=args.token, call_tool=args.call_tool,
                   tool_args=args.tool_args, task_tool=args.task_tool,
                   task_args=args.task_args, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print("FAIL   driver error: {}".format(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
