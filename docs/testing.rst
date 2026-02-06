Testing Your Server
===================

Using curl
----------

Once your server is running on ``http://localhost:8000``, you can test every endpoint with curl.

**Server info:**

.. code-block:: bash

   curl http://localhost:8000/

**Initialize (MCP handshake):**

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

**List tools:**

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

**Call a tool (JSON-RPC):**

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add","arguments":{"a":2,"b":3}}}'

**Call a tool (direct REST):**

.. code-block:: bash

   curl -X POST http://localhost:8000/tools/add \
     -H "Content-Type: application/json" \
     -d '{"a": 7, "b": 3}'

**List resources:**

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":4,"method":"resources/list","params":{}}'

**Read a resource:**

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":5,"method":"resources/read","params":{"uri":"config://settings"}}'

**List prompts:**

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":6,"method":"prompts/list","params":{}}'

**Get a prompt:**

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":7,"method":"prompts/get","params":{"name":"calculate","arguments":{"expression":"2+2"}}}'

**Ping:**

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":8,"method":"ping","params":{}}'

**SSE stream (keep open in a separate terminal):**

.. code-block:: bash

   curl -N http://localhost:8000/sse

**OpenAPI schema:**

.. code-block:: bash

   curl http://localhost:8000/openapi.json

**MCP discovery:**

.. code-block:: bash

   curl http://localhost:8000/.well-known/mcp.json


Python Test Client
------------------

A minimal Python client using only the standard library:

.. code-block:: python

   import json
   try:
       from http.client import HTTPConnection
   except ImportError:
       from httplib import HTTPConnection

   conn = HTTPConnection("localhost", 8000, timeout=5)

   def call(method, params=None):
       """Send a JSON-RPC request and return the parsed response."""
       body = json.dumps({
           "jsonrpc": "2.0",
           "id": 1,
           "method": method,
           "params": params or {}
       }).encode("utf-8")
       conn.request("POST", "/", body=body,
                    headers={"Content-Type": "application/json"})
       resp = conn.getresponse()
       return json.loads(resp.read().decode("utf-8"))

   # Initialize
   result = call("initialize")
   print("Protocol:", result["result"]["protocolVersion"])
   print("Server:", result["result"]["serverInfo"]["name"])

   # List tools
   tools = call("tools/list")
   for t in tools["result"]["tools"]:
       print("Tool:", t["name"], "-", t.get("description", ""))

   # Call a tool
   result = call("tools/call", {"name": "add", "arguments": {"a": 10, "b": 20}})
   print("add(10, 20) =", result["result"]["content"][0]["text"])

   # List resources
   resources = call("resources/list")
   for r in resources["result"]["resources"]:
       print("Resource:", r["uri"])

   # Read a resource
   result = call("resources/read", {"uri": "config://settings"})
   print("Settings:", result["result"]["contents"][0]["text"])

   # List prompts
   prompts = call("prompts/list")
   for p in prompts["result"]["prompts"]:
       print("Prompt:", p["name"])

   # Get a prompt
   result = call("prompts/get", {"name": "calculate", "arguments": {"expression": "2+2"}})
   print("Prompt messages:", result["result"]["messages"])

   conn.close()


Running the Test Suite
----------------------

The project includes an integration test suite that starts a server in a background thread and exercises all 22 endpoints.

.. code-block:: bash

   pip install pytest
   pytest tests/

Expected output::

   tests/test_mcp_server.py ...................... [100%]
   22 passed

The test suite covers:

- Server info (``GET /``)
- SSE transport (``GET /sse``)
- Streamable HTTP (``GET /mcp``)
- JSON-RPC methods: ``initialize``, ``tools/list``, ``tools/call``, ``resources/list``, ``resources/read``, ``prompts/list``, ``prompts/get``, ``ping``
- Error handling: divide by zero, unknown methods, unknown tools
- Notifications (requests without ``id``)
- Direct tool calls (``POST /tools/<name>``)
- OpenAPI schema (``GET /openapi.json``)
- MCP discovery (``GET /.well-known/mcp.json``)
- SSE broadcast (verifying SSE clients receive POST responses)
