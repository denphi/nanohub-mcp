"""Validate live server output against the vendored MCP 2026-07-28 schemas.

These assert conformance to the *specification*, not to our reading of it:
every shape below is checked against the machine-readable JSON Schema
published alongside the protocol revision.

Skipped only when `jsonschema` is absent; the schemas themselves are vendored
under tests/schemas/ so the suite never needs the network.
"""

from __future__ import print_function

import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("jsonschema")
from jsonschema import Draft202012Validator  # noqa: E402

from nanohubmcp import MCPServer  # noqa: E402
from nanohubmcp.server import _SSEQueue  # noqa: E402

_SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")


def _load(name):
    with open(os.path.join(_SCHEMA_DIR, name)) as handle:
        return json.load(handle)


BASE = _load("mcp-2026-07-28.schema.json")
TASKS = _load("ext-tasks-2026-07-28.schema.json")

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
}


def validate(schema, definition, instance):
    """Assert `instance` matches `definition` in `schema`, with a readable diff."""
    root = dict(schema)
    root["$ref"] = "#/$defs/" + definition
    errors = sorted(Draft202012Validator(root).iter_errors(instance),
                    key=lambda e: list(e.path))
    if errors:
        detail = "\n".join(
            "  {}: {}".format(list(e.path) or "<root>", e.message) for e in errors[:5]
        )
        raise AssertionError("does not match {}:\n{}\ninstance: {}".format(
            definition, detail, json.dumps(instance)[:600]))


def modern_rpc(server, method, params=None, msg_id=1, session_id=None):
    payload = dict(params or {})
    payload["_meta"] = dict(MODERN_META, **payload.get("_meta", {}))
    return server._handle_request(
        {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": payload},
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# The validator must be able to fail, or every test below is worthless.
# ---------------------------------------------------------------------------

def test_validator_rejects_malformed_instances():
    good = {
        "taskId": "t", "status": "working", "createdAt": "2026-01-01T00:00:00Z",
        "lastUpdatedAt": "2026-01-01T00:00:00Z", "ttlMs": 1000, "resultType": "task",
    }
    validate(TASKS, "CreateTaskResult", good)

    for broken in (
        {k: v for k, v in good.items() if k != "taskId"},
        {k: v for k, v in good.items() if k != "ttlMs"},
        {k: v for k, v in good.items() if k != "resultType"},
        dict(good, status="finished"),
        dict(good, resultType="complete"),
        dict(good, ttlMs="1000"),
    ):
        with pytest.raises(AssertionError):
            validate(TASKS, "CreateTaskResult", broken)


# ---------------------------------------------------------------------------
# Base protocol
# ---------------------------------------------------------------------------

def _server():
    server = MCPServer("schemasrv", version="0.4.0", instructions="Test server.")

    @server.tool(input_schema={"type": "object", "properties": {}, "required": []})
    def about():
        """Describe this server for schema conformance tests."""
        return {"name": "schemasrv"}

    @server.tool(input_schema={"type": "object", "properties": {}, "required": []})
    def ask(ctx=None):
        """Ask the caller a question, exercising the MRTR path."""
        answer = ctx.elicit("Pick one", requested_schema={
            "type": "object", "properties": {"q": {"type": "string"}},
            "required": ["q"]})
        return {"picked": answer["content"]["q"]}

    @server.resource("ui://schemasrv/app", mime_type="text/html;profile=mcp-app")
    def app():
        """An MCP App resource, the shape most sensitive to read conformance."""
        return "<!DOCTYPE html><html><body>ok</body></html>"

    return server


def test_server_discover_matches_schema():
    validate(BASE, "DiscoverResult", modern_rpc(_server(), "server/discover")["result"])


def test_list_results_are_cacheable():
    server = _server()
    validate(BASE, "ListToolsResult", modern_rpc(server, "tools/list")["result"])
    validate(BASE, "ListResourcesResult", modern_rpc(server, "resources/list")["result"])
    validate(BASE, "ListPromptsResult", modern_rpc(server, "prompts/list")["result"])


def test_read_result_is_cacheable():
    """resources/read is a CacheableResult too — reads were missing the hints.

    ReadResourceResult requires ttlMs and cacheScope just as the list results
    do. A host that validates the response drops the whole read when they are
    absent, which silently breaks every MCP App the server serves.
    """
    result = modern_rpc(_server(), "resources/read",
                        {"uri": "ui://schemasrv/app"})["result"]
    validate(BASE, "ReadResourceResult", result)
    assert result["ttlMs"] > 0
    assert result["cacheScope"] in ("public", "private")


def test_unsupported_version_error_matches_schema():
    response = _server()._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "2099-01-01",
            "io.modelcontextprotocol/clientCapabilities": {}}},
    })
    validate(BASE, "UnsupportedProtocolVersionError", response)


def test_input_required_result_matches_schema():
    """MRTR: the server asks by returning, and the retry completes the call."""
    server = _server()
    first = modern_rpc(server, "tools/call", {"name": "ask", "arguments": {}})["result"]
    validate(BASE, "InputRequiredResult", first)
    assert first["resultType"] == "input_required"

    for request in first["inputRequests"].values():
        validate(BASE, "ElicitRequest", request)

    second = modern_rpc(server, "tools/call", {
        "name": "ask", "arguments": {},
        "requestState": first["requestState"],
        "inputResponses": {"input-1": {"action": "accept", "content": {"q": "blue"}}},
    })["result"]
    validate(BASE, "CallToolResult", second)
    assert second["structuredContent"] == {"picked": "blue"}


def test_subscription_shapes_match_schema():
    server = _server()

    @server.async_tool()
    def slow(ctx=None):
        time.sleep(0.3)
        return "done"

    session = "sess"
    server._set_session_capabilities(
        session, {"extensions": {"io.modelcontextprotocol/tasks": {}}})
    probe = _SSEQueue()
    with server._clients_lock:
        server._clients.setdefault(session, []).append(probe)

    task_id = modern_rpc(server, "tools/call", {"name": "slow", "arguments": {}},
                         session_id=session)["result"]["taskId"]
    listen = {"jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen",
              "params": {"notifications": {"taskIds": [task_id]},
                         "_meta": dict(MODERN_META)}}
    validate(BASE, "SubscriptionsListenRequest", listen)
    assert server._handle_request(listen, session_id=session) is None

    acks = [json.loads(m) for m in probe if "acknowledged" in m]
    assert acks
    validate(BASE, "SubscriptionsAcknowledgedNotification", acks[-1])


# ---------------------------------------------------------------------------
# Tasks extension
# ---------------------------------------------------------------------------

def _task_server():
    server = MCPServer("tasksrv")
    gate = threading.Event()

    def prepare(ctx=None):
        ctx.set_task_metadata(jobHandle="job-1")

    @server.async_tool(prepare=prepare)
    def work(ctx=None):
        gate.wait(5)
        return {"status": "finished"}

    @server.async_tool()
    def stuck(ctx=None):
        ctx.cancel_event.wait(10)
        return {"status": "cancelled"}

    server._set_session_capabilities(
        "sess", {"extensions": {"io.modelcontextprotocol/tasks": {}}})
    return server, gate


def _await_status(server, task_id, wanted, timeout=3.0):
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        result = modern_rpc(server, "tasks/get", {"taskId": task_id},
                            session_id="sess")["result"]
        if result["status"] == wanted:
            return result
        time.sleep(0.02)
    return result


def test_task_lifecycle_shapes_match_schema():
    server, gate = _task_server()
    probe = _SSEQueue()
    with server._clients_lock:
        server._clients.setdefault("sess", []).append(probe)

    created = modern_rpc(server, "tools/call", {"name": "work", "arguments": {}},
                         session_id="sess")["result"]
    validate(TASKS, "CreateTaskResult", created)
    assert created["_meta"]["org.nanohub/jobHandle"] == "job-1"
    task_id = created["taskId"]

    server._handle_request(
        {"jsonrpc": "2.0", "id": "s1", "method": "subscriptions/listen",
         "params": {"notifications": {"taskIds": [task_id]}}}, session_id="sess")

    validate(TASKS, "GetTaskResult", _await_status(server, task_id, "working"))
    gate.set()
    validate(TASKS, "GetTaskResult", _await_status(server, task_id, "completed"))

    pushed = [json.loads(m) for m in probe
              if json.loads(m).get("method") == "notifications/tasks"]
    assert pushed, "no notifications/tasks delivered"
    validate(TASKS, "TaskStatusNotification", pushed[-1])


def test_cancelled_task_shapes_match_schema():
    server, _ = _task_server()
    task_id = modern_rpc(server, "tools/call", {"name": "stuck", "arguments": {}},
                         session_id="sess")["result"]["taskId"]
    time.sleep(0.15)
    validate(TASKS, "CancelTaskResult",
             modern_rpc(server, "tasks/cancel", {"taskId": task_id},
                        session_id="sess")["result"])
    validate(TASKS, "GetTaskResult", _await_status(server, task_id, "cancelled"))


# ---------------------------------------------------------------------------
# MRTR inside a task: park and resume, rather than re-run
# ---------------------------------------------------------------------------

def test_task_input_required_parks_the_worker_and_resumes_in_place():
    """An async tool asks without re-running: prep must happen exactly once.

    This is the whole reason tasks use `tasks/update` instead of the sync
    retry: the worker thread is alive, so it waits and continues rather than
    being re-driven from the top.
    """
    server = MCPServer("taskmrtr")
    effects = []

    @server.async_tool()
    def long_job(deck, ctx=None):
        effects.append("prep")
        # Real prep work; also lets the subscription below land before the
        # worker parks, since the input_required push fires once at that moment.
        time.sleep(0.1)
        answer = ctx.elicit("Proceed with {}?".format(deck), requested_schema={
            "type": "object", "properties": {"go": {"type": "boolean"}},
            "required": ["go"]}, timeout=10)
        if not answer["content"]["go"]:
            return {"status": "declined"}
        effects.append("work")
        return {"status": "finished", "deck": deck}

    server._set_session_capabilities("sess", {"extensions": {
        "io.modelcontextprotocol/tasks": {}}})
    probe = _SSEQueue()
    with server._clients_lock:
        server._clients.setdefault("sess", []).append(probe)

    task_id = modern_rpc(server, "tools/call",
                         {"name": "long_job", "arguments": {"deck": "D1"}},
                         session_id="sess")["result"]["taskId"]
    server._handle_request(
        {"jsonrpc": "2.0", "id": "s1", "method": "subscriptions/listen",
         "params": {"notifications": {"taskIds": [task_id]}}}, session_id="sess")

    parked = _await_status(server, task_id, "input_required")
    assert parked["status"] == "input_required"
    validate(TASKS, "GetTaskResult", parked)
    validate(TASKS, "InputRequiredTask",
             {k: v for k, v in parked.items() if k != "resultType"})

    key = list(parked["inputRequests"])[0]
    validate(BASE, "ElicitRequest", parked["inputRequests"][key])

    # Subscribers are told the task needs something.
    statuses = [json.loads(m)["params"]["status"] for m in probe
                if json.loads(m).get("method") == "notifications/tasks"]
    assert "input_required" in statuses

    # A key that was never asked for is refused.
    rejected = modern_rpc(server, "tasks/update", {
        "taskId": task_id, "inputResponses": {"nope": {"action": "accept"}}},
        session_id="sess")
    assert rejected["error"]["code"] == -32602

    accepted = modern_rpc(server, "tasks/update", {
        "taskId": task_id,
        "inputResponses": {key: {"action": "accept", "content": {"go": True}}}},
        session_id="sess")["result"]
    validate(TASKS, "UpdateTaskResult", accepted)

    finished = _await_status(server, task_id, "completed")
    validate(TASKS, "GetTaskResult", finished)
    assert finished["result"]["structuredContent"] == {
        "status": "finished", "deck": "D1"}

    # The point of parking: no repeated prologue.
    assert effects == ["prep", "work"], effects


def test_tasks_update_rejects_a_task_that_is_not_waiting():
    server, gate = _task_server()
    task_id = modern_rpc(server, "tools/call", {"name": "work", "arguments": {}},
                         session_id="sess")["result"]["taskId"]
    response = modern_rpc(server, "tasks/update", {
        "taskId": task_id, "inputResponses": {"input-1": {"action": "accept"}}},
        session_id="sess")
    gate.set()
    assert response["error"]["code"] == -32602
    assert "not awaiting input" in response["error"]["message"]


# ---------------------------------------------------------------------------
# Mcp-Method / Mcp-Name routing headers
# ---------------------------------------------------------------------------

def test_routing_headers_are_validated_when_present():
    server = _server()
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "about", "arguments": {}}}

    # Absent or agreeing headers: the call proceeds.
    assert "result" in server._handle_request(call)
    assert "result" in server._handle_request(
        call, headers={"Mcp-Method": "tools/call", "Mcp-Name": "about"})

    # Disagreeing headers are a HeaderMismatch, because a gateway routes on
    # them without reading the body.
    for headers in ({"Mcp-Method": "tools/list"},
                    {"Mcp-Method": "tools/call", "Mcp-Name": "other"}):
        response = server._handle_request(call, headers=headers)
        assert response["error"]["code"] == -32020
        validate(BASE, "HeaderMismatchError", response)


# ---------------------------------------------------------------------------
# Remaining 2026-07-28 surface
# ---------------------------------------------------------------------------

def test_resource_templates_list_is_answered():
    """A client walking every resources/* endpoint must not hit -32601."""
    result = modern_rpc(_server(), "resources/templates/list")["result"]
    validate(BASE, "ListResourceTemplatesResult", result)
    assert result["resourceTemplates"] == []


def test_log_messages_only_flow_when_the_request_asked_for_them():
    """Spec: servers MUST NOT emit notifications/message without a logLevel."""
    server = MCPServer("logsrv")

    @server.tool(input_schema={"type": "object", "properties": {}, "required": []})
    def noisy(ctx=None):
        """Log at several levels for the logging test."""
        ctx.debug("dbg")
        ctx.info("hello")
        ctx.error("boom")
        return {"ok": True}

    probe = _SSEQueue()
    with server._clients_lock:
        server._clients.setdefault("S", []).append(probe)

    def call(meta):
        del probe[:]
        server._handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "noisy", "arguments": {}, "_meta": meta},
        }, session_id="S")
        return [json.loads(m) for m in probe
                if json.loads(m).get("method") == "notifications/message"]

    assert call(dict(MODERN_META)) == []

    emitted = call(dict(MODERN_META, **{"io.modelcontextprotocol/logLevel": "info"}))
    assert [m["params"]["level"] for m in emitted] == ["info", "error"]
    validate(BASE, "LoggingMessageNotification", emitted[0])


def test_logging_capability_has_no_subfields():
    """`logging` declares itself by presence; listChanged belongs elsewhere."""
    server = MCPServer("caps")

    @server.tool()
    def noop():
        """Do nothing, so the server has a tool."""
        return 1

    capabilities = server._get_capabilities().to_dict()
    assert capabilities["logging"] == {}
    validate(BASE, "ServerCapabilities", capabilities)


def test_cancelled_notification_stops_the_task_it_started():
    server = MCPServer("cancelnotif")
    stopped = threading.Event()

    @server.async_tool()
    def slow(ctx=None):
        ctx.on_cancel(stopped.set)
        ctx.cancel_event.wait(10)
        return {"status": "cancelled"}

    server._set_session_capabilities("sess", {"extensions": {
        "io.modelcontextprotocol/tasks": {}}})
    task_id = modern_rpc(server, "tools/call", {"name": "slow", "arguments": {}},
                         msg_id="req-9", session_id="sess")["result"]["taskId"]
    time.sleep(0.1)

    assert server._handle_request({
        "jsonrpc": "2.0", "method": "notifications/cancelled",
        "params": {"requestId": "req-9", "reason": "user withdrew"},
    }, session_id="sess") is None

    assert stopped.wait(2), "cancelled notification did not stop the task"
    assert _await_status(server, task_id, "cancelled")["status"] == "cancelled"


def test_cancelled_notification_closes_a_listen_subscription():
    server = _server()

    @server.async_tool()
    def slow(ctx=None):
        time.sleep(0.3)
        return "done"

    server._set_session_capabilities("sess", {"extensions": {
        "io.modelcontextprotocol/tasks": {}}})
    task_id = modern_rpc(server, "tools/call", {"name": "slow", "arguments": {}},
                         session_id="sess")["result"]["taskId"]
    server._handle_request({
        "jsonrpc": "2.0", "id": "sub-9", "method": "subscriptions/listen",
        "params": {"notifications": {"taskIds": [task_id]}}}, session_id="sess")
    assert "sub-9" in server._subscriptions["sess"]

    server._handle_request({
        "jsonrpc": "2.0", "method": "notifications/cancelled",
        "params": {"requestId": "sub-9"}}, session_id="sess")
    assert "sess" not in server._subscriptions


def test_a_cancelled_parked_worker_is_told_it_was_cancelled():
    """Cancellation wakes the same event an answer does.

    Without re-checking, the handler sees "no response supplied" — so a tool
    failing closed on RuntimeError records the wrong cause, and an operator
    reading the log looks for a client capability problem that isn't there.
    """
    server = MCPServer("park")
    seen = {}

    @server.async_tool()
    def asks(ctx=None):
        time.sleep(0.05)
        try:
            ctx.elicit("well?", timeout=30)
        except RuntimeError as exc:
            seen["error"] = str(exc)
            raise
        return {"status": "answered"}

    server._set_session_capabilities("sess", {"extensions": {
        "io.modelcontextprotocol/tasks": {}}})
    task_id = modern_rpc(server, "tools/call", {"name": "asks", "arguments": {}},
                         session_id="sess")["result"]["taskId"]
    assert _await_status(server, task_id, "input_required")["status"] == "input_required"

    server.cancel_job(task_id)
    deadline = time.time() + 3
    while time.time() < deadline and "error" not in seen:
        time.sleep(0.02)
    assert "cancelled" in seen.get("error", ""), seen


def test_cancelling_a_task_releases_a_parked_worker():
    """A worker waiting in input_required must not hang until timeout."""
    server = MCPServer("parkcancel")

    @server.async_tool()
    def asks(ctx=None):
        time.sleep(0.05)
        ctx.elicit("well?", timeout=30)
        return {"status": "answered"}

    server._set_session_capabilities("sess", {"extensions": {
        "io.modelcontextprotocol/tasks": {}}})
    task_id = modern_rpc(server, "tools/call", {"name": "asks", "arguments": {}},
                         session_id="sess")["result"]["taskId"]
    assert _await_status(server, task_id, "input_required")["status"] == "input_required"

    started = time.time()
    assert server.cancel_job(task_id) is True
    deadline = time.time() + 3
    while time.time() < deadline and server._jobs[task_id].get("status") == "input_required":
        time.sleep(0.02)
    # Released promptly rather than sitting out the 30s elicit timeout.
    assert time.time() - started < 3


def test_routing_headers_required_by_default_only_for_2026_07_28():
    """Default "auto": enforce exactly where the declaring revision requires it."""
    server = _server()
    modern = {"name": "about", "arguments": {}, "_meta": dict(MODERN_META)}
    legacy = {"name": "about", "arguments": {}}

    def call(params, headers):
        return server._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
            headers=headers)

    # A 2025-11-25 client never had these headers; demanding them would reject
    # a conformant client.
    assert "result" in call(legacy, {"Host": "x"})

    # A 2026-07-28 client is required by spec to send them.
    missing = call(modern, {"Host": "x"})
    assert missing["error"]["code"] == -32020
    validate(BASE, "HeaderMismatchError", missing)

    assert call(modern, {"Mcp-Method": "tools/call"})["error"]["code"] == -32020
    assert "result" in call(modern, {"Mcp-Method": "tools/call", "Mcp-Name": "about"})

    # A call that names nothing needs no Mcp-Name.
    assert "result" in server._handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
         "params": {"_meta": dict(MODERN_META)}},
        headers={"Mcp-Method": "tools/list"})


def test_transports_without_headers_are_never_rejected():
    """Direct calls, SSE and stdio have nowhere to put a routing header."""
    server = _server()
    modern = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "about", "arguments": {},
                         "_meta": dict(MODERN_META)}}
    assert "result" in server._handle_request(modern)
    assert "result" in server._handle_request(modern, headers=None)


def test_routing_headers_enforcement_can_be_forced_either_way():
    server = _server()
    legacy = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "about", "arguments": {}}}
    modern = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "about", "arguments": {},
                         "_meta": dict(MODERN_META)}}

    # Forced on: even a handshake-era client must supply them.
    server._require_route_headers = True
    assert server._handle_request(legacy, headers={"Host": "x"})["error"]["code"] == -32020

    # Forced off: even a 2026-07-28 client may omit them.
    server._require_route_headers = False
    assert "result" in server._handle_request(modern, headers={"Host": "x"})

    # A contradicting header is rejected regardless of the setting.
    assert server._handle_request(
        modern, headers={"Mcp-Method": "tools/list"})["error"]["code"] == -32020


def test_header_and_version_errors_are_http_400():
    """Both MUST be 400 so a gateway can see them without parsing the body."""
    server = _server()
    header_error = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Mcp-Method": "tools/call"})
    version_error = server._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "2099-01-01",
            "io.modelcontextprotocol/clientCapabilities": {}}}})

    assert server._http_status_for(header_error) == 400
    assert server._http_status_for(version_error) == 400
    assert server._http_status_for({"jsonrpc": "2.0", "id": 1, "result": {}}) == 200
    # Batch: one bad member is enough.
    assert server._http_status_for([{"jsonrpc": "2.0", "id": 1, "result": {}},
                                    header_error]) == 400


def test_mrtr_respects_client_capabilities():
    """A client that can't answer must get RuntimeError, not an unanswerable ask.

    The fail-closed pattern the docs teach is `except RuntimeError` -> refuse.
    If MRTR asked a client that never declared `elicitation`, that handler
    would neither act nor refuse: it would sit in a round trip forever.
    """
    from nanohubmcp import ToolResult

    server = MCPServer("failclosed")

    @server.tool(input_schema={"type": "object", "properties": {}, "required": []})
    def delete_run(ctx=None):
        """Destructive tool guarded by a confirmation prompt."""
        try:
            answer = ctx.elicit("Delete?", requested_schema={
                "type": "object", "properties": {"c": {"type": "boolean"}},
                "required": ["c"]})
        except RuntimeError:
            return ToolResult(content="NOT deleted: no prompt available.",
                              is_error=True)
        return {"status": "deleted" if answer.get("action") == "accept" else "cancelled"}

    def call(capabilities):
        return server._handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "delete_run", "arguments": {}, "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": capabilities}},
        }, session_id="S")["result"]

    refused = call({})
    assert refused["isError"] is True
    assert "NOT deleted" in refused["content"][0]["text"]

    assert call({"elicitation": {}})["resultType"] == "input_required"


def test_input_required_survives_a_broad_except_in_a_handler():
    """InputRequired is control flow: `except Exception` must not eat the ask."""
    server = MCPServer("broad")

    @server.tool(input_schema={"type": "object", "properties": {}, "required": []})
    def careless(ctx=None):
        """A handler that wraps everything in a broad except."""
        try:
            answer = ctx.elicit("Pick", requested_schema={
                "type": "object", "properties": {"q": {"type": "string"}},
                "required": ["q"]})
            return {"picked": answer["content"]["q"]}
        except Exception:
            return {"picked": "swallowed"}

    result = server._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "careless", "arguments": {}, "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}}},
    }, session_id="S")["result"]

    assert result["resultType"] == "input_required"
    assert "structuredContent" not in result


# ---------------------------------------------------------------------------
# The MRTR paths other than form elicitation
# ---------------------------------------------------------------------------

def _mrtr_server(body):
    server = MCPServer("mrtr")
    server.tool(input_schema={"type": "object", "properties": {}, "required": []})(body)
    return server


def _mrtr_call(server, name, capabilities, params=None):
    payload = {"name": name, "arguments": {}, "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": capabilities}}
    payload.update(params or {})
    return server._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": payload},
        session_id="S")["result"]


def test_url_elicitation_asks_by_returning():
    def open_portal(ctx=None):
        """Send the caller through an external authorization flow."""
        answer = ctx.elicit_url("Authorize", url="https://example.org/auth")
        return {"action": answer.get("action")}

    server = _mrtr_server(open_portal)
    asked = _mrtr_call(server, "open_portal", {"elicitation": {"url": True}})
    validate(BASE, "InputRequiredResult", asked)
    request = list(asked["inputRequests"].values())[0]
    assert request["method"] == "elicitation/create"
    assert request["params"]["mode"] == "url"
    # elicitationId is gone in 2026-07-28: the retry reports the outcome.
    assert "elicitationId" not in request["params"]

    done = _mrtr_call(server, "open_portal", {"elicitation": {"url": True}}, {
        "requestState": asked["requestState"],
        "inputResponses": {"input-1": {"action": "accept"}}})
    assert done["structuredContent"] == {"action": "accept"}


def test_sampling_and_roots_use_the_same_route():
    def summarize(ctx=None):
        """Ask the client to sample, exercising the MRTR sampling path."""
        reply = ctx.sample({"messages": [], "maxTokens": 10})
        return {"got": reply.get("role")}

    def where(ctx=None):
        """Ask the client for its roots."""
        return {"count": len(ctx.list_roots().get("roots", []))}

    server = _mrtr_server(summarize)
    server.tool(input_schema={"type": "object", "properties": {},
                              "required": []})(where)

    asked = _mrtr_call(server, "summarize", {"sampling": {}})
    assert list(asked["inputRequests"].values())[0]["method"] == "sampling/createMessage"
    done = _mrtr_call(server, "summarize", {"sampling": {}}, {
        "requestState": asked["requestState"],
        "inputResponses": {"input-1": {"role": "assistant", "content": {}}}})
    assert done["structuredContent"] == {"got": "assistant"}

    asked = _mrtr_call(server, "where", {"roots": {}})
    assert list(asked["inputRequests"].values())[0]["method"] == "roots/list"
    done = _mrtr_call(server, "where", {"roots": {}}, {
        "requestState": asked["requestState"],
        "inputResponses": {"input-1": {"roots": [{"uri": "file:///a"}]}}})
    assert done["structuredContent"] == {"count": 1}


def test_each_mrtr_route_is_capability_gated_independently():
    """Declaring elicitation must not unlock sampling, or vice versa."""
    def summarize(ctx=None):
        """Ask the client to sample."""
        try:
            ctx.sample({"messages": [], "maxTokens": 10})
        except RuntimeError as exc:
            return {"refused": str(exc)}
        return {"refused": None}

    server = _mrtr_server(summarize)
    refused = _mrtr_call(server, "summarize", {"elicitation": {}})
    assert refused["structuredContent"]["refused"], "sampling ran without its capability"


def test_multi_round_answers_accumulate_across_retries():
    """A later ask must not lose the answer given to an earlier one."""
    def wizard(ctx=None):
        """Ask twice, then combine both answers."""
        a = ctx.elicit("first", requested_schema={
            "type": "object", "properties": {"a": {"type": "string"}},
            "required": ["a"]})
        b = ctx.elicit("second", requested_schema={
            "type": "object", "properties": {"b": {"type": "string"}},
            "required": ["b"]})
        return {"joined": a["content"]["a"] + "-" + b["content"]["b"]}

    server = _mrtr_server(wizard)
    caps = {"elicitation": {}}
    first = _mrtr_call(server, "wizard", caps)
    assert "input-1" in first["inputRequests"]

    # The client answers only the newest ask on each retry.
    second = _mrtr_call(server, "wizard", caps, {
        "requestState": first["requestState"],
        "inputResponses": {"input-1": {"action": "accept", "content": {"a": "x"}}}})
    assert "input-2" in second["inputRequests"]

    third = _mrtr_call(server, "wizard", caps, {
        "requestState": second["requestState"],
        "inputResponses": {"input-2": {"action": "accept", "content": {"b": "y"}}}})
    assert third["structuredContent"] == {"joined": "x-y"}
    assert not server._mrtr_states, "state outlived the completed request"


# ---------------------------------------------------------------------------
# 0.4.4: resource subscriptions, pagination, mirrored headers
# ---------------------------------------------------------------------------

def _resource_server():
    server = MCPServer("res")

    @server.resource("config://a", mime_type="application/json")
    def a():
        return {"v": 1}

    @server.resource("config://b", mime_type="application/json")
    def b():
        return {"v": 2}

    server._serving = True
    probe = _SSEQueue()
    with server._clients_lock:
        server._clients["S"] = [probe]
    return server, probe


def test_resource_subscription_acknowledges_only_known_uris():
    server, probe = _resource_server()
    server._handle_request({
        "jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen",
        "params": {"notifications": {
            "resourceSubscriptions": ["config://a", "config://nope"]},
            "_meta": dict(MODERN_META)}}, session_id="S")
    ack = [json.loads(m) for m in probe if "acknowledged" in m][-1]
    assert ack["params"]["notifications"]["resourceSubscriptions"] == ["config://a"]


def test_resource_updated_reaches_only_its_subscribers():
    server, probe = _resource_server()
    server._handle_request({
        "jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen",
        "params": {"notifications": {"resourceSubscriptions": ["config://a"]},
                   "_meta": dict(MODERN_META)}}, session_id="S")
    del probe[:]

    assert server.resource_updated("config://a") == 1
    assert server.resource_updated("config://b") == 0, "notified a non-subscriber"

    pushed = [json.loads(m) for m in probe
              if json.loads(m).get("method") == "notifications/resources/updated"]
    assert len(pushed) == 1
    validate(BASE, "ResourceUpdatedNotification", pushed[0])
    assert pushed[0]["params"]["uri"] == "config://a"
    assert pushed[0]["params"]["_meta"][
        "io.modelcontextprotocol/subscriptionId"] == "sub-1"


def test_subscribe_capability_tracks_whether_resources_exist():
    server = MCPServer("caps")

    @server.tool()
    def only():
        """A server with tools but no resources."""
        return 1

    assert "resources" not in server._get_capabilities().to_dict()

    @server.resource("config://x", mime_type="application/json")
    def x():
        return {}

    assert server._get_capabilities().to_dict()["resources"]["subscribe"] is True


def test_re_registering_a_resource_notifies_subscribers():
    """Replacing a handler is an update, not merely a list change."""
    server, probe = _resource_server()
    server._handle_request({
        "jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen",
        "params": {"notifications": {"resourceSubscriptions": ["config://a"]},
                   "_meta": dict(MODERN_META)}}, session_id="S")
    del probe[:]

    @server.resource("config://a", mime_type="application/json")
    def a_again():
        return {"v": 99}

    methods = [json.loads(m).get("method") for m in probe]
    assert "notifications/resources/updated" in methods


def test_pagination_is_off_unless_a_page_size_is_set():
    server = MCPServer("nopage")

    @server.tool()
    def only():
        """The only tool."""
        return 1

    listed = modern_rpc(server, "tools/list")["result"]
    assert "nextCursor" not in listed
    validate(BASE, "ListToolsResult", listed)


def test_cursor_paging_walks_every_entry_once():
    server = MCPServer("pag", list_page_size=2)
    for i in range(5):
        fn = (lambda n: lambda: n)(i)
        fn.__name__ = "t{}".format(i)
        fn.__doc__ = "Tool {} used for pagination.".format(i)
        server.tool()(fn)

    seen, cursor, pages = [], None, 0
    while pages < 10:
        params = {"cursor": cursor} if cursor else {}
        page = modern_rpc(server, "tools/list", params)["result"]
        validate(BASE, "ListToolsResult", page)
        seen += [t["name"] for t in page["tools"]]
        cursor = page.get("nextCursor")
        pages += 1
        if not cursor:
            break

    assert seen == ["t0", "t1", "t2", "t3", "t4"]
    assert pages == 3
    assert len(set(seen)) == len(seen), "an entry was returned twice"


def test_paging_skips_nothing_when_the_registry_shrinks_mid_walk():
    """Dynamic registration means the list can change between pages.

    An offset cursor loses an entry entirely when something ahead of it is
    removed: everything shifts down and one item is never returned. The cursor
    carries the last key instead, which survives both insertion and removal —
    including removal of the keyed entry itself.
    """
    def build():
        server = MCPServer("pag", list_page_size=2)
        server._serving = True
        for letter in "abcd":
            fn = (lambda x: lambda: x)(letter)
            fn.__name__ = letter
            fn.__doc__ = "Tool {} used for pagination.".format(letter)
            server.tool()(fn)
        return server

    def walk(server, removing):
        first = modern_rpc(server, "tools/list")["result"]
        seen = [t["name"] for t in first["tools"]]
        server.remove_tool(removing)
        cursor = first.get("nextCursor")
        while cursor:
            page = modern_rpc(server, "tools/list", {"cursor": cursor})["result"]
            seen += [t["name"] for t in page["tools"]]
            cursor = page.get("nextCursor")
        return seen

    # Removing an entry behind the cursor must not shift the walk.
    server = build()
    seen = walk(server, "a")
    assert set(server._tools) <= set(seen), set(server._tools) - set(seen)

    # Removing the very entry the cursor names must still resume correctly.
    server = build()
    seen = walk(server, "b")
    assert set(server._tools) <= set(seen), set(server._tools) - set(seen)


def test_a_cursor_the_server_did_not_mint_is_invalid_params():
    server = MCPServer("pag", list_page_size=2)

    @server.tool()
    def only():
        """The only tool."""
        return 1

    # "bogus!!" fails on length once its non-alphabet characters are dropped.
    # A cursor made *only* of non-alphabet characters decodes to b"" instead
    # of raising, and used to silently restart paging at page one — so the
    # rejection has to survive that shape too, not just this one.
    for bogus in ("bogus!!", "!!!", "$$", "~~~~", "  "):
        bad = modern_rpc(server, "tools/list", {"cursor": bogus})
        assert bad["error"]["code"] == -32602, bogus

    # A cursor the server *did* mint still works.
    page = modern_rpc(server, "tools/list", {})["result"]
    assert "nextCursor" not in page  # one tool, one page


def test_mirrored_tool_parameters_are_validated():
    """x-mcp-header lets a client mirror an argument into Mcp-Param-{Name}."""
    import base64 as _b64

    server = MCPServer("hdr")

    @server.tool(input_schema={"type": "object", "properties": {
        "region": {"type": "string", "x-mcp-header": "Region"},
        "query": {"type": "string"}}, "required": ["region", "query"]})
    def execute_sql(region, query):
        """Execute SQL in a named region."""
        return {"region": region}

    base = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
            "Mcp-Name": "execute_sql"}
    body = {"name": "execute_sql",
            "arguments": {"region": "us-west1", "query": "SELECT 1"},
            "_meta": dict(MODERN_META)}

    def call(headers):
        return server._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": body},
            headers=headers)

    assert "result" in call(dict(base, **{"Mcp-Param-Region": "us-west1"}))
    # Field names are case-insensitive per RFC 9110.
    assert "result" in call(dict(base, **{"mcp-param-region": "us-west1"}))
    # The Base64 sentinel must be decoded before comparing.
    encoded = "=?base64?" + _b64.b64encode(b"us-west1").decode() + "?="
    assert "result" in call(dict(base, **{"Mcp-Param-Region": encoded}))

    assert call(dict(base, **{"Mcp-Param-Region": "us-east1"}))["error"]["code"] == -32020
    assert call(base)["error"]["code"] == -32020, "missing mirrored header accepted"


def test_mcp_name_reads_params_uri_for_resources_read():
    """Mcp-Name mirrors params.name for tools, params.uri for resources."""
    server = MCPServer("res")

    @server.resource("file:///a/b.json", mime_type="application/json")
    def cfg():
        return {"a": 1}

    def read(headers):
        return server._handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "resources/read",
            "params": {"uri": "file:///a/b.json", "_meta": dict(MODERN_META)}},
            headers=headers)

    good = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "resources/read",
            "Mcp-Name": "file:///a/b.json"}
    assert "result" in read(good)
    assert read(dict(good, **{"Mcp-Name": "file:///other"}))["error"]["code"] == -32020


def test_protocol_version_header_must_match_the_body():
    server = _server()
    response = server._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        "params": {"_meta": dict(MODERN_META)}},
        headers={"MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/list"})
    assert response["error"]["code"] == -32020
    validate(BASE, "HeaderMismatchError", response)


def test_a_server_side_value_error_is_not_blamed_on_the_caller():
    """-32602 means the caller got it wrong; a server bug must stay -32603."""
    server = MCPServer("v")

    def prepare(ctx=None):
        # A reserved _meta prefix is the tool author's mistake, not the client's.
        ctx.set_task_metadata(**{"io.modelcontextprotocol/x": 1})

    @server.async_tool(prepare=prepare)
    def work(ctx=None):
        return "done"

    server._set_session_capabilities(
        "S", {"extensions": {"io.modelcontextprotocol/tasks": {}}})
    response = server._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "work", "arguments": {}}}, session_id="S")
    assert response["error"]["code"] == -32603


def test_version_header_is_compared_against_the_declared_body_value_only():
    """A revision that has the header but no _meta must not be rejected.

    2025-06-18 introduced MCP-Protocol-Version but carries no `_meta`
    protocolVersion. Comparing the header against the server's *fallback*
    rejected such a client whenever the gateway dropped its session id — a
    known failure mode, not a rare one.
    """
    server = _server()
    legacy = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "about", "arguments": {}}}
    assert "result" in server._handle_request(legacy, headers={
        "MCP-Protocol-Version": "2025-06-18", "Mcp-Method": "tools/call",
        "Mcp-Name": "about"})

    # When the body does declare a version, a disagreeing header is still caught.
    modern = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "about", "arguments": {},
                         "_meta": dict(MODERN_META)}}
    clash = server._handle_request(modern, headers={
        "MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/call",
        "Mcp-Name": "about"})
    assert clash["error"]["code"] == -32020


def test_first_registration_of_a_resource_is_not_an_update():
    """Creation is a list change; only replacing a handler is an update."""
    server = MCPServer("res")
    server._serving = True
    probe = _SSEQueue()
    with server._clients_lock:
        server._clients["S"] = [probe]

    @server.resource("config://new", mime_type="application/json")
    def fresh():
        return {}

    methods = [json.loads(m).get("method") for m in probe]
    assert "notifications/resources/updated" not in methods


def test_mrtr_state_is_bound_to_the_session_that_created_it():
    """One session must not inherit — or destroy — another's pending answers.

    A `requestState` is presented by the client, so without an owner check a
    second session naming it inherits approvals a different user gave. And
    rebinding the owner on save would let any session permanently break an
    in-flight request just by naming its id.
    """
    server = MCPServer("mrtr")

    @server.tool(input_schema={"type": "object", "properties": {}, "required": []})
    def two_step(ctx=None):
        """Confirm twice before acting."""
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                  "required": ["ok"]}
        first = ctx.elicit("step 1", requested_schema=schema)
        second = ctx.elicit("step 2", requested_schema=schema)
        return {"both": [first["content"]["ok"], second["content"]["ok"]]}

    caps = {"elicitation": {}}

    def call(session_id, extra=None):
        params = {"name": "two_step", "arguments": {}, "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": caps}}
        params.update(extra or {})
        return server._handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
            session_id=session_id)["result"]

    first = call("A")
    mid = call("A", {"requestState": first["requestState"],
                     "inputResponses": {
                         "input-1": {"action": "accept", "content": {"ok": True}}}})
    assert "input-2" in mid["inputRequests"]

    # B presents A's in-flight state: it inherits nothing and starts over.
    stolen = call("B", {"requestState": mid["requestState"],
                        "inputResponses": {
                            "input-2": {"action": "accept", "content": {"ok": True}}}})
    assert stolen["resultType"] == "input_required"
    assert "input-1" in stolen["inputRequests"]
    assert "structuredContent" not in stolen

    # ...and A's own flow still completes.
    done = call("A", {"requestState": mid["requestState"],
                      "inputResponses": {
                          "input-2": {"action": "accept", "content": {"ok": True}}}})
    assert done["structuredContent"] == {"both": [True, True]}


def test_subscriptions_per_session_are_capped():
    """Subscription ids are the client's own request ids, so the count is
    client-controlled and has to be bounded somewhere."""
    from nanohubmcp.server import MAX_SUBSCRIPTIONS_PER_SESSION

    server = MCPServer("subs")

    @server.tool()
    def noop():
        """A tool, so the server has something to list."""
        return 1

    last = None
    for i in range(MAX_SUBSCRIPTIONS_PER_SESSION + 8):
        last = server._handle_request({
            "jsonrpc": "2.0", "id": "c{}".format(i),
            "method": "subscriptions/listen",
            "params": {"notifications": {"toolsListChanged": True},
                       "_meta": dict(MODERN_META)}}, session_id="S")

    assert len(server._subscriptions["S"]) == MAX_SUBSCRIPTIONS_PER_SESSION
    assert last["error"]["code"] == -32602

    # Re-using an existing id refreshes it rather than counting again.
    again = server._handle_request({
        "jsonrpc": "2.0", "id": "c0", "method": "subscriptions/listen",
        "params": {"notifications": {"toolsListChanged": True},
                   "_meta": dict(MODERN_META)}}, session_id="S")
    assert again is None
