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

    return server


def test_server_discover_matches_schema():
    validate(BASE, "DiscoverResult", modern_rpc(_server(), "server/discover")["result"])


def test_list_results_are_cacheable():
    server = _server()
    validate(BASE, "ListToolsResult", modern_rpc(server, "tools/list")["result"])
    validate(BASE, "ListResourcesResult", modern_rpc(server, "resources/list")["result"])
    validate(BASE, "ListPromptsResult", modern_rpc(server, "prompts/list")["result"])


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
