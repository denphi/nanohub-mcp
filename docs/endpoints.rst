Server Endpoints
================

The MCP server exposes the following HTTP endpoints:

.. list-table::
   :header-rows: 1
   :widths: 25 10 65

   * - Endpoint
     - Method
     - Description
   * - ``/``
     - GET
     - Server info (name, version, tool/resource/prompt counts)
   * - ``/sse``
     - GET
     - SSE transport — streams responses as Server-Sent Events
   * - ``/mcp``
     - GET
     - Streamable HTTP — SSE stream with ``endpoint`` event
   * - ``/mcp``
     - POST
     - Streamable HTTP — accepts JSON-RPC requests
   * - ``/``
     - POST
     - JSON-RPC endpoint (same behavior as ``POST /mcp``)
   * - ``/tools/<name>``
     - POST
     - Direct REST-style tool call (OpenAPI compatible)
   * - ``/openapi.json``
     - GET
     - Auto-generated OpenAPI 3.1 schema
   * - ``/.well-known/mcp.json``
     - GET
     - MCP discovery document


SSE Transport (``/sse``)
------------------------

The SSE endpoint provides a persistent connection for receiving server responses as Server-Sent Events.

1. Connect to ``GET /sse`` — the server sends an ``open`` event
2. Send JSON-RPC requests via ``POST /`` or ``POST /mcp``
3. Responses are both returned synchronously to the POST and broadcast to all SSE clients

.. code-block:: text

   event: open
   data: {}

   event: message
   data: {"jsonrpc":"2.0","id":1,"result":{}}


Streamable HTTP Transport (``/mcp``)
-------------------------------------

The Streamable HTTP endpoint combines SSE and JSON-RPC in one path:

- **GET /mcp** returns an SSE stream starting with an ``endpoint`` event
- **POST /mcp** accepts JSON-RPC requests and returns synchronous responses

.. code-block:: text

   event: endpoint
   data: /mcp


JSON-RPC Methods
----------------

All JSON-RPC requests use the standard format:

.. code-block:: json

   {
     "jsonrpc": "2.0",
     "id": 1,
     "method": "method/name",
     "params": {}
   }

Supported methods:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Method
     - Description
   * - ``initialize``
     - Initialize connection, returns protocol version and server capabilities
   * - ``ping``
     - Health check, returns ``{}``
   * - ``tools/list``
     - List all registered tools with their input schemas
   * - ``tools/call``
     - Call a tool by name with arguments
   * - ``resources/list``
     - List all registered resources
   * - ``resources/read``
     - Read a resource by URI
   * - ``prompts/list``
     - List all registered prompts with their arguments
   * - ``prompts/get``
     - Get a prompt by name with arguments


Notifications
^^^^^^^^^^^^^

JSON-RPC requests without an ``id`` field are treated as notifications. They receive a ``202 Accepted`` response:

.. code-block:: bash

   curl -X POST http://localhost:8000/ \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","method":"initialized","params":{}}'

.. code-block:: json

   {"status": "accepted"}


Direct Tool Calls (``/tools/<name>``)
--------------------------------------

Tools can be called directly via REST-style POST requests without JSON-RPC wrapping. This is compatible with OpenAPI clients.

.. code-block:: bash

   curl -X POST http://localhost:8000/tools/add \
     -H "Content-Type: application/json" \
     -d '{"a": 7, "b": 3}'

.. code-block:: json

   {"result": "10.0"}

Errors return HTTP 500:

.. code-block:: json

   {"error": "Cannot divide by zero"}

Unknown tools return HTTP 404.


OpenAPI Schema (``/openapi.json``)
-----------------------------------

The server auto-generates an OpenAPI 3.1 schema from registered tools. Each tool is exposed as a ``POST /tools/<name>`` endpoint with the tool's input schema as the request body.

.. code-block:: bash

   curl http://localhost:8000/openapi.json

Example response:

.. code-block:: json

   {
     "openapi": "3.1.0",
     "info": {"title": "my-calculator", "version": "1.0.0"},
     "paths": {
       "/mcp": {
         "get": {"operationId": "mcp_sse", "summary": "MCP Streamable HTTP SSE endpoint"},
         "post": {"operationId": "mcp_message", "summary": "Send MCP JSON-RPC message"}
       },
       "/tools/add": {
         "post": {
           "operationId": "add",
           "summary": "Add two numbers together.",
           "requestBody": {
             "content": {"application/json": {"schema": {"type": "object", "properties": {"a": {}, "b": {}}}}}
           }
         }
       }
     }
   }


MCP Discovery (``/.well-known/mcp.json``)
-------------------------------------------

Returns a standard MCP discovery document listing available transports:

.. code-block:: bash

   curl http://localhost:8000/.well-known/mcp.json

.. code-block:: json

   {
     "mcpVersion": "2024-11-05",
     "serverInfo": {"name": "my-calculator", "version": "1.0.0"},
     "capabilities": {"tools": {}, "resources": {}, "prompts": {}, "logging": {}},
     "transports": [
       {"type": "sse", "endpoint": "/sse"},
       {"type": "streamable-http", "endpoint": "/mcp"}
     ]
   }


Error Handling
--------------

**Tool errors** (exceptions raised in tool handlers) return ``isError: true`` in the MCP response:

.. code-block:: json

   {
     "jsonrpc": "2.0",
     "id": 1,
     "result": {
       "content": [{"type": "text", "text": "Cannot divide by zero"}],
       "isError": true
     }
   }

**Unknown methods** return a JSON-RPC error:

.. code-block:: json

   {
     "jsonrpc": "2.0",
     "id": 1,
     "error": {"code": -32601, "message": "Method not found: unknown/method"}
   }
