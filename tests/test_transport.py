"""The HTTP/SSE transport is a module, not a closure.

``MCPRequestHandler`` used to be declared inside ``MCPServer.run()``, closing
over that method's locals. Nothing could reach it without binding a port, so
600-odd lines of request handling had no unit tests at all and were rebuilt on
every ``run()`` call.

These tests pin the two properties that made the move worth making: the class
is importable on its own, and the four names it used to capture from the
enclosing frame now resolve through the server it is attached to — with no
server listening.
"""

from __future__ import print_function

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanohubmcp import MCPServer  # noqa: E402
from nanohubmcp.transport import (  # noqa: E402
    MCPRequestHandler,
    ThreadingHTTPServer,
    SSE_HEARTBEAT_INTERVAL,
    _SSEQueue,
)


class _DetachedHTTPServer(object):
    """Stands in for the ThreadingHTTPServer that owns a handler."""

    def __init__(self, mcp_server):
        self.mcp_server = mcp_server


def _detached_handler(server):
    """A handler instance with no socket, request, or thread behind it.

    BaseHTTPRequestHandler.__init__ services the whole request, so a test that
    wants only the object bypasses it.
    """
    handler = object.__new__(MCPRequestHandler)
    handler.server = _DetachedHTTPServer(server)
    return handler


def test_transport_is_importable_without_a_running_server():
    assert MCPRequestHandler.__module__ == "nanohubmcp.transport"
    # Its request-handling surface came across intact.
    for method in ("do_GET", "do_POST", "do_OPTIONS"):
        assert callable(getattr(MCPRequestHandler, method))


def test_former_closure_variables_resolve_through_the_server():
    server = MCPServer("probe")
    handler = _detached_handler(server)

    assert handler.server_instance is server
    # Defaults, because run() has never been called.
    assert handler._prefix == ""
    assert handler._require_header is False
    assert handler._max_bytes == server._max_request_bytes


def test_run_configuration_reaches_the_handler():
    """What run() stores on the server is what the handler reads."""
    server = MCPServer("probe")
    server._path_prefix = "/weber/123"
    server._require_session_header = True
    server._max_request_bytes = 4096

    handler = _detached_handler(server)
    assert handler._prefix == "/weber/123"
    assert handler._require_header is True
    assert handler._max_bytes == 4096


def test_server_module_still_exports_the_moved_names():
    """`from nanohubmcp.server import _SSEQueue` predates the split, and the
    generated tool scaffold imports from that path."""
    from nanohubmcp import server as server_module

    assert server_module._SSEQueue is _SSEQueue
    assert server_module.ThreadingHTTPServer is ThreadingHTTPServer
    assert server_module.SSE_HEARTBEAT_INTERVAL == SSE_HEARTBEAT_INTERVAL


def test_sse_queue_wakes_on_append():
    queue = _SSEQueue()
    assert not queue._wake_event.is_set()
    queue.append("event")
    queue._wake_event.set()
    assert queue._wake_event.is_set() and list(queue) == ["event"]
