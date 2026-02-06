Quick Start
===========

Create a file called ``start_mcp.py``:

.. code-block:: python

   from nanohubmcp import MCPServer

   server = MCPServer("my-calculator", version="1.0.0")

   @server.tool()
   def add(a, b):
       # type: (float, float) -> float
       """Add two numbers together."""
       return float(a) + float(b)

   @server.tool()
   def multiply(a, b):
       # type: (float, float) -> float
       """Multiply two numbers."""
       return float(a) * float(b)

   @server.resource("config://settings")
   def get_settings():
       """Get application settings."""
       return {"precision": 10}

   @server.prompt()
   def calculate(expression):
       # type: (str) -> list
       """Generate a calculation prompt."""
       return [
           {
               "role": "user",
               "content": {"type": "text", "text": "Please calculate: {}".format(expression)}
           }
       ]

   if __name__ == "__main__":
       server.run(port=8000)

Run it:

.. code-block:: bash

   python start_mcp.py

The server starts and prints all available endpoints::

   MCP Server 'my-calculator' v1.0.0 listening on 0.0.0.0:8000
     Tools: 2
     Resources: 1
     Prompts: 1
   Endpoints:
     SSE transport:        http://0.0.0.0:8000/sse
     Streamable HTTP:      http://0.0.0.0:8000/mcp
     OpenAPI schema:       http://0.0.0.0:8000/openapi.json
     MCP discovery:        http://0.0.0.0:8000/.well-known/mcp.json
     Direct tool calls:    http://0.0.0.0:8000/tools/<name>

Verify it works:

.. code-block:: bash

   curl http://localhost:8000/

.. code-block:: json

   {
     "name": "my-calculator",
     "version": "1.0.0",
     "status": "running",
     "tools": 2,
     "resources": 1,
     "prompts": 1,
     "endpoints": {"sse": "/sse", "mcp": "/mcp", "openapi": "/openapi.json"}
   }
