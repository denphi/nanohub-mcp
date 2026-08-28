"""
Integration test for the MCP server.

Starts the simple calculator example server in a background thread,
then tests SSE, Streamable HTTP, OpenAPI, MCP discovery, direct tool calls,
and all JSON-RPC methods via HTTP requests.
"""

from __future__ import print_function

import json
import os
import sys
import threading
import time

# Ensure the package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from http.client import HTTPConnection
except ImportError:
    from httplib import HTTPConnection

from nanohubmcp import MCPServer, Context, ToolResult, ImageContent


# ---------------------------------------------------------------------------
# Build a small test server (mirrors examples/simple)
# ---------------------------------------------------------------------------

server = MCPServer("test-calculator", version="1.0.0")


@server.tool()
def add(a, b):
    # type: (float, float) -> float
    """Add two numbers together."""
    return float(a) + float(b)


@server.tool()
def divide(a, b):
    # type: (float, float) -> float
    """Divide a by b."""
    if float(b) == 0:
        raise ValueError("Cannot divide by zero")
    return float(a) / float(b)


@server.resource("config://calculator/settings")
def get_settings():
    """Get calculator settings."""
    return {"precision": 10}


@server.prompt()
def calculate(expression):
    # type: (str) -> list
    """Generate a calculation prompt."""
    return [
        {
            "role": "user",
            "content": {"type": "text", "text": "Please calculate: {}".format(expression)},
        }
    ]


@server.tool()
def progress_tool(ctx):
    """Emit a progress notification then return."""
    ctx.report_progress(0.5, total=1.0, message="halfway")
    return "done"


@server.tool()
def progress_token_echo(ctx):
    """Emit progress and return the token the context observed."""
    ctx.report_progress(0.25, total=1.0)
    return {"token": ctx.progress_token}


@server.tool()
def ask_user(ctx):
    """Request a value through MCP elicitation."""
    result = ctx.elicit(
        "Choose a project name",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"}
            },
            "required": ["project"]
        },
        timeout=5
    )
    return result


@server.async_tool()
def slow_echo(value, delay=0.05):
    """Return a value after a short delay."""
    time.sleep(float(delay))
    return {"value": value}


@server.async_tool()
def slow_image():
    """Return rich content from an async tool."""
    return ToolResult([ImageContent(data="abc", mimeType="image/png")])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PORT = 18765  # High port to avoid conflicts


def _get(path):
    """Send a GET request and return status + parsed JSON body."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    return resp.status, json.loads(raw)


def _post(path, body):
    """Send a JSON-RPC POST and return status + parsed response body."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    data = json.dumps(body).encode("utf-8")
    conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    return resp.status, json.loads(raw)


def _read_sse(path="/sse", lines_to_read=4, timeout=3):
    """Open an SSE connection and read a few lines."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    conn.request("GET", path)
    resp = conn.getresponse()
    content_type = resp.getheader("Content-Type")
    collected = []
    try:
        for _ in range(lines_to_read):
            line = resp.readline()
            if not line:
                break
            collected.append(line.decode("utf-8").rstrip("\r\n"))
    except Exception:
        pass
    conn.close()
    return resp.status, content_type, collected


def _open_mcp_session():
    """Open /mcp SSE and return (conn, resp, session_id) after draining headers."""
    conn_sse = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn_sse.request("GET", "/mcp")
    resp_sse = conn_sse.getresponse()
    session_id = resp_sse.getheader("Mcp-Session-Id")
    assert session_id
    resp_sse.readline()  # event: open
    resp_sse.readline()  # data: {}
    resp_sse.readline()  # empty
    resp_sse.readline()  # event: endpoint
    resp_sse.readline()  # data: /mcp?session_id=...
    resp_sse.readline()  # empty
    return conn_sse, resp_sse, session_id


# ---------------------------------------------------------------------------
# Tests - Server info
# ---------------------------------------------------------------------------


def test_server_info():
    """GET / returns server info JSON with endpoints."""
    status, body = _get("/")
    assert status == 200
    assert body["name"] == "test-calculator"
    assert body["status"] == "running"
    assert body["tools"] >= 2  # add, divide, ask_user, progress_tool
    assert body["resources"] == 1
    assert body["prompts"] == 1
    assert "endpoints" in body
    assert body["endpoints"]["sse"] == "/sse"
    assert body["endpoints"]["mcp"] == "/mcp"


# ---------------------------------------------------------------------------
# Tests - SSE transport
# ---------------------------------------------------------------------------


def test_sse_connection():
    """GET /sse returns an SSE stream with an open event."""
    status, content_type, lines = _read_sse("/sse")
    assert status == 200
    assert "text/event-stream" in content_type
    assert "event: open" in lines[0]


# ---------------------------------------------------------------------------
# Tests - Streamable HTTP transport
# ---------------------------------------------------------------------------


def test_streamable_http_get():
    """GET /mcp returns an SSE stream with open + endpoint events."""
    status, content_type, lines = _read_sse("/mcp", lines_to_read=6)
    assert status == 200
    assert "text/event-stream" in content_type
    assert "event: open" in lines[0]
    # After "event: open", "data: {}", blank line, "event: endpoint", "data: ..."
    endpoint_idx = next(i for i, line in enumerate(lines) if "event: endpoint" in line)
    assert lines[endpoint_idx + 1].startswith("data: /mcp?session_id=")


# ---------------------------------------------------------------------------
# Tests - JSON-RPC via POST (synchronous responses)
# ---------------------------------------------------------------------------


def test_initialize():
    """POST initialize returns protocol version and server info synchronously."""
    status, body = _post("/", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert status == 200
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert "protocolVersion" in body["result"]
    assert body["result"]["serverInfo"]["name"] == "test-calculator"
    assert (
        "io.modelcontextprotocol/tasks"
        in body["result"]["capabilities"]["extensions"]
    )


def test_optional_dict_schema_generation():
    """Optional[Dict[str, str]] must serialize as an object, not a bare string.

    Regression for the bug where strict MCP clients (e.g. ChatGPT) refused to
    send a map argument because the generated wire schema said 'string'.
    """
    from typing import Optional, Dict, List, Any
    from nanohubmcp import decorators as D

    assert D._python_type_to_json_schema(Optional[Dict[str, str]]) == {
        "type": "object", "additionalProperties": {"type": "string"}}
    assert D._python_type_to_json_schema(Dict[str, str]) == {
        "type": "object", "additionalProperties": {"type": "string"}}
    assert D._python_type_to_json_schema(Optional[str]) == {"type": "string"}
    assert D._python_type_to_json_schema(List[str]) == {
        "type": "array", "items": {"type": "string"}}
    assert D._python_type_to_json_schema(Any) == {}

    def sample(sources: Optional[Dict[str, str]] = None, name: str = ""):
        return None

    props = D._generate_input_schema(sample)["properties"]
    assert props["sources"]["type"] == "object"
    assert props["sources"]["additionalProperties"] == {"type": "string"}
    assert props["name"]["type"] == "string"


def test_tools_list():
    """POST tools/list returns registered tools."""
    status, body = _post("/", {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}})
    assert status == 200
    assert body["id"] == 10
    tools = body["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "add" in tool_names
    assert "divide" in tool_names

    add_tool = [t for t in tools if t["name"] == "add"][0]
    add_properties = add_tool["inputSchema"]["properties"]
    assert add_properties["a"]["type"] == "number"
    assert add_properties["b"]["type"] == "number"


def test_tools_call_add():
    """POST tools/call with add returns correct result."""
    status, body = _post(
        "/",
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
        },
    )
    assert status == 200
    assert body["id"] == 20
    assert body["result"]["isError"] is False
    assert "5" in body["result"]["content"][0]["text"]


def test_tools_call_divide_by_zero():
    """POST tools/call divide by zero returns isError=True."""
    status, body = _post(
        "/",
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "divide", "arguments": {"a": 1, "b": 0}},
        },
    )
    assert status == 200
    assert body["id"] == 21
    assert body["result"]["isError"] is True
    assert "zero" in body["result"]["content"][0]["text"].lower()


def test_resources_list():
    """POST resources/list returns registered resources."""
    status, body = _post(
        "/", {"jsonrpc": "2.0", "id": 30, "method": "resources/list", "params": {}}
    )
    assert status == 200
    assert body["id"] == 30
    uris = [r["uri"] for r in body["result"]["resources"]]
    assert "config://calculator/settings" in uris


def test_resources_read():
    """POST resources/read returns resource content."""
    status, body = _post(
        "/",
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "resources/read",
            "params": {"uri": "config://calculator/settings"},
        },
    )
    assert status == 200
    assert body["id"] == 31
    content = json.loads(body["result"]["contents"][0]["text"])
    assert content["precision"] == 10


def test_prompts_list():
    """POST prompts/list returns registered prompts."""
    status, body = _post(
        "/", {"jsonrpc": "2.0", "id": 40, "method": "prompts/list", "params": {}}
    )
    assert status == 200
    assert body["id"] == 40
    names = [p["name"] for p in body["result"]["prompts"]]
    assert "calculate" in names


def test_prompts_get():
    """POST prompts/get returns prompt messages."""
    status, body = _post(
        "/",
        {
            "jsonrpc": "2.0",
            "id": 41,
            "method": "prompts/get",
            "params": {"name": "calculate", "arguments": {"expression": "2+2"}},
        },
    )
    assert status == 200
    assert body["id"] == 41
    assert "2+2" in str(body["result"]["messages"])


def test_ping():
    """POST ping returns empty result."""
    status, body = _post("/", {"jsonrpc": "2.0", "id": 50, "method": "ping", "params": {}})
    assert status == 200
    assert body["id"] == 50
    assert body["result"] == {}


def test_method_not_found():
    """POST unknown method returns error."""
    status, body = _post(
        "/", {"jsonrpc": "2.0", "id": 60, "method": "nonexistent/method", "params": {}}
    )
    assert status == 200
    assert body["id"] == 60
    assert "error" in body
    assert body["error"]["code"] == -32601


def test_notification_returns_accepted():
    """POST notification (no id) returns 202 accepted."""
    status, body = _post("/", {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    assert status == 202
    assert body["status"] == "accepted"


# ---------------------------------------------------------------------------
# Tests - JSON-RPC via /mcp endpoint
# ---------------------------------------------------------------------------


def test_mcp_post_initialize():
    """POST /mcp returns fast JSON-RPC responses synchronously."""
    status, body = _post("/mcp", {"jsonrpc": "2.0", "id": 100, "method": "initialize", "params": {}})
    assert status == 200
    assert body["id"] == 100
    assert body["result"]["serverInfo"]["name"] == "test-calculator"


def test_mcp_post_tools_call():
    """POST /mcp returns non-async tool calls synchronously."""
    status, body = _post(
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 10, "b": 20}},
        },
    )
    assert status == 200
    assert body["id"] == 101
    assert body["result"]["isError"] is False
    assert "30" in body["result"]["content"][0]["text"]


def test_async_tool_returns_task_for_task_capable_client():
    """Task-capable clients get CreateTaskResult for async tools."""
    conn_sse, _resp_sse, session_id = _open_mcp_session()
    task_capability = {
        "_meta": {
            "io.modelcontextprotocol/clientCapabilities": {
                "extensions": {"io.modelcontextprotocol/tasks": {}}
            }
        }
    }
    try:
        status, body = _post(
            "/mcp?session_id={}".format(session_id),
            {
                "jsonrpc": "2.0",
                "id": 110,
                "method": "tools/call",
                "params": dict(
                    task_capability,
                    name="slow_echo",
                    arguments={"value": "hello", "delay": 0.01},
                ),
            },
        )
        assert status == 200
        task = body["result"]
        assert task["resultType"] == "task"
        assert task["status"] in ("working", "completed")
        assert task["taskId"]
        assert task["ttlMs"] > 0
        assert task["pollIntervalMs"] > 0

        deadline = time.time() + 3
        final = None
        while time.time() < deadline:
            status, poll_body = _post(
                "/mcp?session_id={}".format(session_id),
                {
                    "jsonrpc": "2.0",
                    "id": 111,
                    "method": "tasks/get",
                    "params": dict(task_capability, taskId=task["taskId"]),
                },
            )
            assert status == 200
            final = poll_body["result"]
            if final["status"] == "completed":
                break
            time.sleep(0.02)

        assert final["resultType"] == "complete"
        assert final["status"] == "completed"
        result_text = final["result"]["content"][0]["text"]
        assert json.loads(result_text)["value"] == "hello"
    finally:
        conn_sse.close()


def test_async_tool_keeps_legacy_job_result_without_task_capability():
    """Older clients still receive the existing get_job_result flow."""
    status, body = _post(
        "/",
        {
            "jsonrpc": "2.0",
            "id": 112,
            "method": "tools/call",
            "params": {"name": "slow_echo", "arguments": {"value": "legacy"}},
        },
    )
    assert status == 200
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["status"] == "running"
    assert payload["job_id"]


def test_tasks_get_requires_task_capability():
    """tasks/get rejects callers that did not declare task support."""
    status, body = _post(
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 113,
            "method": "tasks/get",
            "params": {"taskId": "missing"},
        },
    )
    assert status == 200
    assert body["error"]["code"] == -32003
    assert "io.modelcontextprotocol/tasks" in str(body["error"]["data"])


def test_task_capable_async_tool_requires_session():
    """Task-capable async calls without a session fail instead of returning unusable tasks."""
    status, body = _post(
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 114,
            "method": "tools/call",
            "params": {
                "name": "slow_echo",
                "arguments": {"value": "no-session"},
                "_meta": {
                    "io.modelcontextprotocol/clientCapabilities": {
                        "extensions": {"io.modelcontextprotocol/tasks": {}}
                    }
                },
            },
        },
    )
    assert status == 200
    assert body["error"]["code"] == -32003
    assert "session" in body["error"]["message"].lower()


def test_tasks_cancel_marks_running_task_cancelled():
    """tasks/cancel acknowledges and marks a running task cancelled."""
    conn_sse, _resp_sse, session_id = _open_mcp_session()
    task_capability = {
        "_meta": {
            "io.modelcontextprotocol/clientCapabilities": {
                "extensions": {"io.modelcontextprotocol/tasks": {}}
            }
        }
    }
    try:
        status, body = _post(
            "/mcp?session_id={}".format(session_id),
            {
                "jsonrpc": "2.0",
                "id": 115,
                "method": "tools/call",
                "params": dict(
                    task_capability,
                    name="slow_echo",
                    arguments={"value": "cancel-me", "delay": 0.5},
                ),
            },
        )
        assert status == 200
        task_id = body["result"]["taskId"]

        status, cancel_body = _post(
            "/mcp?session_id={}".format(session_id),
            {
                "jsonrpc": "2.0",
                "id": 116,
                "method": "tasks/cancel",
                "params": dict(task_capability, taskId=task_id),
            },
        )
        assert status == 200
        assert cancel_body["result"] == {"resultType": "complete"}

        status, poll_body = _post(
            "/mcp?session_id={}".format(session_id),
            {
                "jsonrpc": "2.0",
                "id": 117,
                "method": "tasks/get",
                "params": dict(task_capability, taskId=task_id),
            },
        )
        assert status == 200
        assert poll_body["result"]["status"] == "cancelled"
    finally:
        conn_sse.close()


def test_tasks_are_session_bound():
    """A task created in one MCP session cannot be polled from another."""
    owner_conn, _owner_resp, owner_session = _open_mcp_session()
    other_conn, _other_resp, other_session = _open_mcp_session()
    task_capability = {
        "_meta": {
            "io.modelcontextprotocol/clientCapabilities": {
                "extensions": {"io.modelcontextprotocol/tasks": {}}
            }
        }
    }
    try:
        status, body = _post(
            "/mcp?session_id={}".format(owner_session),
            {
                "jsonrpc": "2.0",
                "id": 118,
                "method": "tools/call",
                "params": dict(
                    task_capability,
                    name="slow_echo",
                    arguments={"value": "private", "delay": 0.1},
                ),
            },
        )
        assert status == 200
        task_id = body["result"]["taskId"]

        status, poll_body = _post(
            "/mcp?session_id={}".format(other_session),
            {
                "jsonrpc": "2.0",
                "id": 119,
                "method": "tasks/get",
                "params": dict(task_capability, taskId=task_id),
            },
        )
        assert status == 200
        assert poll_body["error"]["code"] == -32003
        assert "session" in poll_body["error"]["message"].lower()
    finally:
        owner_conn.close()
        other_conn.close()


def test_async_tool_task_preserves_rich_tool_result_content():
    """Async task completion preserves non-text ToolResult content."""
    conn_sse, _resp_sse, session_id = _open_mcp_session()
    task_capability = {
        "_meta": {
            "io.modelcontextprotocol/clientCapabilities": {
                "extensions": {"io.modelcontextprotocol/tasks": {}}
            }
        }
    }
    try:
        status, body = _post(
            "/mcp?session_id={}".format(session_id),
            {
                "jsonrpc": "2.0",
                "id": 120,
                "method": "tools/call",
                "params": dict(task_capability, name="slow_image", arguments={}),
            },
        )
        assert status == 200
        task_id = body["result"]["taskId"]

        deadline = time.time() + 3
        final = None
        while time.time() < deadline:
            status, poll_body = _post(
                "/mcp?session_id={}".format(session_id),
                {
                    "jsonrpc": "2.0",
                    "id": 121,
                    "method": "tasks/get",
                    "params": dict(task_capability, taskId=task_id),
                },
            )
            assert status == 200
            final = poll_body["result"]
            if final["status"] == "completed":
                break
            time.sleep(0.02)

        assert final["status"] == "completed"
        content = final["result"]["content"]
        assert content == [{"type": "image", "data": "abc", "mimeType": "image/png"}]
    finally:
        conn_sse.close()


# ---------------------------------------------------------------------------
# Tests - Direct tool calls (REST/OpenAPI style)
# ---------------------------------------------------------------------------


def test_direct_tool_call_add():
    """POST /tools/add calls the tool directly."""
    status, body = _post("/tools/add", {"a": 7, "b": 3})
    assert status == 200
    assert "10" in str(body.get("result", ""))


def test_direct_tool_call_not_found():
    """POST /tools/nonexistent returns 404."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    data = json.dumps({"a": 1}).encode("utf-8")
    conn.request("POST", "/tools/nonexistent", body=data, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 404
    resp.read()
    conn.close()


def test_direct_tool_call_error():
    """POST /tools/divide with b=0 returns 500 with error."""
    status, body = _post("/tools/divide", {"a": 1, "b": 0})
    assert status == 500
    assert "error" in body
    assert "zero" in body["error"].lower()


# ---------------------------------------------------------------------------
# Tests - OpenAPI schema
# ---------------------------------------------------------------------------


def test_openapi_schema():
    """GET /openapi.json returns valid OpenAPI schema."""
    status, body = _get("/openapi.json")
    assert status == 200
    assert body["openapi"] == "3.1.0"
    assert body["info"]["title"] == "test-calculator"
    assert "/mcp" in body["paths"]
    assert "/tools/add" in body["paths"]
    assert "/tools/divide" in body["paths"]


# ---------------------------------------------------------------------------
# Tests - MCP discovery
# ---------------------------------------------------------------------------


def test_mcp_discovery():
    """GET /.well-known/mcp.json returns MCP discovery document."""
    status, body = _get("/.well-known/mcp.json")
    assert status == 200
    assert body["mcpVersion"] == "2024-11-05"
    assert body["serverInfo"]["name"] == "test-calculator"
    transport_types = [t["type"] for t in body["transports"]]
    assert "sse" in transport_types
    assert "streamable-http" in transport_types


# ---------------------------------------------------------------------------
# Tests - SSE broadcast still works with synchronous POST
# ---------------------------------------------------------------------------


def test_sse_receives_session_response_only():
    """SSE stream receives only responses for its session."""
    conn_sse = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn_sse.request("GET", "/sse")
    resp_sse = conn_sse.getresponse()
    session_id = resp_sse.getheader("Mcp-Session-Id")
    assert session_id

    resp_sse.readline()  # event: open
    resp_sse.readline()  # data: {}
    resp_sse.readline()  # empty line
    resp_sse.readline()  # event: endpoint
    resp_sse.readline()  # data: /?session_id=...
    resp_sse.readline()  # empty line

    conn_other = HTTPConnection("127.0.0.1", PORT, timeout=1)
    conn_other.request("GET", "/sse")
    resp_other = conn_other.getresponse()
    other_session_id = resp_other.getheader("Mcp-Session-Id")
    assert other_session_id and other_session_id != session_id
    for _ in range(6):
        resp_other.readline()

    _post("/?session_id={}".format(session_id), {
        "jsonrpc": "2.0",
        "id": 200,
        "method": "ping",
        "params": {}
    })

    event_line = resp_sse.readline().decode("utf-8").strip()
    data_line = resp_sse.readline().decode("utf-8").strip()
    conn_sse.close()

    try:
        leaked_line = resp_other.readline()
    except Exception:
        leaked_line = b""
    conn_other.close()

    assert event_line == "event: message"
    assert data_line.startswith("data: ")
    response = json.loads(data_line[6:])
    assert response["id"] == 200
    assert leaked_line == b""


def test_tool_can_request_elicitation_from_client():
    """A tool can send elicitation/create and wait for the client response."""
    conn_sse = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn_sse.request("GET", "/mcp")
    resp_sse = conn_sse.getresponse()
    session_id = resp_sse.getheader("Mcp-Session-Id")
    assert session_id
    resp_sse.readline()  # event: open
    resp_sse.readline()  # data: {}
    resp_sse.readline()  # empty line
    resp_sse.readline()  # event: endpoint
    resp_sse.readline()  # data: /mcp?session_id=...
    resp_sse.readline()  # empty line

    status, body = _post(
        "/mcp?session_id={}".format(session_id),
        {
            "jsonrpc": "2.0",
            "id": 300,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"elicitation": {"form": {}}},
                "clientInfo": {"name": "test-client", "version": "1.0.0"}
            },
        },
    )
    assert status == 200
    assert body["result"]["protocolVersion"] == "2025-11-25"
    resp_sse.readline()  # event: message
    resp_sse.readline()  # data: initialize response
    resp_sse.readline()  # empty line

    tool_result = []

    def call_tool():
        tool_result.append(_post(
            "/mcp?session_id={}".format(session_id),
            {
                "jsonrpc": "2.0",
                "id": 301,
                "method": "tools/call",
                "params": {"name": "ask_user", "arguments": {}},
            },
        ))

    thread = threading.Thread(target=call_tool)
    thread.daemon = True
    thread.start()

    event_line = resp_sse.readline().decode("utf-8").strip()
    data_line = resp_sse.readline().decode("utf-8").strip()
    assert event_line == "event: message"
    request = json.loads(data_line[6:])
    assert request["method"] == "elicitation/create"
    assert request["params"]["mode"] == "form"
    assert request["params"]["requestedSchema"]["required"] == ["project"]

    status, body = _post(
        "/mcp?session_id={}".format(session_id),
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "action": "accept",
                "content": {"project": "demo"}
            },
        },
    )
    assert status == 202
    assert body["status"] == "accepted"

    thread.join(5)
    conn_sse.close()
    assert tool_result
    status, body = tool_result[0]
    assert status == 200
    assert body["id"] == 301
    content = json.loads(body["result"]["content"][0]["text"])
    assert content["action"] == "accept"
    assert content["content"]["project"] == "demo"


def test_report_progress_broadcasts_to_session_sse():
    """Regression: ctx.report_progress must reach the client's SSE stream.

    Previously _broadcast became a no-op without a session_id, and
    Context.report_progress passed none — so notifications were silently
    dropped. This test asserts the notification arrives on /mcp's SSE.
    """
    conn_sse = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn_sse.request("GET", "/mcp")
    resp_sse = conn_sse.getresponse()
    session_id = resp_sse.getheader("Mcp-Session-Id")
    assert session_id
    resp_sse.readline()  # event: open
    resp_sse.readline()  # data: {}
    resp_sse.readline()  # empty
    resp_sse.readline()  # event: endpoint
    resp_sse.readline()  # data: /mcp?session_id=...
    resp_sse.readline()  # empty

    tool_result = []

    def call_tool():
        tool_result.append(_post(
            "/mcp?session_id={}".format(session_id),
            {
                "jsonrpc": "2.0",
                "id": 401,
                "method": "tools/call",
                "params": {
                    "name": "progress_tool",
                    "arguments": {},
                    # Per MCP spec: progressToken in _meta enables progress
                    # notifications. Without it the server suppresses them.
                    "_meta": {"progressToken": "tok-401"},
                },
            },
        ))

    thread = threading.Thread(target=call_tool)
    thread.daemon = True
    thread.start()

    # Drain SSE looking for the progress notification
    saw_progress = False
    deadline = time.time() + 3
    while time.time() < deadline:
        line = resp_sse.readline().decode("utf-8").strip()
        if not line:
            continue
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if payload.get("method") == "notifications/progress":
                params = payload.get("params", {})
                assert params.get("progressToken") == "tok-401"
                assert "requestId" not in params
                assert params.get("progress") == 0.5
                assert params.get("total") == 1.0
                assert params.get("message") == "halfway"
                saw_progress = True
                break

    thread.join(3)
    conn_sse.close()
    assert saw_progress, "report_progress did not reach the SSE stream"


def test_report_progress_suppressed_without_token():
    """Without a progressToken, report_progress must not emit a notification."""
    from nanohubmcp.server import MCPServer
    from nanohubmcp.context import Context

    emitted = []

    class FakeServer(MCPServer):
        def _broadcast(self, message, session_id=None):
            emitted.append((message, session_id))

    s = FakeServer("noprog")
    ctx = Context(server=s, request_id="r", session_id="sess", progress_token=None)
    ctx.report_progress(0.5, total=1.0)
    assert emitted == []

    ctx2 = Context(server=s, request_id="r", session_id="sess", progress_token="tok-1")
    ctx2.report_progress(0.5, total=1.0)
    assert len(emitted) == 1
    msg, sid = emitted[0]
    assert msg["params"]["progressToken"] == "tok-1"
    assert sid == "sess"


def test_jsonrpc_empty_method_returns_minus_32600():
    """Missing/empty method is Invalid Request, not Method Not Found."""
    status, body = _post("/", {"jsonrpc": "2.0", "id": 77, "method": ""})
    assert status == 200
    assert body["error"]["code"] == -32600


def test_notification_invalid_params_gets_no_reply():
    """Notifications (no id) must never receive an error response body."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    data = json.dumps({"jsonrpc": "2.0", "method": "ping", "params": "bad"}).encode("utf-8")
    conn.request("POST", "/", body=data,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    # Server returns 202 accepted with a status body, no JSON-RPC envelope.
    assert resp.status == 202
    assert b"error" not in raw


def test_jsonrpc_batch_returns_responses_for_requests_only():
    """JSON-RPC batches return an array, omitting notification responses."""
    status, body = _post("/", [
        {"jsonrpc": "2.0", "id": 901, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 902,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 1, "b": 2}},
        },
    ])
    assert status == 200
    assert isinstance(body, list)
    assert [item["id"] for item in body] == [901, 902]
    assert body[0]["result"] == {}
    assert body[1]["result"]["isError"] is False


def test_jsonrpc_batch_all_notifications_returns_accepted():
    """A batch with only notifications gets no JSON-RPC response."""
    status, body = _post("/", [
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
    ])
    assert status == 202
    assert body == {"status": "accepted"}


def test_jsonrpc_invalid_top_level_payload_returns_minus_32600():
    """Valid JSON with the wrong top-level shape must not become a 500."""
    status, body = _post("/", "not-an-object")
    assert status == 200
    assert body["error"]["code"] == -32600


def test_jsonrpc_empty_batch_returns_minus_32600():
    """Empty JSON-RPC batches are invalid."""
    status, body = _post("/", [])
    assert status == 200
    assert body["error"]["code"] == -32600


def test_request_client_fails_fast_without_stream():
    """Server-to-client requests must error immediately if no SSE is connected."""
    import pytest
    from nanohubmcp.server import MCPServer

    s = MCPServer("nostream")
    # Mark client as supporting elicitation so we get past the capability check
    s._sessions["sess-x"] = {"capabilities": {"elicitation": {"form": {}}}}

    with pytest.raises(RuntimeError) as excinfo:
        s.request_elicitation(
            session_id="sess-x",
            message="hi",
            requested_schema={"type": "object", "properties": {}, "required": []},
            timeout=10,
        )
    assert "No active client stream" in str(excinfo.value)


def test_session_state_cleared_on_disconnect():
    """When the last SSE queue closes, _sessions and pending entries are dropped."""
    from nanohubmcp.server import MCPServer, _SSEQueue

    s = MCPServer("cleanup")
    s._sessions["sess-y"] = {"capabilities": {"sampling": {}}}
    s._pending_client_requests["sess-y:server-1"] = {
        "event": threading.Event(),
        "response": None,
    }
    q = _SSEQueue()
    s._register_client("sess-y", q)

    s._unregister_client("sess-y", q)

    assert "sess-y" not in s._clients
    assert "sess-y" not in s._sessions
    assert "sess-y:server-1" not in s._pending_client_requests


def test_direct_tool_call_rejects_context_tool():
    """Regression: /tools/<name> must refuse tools that need an MCP session."""
    status, body = _post("/tools/progress_tool", {})
    assert status == 409
    assert "MCP session" in body["error"]


def test_post_malformed_json_returns_400():
    """Bad JSON in the request body produces 400, not 500."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request(
        "POST", "/mcp", body=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 400


def test_post_invalid_content_length_returns_400():
    """A non-integer Content-Length header produces 400."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    # Use raw socket since HTTPConnection enforces numeric content-length
    conn.putrequest("POST", "/mcp")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", "abc")
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 400


def test_post_unknown_path_returns_404_without_reading_body():
    """POST to an unknown path is rejected before reading the body."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8")
    conn.request("POST", "/no-such-endpoint", body=data,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 404


def test_tools_call_non_object_arguments_returns_minus_32602():
    """tools/call with arguments=<string> produces -32602, not -32603."""
    status, body = _post("/", {
        "jsonrpc": "2.0", "id": 88, "method": "tools/call",
        "params": {"name": "add", "arguments": "not-an-object"},
    })
    assert status == 200
    assert body["error"]["code"] == -32602
    assert "object" in body["error"]["message"]


def test_get_unknown_path_returns_404():
    """GET to an unknown path 404s instead of returning server info."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("GET", "/no-such-thing")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 404


def test_get_favicon_returns_204():
    """Browsers probing for /favicon.ico get a cheap 204."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("GET", "/favicon.ico")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 204


def test_jsonrpc_invalid_params_returns_minus_32602():
    """Non-object params produce a JSON-RPC -32602 error, not a 500."""
    status, body = _post("/", {"jsonrpc": "2.0", "id": 99, "method": "ping", "params": "nope"})
    assert status == 200
    assert body["id"] == 99
    assert body["error"]["code"] == -32602
    assert "object" in body["error"]["message"]


def test_post_oversized_body_returns_413():
    """A body larger than MAX_REQUEST_BYTES is refused with 413."""
    from nanohubmcp.server import MAX_REQUEST_BYTES
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.putrequest("POST", "/mcp")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 413


def test_direct_tool_call_rejects_non_object_body():
    """POST /tools/<name> with a non-object body returns 400."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request(
        "POST",
        "/tools/add",
        body=b"[1, 2, 3]",
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    assert resp.status == 400
    body = json.loads(raw)
    assert "JSON object" in body["error"]


def test_direct_tool_call_unknown_kwarg_is_400():
    """Unexpected kwargs surface as 400, not 500."""
    status, body = _post("/tools/add", {"a": 1, "b": 2, "extra": "boom"})
    assert status == 400
    assert "extra" in body["error"] or "unexpected" in body["error"].lower()


def test_async_job_consumed_after_first_successful_poll():
    """get_job_result must drop terminal jobs so memory doesn't leak."""
    from nanohubmcp.server import MCPServer

    s = MCPServer("jobsrv")

    @s.async_tool()
    def slow():
        return "done"

    with s._jobs_lock:
        s._jobs["jid-1"] = {"status": "done", "result": "ok"}

    # First poll returns the result and removes the entry
    handler = s._tools["get_job_result"]["handler"]
    first = handler(job_id="jid-1")
    assert first["status"] == "done"
    assert first["result"] == "ok"
    assert "jid-1" not in s._jobs

    # Second poll sees a not_found
    second = handler(job_id="jid-1")
    assert second["status"] == "not_found"


def test_expired_task_records_are_pruned():
    """Expired task records are removed during request handling."""
    from nanohubmcp.server import MCPServer

    s = MCPServer("prune")
    s._jobs["old"] = {
        "status": "done",
        "result": "ok",
        "session_id": "sess-old",
        "expires_at": 0,
    }

    s._handle_request({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})

    assert "old" not in s._jobs


def test_get_job_result_not_registered_without_async_tools():
    """Servers without any @async_tool must not advertise get_job_result."""
    from nanohubmcp.server import MCPServer

    s = MCPServer("noasync")

    @s.tool()
    def add(a, b):
        return a + b

    assert "get_job_result" not in s._tools
    assert s._job_polling_registered is False


def test_openapi_omits_context_tools():
    """OpenAPI schema must not advertise context-only tools as REST endpoints."""
    status, body = _get("/openapi.json")
    assert status == 200
    paths = body.get("paths", {})
    assert "/tools/add" in paths
    assert "/tools/progress_tool" not in paths
    assert "/tools/ask_user" not in paths


# ---------------------------------------------------------------------------
# Server lifecycle (start once for all tests)
# ---------------------------------------------------------------------------

def start_server():
    """Run the server in a daemon thread."""
    server.run(host="127.0.0.1", port=PORT)


_server_thread = None


def setup_module():
    """Start the test server before any tests run."""
    global _server_thread
    _server_thread = threading.Thread(target=start_server, daemon=True)
    _server_thread.start()
    # Wait for server to be ready
    for _ in range(50):
        try:
            conn = HTTPConnection("127.0.0.1", PORT, timeout=1)
            conn.request("GET", "/")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("Test server did not start within 5 seconds")


# ---------------------------------------------------------------------------
# Task metadata at dispatch + real cancellation
# ---------------------------------------------------------------------------

def _tasks_server(prepare=None, body=None):
    """Build a server with one async tool and a Tasks-capable session."""
    from nanohubmcp.server import MCPServer

    s = MCPServer("tasksrv")
    s.async_tool(prepare=prepare)(body)
    s._set_session_capabilities(
        "sess-1", {"extensions": {"io.modelcontextprotocol/tasks": {}}}
    )
    return s


def _rpc(server, method, params, session_id="sess-1"):
    return server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        session_id=session_id,
    )["result"]


def test_prepare_hook_puts_metadata_on_the_initial_task_handle():
    """A durable handle minted in prepare must ride the CreateTaskResult _meta.

    Task itself has no _meta, but CreateTaskResult is `Result & Task`, so the
    envelope carries it. This is the only point a handle can reach the caller
    at dispatch — the tool body has not run yet.
    """
    def prepare(ctx=None):
        ctx.set_task_metadata(jobHandle="cluster-4711")

    def body(ctx=None):
        return "done"

    s = _tasks_server(prepare=prepare, body=body)
    result = _rpc(s, "tools/call", {"name": "body", "arguments": {}})

    assert result["resultType"] == "task"
    assert result["_meta"] == {"org.nanohub/jobHandle": "cluster-4711"}

    # tasks/get carries it too, for clients that only see the poll.
    got = _rpc(s, "tasks/get", {"taskId": result["taskId"]})
    assert got["_meta"] == {"org.nanohub/jobHandle": "cluster-4711"}


def test_prepare_failure_starts_no_job():
    """A raising prepare must not leave a phantom 'running' task behind."""
    def prepare(ctx=None):
        raise ValueError("submit refused")

    def body(ctx=None):
        return "never"

    s = _tasks_server(prepare=prepare, body=body)
    response = s._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "body", "arguments": {}}},
        session_id="sess-1",
    )

    assert "error" in response
    assert s._jobs == {}


def test_tasks_cancel_signals_the_worker_and_fires_callbacks():
    """tasks/cancel must stop the work, not just relabel the record."""
    fired = threading.Event()
    running = threading.Event()

    def body(ctx=None):
        ctx.on_cancel(fired.set)
        running.set()
        # Would be a solver wait; returns early only if cancellation lands.
        if ctx.cancel_event.wait(10):
            return {"status": "cancelled"}
        return {"status": "finished"}

    s = _tasks_server(body=body)
    task_id = _rpc(s, "tools/call", {"name": "body", "arguments": {}})["taskId"]
    assert running.wait(2), "worker never started"

    started = time.time()
    assert _rpc(s, "tasks/cancel", {"taskId": task_id}) == {"resultType": "complete"}
    assert fired.wait(2), "on_cancel callback never ran"

    deadline = time.time() + 3
    status = None
    while time.time() < deadline:
        status = _rpc(s, "tasks/get", {"taskId": task_id})["status"]
        if status == "cancelled":
            break
        time.sleep(0.02)

    assert status == "cancelled"
    # The tool asked to wait 10s; cancellation must cut that short.
    assert time.time() - started < 3


def test_on_cancel_after_cancellation_fires_immediately():
    """Registering a callback post-cancel must not silently drop it."""
    from nanohubmcp.server import MCPServer

    fired = []
    s = MCPServer("late")

    @s.async_tool()
    def worker(ctx=None):
        while not ctx.is_cancelled():
            time.sleep(0.01)
        ctx.on_cancel(lambda: fired.append(1))
        return "stopped"

    job_id = s._start_async_tool_job(s._tools["worker"]["handler"], 1, {})
    time.sleep(0.05)
    assert s.cancel_job(job_id) is True

    deadline = time.time() + 2
    while time.time() < deadline and not fired:
        time.sleep(0.01)
    assert fired == [1]


def test_cancelled_job_polls_as_cancelled_not_done():
    """A cancelled job must not report 'done' with a null result."""
    from nanohubmcp.server import MCPServer

    s = MCPServer("cancelpoll")

    @s.async_tool()
    def worker():
        return "x"

    with s._jobs_lock:
        s._jobs["jid"] = {"status": "cancelled", "result": None, "task_meta": {}}

    polled = s._tools["get_job_result"]["handler"](job_id="jid")
    assert polled["status"] == "cancelled"


def test_legacy_clients_get_task_metadata_in_the_body():
    """Non-Tasks clients never read _meta — give them the handles they can see."""
    from nanohubmcp.server import MCPServer

    s = MCPServer("legacy")

    def prepare(ctx=None):
        ctx.set_task_metadata(jobHandle="h-1")

    @s.async_tool(prepare=prepare)
    def worker(ctx=None):
        time.sleep(0.2)
        return "done"

    response = s._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "worker", "arguments": {}},
    })
    body = json.loads(response["result"]["content"][0]["text"])
    assert body["meta"] == {"org.nanohub/jobHandle": "h-1"}

    polled = s._tools["get_job_result"]["handler"](job_id=body["job_id"])
    assert polled["meta"] == {"org.nanohub/jobHandle": "h-1"}


def test_task_metadata_keys_are_namespaced_and_reserved_prefixes_rejected():
    """_meta keys must follow the MCP naming rules."""
    from nanohubmcp.server import MCPServer

    assert MCPServer._normalize_task_meta({"jobHandle": "h"}) == {
        "org.nanohub/jobHandle": "h"
    }
    # Second label 'example' is not reserved — passes through untouched.
    assert MCPServer._normalize_task_meta({"com.example.mcp/x": 1}) == {
        "com.example.mcp/x": 1
    }
    for reserved in ("io.modelcontextprotocol/x", "dev.mcp/x",
                     "org.modelcontextprotocol.api/x"):
        try:
            MCPServer._normalize_task_meta({reserved: 1})
            assert False, "expected {} to be rejected".format(reserved)
        except ValueError:
            pass


def test_is_cancelled_is_safe_in_sync_tools():
    """The same guard code must work outside a task without blowing up."""
    from nanohubmcp.server import MCPServer

    ctx = Context(server=MCPServer("sync"))
    assert ctx.is_cancelled() is False
    assert ctx.cancel_event.wait(0) is False
    assert ctx.task_metadata == {}
    ctx.on_cancel(lambda: None)   # no-op, must not raise


# ---------------------------------------------------------------------------
# subscriptions/listen + notifications/tasks
# ---------------------------------------------------------------------------

def _sse_probe(server, session_id):
    """Attach a queue standing in for a connected SSE client."""
    from nanohubmcp.server import _SSEQueue

    queue = _SSEQueue()
    with server._clients_lock:
        server._clients.setdefault(session_id, []).append(queue)
    return queue


def _messages(queue, method=None):
    out = [json.loads(m) for m in queue]
    if method is not None:
        out = [m for m in out if m.get("method") == method]
    return out


def test_listen_acknowledges_and_keeps_the_stream_open():
    """The listen response is deferred; the ack names the tasks agreed to."""
    def body(ctx=None):
        time.sleep(0.3)
        return "done"

    s = _tasks_server(body=body)
    probe = _sse_probe(s, "sess-1")
    task_id = _rpc(s, "tools/call", {"name": "body", "arguments": {}})["taskId"]

    response = s._handle_request(
        {"jsonrpc": "2.0", "id": "sub-7", "method": "subscriptions/listen",
         "params": {"notifications": {"taskIds": [task_id]}}},
        session_id="sess-1",
    )
    # A listen stream's result is only sent on graceful teardown.
    assert response is None

    acks = _messages(probe, "notifications/subscriptions/acknowledged")
    assert len(acks) == 1
    assert acks[0]["params"]["notifications"]["taskIds"] == [task_id]
    assert acks[0]["params"]["_meta"]["io.modelcontextprotocol/subscriptionId"] == "sub-7"


def test_task_notifications_are_pushed_and_polling_still_works():
    """Notifications supplement tasks/get; they do not replace it."""
    gate = threading.Event()

    def prepare(ctx=None):
        ctx.set_task_metadata(jobHandle="cluster-99")

    def body(ctx=None):
        gate.wait(5)
        return {"status": "finished"}

    s = _tasks_server(prepare=prepare, body=body)
    probe = _sse_probe(s, "sess-1")
    task_id = _rpc(s, "tools/call", {"name": "body", "arguments": {}})["taskId"]
    s._handle_request(
        {"jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen",
         "params": {"notifications": {"taskIds": [task_id]}}},
        session_id="sess-1",
    )

    gate.set()
    deadline = time.time() + 3
    pushed = []
    while time.time() < deadline:
        pushed = _messages(probe, "notifications/tasks")
        if pushed:
            break
        time.sleep(0.02)

    assert pushed, "no notifications/tasks delivered"
    params = pushed[-1]["params"]
    assert params["taskId"] == task_id
    assert params["status"] == "completed"
    # Carries the full DetailedTask, including the terminal payload...
    assert params["result"]["structuredContent"] == {"status": "finished"}
    # ...the subscription id, and the tool's own task metadata.
    assert params["_meta"]["io.modelcontextprotocol/subscriptionId"] == "sub-1"
    assert params["_meta"]["org.nanohub/jobHandle"] == "cluster-99"

    # Polling remains valid and returns the same terminal state.
    assert _rpc(s, "tasks/get", {"taskId": task_id})["status"] == "completed"


def test_no_task_notifications_without_a_subscription():
    """The spec forbids sending notification types the client didn't request."""
    def body(ctx=None):
        return "done"

    s = _tasks_server(body=body)
    probe = _sse_probe(s, "sess-1")
    _rpc(s, "tools/call", {"name": "body", "arguments": {}})

    time.sleep(0.3)
    assert _messages(probe, "notifications/tasks") == []


def test_listen_only_acknowledges_tasks_the_session_owns():
    """Unknown or foreign task ids are omitted from the acknowledgement."""
    def body(ctx=None):
        time.sleep(0.3)
        return "done"

    s = _tasks_server(body=body)
    probe = _sse_probe(s, "sess-1")
    mine = _rpc(s, "tools/call", {"name": "body", "arguments": {}})["taskId"]

    s._handle_request(
        {"jsonrpc": "2.0", "id": "sub-2", "method": "subscriptions/listen",
         "params": {"notifications": {"taskIds": [mine, "not-a-real-task"]}}},
        session_id="sess-1",
    )
    ack = _messages(probe, "notifications/subscriptions/acknowledged")[0]
    assert ack["params"]["notifications"]["taskIds"] == [mine]


def test_cancellation_is_pushed_to_subscribers():
    """A cancelled task notifies, so a subscriber need not poll to learn it."""
    running = threading.Event()

    def body(ctx=None):
        running.set()
        ctx.cancel_event.wait(10)
        return {"status": "cancelled"}

    s = _tasks_server(body=body)
    probe = _sse_probe(s, "sess-1")
    task_id = _rpc(s, "tools/call", {"name": "body", "arguments": {}})["taskId"]
    s._handle_request(
        {"jsonrpc": "2.0", "id": "sub-3", "method": "subscriptions/listen",
         "params": {"notifications": {"taskIds": [task_id]}}},
        session_id="sess-1",
    )
    assert running.wait(2)

    _rpc(s, "tasks/cancel", {"taskId": task_id})

    deadline = time.time() + 3
    statuses = []
    while time.time() < deadline:
        statuses = [m["params"]["status"] for m in _messages(probe, "notifications/tasks")]
        if "cancelled" in statuses:
            break
        time.sleep(0.02)
    assert "cancelled" in statuses


def test_listen_requires_a_notifications_filter():
    """A malformed listen is a -32602, not a silent subscription."""
    def body(ctx=None):
        return "done"

    s = _tasks_server(body=body)
    response = s._handle_request(
        {"jsonrpc": "2.0", "id": 9, "method": "subscriptions/listen", "params": {}},
        session_id="sess-1",
    )
    assert response["error"]["code"] == -32602


def test_subscriptions_dropped_when_the_session_disconnects():
    """Subscription state must not outlive the session that opened it."""
    def body(ctx=None):
        time.sleep(0.3)
        return "done"

    s = _tasks_server(body=body)
    probe = _sse_probe(s, "sess-1")
    task_id = _rpc(s, "tools/call", {"name": "body", "arguments": {}})["taskId"]
    s._handle_request(
        {"jsonrpc": "2.0", "id": "sub-4", "method": "subscriptions/listen",
         "params": {"notifications": {"taskIds": [task_id]}}},
        session_id="sess-1",
    )
    assert s._subscriptions.get("sess-1")

    s._unregister_client("sess-1", probe)
    assert "sess-1" not in s._subscriptions


# ---------------------------------------------------------------------------
# Tests - Streamable HTTP sessions are established by initialize, not by SSE
# ---------------------------------------------------------------------------


def _post_raw(path, data, headers=None):
    """POST arbitrary bytes and return (status, headers, parsed-or-raw body)."""
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("POST", path, body=data,
                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw
    return resp.status, dict(resp.getheaders()), body


def test_initialize_post_mints_a_session_id():
    """A POST-only Streamable HTTP client never opens an SSE stream first.

    The session -- and with it every capability the client negotiated -- must
    be created on the initialize POST and returned in Mcp-Session-Id.
    """
    status, headers, body = _post_raw("/mcp", json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "capabilities": {"elicitation": {},
                                    "extensions": {"io.modelcontextprotocol/tasks": {}}},
                   "clientInfo": {"name": "post-only", "version": "1"}}}).encode())
    assert status == 200, body
    assert "serverInfo" in body["result"]
    session_id = headers.get("Mcp-Session-Id")
    assert session_id, "initialize did not return Mcp-Session-Id"
    assert "Mcp-Session-Id" in headers.get("Access-Control-Expose-Headers", "")

    # The negotiated capabilities live under that session on the NEXT request.
    assert server._client_capabilities(session_id).get("elicitation") == {}
    status, _h, body = _post_raw("/mcp", json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tasks/get",
        "params": {"taskId": "no-such-task"}}).encode(),
        headers={"Mcp-Session-Id": session_id})
    error = body.get("error") or {}
    assert "required client capability" not in str(error.get("message", "")).lower(), body
    assert error.get("code") != -32003, body


def test_initialize_with_a_supplied_session_keeps_it():
    """A client that already has a session (from SSE) must not get a new one."""
    conn_sse, resp_sse, session_id = _open_mcp_session()
    try:
        status, headers, body = _post_raw("/mcp", json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "sse-first", "version": "1"}}}).encode(),
            headers={"Mcp-Session-Id": session_id})
        assert status == 200
        assert headers.get("Mcp-Session-Id") in (None, session_id)
    finally:
        conn_sse.close()


def test_non_initialize_post_without_session_mints_nothing():
    status, headers, body = _post_raw("/mcp", json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}).encode())
    assert status == 200
    assert headers.get("Mcp-Session-Id") is None


def test_malformed_json_is_a_jsonrpc_parse_error():
    """-32700 with a JSON body, never an HTML 400 a proxy will mangle."""
    status, headers, body = _post_raw("/mcp", b"{")
    assert isinstance(body, dict), body
    assert body["error"]["code"] == -32700
    assert body["id"] is None
    assert "application/json" in headers.get("Content-Type", "")


def test_preflight_allows_and_exposes_the_session_header():
    conn = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("OPTIONS", "/mcp")
    resp = conn.getresponse()
    headers = dict(resp.getheaders())
    conn.close()
    assert "Mcp-Session-Id" in headers.get("Access-Control-Allow-Headers", "")
    assert "Mcp-Session-Id" in headers.get("Access-Control-Expose-Headers", "")


# ---------------------------------------------------------------------------
# Dynamic registration
# ---------------------------------------------------------------------------

def _dyn_server():
    """A serving server with one import-time tool and two attached sessions."""
    from nanohubmcp.server import MCPServer, _SSEQueue

    s = MCPServer("dyn")

    @s.tool(input_schema={"type": "object", "properties": {}, "required": []})
    def alpha():
        """First tool, registered at import time."""
        return 1

    s._serving = True   # what run() sets

    modern, legacy = _SSEQueue(), _SSEQueue()
    with s._clients_lock:
        s._clients["M"] = [modern]
        s._clients["L"] = [legacy]
    # The legacy session negotiates a handshake-era revision.
    s._handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25",
                                  "capabilities": {}}}, session_id="L")
    return s, modern, legacy


def _methods(queue):
    return [json.loads(m).get("method") for m in queue]


def test_registering_a_tool_while_serving_notifies_both_client_generations():
    s, modern, legacy = _dyn_server()

    # A 2026-07-28 client must opt in; an older one cannot and does not.
    s._handle_request({
        "jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen",
        "params": {"notifications": {"toolsListChanged": True}, "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {}}},
    }, session_id="M")
    ack = [json.loads(m) for m in modern if "acknowledged" in m][-1]
    assert ack["params"]["notifications"]["toolsListChanged"] is True

    del modern[:]
    del legacy[:]

    @s.tool(input_schema={"type": "object", "properties": {}, "required": []})
    def beta():
        """Second tool, registered while serving."""
        return 2

    assert "notifications/tools/list_changed" in _methods(modern)
    assert "notifications/tools/list_changed" in _methods(legacy)
    # The subscriber's copy is tagged with the subscription it belongs to.
    tagged = json.loads(modern[0])["params"]["_meta"]
    assert tagged["io.modelcontextprotocol/subscriptionId"] == "sub-1"

    listed = s._handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                "params": {}}, session_id="L")["result"]
    assert [t["name"] for t in listed["tools"]] == ["alpha", "beta"]

    called = s._handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "beta", "arguments": {}}}, session_id="L")
    assert "error" not in called


def test_remove_tool_notifies_and_makes_it_uncallable():
    s, modern, legacy = _dyn_server()
    del legacy[:]

    assert s.remove_tool("alpha") is True
    assert "notifications/tools/list_changed" in _methods(legacy)

    gone = s._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "alpha", "arguments": {}}}, session_id="L")
    assert "error" in gone

    # Removing something absent is a no-op, not an error.
    assert s.remove_tool("alpha") is False


def test_list_changed_is_advertised_only_once_the_registry_is_dynamic():
    """A static server must not claim it will send notifications it never sends."""
    from nanohubmcp.server import MCPServer

    s = MCPServer("static")

    @s.tool()
    def only():
        """The one tool this server has."""
        return 1

    assert s._get_capabilities().to_dict()["tools"]["listChanged"] is False

    s._serving = True

    @s.tool()
    def later():
        """Registered after start-up, which makes the claim true."""
        return 2

    assert s._get_capabilities().to_dict()["tools"]["listChanged"] is True


def test_unsubscribed_modern_session_gets_no_list_changed():
    """2026-07-28 forbids sending a notification type the client didn't request."""
    from nanohubmcp.server import MCPServer, _SSEQueue

    s = MCPServer("quiet")
    s._serving = True
    probe = _SSEQueue()
    with s._clients_lock:
        s._clients["N"] = [probe]
    s._handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2026-07-28",
                                  "capabilities": {}}}, session_id="N")
    del probe[:]

    @s.tool()
    def added():
        """Registered with nobody subscribed."""
        return 1

    assert _methods(probe) == []


def test_registry_survives_concurrent_registration_and_listing():
    """tools/list must not trip over a registration on another thread."""
    from nanohubmcp.server import MCPServer

    s = MCPServer("race")
    errors = []

    def register():
        try:
            for i in range(60):
                fn = (lambda n: lambda: n)(i)
                fn.__name__ = "tool_{}".format(i)
                fn.__doc__ = "Dynamically registered tool number {}.".format(i)
                s.tool()(fn)
                s.remove_tool("tool_{}".format(i // 2))
        except Exception as exc:
            errors.append(exc)

    def lister():
        try:
            for _ in range(120):
                s._handle_request({"jsonrpc": "2.0", "id": 1,
                                   "method": "tools/list", "params": {}})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=register), threading.Thread(target=lister),
               threading.Thread(target=lister)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
