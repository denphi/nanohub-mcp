Running on nanoHUB
==================

nanohub-mcp is designed to work with nanoHUB's tool infrastructure. The ``start_mcp`` CLI command handles proxy configuration automatically.


CLI Usage
---------

.. code-block:: bash

   start_mcp --app start_mcp.py

With a conda environment:

.. code-block:: bash

   start_mcp --app start_mcp.py --python-env AIIDA

All options:

.. code-block:: text

   usage: start_mcp [-h] --app APP [--host HOST] [--port PORT]
                    [--python-env PYTHON_ENV] [--debug]

   Start an MCP server on nanoHUB or locally

   optional arguments:
     -h, --help            show this help message and exit
     --app APP, -a APP     Path to Python file defining 'server = MCPServer(...)'
     --host HOST           Host to bind to (default: 0.0.0.0)
     --port PORT, -p PORT  Port to listen on (default: 8000)
     --python-env PYTHON_ENV
                           Conda environment name (e.g., AIIDA, ALIGNN)
     --debug               Enable debug mode with verbose logging


How It Works
------------

When ``start_mcp`` runs on nanoHUB, it:

1. Detects the nanoHUB environment via ``SESSION`` and ``SESSIONDIR`` environment variables
2. Reads the session resources file to build the proxy URL
3. Searches for an available ``wrwroxy`` reverse proxy using the ``use`` command
4. Starts the MCP server and proxy

**With wrwroxy available:**

- MCP server listens on port **8001**
- wrwroxy listens on port **8000** and forwards to 8001
- wrwroxy strips the weber path prefix, so the MCP server sees clean paths

**Without wrwroxy:**

- MCP server listens directly on port **8000**
- The ``path_prefix`` is set to the weber path so the server strips it internally
- SSE transport works directly without a reverse proxy


Proxy URL Format
----------------

The nanoHUB weber proxy URL follows this pattern::

   https://proxy.nanohub.org/weber/{session}/{cookie}/{port}/

Where:

- ``{session}`` — nanoHUB session ID
- ``{cookie}`` — file transfer cookie
- ``{port}`` — file transfer port (mod 1000)

The proxy URL and SSE endpoint are printed at startup::

   Proxy URL : https://proxy.nanohub.org/weber/2806628/QFknvaEtyl5V31wR/13/
   MCP Server ready! Access it at: https://proxy.nanohub.org/weber/2806628/QFknvaEtyl5V31wR/13/
   SSE endpoint: https://proxy.nanohub.org/weber/2806628/QFknvaEtyl5V31wR/13/sse


Testing Through the Proxy
--------------------------

Replace the placeholder values with your actual session details:

.. code-block:: bash

   PROXY_URL="https://proxy.nanohub.org/weber/{session}/{cookie}/{port}"
   COOKIE="weber-auth-nanohub-org={session}%3A{auth_token}"

Server info:

.. code-block:: bash

   curl -b "$COOKIE" "$PROXY_URL/"

Initialize:

.. code-block:: bash

   curl -b "$COOKIE" -X POST "$PROXY_URL/" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

List tools:

.. code-block:: bash

   curl -b "$COOKIE" -X POST "$PROXY_URL/" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

Call a tool:

.. code-block:: bash

   curl -b "$COOKIE" -X POST "$PROXY_URL/" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add","arguments":{"a":2,"b":3}}}'

Direct tool call:

.. code-block:: bash

   curl -b "$COOKIE" -X POST "$PROXY_URL/tools/add" \
     -H "Content-Type: application/json" \
     -d '{"a": 7, "b": 3}'

Using the nanoHUB API
---------------------

Use the nanoHUB API when calling tools from outside a nanoHUB session. All endpoints require a Bearer token.

**Base URL:**

.. code-block:: text

   https://nanohub.org

**Recommended: Streamable HTTP (auto-session)**

.. code-block:: bash

   API_BASE="https://nanohub.org"
   TOOL="mcpdemo"
   TOKEN="your_token"

   # Get connection info (creates or reuses a session)
   curl -H "Authorization: Bearer $TOKEN" \
     "$API_BASE/api/mcp/$TOOL/mcp"

   # Initialize
   curl -H "Authorization: Bearer $TOKEN" -X POST "$API_BASE/api/mcp/$TOOL/mcp" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"example-client","version":"0.1.0"}}}'

   # List tools
   curl -H "Authorization: Bearer $TOKEN" -X POST "$API_BASE/api/mcp/$TOOL/mcp" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

   # Call a tool
   curl -H "Authorization: Bearer $TOKEN" -X POST "$API_BASE/api/mcp/$TOOL/mcp" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add","arguments":{"a":2,"b":3}}}'

**Direct JSON-RPC (session-aware)**

Use these endpoints if you want to pin or reuse a session id:

.. code-block:: text

   POST /api/mcp/{tool}           # creates a session if not provided
   POST /api/mcp/{tool}/{session} # uses an existing session

**Direct tool calls (REST/OpenAPI style)**

.. code-block:: bash

   # Call a tool directly
   curl -H "Authorization: Bearer $TOKEN" -X POST "$API_BASE/api/mcp/$TOOL/tools/add" \
     -H "Content-Type: application/json" \
     -d '{"a": 7, "b": 3}'

   # Fetch OpenAPI schema for the tool
   curl -H "Authorization: Bearer $TOKEN" \
     "$API_BASE/api/mcp/$TOOL/openapi.json"

**SSE connection info**

If you need SSE transport, the API will return the proxy endpoint and cookie you must use:

.. code-block:: bash

   curl -H "Authorization: Bearer $TOKEN" \
     "$API_BASE/api/mcp/$TOOL/sse"

The response includes ``sse_endpoint``, ``post_endpoint``, and ``cookie_header``. Connect to
``sse_endpoint`` with the cookie header and POST JSON-RPC to ``post_endpoint``.

**Revision selection**

Most endpoints accept an optional ``revision`` query parameter (``default``, ``dev``, ``test``,
or a specific version). Example:

.. code-block:: bash

   curl -H "Authorization: Bearer $TOKEN" \
     "$API_BASE/api/mcp/$TOOL/mcp?revision=dev"

**Reference client**

See ``App/nanohub_mcp_client.py`` for a complete stdio-to-nanoHUB MCP bridge. It uses:

.. code-block:: text

   NANOHUB_TOKEN=...   # API token (required)
   NANOHUB_API=...     # Base URL (default: https://nanohub.org)
   NANOHUB_TOOLS=...   # Comma-separated tool list (default: mcpdemo)


Local Development
-----------------

When running outside of nanoHUB (no ``SESSION`` environment variable), ``start_mcp`` runs the server directly without any proxy:

.. code-block:: bash

   start_mcp --app start_mcp.py --port 9000

This is equivalent to calling ``server.run(port=9000)`` directly in your script.
