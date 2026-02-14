Connecting MCP Clients
======================

Once your MCP server is running (locally or on nanoHUB), you can connect it to several AI services. All clients use the nanoHUB API endpoint — no direct access to the weber proxy is needed.

**Base URL for all examples:**

.. code-block:: text

   https://nanohub.org/api/mcp/{tool}/mcp

Replace ``{tool}`` with your tool name (e.g. ``mcpdemo``) and ``{token}`` with your nanoHUB Bearer token.


VS Code (GitHub Copilot)
------------------------

1. Open the Command Palette: ``Cmd+Shift+P`` (macOS) or ``Ctrl+Shift+P`` (Windows/Linux)
2. Run **"MCP: Open Workspace Folder MCP Configuration"**
3. This opens ``{workspace}/.vscode/mcp.json``. Add your server:

.. code-block:: json

   {
     "servers": {
       "nanohub-mcpdemo": {
         "type": "http",
         "url": "https://nanohub.org/api/mcp/mcpdemo/mcp",
         "headers": {
           "Authorization": "Bearer ${env:NANOHUB_TOKEN}"
         }
       }
     }
   }

4. Set the ``NANOHUB_TOKEN`` environment variable, or replace ``${env:NANOHUB_TOKEN}`` with your token directly.
5. Open the Copilot chat panel and select **Agent mode** — your tool's functions will appear in the tool list.


Antigravity
-----------

1. Click the **"..."** menu in the Antigravity interface
2. Select **MCP Servers**
3. Click **Manage MCP Servers**
4. Click **View raw config**
5. Add your server entry:

.. code-block:: json

   {
     "mcpServers": {
       "nanohub-mcpdemo": {
         "serverUrl": "https://nanohub.org/api/mcp/mcpdemo/mcp",
         "headers": {
           "Authorization": "Bearer {token}",
           "Content-Type": "application/json"
         }
       }
     }
   }

Antigravity sends a ``GET /mcp`` with ``Accept: text/event-stream`` to discover the endpoint, then POSTs all JSON-RPC messages.


OpenAI Codex
------------

1. Click the **gear icon** to open Settings
2. Go to **MCP Settings**
3. Click **Open MCP Settings**
4. Click **Add Server**
5. Select **Streamable HTTP** as the transport type
6. Enter the server URL: ``https://nanohub.org/api/mcp/mcpdemo/mcp``
7. For authentication, add a **Bearer token env var** — set an environment variable (e.g. ``NANOHUB_TOKEN``) containing your token, then reference it as the Bearer value

The resulting config entry looks like:

.. code-block:: json

   {
     "mcpServers": [
       {
         "name": "nanohub-mcpdemo",
         "url": "https://nanohub.org/api/mcp/mcpdemo/mcp",
         "headers": {
           "Authorization": "Bearer ${NANOHUB_TOKEN}"
         }
       }
     ]
   }


Claude Desktop
--------------

Claude Desktop only supports local stdio MCP servers. Use the ``nanohub_mcp_client.py`` bridge included in the repository, which proxies stdio to the nanoHUB HTTP API.

**macOS:** ``~/Library/Application Support/Claude/claude_desktop_config.json``

**Windows:** ``%APPDATA%\Claude\claude_desktop_config.json``

.. code-block:: json

   {
     "mcpServers": {
       "nanohub": {
         "command": "python3",
         "args": ["/path/to/nanohub_mcp_client.py"],
         "env": {
           "NANOHUB_TOKEN": "{token}",
           "NANOHUB_TOOLS": "mcpdemo"
         }
       }
     }
   }

Set ``NANOHUB_TOOLS`` to a comma-separated list to expose multiple tools:

.. code-block:: json

   "NANOHUB_TOOLS": "mcpdemo,rappture,another_tool"


Multiple Tools
--------------

HTTP clients (VS Code, Antigravity, Codex) connect to one tool per server entry. To expose multiple nanoHUB tools, add one entry per tool:

.. code-block:: json

   {
     "mcpServers": {
       "nanohub-mcpdemo": {
         "serverUrl": "https://nanohub.org/api/mcp/mcpdemo/mcp",
         "headers": { "Authorization": "Bearer {token}" }
       },
       "nanohub-rappture": {
         "serverUrl": "https://nanohub.org/api/mcp/rappture/mcp",
         "headers": { "Authorization": "Bearer {token}" }
       }
     }
   }


Local Development
-----------------

When running the MCP server locally (without nanoHUB), point clients directly at your local server:

.. code-block:: text

   http://localhost:8000/mcp

**VS Code** (via ``MCP: Open Workspace Folder MCP Configuration``):

.. code-block:: json

   {
     "servers": {
       "my-local-server": {
         "type": "http",
         "url": "http://localhost:8000/mcp"
       }
     }
   }

**Verify the server is reachable** before connecting a client:

.. code-block:: bash

   curl -s http://localhost:8000/mcp
   curl -s -X POST http://localhost:8000/mcp \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
