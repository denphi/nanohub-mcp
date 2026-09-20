"""Behaviours the specification requires that a schema check cannot see.

`test_schema_conformance.py` proves every response has the right *shape*.
These prove the right *thing happens*: that an advertised capability is
backed by a working method, that an error carries the code the spec names,
and that a session can be ended.
"""

from __future__ import print_function

import json
import os
import sys
import time

import pytest

from typing import Any, Union  # noqa: F401  (used by a type comment below)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanohubmcp import MCPServer, ToolResult  # noqa: E402
from nanohubmcp.server import (  # noqa: E402
    LOG_LEVELS,
    SESSION_IDLE_TIMEOUT_SECONDS,
    _SSEQueue,
)

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}


def rpc(server, method, params=None, session_id="S", msg_id=1):
    return server._handle_request(
        {"jsonrpc": "2.0", "id": msg_id, "method": method,
         "params": params or {}},
        session_id=session_id)


def server_with(session_version="2025-06-18", session_id="S"):
    server = MCPServer("spec")

    @server.tool()
    def echo(value):
        """Echo a value"""
        return value

    @server.resource("config://settings")
    def settings():
        """Settings"""
        return {"theme": "dark"}

    @server.prompt()
    def greet(name):
        """Greet"""
        return "Hello {}".format(name)

    if session_version:
        rpc(server, "initialize",
            {"protocolVersion": session_version, "capabilities": {}}, session_id)
    return server


# ---------------------------------------------------------------------------
# Error codes the spec names by number
# ---------------------------------------------------------------------------

def test_unknown_tool_is_invalid_params_not_method_not_found():
    """The spec's own example: `Unknown tool: ...` carries -32602.

    -32601 means the *method* does not exist, and `tools/call` does.
    """
    error = rpc(server_with(), "tools/call",
                {"name": "nope", "arguments": {}})["error"]
    assert error["code"] == -32602


def test_unknown_prompt_is_invalid_params():
    error = rpc(server_with(), "prompts/get", {"name": "nope"})["error"]
    assert error["code"] == -32602


@pytest.mark.parametrize("version,code", [
    ("2024-11-05", -32002),
    ("2025-06-18", -32002),
    ("2025-11-25", -32002),
])
def test_missing_resource_uses_the_code_that_revision_defines(version, code):
    """-32002 through 2025-11-25; 2026-07-28 renumbered it onto -32602."""
    server = server_with(version)
    error = rpc(server, "resources/read", {"uri": "config://nope"})["error"]
    assert error["code"] == code
    assert error["data"]["uri"] == "config://nope"


def test_missing_resource_is_invalid_params_under_2026_07_28():
    server = server_with(None)
    error = rpc(server, "resources/read",
                {"uri": "config://nope", "_meta": dict(MODERN)})["error"]
    assert error["code"] == -32602


# ---------------------------------------------------------------------------
# resources.subscribe must be backed by the methods it promises
# ---------------------------------------------------------------------------

def test_advertised_subscribe_capability_has_a_working_method():
    """Advertising `subscribe: true` obliges `resources/subscribe` to exist.

    It was advertised while the method returned -32601, so every
    conformant client that acted on the capability failed.
    """
    server = server_with()
    caps = rpc(server, "initialize",
               {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert caps["capabilities"]["resources"]["subscribe"] is True

    assert rpc(server, "resources/subscribe",
               {"uri": "config://settings"})["result"] == {}


def test_subscribed_client_receives_resource_updated():
    server = server_with()
    queue = _SSEQueue()
    server._register_client("S", queue)

    rpc(server, "resources/subscribe", {"uri": "config://settings"})
    assert server.resource_updated("config://settings") == 1

    import json
    sent = [json.loads(m) for m in queue]
    assert [m["method"] for m in sent] == ["notifications/resources/updated"]
    assert sent[0]["params"]["uri"] == "config://settings"
    # Handshake revisions have no subscription id to correlate against.
    assert "_meta" not in sent[0]["params"]


def test_unsubscribe_stops_the_notifications():
    server = server_with()
    queue = _SSEQueue()
    server._register_client("S", queue)

    rpc(server, "resources/subscribe", {"uri": "config://settings"})
    assert rpc(server, "resources/unsubscribe",
               {"uri": "config://settings"})["result"] == {}
    queue[:] = []
    assert server.resource_updated("config://settings") == 0
    assert list(queue) == []


def test_subscribe_to_unknown_resource_is_not_found():
    error = rpc(server_with(), "resources/subscribe",
                {"uri": "config://nope"})["error"]
    assert error["code"] == -32002


def test_subscribe_is_gone_under_2026_07_28():
    """2026-07-28 replaced both methods with `subscriptions/listen`."""
    error = rpc(server_with(None), "resources/subscribe",
                {"uri": "config://settings", "_meta": dict(MODERN)})["error"]
    assert error["code"] == -32601
    assert "subscriptions/listen" in error["message"]


def test_removing_a_resource_drops_its_subscriptions():
    server = server_with()
    server._register_client("S", _SSEQueue())
    rpc(server, "resources/subscribe", {"uri": "config://settings"})

    assert server.remove_resource("config://settings") is True
    assert server.resource_updated("config://settings") == 0


# ---------------------------------------------------------------------------
# logging: setLevel must actually set a level
# ---------------------------------------------------------------------------

def test_set_level_is_honoured_and_notifications_arrive():
    """`logging` was advertised while setLevel discarded its argument.

    The capability, the accepted request and the silence that followed
    were each individually plausible; together they meant a client could
    never receive a log line.
    """
    import json

    server = MCPServer("logs")

    @server.tool()
    def noisy(ctx):
        """Log one line"""
        ctx.warning("careful")
        return "done"

    rpc(server, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}})
    queue = _SSEQueue()
    server._register_client("S", queue)

    assert rpc(server, "logging/setLevel", {"level": "debug"})["result"] == {}
    queue[:] = []
    rpc(server, "tools/call", {"name": "noisy", "arguments": {}})

    sent = [json.loads(m) for m in queue]
    assert [m["method"] for m in sent] == ["notifications/message"]
    assert sent[0]["params"]["level"] == "warning"


def test_set_level_filters_below_the_threshold():
    import json

    server = MCPServer("logs")

    @server.tool()
    def quiet(ctx):
        """Log below the threshold"""
        ctx.debug("noise")
        return "done"

    rpc(server, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}})
    queue = _SSEQueue()
    server._register_client("S", queue)
    rpc(server, "logging/setLevel", {"level": "error"})
    queue[:] = []
    rpc(server, "tools/call", {"name": "quiet", "arguments": {}})
    assert [json.loads(m) for m in queue] == []


@pytest.mark.parametrize("level", ["chatty", "", None, 3, "DEBUG"])
def test_invalid_log_level_is_rejected(level):
    """"Invalid log level: -32602"."""
    error = rpc(server_with(), "logging/setLevel", {"level": level})["error"]
    assert error["code"] == -32602


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_every_rfc5424_level_is_accepted(level):
    assert rpc(server_with(), "logging/setLevel",
               {"level": level})["result"] == {}


def test_no_log_notification_without_a_level_under_2026_07_28():
    """"servers MUST NOT emit notifications/message for requests that did
    not include this field"."""
    import json

    server = MCPServer("logs")

    @server.tool()
    def noisy(ctx):
        """Log one line"""
        ctx.error("boom")
        return "done"

    queue = _SSEQueue()
    server._register_client("S", queue)
    # A level set earlier by a handshake client must not leak into a
    # stateless request that carried none.
    rpc(server, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}})
    rpc(server, "logging/setLevel", {"level": "debug"})
    queue[:] = []

    rpc(server, "tools/call",
        {"name": "noisy", "arguments": {}, "_meta": dict(MODERN)})
    assert [json.loads(m) for m in queue] == []


# ---------------------------------------------------------------------------
# outputSchema is a promise the server has to keep
# ---------------------------------------------------------------------------

def _schema_server():
    server = MCPServer("schemas")

    @server.tool(output_schema={"type": "object", "required": ["temp"],
                                "properties": {"temp": {"type": "number"}}})
    def good():
        """Conforms"""
        return {"temp": 21.5}

    @server.tool(output_schema={"type": "object", "required": ["temp"],
                                "properties": {"temp": {"type": "number"}}})
    def wrong_type():
        """Returns a string"""
        return "sunny"

    @server.tool(output_schema={"type": "object", "required": ["temp"],
                                "properties": {"temp": {"type": "number"}}})
    def missing_field():
        """Omits a required property"""
        return {"humidity": 65}

    @server.tool(output_schema={"type": "object",
                                "properties": {"temp": {"type": "number"}}})
    def wrong_property_type():
        """Right key, wrong type"""
        return {"temp": "warm"}

    rpc(server, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}})
    return server


def test_conforming_structured_result_passes_through():
    result = rpc(_schema_server(), "tools/call",
                 {"name": "good", "arguments": {}})["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"temp": 21.5}
    # "a tool that returns structured content SHOULD also return the
    # serialized JSON in a TextContent block".
    assert result["content"][0]["type"] == "text"


@pytest.mark.parametrize("tool", ["wrong_type", "missing_field",
                                  "wrong_property_type"])
def test_result_violating_output_schema_is_reported_not_shipped(tool):
    """"Servers MUST provide structured results that conform to this schema."

    The server cannot invent a conforming result, so it reports the tool's
    broken contract instead of publishing a payload that contradicts what
    `tools/list` advertised.
    """
    result = rpc(_schema_server(), "tools/call",
                 {"name": tool, "arguments": {}})["result"]
    assert result["isError"] is True
    assert "outputSchema" in result["content"][0]["text"]


def test_tool_without_output_schema_is_unconstrained():
    server = MCPServer("free")

    @server.tool()
    def anything():
        """No schema declared"""
        return "whatever"

    result = rpc(server, "tools/call",
                 {"name": "anything", "arguments": {}})["result"]
    assert result["isError"] is False
    assert "structuredContent" not in result


def test_tool_result_meta_reaches_the_wire():
    """`ToolResult(meta=...)` was accepted and then dropped by to_dict()."""
    server = MCPServer("meta")

    @server.tool()
    def tagged():
        """Carries metadata"""
        return ToolResult(content="ok", meta={"trace": "abc"})

    result = rpc(server, "tools/call",
                 {"name": "tagged", "arguments": {}})["result"]
    assert result["_meta"] == {"trace": "abc"}


# ---------------------------------------------------------------------------
# URI templates
# ---------------------------------------------------------------------------

def _template_server():
    server = MCPServer("templates")

    @server.resource("weather://{city}/current", mime_type="application/json")
    def weather(city):
        """Weather by city"""
        return {"city": city}

    @server.resource("config://settings")
    def settings():
        """Settings"""
        return {"theme": "dark"}

    rpc(server, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}})
    return server


def test_template_is_listed_as_a_template_not_a_resource():
    server = _template_server()
    resources = rpc(server, "resources/list")["result"]["resources"]
    templates = rpc(server, "resources/templates/list")["result"]["resourceTemplates"]

    assert [r["uri"] for r in resources] == ["config://settings"]
    assert [t["uriTemplate"] for t in templates] == ["weather://{city}/current"]


def test_reading_an_instantiated_template_passes_the_variables():
    result = rpc(_template_server(), "resources/read",
                 {"uri": "weather://paris/current"})["result"]
    assert result["contents"][0]["uri"] == "weather://paris/current"
    assert "paris" in result["contents"][0]["text"]


def test_template_placeholder_does_not_span_path_segments():
    """`{city}` matches one segment, so it cannot swallow a whole path."""
    error = rpc(_template_server(), "resources/read",
                {"uri": "weather://a/b/current"})["error"]
    assert error["code"] == -32002


def test_unsupported_rfc6570_operator_is_refused_at_registration():
    server = MCPServer("templates")
    with pytest.raises(ValueError) as caught:
        @server.resource("files://{+path}")
        def files(path):
            """Reserved expansion, which this does not implement"""
            return path
    assert "RFC 6570" in str(caught.value)


def test_repeated_template_variable_is_refused():
    server = MCPServer("templates")
    with pytest.raises(ValueError):
        @server.resource("pair://{x}/{x}")
        def pair(x):
            """Same name twice"""
            return x


# ---------------------------------------------------------------------------
# JSON-RPC batching was removed in 2025-06-18
# ---------------------------------------------------------------------------

def test_batch_is_accepted_for_a_2024_11_05_session():
    server = server_with("2024-11-05")
    responses = server._handle_jsonrpc_payload([
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ], session_id="S")
    assert [r["id"] for r in responses] == [1, 2]


@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25"])
def test_batch_is_refused_from_2025_06_18(version):
    """"The body of the POST request MUST be a single JSON-RPC request"."""
    server = server_with(version)
    response = server._handle_jsonrpc_payload(
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"}], session_id="S")
    assert response["error"]["code"] == -32600
    assert "batching" in response["error"]["message"]


def test_batch_is_refused_without_a_session():
    server = server_with(None)
    response = server._handle_jsonrpc_payload(
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"}], session_id=None)
    assert response["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def test_terminate_session_forgets_its_state():
    server = server_with()
    rpc(server, "resources/subscribe", {"uri": "config://settings"})
    assert server.session_exists("S") is True

    assert server.terminate_session("S") is True
    assert server.session_exists("S") is False
    assert server.terminate_session("S") is False
    assert server.resource_updated("config://settings") == 0


def test_idle_post_only_session_is_collected():
    """A client that never opens a stream never disconnects one.

    Its negotiated state used to live in `_sessions` for the life of the
    process, because only an SSE disconnect ever cleaned one up.
    """
    server = server_with()
    assert server.session_exists("S") is True

    server._sessions["S"]["last_seen"] -= SESSION_IDLE_TIMEOUT_SECONDS + 1
    server._prune_expired_sessions()
    assert server.session_exists("S") is False


def test_session_with_an_open_stream_is_never_collected():
    server = server_with()
    server._register_client("S", _SSEQueue())
    server._sessions["S"]["last_seen"] -= SESSION_IDLE_TIMEOUT_SECONDS * 10

    server._prune_expired_sessions()
    assert server.session_exists("S") is True


def test_activity_defers_expiry():
    server = server_with()
    server._sessions["S"]["last_seen"] -= SESSION_IDLE_TIMEOUT_SECONDS + 1
    # Any request refreshes the stamp before the sweep can reach it.
    rpc(server, "tools/list")
    server._prune_expired_sessions()
    assert server.session_exists("S") is True


# ---------------------------------------------------------------------------
# notifications/initialized
# ---------------------------------------------------------------------------

def test_notifications_initialized_is_routed():
    """The name every revision through 2025-11-25 actually sends."""
    server = server_with()
    assert "notifications/initialized" in server._RPC_METHODS
    assert server._handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        session_id="S") is None


# ---------------------------------------------------------------------------
# Proxy-prefix normalization must not reach past a path boundary
# ---------------------------------------------------------------------------

def test_proxy_prefix_match_requires_a_path_boundary():
    server = server_with()
    assert server._strip_proxy_prefix(
        "/weber/proxy/config://settings") == "config://settings"
    # A bare suffix match resolved anything ending in the URI.
    assert server._strip_proxy_prefix(
        "https://elsewhereconfig://settings") == "https://elsewhereconfig://settings"


# ---------------------------------------------------------------------------
# Tool and prompt inputs are validated against what was published
# ---------------------------------------------------------------------------

def _typed_server():
    server = MCPServer("typed")

    @server.tool()
    def add(a, b):
        """Add two integers"""
        return a + b

    @server.tool()
    def typed(a, b=None):
        """Typed signature"""
        return "{}/{}".format(a, b)

    rpc(server, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}})
    return server


def test_missing_required_argument_is_a_protocol_error():
    """"Servers MUST validate all tool inputs."

    A call that never reached the handler is not a tool failure. Reporting
    it as `isError` told the model the tool ran, and the message leaked the
    handler's Python signature.
    """
    server = MCPServer("typed")

    @server.tool()
    def add(a: int, b: int):
        """Add two integers"""
        return a + b

    response = rpc(server, "tools/call", {"name": "add", "arguments": {"a": 1}})
    assert "result" not in response
    assert response["error"]["code"] == -32602
    assert "b" in response["error"]["message"]
    assert "positional argument" not in response["error"]["message"]


def test_wrongly_typed_argument_is_a_protocol_error():
    server = MCPServer("typed")

    @server.tool()
    def add(a: int, b: int):
        """Add two integers"""
        return a + b

    error = rpc(server, "tools/call",
                {"name": "add", "arguments": {"a": "x", "b": 2}})["error"]
    assert error["code"] == -32602


def test_valid_arguments_still_reach_the_handler():
    server = MCPServer("typed")

    @server.tool()
    def add(a: int, b: int):
        """Add two integers"""
        return a + b

    result = rpc(server, "tools/call",
                 {"name": "add", "arguments": {"a": 1, "b": 2}})["result"]
    assert result["content"][0]["text"] == "3"


def test_null_is_accepted_for_an_optional_parameter():
    """The published schema admits null, so the server must too."""
    from typing import Optional

    server = MCPServer("typed")

    @server.tool()
    def maybe(a: int, b: Optional[str] = None):
        """Optional second argument"""
        return "{}/{}".format(a, b)

    tool = rpc(server, "tools/list")["result"]["tools"][0]
    assert tool["inputSchema"]["properties"]["b"]["type"] == ["string", "null"]

    result = rpc(server, "tools/call",
                 {"name": "maybe", "arguments": {"a": 1, "b": None}})["result"]
    assert result["isError"] is False


def test_unannotated_parameter_is_unconstrained_not_a_string():
    """An unannotated parameter used to be published as `"type": "string"`.

    That is a guess stated as fact: a client reading the schema would not
    send a number, and with inputs now validated it could not.
    """
    server = _typed_server()
    schema = [t for t in rpc(server, "tools/list")["result"]["tools"]
              if t["name"] == "add"][0]["inputSchema"]
    assert schema["properties"]["a"] == {}

    result = rpc(server, "tools/call",
                 {"name": "add", "arguments": {"a": 1, "b": 2}})["result"]
    assert result["content"][0]["text"] == "3"


def test_tool_failure_is_still_reported_as_a_tool_error():
    """Validation must not turn genuine tool failures into protocol errors."""
    server = MCPServer("failing")

    @server.tool()
    def boom(a: int):
        """Raises"""
        raise RuntimeError("upstream is down")

    result = rpc(server, "tools/call",
                 {"name": "boom", "arguments": {"a": 1}})["result"]
    assert result["isError"] is True
    assert "upstream is down" in result["content"][0]["text"]


def test_missing_required_prompt_argument_is_invalid_params():
    """It was -32603, which blames the server for the caller's omission."""
    error = rpc(server_with(), "prompts/get",
                {"name": "greet", "arguments": {}})["error"]
    assert error["code"] == -32602
    assert "name" in error["message"]


def test_prompt_with_its_arguments_still_works():
    result = rpc(server_with(), "prompts/get",
                 {"name": "greet", "arguments": {"name": "ada"}})["result"]
    assert "ada" in result["messages"][0]["content"]["text"]


@pytest.mark.parametrize("name", [{"a": 1}, ["x"], 7, None])
def test_unhashable_or_wrong_typed_names_are_invalid_params(name):
    """`name` is caller-supplied JSON; an unhashable one must not crash."""
    server = server_with()
    for method in ("tools/call", "prompts/get"):
        response = rpc(server, method, {"name": name, "arguments": {}})
        assert response["error"]["code"] == -32602, (method, response)


# ---------------------------------------------------------------------------
# Optional spec fields must be reachable from the public API
# ---------------------------------------------------------------------------

def test_title_is_settable_on_tools_prompts_and_resources():
    """A field the types accept but no decorator passes is a field nobody has.

    `title` is the spec's human-readable display name, distinct from `name`,
    which is the programmatic identifier.
    """
    server = MCPServer("titles")

    @server.tool(title="Add Numbers")
    def add(a: int, b: int):
        """Add"""
        return a + b

    @server.prompt(title="Greeting")
    def greet(name: str):
        """Greet"""
        return "hi"

    @server.resource("config://x", title="Configuration")
    def config():
        """Config"""
        return {}

    @server.resource("t://{v}/x", title="Templated")
    def templated(v):
        """Templated"""
        return {}

    rpc(server, "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}})

    assert rpc(server, "tools/list")["result"]["tools"][0]["title"] == "Add Numbers"
    assert rpc(server, "prompts/list")["result"]["prompts"][0]["title"] == "Greeting"
    assert rpc(server, "resources/list")["result"]["resources"][0][
        "title"] == "Configuration"
    assert rpc(server, "resources/templates/list")["result"][
        "resourceTemplates"][0]["title"] == "Templated"


def test_async_tool_carries_title_and_output_schema():
    server = MCPServer("titles")

    @server.async_tool(title="Slow Job",
                       output_schema={"type": "object",
                                      "properties": {"ok": {"type": "boolean"}}})
    def slow():
        """Slow"""
        return {"ok": True}

    tool = [t for t in rpc(server, "tools/list")["result"]["tools"]
            if t["name"] == "slow"][0]
    assert tool["title"] == "Slow Job"
    assert tool["outputSchema"]["properties"]["ok"] == {"type": "boolean"}


def test_resource_size_reaches_the_wire():
    """`size` was serialized by `Resource.to_dict` and settable by nobody."""
    server = MCPServer("sized")

    @server.resource("config://x", size=42)
    def config():
        """Config"""
        return {}

    assert rpc(server, "resources/list")["result"]["resources"][0]["size"] == 42


def test_resource_annotations_reach_the_wire():
    server = MCPServer("annotated")

    @server.resource("config://x",
                     annotations={"audience": ["user"], "priority": 0.8})
    def config():
        """Config"""
        return {}

    resource = rpc(server, "resources/list")["result"]["resources"][0]
    assert resource["annotations"] == {"audience": ["user"], "priority": 0.8}


# ---------------------------------------------------------------------------
# Regressions found reviewing the conformance work itself
# ---------------------------------------------------------------------------

def test_var_keyword_tool_stays_callable():
    """`**kwargs` is not a named argument and must not be a required property.

    The generated schema listed it as one (it has no default), which was
    inert until tools/call began validating against the schema — at which
    point every call was rejected and no argument name could satisfy it.
    """
    server = MCPServer("varkw")

    @server.tool()
    def kw(a, **extra):
        """Takes arbitrary extra keywords"""
        return {"a": a, "extra": extra}

    schema = rpc(server, "tools/list")["result"]["tools"][0]["inputSchema"]
    assert "extra" not in schema["properties"]
    assert schema["required"] == ["a"]

    result = rpc(server, "tools/call",
                 {"name": "kw", "arguments": {"a": 1, "b": 2}})["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"a": 1, "extra": {"b": 2}}


def test_var_positional_tool_stays_callable():
    server = MCPServer("varpos")

    @server.tool()
    def va(*items):
        """Takes varargs"""
        return {"count": len(items)}

    schema = rpc(server, "tools/list")["result"]["tools"][0]["inputSchema"]
    assert schema["properties"] == {}
    assert schema["required"] == []
    assert rpc(server, "tools/call",
               {"name": "va", "arguments": {}})["result"]["isError"] is False


def _array_output_server():
    server = MCPServer("arrays")

    @server.tool(output_schema={"type": "array", "items": {"type": "integer"}})
    def listy():
        """Returns a JSON array"""
        return [1, 2, 3]

    @server.tool(output_schema={"type": "array", "items": {"type": "integer"}})
    def bad():
        """Violates its own array schema"""
        return ["x"]

    return server


@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18", "2025-11-25"])
def test_non_object_structured_content_is_withheld_pre_2026(version):
    """Through 2025-11-25 `structuredContent` is typed as an object.

    Emitting an array produced a CallToolResult that the revision's own
    schema rejects. The data still travels, as the serialized-JSON text
    block the spec asks for.
    """
    server = _array_output_server()
    session = "S-arr-{}".format(version)
    rpc(server, "initialize",
        {"protocolVersion": version, "capabilities": {}}, session)

    result = rpc(server, "tools/call",
                 {"name": "listy", "arguments": {}}, session)["result"]
    assert "structuredContent" not in result
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == [1, 2, 3]


def test_non_object_structured_content_is_sent_under_2026_07_28():
    """SEP-2106 loosened the field to any JSON value."""
    server = _array_output_server()
    result = rpc(server, "tools/call",
                 {"name": "listy", "arguments": {},
                  "_meta": dict(MODERN)}, "S-arr-modern")["result"]
    assert result["structuredContent"] == [1, 2, 3]


@pytest.mark.parametrize("version", ["2025-11-25", None])
def test_output_schema_still_enforced_when_content_is_withheld(version):
    """Withholding the field must not let a tool escape its own contract.

    Validation reads the handler's value, not the shaped result, so the
    breach is caught on a revision that could not have carried it either.
    """
    server = _array_output_server()
    params = {"name": "bad", "arguments": {}}
    session = "S-bad"
    if version:
        rpc(server, "initialize",
            {"protocolVersion": version, "capabilities": {}}, session)
    else:
        params["_meta"] = dict(MODERN)

    result = rpc(server, "tools/call", params, session)["result"]
    assert result["isError"] is True
    assert "outputSchema" in result["content"][0]["text"]


def test_tool_result_structured_content_is_also_fitted():
    """The same rule applies to a hand-built ToolResult."""
    server = MCPServer("arrays")

    @server.tool()
    def listy():
        """Array via ToolResult"""
        return ToolResult(content="[1, 2]", structured_content=[1, 2])

    rpc(server, "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}}, "S-tr")
    legacy = rpc(server, "tools/call",
                 {"name": "listy", "arguments": {}}, "S-tr")["result"]
    assert "structuredContent" not in legacy

    modern = rpc(server, "tools/call",
                 {"name": "listy", "arguments": {}, "_meta": dict(MODERN)},
                 "S-tr2")["result"]
    assert modern["structuredContent"] == [1, 2]


def test_subscribe_accepts_a_uri_that_read_serves():
    """Subscribe and read must agree about whether a URI exists.

    A template instance was readable but not subscribable, so a client that
    had just read a resource could not watch it.
    """
    server = MCPServer("templates")

    @server.resource("weather://{city}/current")
    def weather(city):
        """Weather by city"""
        return {"city": city}

    rpc(server, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert "result" in rpc(server, "resources/read",
                           {"uri": "weather://paris/current"})
    assert rpc(server, "resources/subscribe",
               {"uri": "weather://paris/current"})["result"] == {}
    assert server.resource_updated("weather://paris/current") == 1

    # A URI no template matches is still not found.
    assert rpc(server, "resources/subscribe",
               {"uri": "weather://a/b/current"})["error"]["code"] == -32002


def test_template_only_server_advertises_subscribe():
    server = MCPServer("templates")

    @server.resource("weather://{city}/current")
    def weather(city):
        """Weather by city"""
        return {"city": city}

    caps = rpc(server, "initialize",
               {"protocolVersion": "2025-06-18",
                "capabilities": {}})["result"]["capabilities"]
    assert caps["resources"]["subscribe"] is True


def test_sweep_decides_and_removes_under_one_lock():
    """Staleness is evaluated and acted on without releasing `_sessions_lock`.

    The sweep used to select stale ids, release the lock, and only then call
    `terminate_session` (which re-takes it twice). A request arriving in that
    window refreshed `last_seen`, the sweep collected the session anyway, and
    the client got a 404 immediately after being served. Counting the
    acquisitions pins the absence of that window: three meant three chances
    for state to change underneath, one means none.
    """
    server = server_with()
    server._sessions["S"]["last_seen"] -= SESSION_IDLE_TIMEOUT_SECONDS + 1

    real_lock = server._sessions_lock
    acquisitions = []

    class _CountingLock(object):
        def __enter__(self):
            acquisitions.append(1)
            real_lock.acquire()
            return self

        def __exit__(self, *exc):
            real_lock.release()

    server._sessions_lock = _CountingLock()
    try:
        server._prune_expired_sessions()
    finally:
        server._sessions_lock = real_lock

    assert server.session_exists("S") is False, "the stale session was not collected"
    assert len(acquisitions) == 1, (
        "expected one atomic decide-and-remove, saw {}".format(len(acquisitions)))


def test_sweep_spares_a_session_that_is_not_yet_idle():
    """The boundary the sweep is deciding: just under the timeout survives."""
    server = server_with()
    server._sessions["S"]["last_seen"] -= SESSION_IDLE_TIMEOUT_SECONDS - 5
    server._prune_expired_sessions()
    assert server.session_exists("S") is True


def test_sweep_releases_the_state_a_collected_session_owned():
    """Subscriptions and waiters must go with the session, as DELETE does."""
    server = server_with()
    rpc(server, "resources/subscribe", {"uri": "config://settings"})
    assert server._resource_subs.get("S")

    server._sessions["S"]["last_seen"] -= SESSION_IDLE_TIMEOUT_SECONDS + 1
    server._prune_expired_sessions()

    assert server.session_exists("S") is False
    assert "S" not in server._resource_subs
    assert server.resource_updated("config://settings") == 0


# ---------------------------------------------------------------------------
# Inference must not assert a type it only guessed
#
# Validating tools/call against inputSchema turned every guess in the schema
# generator into a hard constraint. These pin the rule that survived: a type
# the generator actually knows is enforced, a type it invented is not
# published at all.
# ---------------------------------------------------------------------------

class _Widget(object):
    """A class the schema generator has no model for."""


def _guessy_server():
    server = MCPServer("guesses")

    @server.tool()
    def scale(factor=1):
        """Type is only inferable from the default"""
        return {"factor": factor}

    @server.tool()
    def flags(opts={"a": 1}):
        """Dict default"""
        return {"opts": opts}

    @server.tool()
    def custom(w: _Widget):
        """Annotated with an unmodelled class"""
        return {"w": repr(w)}

    @server.tool()
    def commented(a, b):
        # type: (Union[int, str], Any) -> dict
        """Type comment with a mixed union and Any"""
        return {"a": a, "b": b}

    rpc(server, "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}})
    return server


def _schema_of(server, name):
    return [t for t in rpc(server, "tools/list")["result"]["tools"]
            if t["name"] == name][0]["inputSchema"]


def test_default_value_publishes_default_not_a_guessed_type():
    """`def scale(factor=1)` must not advertise `"type": "integer"`.

    The default is evidence of the default, not of what the parameter
    accepts. Publishing a type inferred from it made `factor=2.5` a -32602,
    where published 0.4.3 ran the tool.
    """
    server = _guessy_server()
    assert _schema_of(server, "scale")["properties"]["factor"] == {"default": 1}

    for value in (2.5, "x", None, [1]):
        result = rpc(server, "tools/call",
                     {"name": "scale", "arguments": {"factor": value}})
        assert "error" not in result, (value, result)


def test_dict_default_does_not_constrain_the_argument():
    server = _guessy_server()
    assert _schema_of(server, "flags")["properties"]["opts"] == {"default": {"a": 1}}
    assert "error" not in rpc(server, "tools/call",
                              {"name": "flags", "arguments": {"opts": [1]}})


def test_unmodelled_class_annotation_constrains_nothing():
    """The "unknown -> string" fallback rejected every object sent."""
    server = _guessy_server()
    assert _schema_of(server, "custom")["properties"]["w"] == {}
    # Still required — presence is structural, and known.
    assert _schema_of(server, "custom")["required"] == ["w"]

    assert "error" not in rpc(server, "tools/call",
                              {"name": "custom", "arguments": {"w": {"k": 1}}})
    missing = rpc(server, "tools/call", {"name": "custom", "arguments": {}})
    assert missing["error"]["code"] == -32602


def test_type_comment_union_and_any_constrain_nothing():
    """The comment path mapped `Any` and mixed unions to "string".

    The resolved-annotation path returns `{}` for both; the two disagreed,
    and only the comment path rejected valid calls.
    """
    server = _guessy_server()
    props = _schema_of(server, "commented")["properties"]
    assert props["a"] == {}
    assert props["b"] == {}

    for args in ({"a": 1, "b": 2}, {"a": "s", "b": [1]}, {"a": 1, "b": None}):
        assert "error" not in rpc(server, "tools/call",
                                  {"name": "commented", "arguments": args}), args


def test_real_annotations_are_still_enforced():
    """The fixes must not disarm validation where the type is actually known."""
    server = MCPServer("typed")

    @server.tool()
    def typed(a: int, b: str = "x"):
        """Properly annotated"""
        return {"a": a, "b": b}

    assert rpc(server, "tools/call",
               {"name": "typed", "arguments": {"a": "no"}})["error"]["code"] == -32602
    assert rpc(server, "tools/call",
               {"name": "typed", "arguments": {"a": 1, "b": 2}})["error"]["code"] == -32602
    assert rpc(server, "tools/call",
               {"name": "typed", "arguments": {}})["error"]["code"] == -32602
    assert "error" not in rpc(server, "tools/call",
                              {"name": "typed", "arguments": {"a": 1}})
