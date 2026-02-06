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

from nanohubmcp import MCPServer, Context


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


# ---------------------------------------------------------------------------
# Tests - Server info
# ---------------------------------------------------------------------------


def test_server_info():
    """GET / returns server info JSON with endpoints."""
    status, body = _get("/")
    assert status == 200
    assert body["name"] == "test-calculator"
    assert body["status"] == "running"
    assert body["tools"] == 2
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
    """GET /mcp returns an SSE stream with an endpoint event."""
    status, content_type, lines = _read_sse("/mcp")
    assert status == 200
    assert "text/event-stream" in content_type
    assert "event: endpoint" in lines[0]
    assert "data: /mcp" in lines[1]


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


def test_tools_list():
    """POST tools/list returns registered tools."""
    status, body = _post("/", {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}})
    assert status == 200
    assert body["id"] == 10
    tool_names = [t["name"] for t in body["result"]["tools"]]
    assert "add" in tool_names
    assert "divide" in tool_names


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
    """POST /mcp also handles JSON-RPC requests."""
    status, body = _post("/mcp", {"jsonrpc": "2.0", "id": 100, "method": "initialize", "params": {}})
    assert status == 200
    assert body["id"] == 100
    assert "protocolVersion" in body["result"]


def test_mcp_post_tools_call():
    """POST /mcp handles tool calls."""
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
    assert "30" in body["result"]["content"][0]["text"]


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


def test_sse_receives_broadcast():
    """SSE stream receives broadcast when POST is made."""
    conn_sse = HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn_sse.request("GET", "/sse")
    resp_sse = conn_sse.getresponse()
    # Read the open event
    resp_sse.readline()  # event: open
    resp_sse.readline()  # data: {}
    resp_sse.readline()  # empty line

    # Send a request
    _post("/", {"jsonrpc": "2.0", "id": 200, "method": "ping", "params": {}})

    # Read broadcast from SSE
    event_line = resp_sse.readline().decode("utf-8").strip()
    data_line = resp_sse.readline().decode("utf-8").strip()
    conn_sse.close()

    assert event_line == "event: message"
    assert data_line.startswith("data: ")
    response = json.loads(data_line[6:])
    assert response["id"] == 200


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
