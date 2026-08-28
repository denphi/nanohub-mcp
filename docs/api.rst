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
   * - ``output_schema``
     - dict
     - ``None``
     - JSON Schema describing the dict the tool returns. When set, it is emitted as ``outputSchema`` in ``tools/list``.
   * - ``annotations``
     - dict
     - ``None``
     - MCP ``ToolAnnotations`` hints emitted in ``tools/list``: ``readOnlyHint``, ``destructiveHint``, ``idempotentHint``, ``openWorldHint`` (bool) and ``title`` (str). Unknown keys or wrong value types raise ``ValueError`` at decoration time. Per the MCP spec these are hints, not guarantees — clients use them for result caching, retry policy, and app-widget tool gating.

.. note::

   When a tool returns a ``dict``, the server also includes it as
   ``structuredContent`` in the call result (per the MCP spec), in addition to
   the JSON-encoded ``text`` content. This lets clients and ``mcp-apps``
   widgets consume the data without re-parsing the text block.


Dynamic registration
--------------------

Tools, resources, and prompts may be registered *after* the server starts. The
decorators are the same ones used at import time — calling one later also
notifies connected clients:

.. code-block:: python

   @server.tool(input_schema=...)          # at import: just registers
   def alpha():
       ...

   # ... later, while serving:
   @server.tool(input_schema=...)          # also emits list_changed
   def beta():
       ...

   server.remove_tool("beta")              # likewise
   server.remove_resource("config://x")
   server.remove_prompt("summarize")

Each ``remove_*`` returns ``True`` when something was removed and ``False``
when the name was already absent. Removal affects only later lookups: a call
already in flight holds its handler and completes normally.

**Both client generations are served.** A client on ``2026-07-28`` must opt in
through ``subscriptions/listen`` (``toolsListChanged``, ``resourcesListChanged``,
``promptsListChanged``) and receives notifications tagged with its subscription
id; a session on an earlier revision has no way to opt in, so it receives them
because the server advertised ``listChanged``. A ``2026-07-28`` session that did
not subscribe receives nothing, as that revision requires.

``listChanged`` is advertised as ``false`` until the first registration or
removal happens while serving — a static server must not claim it will send a
notification it never sends.

Registration and listing are guarded by a re-entrant lock, so a background
thread may register while requests are being served. Single-key lookups on the
hot path stay lock-free.

@server.async_tool()
---------------------

Register a long-running function as an async MCP tool. When called, the server returns a ``job_id`` immediately (within milliseconds) instead of blocking the HTTP connection. The actual work runs in a background thread. The client polls for the result using the built-in ``get_job_result`` tool.

Use this for any tool that may exceed the reverse proxy timeout (typically 30–60 seconds).

.. code-block:: python

   from nanohubmcp import MCPServer

   server = MCPServer("my-server")

   @server.async_tool()
   def run_simulation(verilog_code, design_name):
       # type: (str, str) -> str
       """Run a long RTL-to-GDSII flow. Can take up to 10 minutes."""
       # ... long-running work ...
       return result

**Cancellation.** ``tasks/cancel`` sets a per-job cancel event and fires any
callbacks the handler registered, so cancelling a task stops the work instead of
just relabelling the record. Cancellation is cooperative — the MCP Tasks
extension lets a cancelled task still reach a non-``cancelled`` terminal state —
so the handler decides how promptly to stop:

.. code-block:: python

   @server.async_tool()
   def run_simulation(deck, ctx=None):
       proc = subprocess.Popen(argv, start_new_session=True)
       ctx.on_cancel(lambda: os.killpg(proc.pid, signal.SIGTERM))
       while proc.poll() is None:
           if ctx.cancel_event.wait(0.5):   # returns True the moment cancel lands
               break
       return {"status": "cancelled" if ctx.is_cancelled() else "finished"}

``ctx.on_cancel`` callbacks run on the thread handling ``tasks/cancel``, so keep
them short. A callback registered after cancellation already happened fires
immediately rather than being dropped. ``server.cancel_job(job_id)`` exposes the
same path, so you can offer cancellation to legacy clients by registering your
own tool that calls it under whatever authorization you require.

**Task metadata at dispatch.** To hand the caller a durable identifier — a
scheduler job id, a run handle — before the work finishes, use a ``prepare``
hook. It runs synchronously *before* the worker thread starts, so what it
publishes is already on the task handle the call returns:

.. code-block:: python

   def _submit(deck, ctx=None):
       ctx.set_task_metadata(jobHandle=cluster.submit(deck))

   @server.async_tool(prepare=_submit)
   def run_simulation(deck, ctx=None):
       return cluster.wait(ctx.task_metadata["org.nanohub/jobHandle"])

The values appear as ``_meta`` on the initial task handle and on every
``tasks/get`` response. (The Tasks ``Task`` object has no ``_meta`` of its own,
but ``CreateTaskResult`` is ``Result & Task``, so the response envelope carries
it.) Unqualified keys are namespaced with ``org.nanohub/``; MCP-reserved
prefixes raise ``ValueError``. Legacy clients, which never read ``_meta``, get
the same values under ``meta`` in the JSON body and in ``get_job_result``.

A ``prepare`` hook that raises aborts the call and starts no job.

**Status notifications (opt-in).** Clients that would rather not poll can
subscribe with ``subscriptions/listen``, naming the task ids they care about:

.. code-block:: json

   {"jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen",
    "params": {"notifications": {"taskIds": ["<taskId>"]}}}

The server acknowledges with ``notifications/subscriptions/acknowledged`` —
listing only the tasks it agreed to, which are those that exist and belong to
the calling session — and then pushes ``notifications/tasks`` on every status
change, carrying the complete task (terminal result included) plus the
subscription id. Because the listen stream is long-lived, the request itself
gets no immediate response; the transport replies ``202`` and the stream stays
open.

Notifications **supplement** polling, they do not replace it: the extension says
servers MAY push updates *in addition to* servicing ``tasks/get``, and clients
MAY keep polling. Sending is strictly opt-in — a session that never subscribes
receives nothing, per the rule that a server MUST NOT send a notification type
the client did not request.

.. note::

   ``subscriptions/listen`` is implemented in the shape defined by the current
   MCP draft while the server negotiates ``2026-07-28`` and earlier. Clients
   that do not know the method simply never call it and keep polling.

.. note::

   **Routing headers.** ``Mcp-Method`` and ``Mcp-Name`` are required by default
   of requests that declare ``2026-07-28`` — the revision that requires them —
   and never of earlier revisions, which never defined them. A contradicting
   header is rejected at any revision. Enforcement applies only to transports
   that carry headers; a direct call, SSE, or stdio is unaffected. Override with
   ``start_mcp --require-route-headers`` (all revisions) or
   ``--allow-missing-route-headers`` (never), or ``run(require_route_headers=…)``.

Calling ``run_simulation`` returns immediately:

.. code-block:: json

   {
     "status": "running",
     "job_id": "550e8400-e29b-41d4-a716-446655440000",
     "message": "Job started. Poll with get_job_result(job_id=\"...\")"
   }

The client then polls with the built-in ``get_job_result`` tool until complete:

.. code-block:: json

   { "status": "done", "job_id": "...", "result": "..." }

Or on failure:

.. code-block:: json

   { "status": "error", "job_id": "...", "error": "..." }

The decorator accepts the same parameters as ``@server.tool()``:

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
   * - ``output_schema``
     - dict
     - ``None``
     - JSON Schema describing the dict the tool returns. Emitted as ``outputSchema`` in ``tools/list``.
   * - ``annotations``
     - dict
     - ``None``
     - MCP ``ToolAnnotations`` hints emitted in ``tools/list``: ``readOnlyHint``, ``destructiveHint``, ``idempotentHint``, ``openWorldHint`` (bool) and ``title`` (str). Unknown keys or wrong value types raise ``ValueError`` at decoration time. Per the MCP spec these are hints, not guarantees — clients use them for result caching, retry policy, and app-widget tool gating.

.. note::

   Every server automatically registers a built-in ``get_job_result(job_id)`` tool.
   It is always present in ``tools/list`` and requires no configuration.


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

Tools can receive a ``Context`` object for logging, progress reporting, and client interactions such as elicitation.
Add a ``ctx`` (or ``context``) parameter as the first argument:

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

   @server.tool()
   def ask_for_label(ctx):
       # type: (Context) -> dict
       """Ask the MCP client to collect a label from the user."""
       return ctx.elicit(
           "Enter a label",
           {
               "type": "object",
               "properties": {
                   "label": {"type": "string", "title": "Label"}
               },
               "required": ["label"]
           }
       )

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
   * - ``ctx.elicit(message, requested_schema=None, timeout=60)``
     - Request form-mode user input through a client that declared the ``elicitation`` capability
   * - ``ctx.elicit_url(message, url, elicitation_id=None, timeout=60)``
     - Request URL-mode elicitation for sensitive or out-of-band flows
   * - ``ctx.sample(params, timeout=60)``
     - Request LLM sampling from a client that declared the ``sampling`` capability
   * - ``ctx.list_roots(timeout=60)``
     - Request roots from a client that declared the ``roots`` capability
   * - ``ctx.is_cancelled()``
     - True once the client has cancelled this async task; poll it between iterations
   * - ``ctx.cancel_event``
     - ``threading.Event`` set on cancellation — pass it to blocking waits
   * - ``ctx.on_cancel(callback)``
     - Register a callback fired on cancellation (terminate a process group, delete a remote job)
   * - ``ctx.task_metadata``
     - Metadata published for this task, as a dict
   * - ``ctx.set_task_metadata(metadata=None, **kwargs)``
     - Publish durable identifiers that surface as ``_meta`` on the task handle

.. note::

   ``ctx.elicit(...)`` requires the client to initialize with the ``elicitation`` capability.
   Form-mode elicitation should only be used for non-sensitive data. Use ``ctx.elicit_url(...)``
   when the user needs to enter credentials, API keys, tokens, payment details, or other secrets.


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
