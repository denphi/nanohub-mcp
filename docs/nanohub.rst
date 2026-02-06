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


Local Development
-----------------

When running outside of nanoHUB (no ``SESSION`` environment variable), ``start_mcp`` runs the server directly without any proxy:

.. code-block:: bash

   start_mcp --app start_mcp.py --port 9000

This is equivalent to calling ``server.run(port=9000)`` directly in your script.
