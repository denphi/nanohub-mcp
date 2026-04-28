"""
Integration tests for the example servers under ``examples/``.

Each test:
  1. Starts the example's ``start_mcp.py`` as a subprocess on a high port.
  2. Waits for the HTTP endpoint to become reachable.
  3. Drives the server with real HTTP/JSON-RPC traffic, asserting expected
     behavior (tool calls, resources, prompts, isError, async polling).
  4. Streams the subprocess's stdout/stderr to test stdout so the full
     server-side interaction log is visible (use ``pytest -s`` to see it).
  5. Kills the subprocess on teardown — even if assertions fail.

Run a single example's tests with:

    python3 -m pytest tests/test_examples.py::test_simple_example -s
"""

from __future__ import print_function

import json
import os
import subprocess
import sys
import threading
import time

try:
    from http.client import HTTPConnection
except ImportError:  # pragma: no cover - py2 fallback
    from httplib import HTTPConnection


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES_DIR = os.path.join(REPO_ROOT, "examples")


# ---------------------------------------------------------------------------
# Subprocess + HTTP helpers
# ---------------------------------------------------------------------------


class ExampleServer(object):
    """Spawn an example MCP server, capture its logs, kill it cleanly."""

    def __init__(self, example_name, port):
        self.example_name = example_name
        self.port = port
        self.script = os.path.join(EXAMPLES_DIR, example_name, "start_mcp.py")
        self.proc = None
        self._reader = None

    def __enter__(self):
        env = os.environ.copy()
        # Make the in-tree package importable from the example
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"

        print("\n[{}] launching: {} {}".format(self.example_name, self.script, self.port))
        self.proc = subprocess.Popen(
            [sys.executable, self.script, str(self.port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=os.path.dirname(self.script),
        )

        # Stream child output to test stdout in a background thread so the log
        # interleaves with HTTP request/response logs from this side.
        def _pump():
            assert self.proc.stdout is not None
            for line in iter(self.proc.stdout.readline, b""):
                sys.stdout.write("[{}] {}".format(
                    self.example_name, line.decode("utf-8", errors="replace")
                ))
                sys.stdout.flush()

        self._reader = threading.Thread(target=_pump)
        self._reader.daemon = True
        self._reader.start()

        # Wait for the server to start listening
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Example server exited early (rc={})".format(
                    self.proc.returncode
                ))
            try:
                conn = HTTPConnection("127.0.0.1", self.port, timeout=1)
                conn.request("GET", "/")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    print("[{}] server ready on :{}".format(self.example_name, self.port))
                    return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("Timed out waiting for example server on :{}".format(self.port))

    def __exit__(self, exc_type, exc, tb):
        if self.proc and self.proc.poll() is None:
            print("[{}] terminating pid={}".format(self.example_name, self.proc.pid))
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("[{}] terminate timed out — killing".format(self.example_name))
                self.proc.kill()
                self.proc.wait()
        if self._reader:
            self._reader.join(timeout=2)
        if self.proc:
            print("[{}] exit rc={}".format(self.example_name, self.proc.returncode))
        return False  # don't swallow exceptions

    # ---- HTTP helpers --------------------------------------------------

    def post(self, path, body):
        """POST a JSON body and return (status, parsed_response).

        Logs the outgoing request and incoming reply so test output shows
        the full client/server interaction.
        """
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode("utf-8")
        method = body.get("method", "<no-method>") if isinstance(body, dict) else "<raw>"
        request_text = json.dumps(body, indent=2) if isinstance(body, dict) else body
        print("[{}] -> POST {} {}\n{}".format(
            self.example_name, path, method, request_text
        ))
        conn.request("POST", path, body=data,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        response_text = json.dumps(parsed, indent=2) if isinstance(parsed, (dict, list)) else parsed
        print("[{}] <- {}\n{}".format(self.example_name, resp.status, response_text))
        return resp.status, parsed

    def call_tool(self, name, arguments=None, request_id=1, _meta=None):
        params = {"name": name, "arguments": arguments or {}}
        if _meta is not None:
            params["_meta"] = _meta
        return self.post("/", {
            "jsonrpc": "2.0", "id": request_id,
            "method": "tools/call", "params": params,
        })

    def list_tools(self):
        return self.post("/", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    def read_resource(self, uri):
        return self.post("/", {
            "jsonrpc": "2.0", "id": 2,
            "method": "resources/read", "params": {"uri": uri},
        })

    def get_prompt(self, name, arguments):
        return self.post("/", {
            "jsonrpc": "2.0", "id": 3,
            "method": "prompts/get",
            "params": {"name": name, "arguments": arguments},
        })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _tool_text(response):
    """Pull the first text-content item out of a tools/call response."""
    return response["result"]["content"][0]["text"]


def test_simple_example():
    """Exercise tools, resource, prompt, ToolResult error, and async polling."""
    with ExampleServer("simple", port=18801) as srv:
        # tools/list — verify the expected tools registered
        status, body = srv.list_tools()
        assert status == 200
        names = {t["name"] for t in body["result"]["tools"]}
        for required in ("add", "subtract", "multiply", "divide", "power",
                         "factorial_with_progress", "slow_sum", "get_job_result"):
            assert required in names, "missing tool: {}".format(required)

        # add: plain numeric tool
        status, body = srv.call_tool("add", {"a": 2, "b": 3}, request_id=10)
        assert status == 200 and body["result"]["isError"] is False
        assert _tool_text(body) == "5.0"

        # divide: ToolResult success path
        status, body = srv.call_tool("divide", {"a": 10, "b": 4}, request_id=11)
        assert status == 200 and body["result"]["isError"] is False
        assert _tool_text(body) == "2.5"

        # divide: ToolResult error path → isError True with message
        status, body = srv.call_tool("divide", {"a": 1, "b": 0}, request_id=12)
        assert status == 200
        assert body["result"]["isError"] is True
        assert "zero" in _tool_text(body).lower()

        # power: context-using tool still works (no progressToken needed)
        status, body = srv.call_tool("power", {"base": 2, "exponent": 10}, request_id=13)
        assert status == 200 and body["result"]["isError"] is False
        assert _tool_text(body) == "1024.0"

        # async tool: should return a job_id wrapper, then resolve via polling
        status, body = srv.call_tool(
            "slow_sum", {"values": "1,2,3,4", "delay": 0.1}, request_id=14
        )
        assert status == 200
        wrapper = json.loads(_tool_text(body))
        assert wrapper["status"] == "running"
        job_id = wrapper["job_id"]

        # Poll until done (allow up to 3s)
        deadline = time.time() + 3
        final = None
        while time.time() < deadline:
            status, body = srv.call_tool(
                "get_job_result", {"job_id": job_id}, request_id=15
            )
            payload = json.loads(_tool_text(body))
            if payload["status"] != "running":
                final = payload
                break
            time.sleep(0.1)
        assert final is not None and final["status"] == "done"
        assert float(final["result"]) == 10.0

        # resource
        status, body = srv.read_resource("config://calculator/settings")
        assert status == 200
        contents = body["result"]["contents"][0]["text"]
        assert "precision" in contents

        # prompt
        status, body = srv.get_prompt("calculate", {"expression": "1+1"})
        assert status == 200
        msgs = body["result"]["messages"]
        assert msgs[0]["content"]["text"] == "Please calculate: 1+1"


def test_data_analysis_example():
    """Statistical tools and ToolResult-based input validation."""
    with ExampleServer("data_analysis", port=18802) as srv:
        # descriptive_stats: happy path
        status, body = srv.call_tool(
            "descriptive_stats", {"data": "1,2,3,4,5"}, request_id=20
        )
        assert status == 200 and body["result"]["isError"] is False
        stats = json.loads(_tool_text(body))
        assert stats["count"] == 5
        assert stats["mean"] == 3.0

        # descriptive_stats: empty input → ToolResult.isError
        status, body = srv.call_tool("descriptive_stats", {"data": ""}, request_id=21)
        assert status == 200 and body["result"]["isError"] is True
        assert "empty" in _tool_text(body).lower()

        # descriptive_stats: malformed input → parse error via ToolResult
        status, body = srv.call_tool(
            "descriptive_stats", {"data": "1,not-a-number,3"}, request_id=22
        )
        assert status == 200 and body["result"]["isError"] is True

        # correlation: mismatched lengths
        status, body = srv.call_tool(
            "correlation", {"x_data": "1,2,3", "y_data": "1,2"}, request_id=23
        )
        assert status == 200 and body["result"]["isError"] is True
        assert "length" in _tool_text(body).lower()

        # linear_regression: happy path
        status, body = srv.call_tool(
            "linear_regression",
            {"x_data": "1,2,3,4", "y_data": "2,4,6,8"},
            request_id=24,
        )
        assert status == 200 and body["result"]["isError"] is False
        reg = json.loads(_tool_text(body))
        assert abs(reg["slope"] - 2.0) < 1e-6
        assert abs(reg["intercept"]) < 1e-6
        assert abs(reg["r_squared"] - 1.0) < 1e-6

        # normalize: unknown method → ToolResult.isError
        status, body = srv.call_tool(
            "normalize", {"data": "1,2,3", "method": "bogus"}, request_id=25
        )
        assert status == 200 and body["result"]["isError"] is True
        assert "unknown method" in _tool_text(body).lower()

        # resource
        status, body = srv.read_resource("data://samples/temperatures")
        assert status == 200
        contents = json.loads(body["result"]["contents"][0]["text"])
        assert "data" in contents and len(contents["data"]) == 12


def test_simulator_example():
    """Physics tools, including ToolResult error path and a context tool."""
    with ExampleServer("simulator", port=18803) as srv:
        # projectile_motion: happy path
        status, body = srv.call_tool(
            "projectile_motion", {"v0": 20, "angle": 45}, request_id=30
        )
        assert status == 200 and body["result"]["isError"] is False
        traj = json.loads(_tool_text(body))
        assert traj["range"] > 0 and traj["max_height"] > 0
        assert len(traj["trajectory"]) == 21

        # ideal_gas: missing variable count → ToolResult error
        status, body = srv.call_tool(
            "ideal_gas", {"pressure": 101325, "volume": 0.022}, request_id=31
        )
        assert status == 200 and body["result"]["isError"] is True
        assert "exactly 3" in _tool_text(body)

        # relativistic_energy: validation error via ToolResult
        status, body = srv.call_tool(
            "relativistic_energy",
            {"rest_mass": 1.0, "velocity": 4e8},  # > c
            request_id=32,
        )
        assert status == 200 and body["result"]["isError"] is True
        assert "speed of light" in _tool_text(body).lower()

        # relativistic_energy: happy path (uses Context internally)
        status, body = srv.call_tool(
            "relativistic_energy",
            {"rest_mass": 9.1093837015e-31, "velocity": 1e8},
            request_id=33,
        )
        assert status == 200 and body["result"]["isError"] is False
        out = json.loads(_tool_text(body))
        assert out["lorentz_factor"] > 1.0

        # resource: physical constants
        status, body = srv.read_resource("constants://physics")
        assert status == 200
        constants = json.loads(body["result"]["contents"][0]["text"])
        assert constants["speed_of_light"]["value"] == 299792458
