"""Error and result branches of the JSON-RPC methods.

These are the paths a malformed or unlucky client takes. They rarely run in a
happy-path test, and each one is a response shape a client has to understand,
so they are pinned directly.
"""

from __future__ import print_function

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanohubmcp import MCPServer, PromptResult, Message  # noqa: E402

TASKS = {"extensions": {"io.modelcontextprotocol/tasks": {}}}


def _task_server():
    server = MCPServer("tasks")

    @server.async_tool()
    def work(ctx=None):
        return "done"

    server._set_session_capabilities("S", TASKS)
    return server


def _rpc(server, method, params, session_id="S"):
    return server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        session_id=session_id)


# ---------------------------------------------------------------------------
# tasks/* argument validation
# ---------------------------------------------------------------------------

def test_task_methods_require_a_task_id():
    server = _task_server()
    for method in ("tasks/get", "tasks/cancel", "tasks/update"):
        for params in ({}, {"taskId": ""}, {"taskId": 42}):
            error = _rpc(server, method, params)["error"]
            assert error["code"] == -32602, (method, params)


def test_task_methods_require_the_tasks_capability():
    """Without the extension declared, the task methods are not available."""
    server = _task_server()
    server._set_session_capabilities("Other", {})
    for method in ("tasks/get", "tasks/cancel", "tasks/update"):
        error = _rpc(server, method, {"taskId": "whatever"},
                     session_id="Other")["error"]
        assert error["code"] == -32003
        assert "requiredCapabilities" in error["data"]


def test_tasks_update_requires_input_responses():
    server = _task_server()
    task_id = _rpc(server, "tools/call",
                   {"name": "work", "arguments": {}})["result"]["taskId"]
    for params in ({"taskId": task_id},
                   {"taskId": task_id, "inputResponses": {}},
                   {"taskId": task_id, "inputResponses": "no"}):
        error = _rpc(server, "tasks/update", params)["error"]
        assert error["code"] == -32602
        assert "inputResponses" in error["message"]


def test_a_task_from_another_session_is_not_reachable():
    server = _task_server()
    task_id = _rpc(server, "tools/call",
                   {"name": "work", "arguments": {}})["result"]["taskId"]
    server._set_session_capabilities("Intruder", TASKS)
    error = _rpc(server, "tasks/get", {"taskId": task_id},
                 session_id="Intruder")["error"]
    assert error["code"] == -32003
    assert "not available in this session" in error["message"]


def test_an_unknown_task_id_is_invalid_params():
    server = _task_server()
    error = _rpc(server, "tasks/get", {"taskId": "no-such-task"})["error"]
    assert error["code"] == -32602
    assert "Unknown taskId" in error["message"]


# ---------------------------------------------------------------------------
# prompts/get result shapes
# ---------------------------------------------------------------------------

def _prompt_server():
    server = MCPServer("prompts")

    @server.prompt()
    def structured():
        """Return a PromptResult."""
        return PromptResult(messages=[Message("hello")], description="d")

    @server.prompt()
    def as_list():
        """Return a bare list of message dicts."""
        return [{"role": "user", "content": {"type": "text", "text": "hi"}}]

    @server.prompt()
    def as_text():
        """Return a plain string."""
        return "just text"

    @server.prompt()
    def broken():
        """Raise, to exercise the error path."""
        raise RuntimeError("prompt exploded")

    return server


def test_prompt_results_are_normalised_to_messages():
    server = _prompt_server()

    structured = _rpc(server, "prompts/get", {"name": "structured"})["result"]
    assert structured["messages"][0]["content"]["text"] == "hello"
    assert structured["description"] == "d"

    listed = _rpc(server, "prompts/get", {"name": "as_list"})["result"]
    assert listed["messages"][0]["content"]["text"] == "hi"

    text = _rpc(server, "prompts/get", {"name": "as_text"})["result"]
    assert text["messages"][0]["content"]["text"] == "just text"
    assert text["messages"][0]["role"] == "user"


def test_a_raising_prompt_is_an_internal_error():
    error = _rpc(_prompt_server(), "prompts/get", {"name": "broken"})["error"]
    assert error["code"] == -32603
    assert "prompt exploded" in error["message"]


def test_an_unknown_prompt_is_reported():
    assert "error" in _rpc(_prompt_server(), "prompts/get", {"name": "nope"})


# ---------------------------------------------------------------------------
# Version-gated methods
# ---------------------------------------------------------------------------

def test_logging_set_level_still_serves_handshake_era_clients():
    server = MCPServer("log")

    @server.tool()
    def noop():
        """A tool, so the server has something to list."""
        return 1

    assert _rpc(server, "logging/setLevel", {"level": "info"})["result"] == {}

    modern = server._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "logging/setLevel",
        "params": {"level": "info", "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {}}}})
    assert modern["error"]["code"] == -32601


def test_unknown_methods_and_malformed_envelopes():
    server = MCPServer("edge")

    assert _rpc(server, "no/such/method", {})["error"]["code"] == -32601
    # A notification never gets a reply, even a malformed one.
    assert server._handle_request(
        {"jsonrpc": "2.0", "method": "no/such/method", "params": {}}) is None
    assert server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "", "params": {}}
    )["error"]["code"] == -32600
    assert server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []}
    )["error"]["code"] == -32602
    assert server._handle_request("not an object")["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# x-mcp-header annotation validation
# ---------------------------------------------------------------------------

def _try_register(schema):
    """Register a tool with this input schema; return None or the error text."""
    import pytest  # noqa: F401  (imported here to keep the helper local)

    server = MCPServer("x")
    try:
        @server.tool(input_schema=schema)
        def t(**kwargs):
            """A tool."""
            return {}
        return None
    except ValueError as exc:
        return str(exc)


def _props(**properties):
    return {"type": "object", "properties": properties, "required": []}


def test_valid_mirror_annotations_register():
    assert _try_register(_props(
        region={"type": "string", "x-mcp-header": "Region"})) is None
    # Nested is fine as long as every step is a `properties` key.
    assert _try_register(_props(cfg={
        "type": "object",
        "properties": {"r": {"type": "string", "x-mcp-header": "R"}}})) is None


def test_mirror_annotation_must_be_a_mirrorable_primitive():
    """`number` is excluded: a float has no canonical header representation."""
    error = _try_register(_props(ratio={"type": "number", "x-mcp-header": "Ratio"}))
    assert error and "string, integer, boolean" in error


def test_mirror_annotation_must_be_a_valid_field_name():
    assert "field-name token" in _try_register(
        _props(a={"type": "string", "x-mcp-header": "Bad Name"}))
    assert "non-empty" in _try_register(
        _props(a={"type": "string", "x-mcp-header": ""}))
    # A newline in a header name is header injection, not just invalid.
    assert "field-name token" in _try_register(
        _props(a={"type": "string", "x-mcp-header": "A\r\nX-Evil: 1"}))


def test_mirror_annotation_names_are_case_insensitively_unique():
    error = _try_register(_props(
        one={"type": "string", "x-mcp-header": "Dup"},
        two={"type": "string", "x-mcp-header": "dup"}))
    assert error and "duplicates" in error


def test_mirror_annotation_must_be_statically_reachable():
    """Behind items, oneOf, or a $ref it is invalid — and invalidates the tool."""
    for schema in (
        _props(xs={"type": "array",
                   "items": {"type": "string", "x-mcp-header": "X"}}),
        _props(a={"oneOf": [{"type": "string", "x-mcp-header": "Y"}]}),
        _props(a={"if": {"type": "string", "x-mcp-header": "Z"}}),
    ):
        error = _try_register(schema)
        assert error and "statically reachable" in error, schema


def test_an_invalid_annotation_keeps_the_tool_out_of_the_registry():
    """A conforming client would drop such a tool silently; fail loudly instead."""
    server = MCPServer("x")
    try:
        @server.tool(input_schema=_props(
            r={"type": "number", "x-mcp-header": "R"}))
        def broken(r=None):
            """A tool a conforming client would refuse to list."""
            return {}
    except ValueError:
        pass
    assert "broken" not in server._tools


# ---------------------------------------------------------------------------
# Origin validation (DNS rebinding)
# ---------------------------------------------------------------------------

def test_origin_policy_is_opt_in_and_ignores_non_browser_clients():
    server = MCPServer("o")

    # No allowlist configured: a library cannot guess valid origins.
    assert server.origin_allowed("https://evil.example") is True

    server._allowed_origins = {"https://claude.ai"}
    assert server.origin_allowed("https://claude.ai") is True
    assert server.origin_allowed("https://claude.ai/") is True   # trailing slash
    assert server.origin_allowed("HTTPS://Claude.AI") is True    # case
    assert server.origin_allowed("https://evil.example") is False
    # A request with no Origin is not a browser and is not policed.
    assert server.origin_allowed(None) is True
    assert server.origin_allowed("") is True


# ---------------------------------------------------------------------------
# Malformed params must be the caller's fault, not the server's
# ---------------------------------------------------------------------------

def test_non_string_uri_and_name_are_invalid_params_not_internal_errors():
    """A dict `uri` reached a dict lookup and raised TypeError.

    That surfaced as -32603 with a stack trace in the log: the server blamed
    for the caller's malformed request, and a traceback written per bad
    request.
    """
    server = MCPServer("fuzz")

    @server.resource("config://a", mime_type="application/json")
    def res():
        return {"v": 1}

    @server.prompt()
    def hello():
        """A prompt."""
        return "hi"

    for params in ({"uri": {}}, {"uri": []}, {"uri": 5}, {"uri": ""}, {}):
        assert _rpc(server, "resources/read", params)["error"]["code"] == -32602, params
    assert "result" in _rpc(server, "resources/read", {"uri": "config://a"})

    for params in ({"name": {}}, {"name": 5}, {"name": ""}, {}):
        assert _rpc(server, "prompts/get", params)["error"]["code"] == -32602, params
    assert "result" in _rpc(server, "prompts/get", {"name": "hello"})


def test_malformed_subscription_filters_are_rejected_or_ignored_safely():
    server = MCPServer("subs")

    @server.tool()
    def noop():
        """A tool."""
        return 1

    @server.resource("config://a", mime_type="application/json")
    def res():
        return {}

    def listen(notifications, msg_id="s"):
        return server._handle_request({
            "jsonrpc": "2.0", "id": msg_id, "method": "subscriptions/listen",
            "params": {"notifications": notifications}}, session_id="S")

    for bad in ("x", 5, [], None):
        assert listen(bad)["error"]["code"] == -32602, bad
    for bad_ids in ("a", 5, [1, None], [{}]):
        assert listen({"taskIds": bad_ids})["error"]["code"] == -32602, bad_ids

    # Junk resource URIs are dropped rather than rejected: the ack reports
    # exactly what is being watched, which is honest and keeps the set bounded.
    assert listen({"resourceSubscriptions": [1, {}, None, "config://nope"]},
                  msg_id="s2") is None
    assert server._subscriptions["S"]["s2"]["resource_uris"] == set()
