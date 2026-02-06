API Reference
=============

MCPServer
---------

The main server class. Create an instance, register tools/resources/prompts with decorators, then call ``run()``.

.. code-block:: python

   from nanohubmcp import MCPServer

   server = MCPServer("my-server", version="1.0.0")
   server.run(host="0.0.0.0", port=8000, path_prefix="")

**Constructor parameters:**

.. list-table::
   :header-rows: 1
   :widths: 15 10 15 60

   * - Parameter
     - Type
     - Default
     - Description
   * - ``name``
     - str
     - required
     - Server name (appears in MCP discovery and server info)
   * - ``version``
     - str
     - ``"1.0.0"``
     - Server version

**``server.run()`` parameters:**

.. list-table::
   :header-rows: 1
   :widths: 15 10 15 60

   * - Parameter
     - Type
     - Default
     - Description
   * - ``host``
     - str
     - ``"0.0.0.0"``
     - Host to bind to
   * - ``port``
     - int
     - ``8000``
     - Port to listen on
   * - ``path_prefix``
     - str
     - ``""``
     - URL prefix for proxy environments (e.g. ``/weber/.../``)


@server.tool()
---------------

Register a function as an MCP tool. The function name becomes the tool name, and the docstring becomes the description. Parameters are auto-detected from the function signature.

.. code-block:: python

   @server.tool()
   def add(a, b):
       # type: (float, float) -> float
       """Add two numbers together."""
       return float(a) + float(b)

With explicit options:

.. code-block:: python

   @server.tool(name="custom_name", description="Custom description", tags={"math"})
   def my_func(a, b):
       return a + b

.. list-table::
   :header-rows: 1
   :widths: 15 10 15 60

   * - Parameter
     - Type
     - Default
     - Description
   * - ``name``
     - str
     - function name
     - Tool name
   * - ``description``
     - str
     - docstring
     - Tool description
   * - ``tags``
     - set
     - ``None``
     - Tags for categorization
   * - ``meta``
     - dict
     - ``None``
     - Metadata dictionary
   * - ``input_schema``
     - dict
     - auto-generated
     - JSON Schema for inputs


@server.resource()
-------------------

Register a function as an MCP resource.

.. code-block:: python

   @server.resource("config://calculator/settings")
   def get_settings():
       """Get calculator settings."""
       return {"precision": 10}

With MIME type:

.. code-block:: python

   @server.resource("data://samples/temperatures", mime_type="application/json")
   def temperature_data():
       """Monthly average temperatures."""
       return {"data": [2.1, 3.5, 7.2, 12.1]}

.. list-table::
   :header-rows: 1
   :widths: 15 10 15 60

   * - Parameter
     - Type
     - Default
     - Description
   * - ``uri``
     - str
     - required
     - Resource URI (e.g. ``config://settings``, ``file:///path``)
   * - ``name``
     - str
     - function name
     - Resource name
   * - ``description``
     - str
     - docstring
     - Resource description
   * - ``mime_type``
     - str
     - ``None``
     - MIME type of content
   * - ``tags``
     - set
     - ``None``
     - Tags for categorization
   * - ``meta``
     - dict
     - ``None``
     - Metadata dictionary


@server.prompt()
-----------------

Register a function as an MCP prompt template.

.. code-block:: python

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

.. list-table::
   :header-rows: 1
   :widths: 15 10 15 60

   * - Parameter
     - Type
     - Default
     - Description
   * - ``name``
     - str
     - function name
     - Prompt name
   * - ``description``
     - str
     - docstring
     - Prompt description
   * - ``tags``
     - set
     - ``None``
     - Tags for categorization
   * - ``meta``
     - dict
     - ``None``
     - Metadata dictionary


Context
-------

Tools can receive a ``Context`` object for logging and progress reporting. Add a ``ctx`` (or ``context``) parameter as the first argument:

.. code-block:: python

   from nanohubmcp import MCPServer, Context

   server = MCPServer("my-server")

   @server.tool()
   def power(ctx, base, exponent):
       # type: (Context, float, float) -> float
       """Raise base to the power of exponent."""
       ctx.info("Computing {}^{}".format(base, exponent))
       ctx.report_progress(0.5, total=1.0, message="Computing...")
       return float(base) ** float(exponent)

**Context methods:**

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Method
     - Description
   * - ``ctx.debug(msg)``
     - Log a debug-level message
   * - ``ctx.info(msg)``
     - Log an info-level message
   * - ``ctx.warning(msg)``
     - Log a warning-level message
   * - ``ctx.error(msg)``
     - Log an error-level message
   * - ``ctx.report_progress(progress, total=None, message=None)``
     - Report progress to connected clients


Return Types
------------

Tool handlers can return:

- **Scalar values** (``str``, ``int``, ``float``) — wrapped as ``{"content": [{"type": "text", "text": "..."}], "isError": false}``
- **Dictionaries** — JSON-serialized and wrapped as text content
- **``ToolResult``** — returned as-is for full control

Resource handlers can return:

- **Dictionaries** — JSON-serialized as resource content
- **Strings** — returned as text content
- **``ResourceResult``** — returned as-is for full control

Prompt handlers can return:

- **List of message dicts** — used directly as prompt messages
- **Strings** — wrapped in a user message
- **``PromptResult``** — returned as-is for full control

Raising an exception in a tool handler sets ``isError: true`` in the response.
