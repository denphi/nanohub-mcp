"""HTTP + SSE transport for :class:`nanohubmcp.server.MCPServer`.

The request handler used to be declared inside ``MCPServer.run()``, closing
over that method's locals. That made 600-odd lines of transport unreachable
to anything but a running server: it could not be imported, unit-tested, or
subclassed, and it was rebuilt on every ``run()`` call. It lives here
instead and reaches the server through ``self.server.mcp_server``.

This module is named ``transport`` rather than ``http`` on purpose — a
module named ``http`` inside this package shadows the standard library's
``http`` package, which is exactly what it imports from.
"""

from __future__ import print_function

import json
import threading
import traceback
import uuid

from typing import Any

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:  # pragma: no cover - Python 2 fallback
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn

try:
    from urllib.parse import parse_qs, urlparse
except ImportError:  # pragma: no cover - Python 2 fallback
    from urlparse import parse_qs, urlparse

from .types import ToolResult

# Seconds between SSE heartbeats - keeps idle connections alive through
# proxies (nginx, weber, etc.) that drop connections after ~30-60s.
SSE_HEARTBEAT_INTERVAL = 20.0


class _SSEQueue(list):
    """A list with an attached threading.Event for wake-on-append semantics."""

    def __init__(self):
        list.__init__(self)
        self._wake_event = threading.Event()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server for handling multiple SSE connections."""

    daemon_threads = True
    # Set by MCPServer.run() immediately after construction, before any
    # request is served. It is how the handler reaches its server.
    mcp_server = None


class MCPRequestHandler(BaseHTTPRequestHandler):

    # ── The four names this class used to close over when it was defined
    # inside MCPServer.run(). It now lives at module scope and reaches them
    # through the HTTPServer that owns it, so the transport can be imported,
    # read and tested without starting a server.

    @property
    def server_instance(self):
        # type: () -> Any
        return self.server.mcp_server

    @property
    def _prefix(self):
        # type: () -> str
        return self.server_instance._path_prefix

    @property
    def _require_header(self):
        # type: () -> bool
        return self.server_instance._require_session_header

    @property
    def _max_bytes(self):
        # type: () -> int
        return self.server_instance._max_request_bytes

    def log_message(self, format, *args):
        print("[{}] {}".format(self.log_date_time_string(), format % args))

    def _strip_prefix(self):
        """Strip the path prefix to get the local route."""
        path = self.path
        if self._prefix and path.startswith(self._prefix):
            path = path[len(self._prefix):]
        if not path.startswith("/"):
            path = "/" + path
        return path

    def _session_id(self):
        """Get the client session id from headers or query parameters.

        When ``require_session_header`` is set, query-string session ids
        are ignored — only the ``Mcp-Session-Id`` header is honored.
        """
        session_id = self.headers.get("Mcp-Session-Id")
        if session_id:
            return session_id
        if self._require_header:
            return None
        query = parse_qs(urlparse(self.path).query)
        values = query.get("session_id") or query.get("sessionId")
        if values:
            return values[0]
        return None

    def _new_session_id(self):
        """Create a session id (for an SSE stream or an initialize POST)."""
        return str(uuid.uuid4())

    @staticmethod
    def _is_initialize(request):
        """Whether a JSON-RPC payload (or batch) carries `initialize`."""
        messages = request if isinstance(request, list) else [request]
        return any(isinstance(m, dict) and m.get("method") == "initialize"
                   for m in messages)

    def _send_session_headers(self, minted_session):
        """Return a session minted on this request, and let browsers see it.

        `Access-Control-Expose-Headers` is what lets a browser client
        read the header from a cross-origin response; the preflight's
        Allow-Headers only lets it be *sent*.
        """
        if minted_session:
            self.send_header("Mcp-Session-Id", minted_session)
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _endpoint_path(self, path, session_id):
        """Build a relative endpoint path for the SSE stream.

        In header-only mode the session id is *not* embedded in the URL —
        clients must echo it back via the ``Mcp-Session-Id`` header so
        it doesn't leak through proxy access logs.
        """
        base = "{}{}".format(self._prefix, path) if self._prefix else path
        if self._require_header:
            return base
        separator = "&" if "?" in base else "?"
        return "{}{}session_id={}".format(base, separator, session_id)

    # Revisions this server speaks, mirrored from the server module so a
    # bad `MCP-Protocol-Version` can be rejected before the body is parsed.
    def _supported_versions(self):
        from .server import SUPPORTED_PROTOCOL_VERSIONS
        return SUPPORTED_PROTOCOL_VERSIONS

    def _reject_bad_protocol_header(self):
        """Refuse an `MCP-Protocol-Version` header naming a revision we lack.

        "If the server receives a request with an invalid or unsupported
        `MCP-Protocol-Version`, it MUST respond with 400 Bad Request." This
        was previously only cross-checked against the body's `_meta`, so a
        header alone — which is all a 2025-06-18 client sends — went
        unvalidated and the request was answered as if it had matched.
        """
        declared = self.headers.get("MCP-Protocol-Version")
        if declared is None or declared in self._supported_versions():
            return False
        self._send_json({
            "jsonrpc": "2.0", "id": None,
            "error": {
                "code": -32602,
                "message": "Unsupported MCP-Protocol-Version: {}".format(declared),
                "data": {"supported": list(self._supported_versions())},
            },
        }, status=400)
        return True

    def _reject_unknown_session(self):
        """Refuse a session id the server no longer holds, with 404.

        "The server MAY terminate the session at any time, after which it
        MUST respond to requests containing that session ID with HTTP 404
        Not Found" — which is how a client knows to re-initialize. An id
        that was silently accepted instead left the client believing in a
        session whose negotiated state had been dropped.

        Only an id the client actually *sent* is checked. An initialize is
        exempt: that is the request that creates the session.
        """
        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id:
            return False
        if self.server_instance.session_exists(session_id):
            return False
        self._send_json({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32600,
                      "message": "Session not found: re-initialize"},
        }, status=404)
        return True

    def _reject_forbidden_origin(self):
        """Refuse a browser request from an origin outside the allowlist.

        Checked on every method, not just POST: the SSE stream is a
        long-lived channel a page can open cross-origin with
        EventSource, which is the more useful target of the two.
        """
        if self.server_instance.origin_allowed(self.headers.get("Origin")):
            return False
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Origin not allowed"},
        }).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        if self._reject_forbidden_origin():
            return
        if self._reject_bad_protocol_header():
            return
        path = self._strip_prefix()
        # Remove query string for path matching
        path_only = path.split("?")[0]

        if path_only.rstrip("/") == "/sse" or path_only == "/sse":
            self._handle_sse()
        elif path_only.rstrip("/") == "/mcp" or path_only == "/mcp":
            # Streamable HTTP - GET returns SSE stream for responses
            self._handle_streamable_http_get()
        elif path_only == "/openapi.json":
            self._handle_openapi()
        elif path_only == "/.well-known/mcp.json":
            self._handle_mcp_discovery()
        elif path_only == "/favicon.ico":
            # Browsers probe for this; respond cheaply without payload.
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        elif path_only in ("/", ""):
            # Root: server info / health page.
            info = {
                "name": self.server_instance.name,
                "version": self.server_instance.version,
                "status": "running",
                "tools": len(self.server_instance._tools),
                "resources": len(self.server_instance._resources),
                "prompts": len(self.server_instance._prompts),
                "endpoints": {
                    "sse": "/sse",
                    "mcp": "/mcp",
                    "openapi": "/openapi.json"
                }
            }
            body = json.dumps(info).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, "No GET endpoint at {}".format(path_only))

    def _sse_pump_loop(self, client_queue):
        """Drain queued messages and emit periodic heartbeats.

        Blocks on a threading.Event so the thread doesn't spin; sends a
        comment-line heartbeat every SSE_HEARTBEAT_INTERVAL seconds to
        keep proxies (nginx, wrwroxy) from closing idle connections.
        """
        event = client_queue._wake_event
        while True:
            while client_queue:
                msg = client_queue.pop(0)
                self.wfile.write(
                    "event: message\ndata: {}\n\n".format(msg).encode("utf-8")
                )
                self.wfile.flush()

            event.clear()
            if event.wait(SSE_HEARTBEAT_INTERVAL):
                # Woken by a new message; loop and drain
                continue
            # Timed out — emit a heartbeat comment to keep the link warm
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()

    def _handle_sse(self):
        session_id = self._session_id() or self._new_session_id()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Mcp-Session-Id", session_id)
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        self.end_headers()

        client_queue = _SSEQueue()
        self.server_instance._register_client(session_id, client_queue)
        print("SSE client connected. Session: {} Total: {}".format(
            session_id, self.server_instance._client_count()
        ))

        try:
            # Send connection event
            self.wfile.write(b"event: open\ndata: {}\n\n")
            endpoint = self._endpoint_path("/", session_id)
            self.wfile.write("event: endpoint\ndata: {}\n\n".format(endpoint).encode("utf-8"))
            self.wfile.flush()

            self._sse_pump_loop(client_queue)
        except (BrokenPipeError, ConnectionResetError) as e:
            print("SSE client disconnected: {}".format(e))
        except Exception:
            # Real bug in the pump — log a traceback so it's debuggable
            # rather than masquerading as a clean disconnect.
            traceback.print_exc()
        finally:
            self.server_instance._unregister_client(session_id, client_queue)

    def do_POST(self):
        try:
            # Before anything else, including reading the body: a
            # rebinding attempt should cost nothing.
            if self._reject_forbidden_origin():
                return
            if self._reject_bad_protocol_header():
                return

            path = self._strip_prefix()
            path_only = path.split("?")[0]

            # Validate the route *before* reading the body so we don't
            # buffer a large payload only to 404 it.
            is_direct_tool = path_only.startswith("/tools/")
            if not is_direct_tool and path_only not in ("/", "/mcp", "/mcp/"):
                self.send_error(404, "No JSON-RPC endpoint at {}".format(path_only))
                return

            # Parse Content-Length defensively — a non-integer header
            # is bad input, not a server bug.
            raw_len = self.headers.get("Content-Length", "0")
            try:
                content_length = int(raw_len)
            except (TypeError, ValueError):
                self.send_error(400, "Invalid Content-Length header")
                return
            if content_length < 0:
                self.send_error(400, "Negative Content-Length")
                return
            if content_length > self._max_bytes:
                self.send_error(413, "Request body exceeds {} bytes".format(self._max_bytes))
                return

            post_data = self.rfile.read(content_length)

            # Decode + parse JSON in their own scope so malformed input
            # surfaces as a structured error, not a 500 with a noisy
            # traceback. On the JSON-RPC endpoint that error is a
            # JSON-RPC Parse error (-32700) with a JSON body: an HTML
            # 400 is not something a JSON-RPC client -- or the proxy in
            # front of it -- can interpret, and was observed wrapped
            # into a bogus "Proxy error" downstream.
            try:
                request = json.loads(post_data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as e:
                if is_direct_tool:
                    self.send_error(400, "Malformed JSON: {}".format(e))
                    return
                self._send_json({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700,
                              "message": "Parse error: {}".format(e)},
                }, status=400)
                return

            # A session id the server no longer holds is a 404, so the
            # client re-initializes instead of running against state that
            # was dropped. `initialize` is what creates the session, and
            # the REST endpoint has none, so both are exempt.
            if (not is_direct_tool and not self._is_initialize(request)
                    and self._reject_unknown_session()):
                return

            session_id = self._session_id()
            # Streamable HTTP: a client initializes with a POST and
            # expects the server to mint the session and return it in
            # `Mcp-Session-Id` on that response. Creating sessions only
            # when a GET/SSE stream was opened first left every
            # POST-only client stateless: the capabilities it
            # negotiated (elicitation, tasks) were stored under no
            # session and were gone on the next request.
            minted_session = None
            if (not session_id and not is_direct_tool
                    and self._is_initialize(request)):
                session_id = minted_session = self._new_session_id()

            # Direct tool call via /tools/{name}
            if is_direct_tool:
                tool_name = path_only[len("/tools/"):]
                # Reject nested paths like /tools/foo/bar — only the
                # bare name is a valid tool identifier.
                if "/" in tool_name or not tool_name:
                    self.send_error(404, "Tool not found: {}".format(tool_name))
                    return
                self._handle_direct_tool_call(tool_name, request)
                return

            if isinstance(request, list):
                print("Received: batch[{}]".format(len(request)))
            elif isinstance(request, dict):
                print("Received: {}".format(request.get("method", "unknown")))
            else:
                print("Received: invalid JSON-RPC payload")

            if path_only in ("/mcp", "/mcp/"):
                # `subscriptions/listen` (2026-07-28) is answered by holding
                # *this* POST open as an SSE stream and delivering the
                # notifications on it. That revision removed sessions and the
                # GET endpoint, so requiring a session id — which is what
                # happened while the listen stream borrowed the GET channel —
                # made the method unreachable for the only revision that has
                # it.
                if (isinstance(request, dict)
                        and request.get("method") == "subscriptions/listen"):
                    self._handle_subscription_stream(request, session_id)
                    return

                # Every method is handled on this thread. An @async_tool is
                # not an exception: `tools/call` on one *returns immediately*
                # with the task handle (or the job_id wrapper) and starts the
                # worker in the background, so there is nothing to wait for.
                #
                # This used to hand the same call to a second thread, reply
                # 202, and push that same immediate handle onto the session's
                # SSE stream. It bought no concurrency — the work was already
                # off-thread — and it put a response on a stream the spec
                # reserves for server-initiated messages.
                response = self.server_instance._handle_jsonrpc_payload(
                    request, session_id=session_id, headers=self.headers
                )
                # Streamable HTTP: the response belongs to *this* HTTP
                # reply and nowhere else. The transport spec is explicit
                # on both halves of that — a server "MUST NOT broadcast
                # the same message across multiple streams", and "MUST
                # NOT send a JSON-RPC response on the [GET] stream unless
                # resuming". Echoing it onto the session's SSE queues did
                # both, and left clients listening on both channels to
                # dedupe by id.
                if response:
                    body = json.dumps(response).encode("utf-8")
                    self.send_response(
                        self.server_instance._http_status_for(response))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                else:
                    # A notification or response was accepted. The spec
                    # asks for 202 *with no body*, so send none.
                    body = b""
                    self.send_response(202)
                    self.send_header("Content-Length", "0")

                self.send_header("Access-Control-Allow-Origin", "*")
                self._send_session_headers(minted_session)
                self.end_headers()
                if body:
                    self.wfile.write(body)
                return

            # Legacy HTTP+SSE transport (2024-11-05): POST / pairs with
            # GET /sse, and that revision *does* deliver responses on the
            # SSE stream — unlike Streamable HTTP above, where doing so is
            # a MUST NOT. The HTTP echo is the extra here, kept for
            # REST-style clients that never open an SSE channel.
            response = self.server_instance._handle_jsonrpc_payload(
                request, session_id=session_id, headers=self.headers
            )

            if response:
                self.server_instance._broadcast(response, session_id=session_id)
                body = json.dumps(response).encode("utf-8")
                self.send_response(
                    self.server_instance._http_status_for(response))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
            else:
                body = b""
                self.send_response(202)
                self.send_header("Content-Length", "0")

            self.send_header("Access-Control-Allow-Origin", "*")
            self._send_session_headers(minted_session)
            self.end_headers()
            if body:
                self.wfile.write(body)

        except Exception as e:
            print("Error handling POST: {}".format(e))
            traceback.print_exc()
            self.send_error(500, str(e))

    def _handle_direct_tool_call(self, tool_name, arguments):
        """Handle direct REST-style tool call (OpenAPI compatible)."""
        if tool_name not in self.server_instance._tools:
            self.send_error(404, "Tool not found: {}".format(tool_name))
            return

        # The body must be a JSON object whose keys are the tool's
        # named arguments. Arrays, strings, null, etc. are 400.
        if not isinstance(arguments, dict):
            self._send_json_error(
                400, "Request body must be a JSON object of tool arguments"
            )
            return

        handler = self.server_instance._tools[tool_name]["handler"]

        # Direct REST has no SSE channel, so server-to-client requests
        # (elicit/sample/list_roots/progress) cannot work here. Refuse
        # context-bearing tools with 409 and steer callers to /mcp.
        if self.server_instance._needs_context(handler):
            self._send_json_error(
                409,
                (
                    "Tool '{}' requires an MCP session. "
                    "Call it via POST /mcp with an active SSE connection "
                    "instead of /tools/{}."
                ).format(tool_name, tool_name),
            )
            return

        # Invoke the tool. TypeError here is most commonly bad kwargs
        # from the caller — surface as 400. Anything else is a tool
        # failure: 500 with a logged traceback.
        try:
            result = self.server_instance._call_handler(handler, None, arguments)
        except TypeError as e:
            self._send_json_error(400, str(e))
            return
        except Exception as e:
            self._send_json_error(500, str(e))
            return

        # Shape and serialize the response. Failures here are server
        # bugs (non-serializable return value) — always 500.
        try:
            if isinstance(result, ToolResult):
                payload = result.to_dict()
                if payload.get("isError"):
                    # Tool-domain failure (the tool itself reported an
                    # error) — 422 Unprocessable Entity rather than 500
                    # so we don't log a traceback for an expected case.
                    items = payload.get("content", [])
                    if len(items) == 1 and isinstance(items[0].get("text"), str):
                        msg = items[0]["text"]
                    else:
                        msg = "Tool returned isError"
                    self._send_json_error(422, msg, details=payload)
                    return
                items = payload.get("content", [])
                unwrapped = items[0]["text"] if len(items) == 1 else payload
                body = json.dumps(
                    unwrapped if isinstance(unwrapped, dict) else {"result": unwrapped}
                ).encode("utf-8")
            elif isinstance(result, dict):
                body = json.dumps(result).encode("utf-8")
            else:
                body = json.dumps({"result": str(result)}).encode("utf-8")
        except (TypeError, ValueError) as e:
            self._send_json_error(500, "Failed to serialize tool result: {}".format(e))
            return

        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, status, message, details=None):
        """Write a JSON error body with the given HTTP status.

        Logs a traceback when status >= 500 so server errors leave a
        trail without each callsite needing to remember.
        """
        if status >= 500:
            traceback.print_exc()
        payload = {"error": message}
        if details is not None:
            payload["details"] = details
        try:
            error_body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError):
            # `details` carried a non-JSON-serializable object (e.g. a
            # ToolResult containing a custom class). Drop it rather
            # than losing the whole error response to a serialize bug.
            error_body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(error_body)))
        self.end_headers()
        self.wfile.write(error_body)

    def _is_stateless_request(self, request):
        """Whether this message declares a revision that has no sessions."""
        from .server import MCPServer
        params = request.get("params") if isinstance(request, dict) else None
        params = params if isinstance(params, dict) else {}
        return MCPServer._is_stateless(
            self.server_instance._request_protocol_version(params))

    def _handle_subscription_stream(self, request, session_id):
        """Answer `subscriptions/listen` with a long-lived SSE response.

        The stream is scoped to this request. When the client already has a
        session it keeps using it, so a handshake-era client sees the
        notifications on the channel it knows; otherwise a stream id is
        minted and torn down with the connection, which is what a stateless
        2026-07-28 caller needs.
        """
        if not self._accepts_event_stream():
            # 406, not 405: POST *is* supported here, so 405 would assert
            # the wrong thing (and owes an Allow header). A client running
            # the transport spec's backwards-compatibility probe reads a
            # 4xx on POST as "this is the legacy HTTP+SSE server" and
            # downgrades, rather than fixing its Accept header.
            self.send_error(
                406, "subscriptions/listen is answered with text/event-stream")
            return

        owned = not session_id
        stream_id = session_id or self._new_session_id()
        client_queue = _SSEQueue()
        self.server_instance._register_client(stream_id, client_queue)

        try:
            # Registers the subscription and queues the acknowledgment,
            # which the pump below writes out as the stream's first event.
            # A *rejected* listen (bad filter, too many subscriptions)
            # returns an error envelope instead, and that has to go back as
            # JSON — opening an empty stream would leave the client waiting
            # forever for notifications it was never subscribed to.
            refusal = self.server_instance._handle_request(
                request, session_id=stream_id, headers=self.headers)
        except Exception:
            traceback.print_exc()
            self.server_instance._unregister_client(stream_id, client_queue)
            self.send_error(500, "subscriptions/listen failed")
            return

        if refusal is not None:
            self.server_instance._unregister_client(stream_id, client_queue)
            self._send_json(
                refusal,
                status=self.server_instance._http_status_for(refusal))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        # 2026-07-28 removed sessions and this header, and a caller speaking
        # it correlates by the stream itself. Only a handshake-era client
        # that arrived without a session is told the id we minted.
        if owned and not self._is_stateless_request(request):
            self.send_header("Mcp-Session-Id", stream_id)
            self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        self.end_headers()

        try:
            self._sse_pump_loop(client_queue)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
        finally:
            self.server_instance._unregister_client(stream_id, client_queue)

    def _accepts_event_stream(self):
        """Whether the client's Accept header allows an SSE response.

        "The client MUST include an `Accept` header, listing
        `text/event-stream` as a supported content type", and the server
        MUST answer such a GET with either that content type or 405. A
        client that asked only for JSON gets the 405 rather than a stream
        it said it could not read. A missing header is treated as `*/*`,
        which is what HTTP says an absent Accept means.
        """
        accept = self.headers.get("Accept")
        if not accept:
            return True
        accept = accept.lower()
        return ("text/event-stream" in accept
                or "*/*" in accept or "text/*" in accept)

    def _handle_streamable_http_get(self):
        """Handle Streamable HTTP GET - returns SSE stream for async responses."""
        if not self._accepts_event_stream():
            self.send_error(
                405, "This endpoint answers GET with text/event-stream only")
            return
        if self._reject_unknown_session():
            return
        session_id = self._session_id() or self._new_session_id()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Mcp-Session-Id", session_id)
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        self.end_headers()

        client_queue = _SSEQueue()
        self.server_instance._register_client(session_id, client_queue)
        print("Streamable HTTP client connected. Session: {} Total: {}".format(
            session_id, self.server_instance._client_count()
        ))

        try:
            # No `open`/`endpoint` events here. Those belong to the
            # 2024-11-05 HTTP+SSE transport, where the client learns its
            # POST target from the stream; Streamable HTTP has a single
            # known endpoint and defines neither event. A comment line
            # keeps proxies from buffering the empty stream.
            self.wfile.write(b": stream open\n\n")
            self.wfile.flush()

            self._sse_pump_loop(client_queue)
        except (BrokenPipeError, ConnectionResetError) as e:
            print("Streamable HTTP client disconnected: {}".format(e))
        except Exception:
            traceback.print_exc()
        finally:
            self.server_instance._unregister_client(session_id, client_queue)

    def _handle_openapi(self):
        """Return OpenAPI schema for tool discovery."""
        tools_paths = {}
        for tool_name, tool_info in self.server_instance._tools.items():
            # Skip context-requiring tools — they need an MCP session
            # (server-to-client requests over SSE) and cannot be called
            # via the stateless /tools/<name> REST endpoint.
            if self.server_instance._needs_context(tool_info["handler"]):
                continue
            tool_def = tool_info["definition"]
            schema = tool_def.inputSchema if hasattr(tool_def, 'inputSchema') else {}

            tools_paths["/tools/{}".format(tool_name)] = {
                "post": {
                    "operationId": tool_name,
                    "summary": tool_def.description if hasattr(tool_def, 'description') else tool_name,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": schema
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Tool result",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            }
                        }
                    }
                }
            }

        openapi = {
            "openapi": "3.1.0",
            "info": {
                "title": self.server_instance.name,
                "version": self.server_instance.version,
                "description": "MCP Server exposing tools as OpenAPI endpoints"
            },
            "paths": {
                "/mcp": {
                    "get": {
                        "operationId": "mcp_sse",
                        "summary": "MCP Streamable HTTP SSE endpoint",
                        "responses": {"200": {"description": "SSE stream"}}
                    },
                    "post": {
                        "operationId": "mcp_message",
                        "summary": "Send MCP JSON-RPC message",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            }
                        },
                        "responses": {"200": {"description": "JSON-RPC response"}}
                    }
                },
                **tools_paths
            }
        }

        body = json.dumps(openapi).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_mcp_discovery(self):
        """Return MCP discovery document."""
        from .server import DEFAULT_NEGOTIATED_VERSION, SUPPORTED_PROTOCOL_VERSIONS
        discovery = {
            # Was pinned at "2024-11-05" — the oldest revision this server
            # speaks, advertised as if it were the only one.
            "mcpVersion": DEFAULT_NEGOTIATED_VERSION,
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "serverInfo": {
                "name": self.server_instance.name,
                "version": self.server_instance.version
            },
            "capabilities": self.server_instance._get_capabilities().to_dict(
                DEFAULT_NEGOTIATED_VERSION),
            "transports": [
                {"type": "sse", "endpoint": "/sse"},
                {"type": "streamable-http", "endpoint": "/mcp"}
            ]
        }

        body = json.dumps(discovery).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        """Terminate a session, per the Streamable HTTP session lifecycle.

        "Clients that no longer need a particular session SHOULD send an
        HTTP DELETE to the MCP endpoint with the `Mcp-Session-Id` header."
        Without a handler, BaseHTTPRequestHandler answered 501 and the
        session was never released — a POST-only client had no way at all
        to tell the server it was done.
        """
        if self._reject_forbidden_origin():
            return
        path_only = self._strip_prefix().split("?")[0]
        if path_only.rstrip("/") not in ("/mcp", "/sse", ""):
            self.send_error(404, "No endpoint at {}".format(path_only))
            return

        session_id = self._session_id()
        if not session_id:
            self._send_json({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600,
                          "message": "DELETE requires an Mcp-Session-Id header"},
            }, status=400)
            return
        if not self.server_instance.terminate_session(session_id):
            # Already gone — the same 404 any later request with this id gets.
            self._send_json({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Session not found"},
            }, status=404)
            return

        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, Mcp-Session-Id, "
                         "MCP-Protocol-Version, Mcp-Method, Mcp-Name")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
        self.send_header("Content-Length", "0")
        self.end_headers()
