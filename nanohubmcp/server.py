"""
MCP Server implementation with HTTP + SSE transport.
Compatible with Python 3.7+.

Usage:
    server = MCPServer("my-tool")

    @server.tool()
    def add(a, b):
        '''Add two numbers'''
        return a + b

    server.run()
"""

from __future__ import print_function

import inspect
import json
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:
    # Python 2 fallback (not officially supported but helps with syntax)
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn

try:
    from urllib.parse import parse_qs, unquote, urlparse
except ImportError:
    from urlparse import parse_qs, urlparse
    from urllib import unquote

from typing import Any, Callable, Dict, List, Optional, Set

from .types import (
    Tool, Resource, Prompt, TextContent, ImageContent,
    ToolResult, ResourceResult, ResourceContent,
    PromptResult, Message, Role,
    ServerCapabilities, ServerInfo
)
from .decorators import tool, async_tool, resource, prompt
from .context import Context


SUPPORTED_PROTOCOL_VERSIONS = ["2026-01-26", "2025-11-25", "2025-06-18", "2024-11-05"]

# MCP Apps extension (https://github.com/modelcontextprotocol/ext-apps).
# Servers advertising this extension can attach UI resources to tools via
# `_meta.ui.resourceUri` and serve `text/html;profile=mcp-app` resources.
MCP_APPS_EXTENSION_ID = "io.modelcontextprotocol/ui"
MCP_APPS_MIME_TYPE = "text/html;profile=mcp-app"

# MCP Tasks extension (https://github.com/modelcontextprotocol/experimental-ext-tasks).
# Async tools can return a task handle to clients that opt into this extension,
# while older clients continue to receive the existing get_job_result flow.
MCP_TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
MCP_TASK_TTL_MS = 60 * 60 * 1000
MCP_TASK_POLL_INTERVAL_MS = 1000

# Seconds between SSE heartbeats — keeps idle connections alive through proxies
# (nginx, wrwroxy, etc.) that drop connections after ~30-60s of inactivity.
SSE_HEARTBEAT_INTERVAL = 20.0

# Cap on request bodies. Anything larger gets a 413 without being read into
# memory. Generous default for tool payloads but bounds worst-case allocation.
MAX_REQUEST_BYTES = 16 * 1024 * 1024  # 16 MiB


class _SSEQueue(list):
    """A list with an attached threading.Event for wake-on-append semantics."""

    def __init__(self):
        list.__init__(self)
        self._wake_event = threading.Event()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server for handling multiple SSE connections."""
    daemon_threads = True


class MCPServer(object):
    """
    Model Context Protocol server for nanoHUB/HubZero tools.

    Usage:
        server = MCPServer("my-tool")

        @server.tool()
        def add(a, b):
            '''Add two numbers'''
            return a + b

        @server.resource("config://settings")
        def get_settings():
            '''Get settings'''
            return {"theme": "dark"}

        @server.prompt()
        def ask(topic):
            '''Ask about a topic'''
            return "Tell me about {}".format(topic)

        server.run()

    Proxy Support:
        When running behind a reverse proxy that rewrites URIs (like weber),

        server = MCPServer("my-tool")

    """

    def __init__(
        self,
        name,  # type: str
        version="1.0.0"  # type: str
    ):
        # type: (...) -> None
        """
        Initialize an MCP server.

        Args:
            name: Server name
            version: Server version
        """
        self.name = name
        self.version = version

        self._tools = {}  # type: Dict[str, Dict[str, Any]]
        self._resources = {}  # type: Dict[str, Dict[str, Any]]
        self._prompts = {}  # type: Dict[str, Dict[str, Any]]
        self._clients = {}  # type: Dict[str, List[_SSEQueue]]
        self._clients_lock = threading.Lock()
        self._sessions = {}  # type: Dict[str, Dict[str, Any]]
        self._sessions_lock = threading.Lock()
        self._pending_client_requests = {}  # type: Dict[str, Dict[str, Any]]
        self._pending_lock = threading.Lock()
        self._path_prefix = ""  # type: str
        # job_id -> {"status": "running"|"done"|"error", "result": Any}
        self._jobs = {}  # type: Dict[str, Dict[str, Any]]
        self._jobs_lock = threading.Lock()
        # get_job_result is registered lazily the first time an async tool is
        # registered, so servers without any async tools don't advertise it.
        self._job_polling_registered = False  # type: bool

    def _register_get_job_result(self):
        # type: () -> None
        """Auto-register the built-in get_job_result polling tool."""
        server_instance = self

        def get_job_result(job_id):
            # type: (str) -> Dict[str, Any]
            """Poll the result of a long-running async tool call.

            Returns status 'running' while the job is in progress, or the final
            result/error once it completes. The first successful poll consumes
            the job — subsequent polls return ``not_found`` — so the server
            doesn't accumulate finished-job state for the lifetime of the
            process.

            Args:
                job_id: The job ID returned by an async tool call.
            """
            with server_instance._jobs_lock:
                job = server_instance._jobs.get(job_id)
                if job is None:
                    return {"status": "not_found", "job_id": job_id}
                if job["status"] == "running":
                    return {"status": "running", "job_id": job_id}
                # Terminal state — remove so memory doesn't grow unbounded.
                server_instance._jobs.pop(job_id, None)

            if job["status"] == "error":
                return {"status": "error", "job_id": job_id, "error": job["result"]}
            return {"status": "done", "job_id": job_id, "result": job["result"]}

        decorated = tool(
            name="get_job_result",
            description=(
                "Poll the result of a long-running async tool call. "
                "Pass the job_id returned by an async tool. "
                "Returns {\"status\": \"running\"} until complete, then the final result."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"]
            }
        )(get_job_result)
        self._register_tool_function(decorated)

    def _start_async_tool_job(self, handler, msg_id, arguments, session_id=None,
                              progress_token=None, meta=None):
        # type: (Any, Any, Dict[str, Any], Optional[str], Optional[Any], Optional[Dict[str, Any]]) -> str
        """Spawn a background thread for an async tool; return a job_id immediately."""
        # Snapshot the arguments dict — the closure runs on a background thread
        # and we don't want later mutations of the caller's dict to leak in.
        arguments = dict(arguments) if arguments else {}

        job_id = str(uuid.uuid4())
        now = self._utc_now()
        with self._jobs_lock:
            self._jobs[job_id] = {
                "status": "running",
                "result": None,
                "createdAt": now,
                "lastUpdatedAt": now,
                "ttlMs": MCP_TASK_TTL_MS,
                "pollIntervalMs": MCP_TASK_POLL_INTERVAL_MS,
                "session_id": session_id,
                "expires_at": time.time() + (MCP_TASK_TTL_MS / 1000.0),
            }

        server_instance = self

        def _run():
            try:
                call_result = self._call_handler(
                    handler, msg_id, arguments,
                    session_id=session_id,
                    progress_token=progress_token,
                    meta=meta,
                )

                # If the tool signalled failure via ToolResult(isError=True),
                # surface that as a job error rather than a successful result.
                if isinstance(call_result, ToolResult):
                    payload = call_result.to_dict()
                    if payload.get("isError"):
                        items = payload.get("content", [])
                        message = (
                            items[0]["text"]
                            if len(items) == 1 and "text" in items[0]
                            else payload
                        )
                        with server_instance._jobs_lock:
                            if server_instance._jobs[job_id].get("status") == "cancelled":
                                return
                            server_instance._jobs[job_id]["status"] = "error"
                            server_instance._jobs[job_id]["result"] = message
                            server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
                        return
                    result = payload
                elif isinstance(call_result, dict):
                    result = call_result
                else:
                    result = str(call_result)
                with server_instance._jobs_lock:
                    if server_instance._jobs[job_id].get("status") == "cancelled":
                        return
                    server_instance._jobs[job_id]["status"] = "done"
                    server_instance._jobs[job_id]["result"] = result
                    server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
            except Exception as e:
                with server_instance._jobs_lock:
                    if server_instance._jobs[job_id].get("status") == "cancelled":
                        return
                    server_instance._jobs[job_id]["status"] = "error"
                    server_instance._jobs[job_id]["result"] = str(e)
                    server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
                traceback.print_exc()

        t = threading.Thread(target=_run)
        t.daemon = True
        t.start()
        return job_id

    @staticmethod
    def _utc_now():
        # type: () -> str
        """Return an MCP-friendly UTC timestamp."""
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    def _job_to_task(self, task_id, job, include_terminal_payload=True):
        # type: (str, Dict[str, Any], bool) -> Dict[str, Any]
        """Convert an internal async-job record into an MCP Task object."""
        status_map = {
            "running": "working",
            "done": "completed",
            "error": "failed",
            "cancelled": "cancelled",
        }
        status = status_map.get(job.get("status"), "working")
        now = self._utc_now()
        task = {
            "taskId": task_id,
            "status": status,
            "createdAt": job.get("createdAt", now),
            "lastUpdatedAt": job.get("lastUpdatedAt", now),
            "ttlMs": job.get("ttlMs", MCP_TASK_TTL_MS),
            "pollIntervalMs": job.get("pollIntervalMs", MCP_TASK_POLL_INTERVAL_MS),
        }
        if status == "working":
            task["statusMessage"] = "The operation is in progress."
        elif status == "cancelled":
            task["statusMessage"] = "Cancellation was requested."
        elif include_terminal_payload and status == "completed":
            task["result"] = self._tool_result_payload(job.get("result"))
        elif include_terminal_payload and status == "failed":
            task["error"] = {
                "code": -32603,
                "message": str(job.get("result", "Task failed")),
            }
        return task

    def _tool_result_payload(self, value):
        # type: (Any) -> Dict[str, Any]
        """Wrap a stored async-tool value as a CallToolResult payload."""
        if isinstance(value, ToolResult):
            return value.to_dict()
        if self._is_tool_result_payload(value):
            return value
        if isinstance(value, dict):
            return {
                "content": [{"type": "text", "text": json.dumps(value)}],
                "structuredContent": value,
                "isError": False,
            }
        return {
            "content": [{"type": "text", "text": str(value)}],
            "isError": False,
        }

    @staticmethod
    def _is_tool_result_payload(value):
        # type: (Any) -> bool
        """Return True for dicts that already look like CallToolResult."""
        return (
            isinstance(value, dict)
            and isinstance(value.get("content"), list)
            and "isError" in value
        )

    def _task_access_error(self, task_id, job, session_id):
        # type: (str, Optional[Dict[str, Any]], Optional[str]) -> Optional[Dict[str, Any]]
        """Return a JSON-RPC error when the session cannot access a task."""
        if job is None:
            return {"code": -32602, "message": "Unknown taskId: {}".format(task_id)}
        owner = job.get("session_id")
        if not owner or owner != session_id:
            return {
                "code": -32003,
                "message": "Task is not available in this session",
            }
        return None

    def _prune_expired_jobs(self):
        # type: () -> None
        """Remove task/job records whose retention window has elapsed."""
        now = time.time()
        with self._jobs_lock:
            expired = [
                job_id for job_id, job in self._jobs.items()
                if job.get("expires_at") is not None and job.get("expires_at") <= now
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)

    def _strip_proxy_prefix(self, uri):
        # type: (str) -> str
        """
        Normalize proxied resource URIs to registered resource keys.

        Some proxy/client stacks may pass a full proxied URL/path instead of the
        raw MCP resource URI. This method attempts a safe normalization by
        matching known resource URIs.
        """
        if not uri or not isinstance(uri, str):
            return uri

        # Exact-match fast path.
        if uri in self._resources:
            return uri

        # Try common normalizations before suffix matching.
        candidates = [uri.strip()]
        if self._path_prefix:
            prefix = self._path_prefix.rstrip("/")
            if prefix and candidates[0].startswith(prefix):
                stripped = candidates[0][len(prefix):]
                candidates.append(stripped if stripped.startswith("/") else "/" + stripped)

        decoded = unquote(candidates[0])
        if decoded not in candidates:
            candidates.append(decoded)

        # Remove query/fragment and leading slashes variants.
        normalized_candidates = []
        for candidate in candidates:
            base = candidate.split("?", 1)[0].split("#", 1)[0]
            for value in (candidate, base, base.lstrip("/")):
                if value and value not in normalized_candidates:
                    normalized_candidates.append(value)

        # Exact match after normalization.
        for candidate in normalized_candidates:
            if candidate in self._resources:
                return candidate

        # Fallback: match by registered URI suffix (prefer longest match).
        resource_uris = sorted(self._resources.keys(), key=len, reverse=True)
        for candidate in normalized_candidates:
            for resource_uri in resource_uris:
                if candidate.endswith(resource_uri):
                    return resource_uri
                if candidate.endswith("/" + resource_uri):
                    return resource_uri

        return uri

    def _register_tool_function(self, func):
        # type: (Callable) -> None
        """Register a decorated tool function."""
        name = func._mcp_tool_name
        is_async = getattr(func, "_mcp_async_tool", False)
        self._tools[name] = {
            "definition": Tool(
                name=name,
                description=func._mcp_tool_description,
                inputSchema=func._mcp_tool_input_schema,
                meta=getattr(func, "_mcp_tool_meta", None) or {},
                outputSchema=getattr(func, "_mcp_tool_output_schema", None),
            ),
            "handler": func,
            "is_async": is_async,
        }
        if is_async:
            # Lazily install get_job_result so servers without async tools
            # don't advertise it in tools/list. Guarded so concurrent decorator
            # evaluations can't both call _register_get_job_result.
            with self._jobs_lock:
                already_registered = self._job_polling_registered
                self._job_polling_registered = True
            if not already_registered:
                self._register_get_job_result()

    def _register_resource_function(self, func):
        # type: (Callable) -> None
        """Register a decorated resource function."""
        uri = func._mcp_resource_uri
        self._resources[uri] = {
            "definition": Resource(
                uri=uri,
                name=func._mcp_resource_name,
                description=func._mcp_resource_description,
                mimeType=func._mcp_resource_mime_type,
                meta=getattr(func, "_mcp_resource_meta", None) or {},
            ),
            "handler": func
        }

    def _register_prompt_function(self, func):
        # type: (Callable) -> None
        """Register a decorated prompt function."""
        name = func._mcp_prompt_name
        self._prompts[name] = {
            "definition": Prompt(
                name=name,
                description=func._mcp_prompt_description,
                arguments=func._mcp_prompt_arguments
            ),
            "handler": func
        }

    def tool(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None,  # type: Optional[Dict[str, Any]]
        input_schema=None,  # type: Optional[Dict[str, Any]]
        output_schema=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a tool on this server.
        Aligned with FastMCP @mcp.tool decorator.

        Args:
            name: Tool name (defaults to function name)
            description: Tool description (defaults to docstring)
            tags: Optional set of tags for categorization
            meta: Optional metadata dictionary
            input_schema: JSON Schema for inputs (auto-generated if not provided)
            output_schema: JSON Schema describing the dict the tool returns.
                Emitted as `outputSchema` in tools/list; dict results always
                carry `structuredContent` per the MCP spec.
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = tool(name, description, tags, meta, input_schema, output_schema)(func)
            self._register_tool_function(decorated)
            return decorated

        if callable(name):
            func = name
            name = None
            return decorator(func)

        return decorator

    def async_tool(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None,  # type: Optional[Dict[str, Any]]
        input_schema=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a long-running tool that returns a job_id immediately.

        The server runs the function in a background thread and the client polls
        for the result using the built-in ``get_job_result`` tool.

        Usage::

            @server.async_tool()
            def run_openlane(verilog_code: str, design_name: str) -> str:
                # takes minutes — won't block the HTTP response
                ...
                return result
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = async_tool(name, description, tags, meta, input_schema)(func)
            self._register_tool_function(decorated)
            return decorated

        if callable(name):
            func = name
            name = None
            return decorator(func)

        return decorator

    def resource(
        self,
        uri,  # type: str
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        mime_type=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a resource on this server.
        Aligned with FastMCP @mcp.resource decorator.

        Args:
            uri: Resource URI (e.g., "file:///path" or "config://settings")
            name: Resource name (defaults to function name)
            description: Resource description (defaults to docstring)
            mime_type: MIME type of the resource content
            tags: Optional set of tags for categorization
            meta: Optional metadata dictionary
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = resource(uri, name, description, mime_type, tags, meta)(func)
            self._register_resource_function(decorated)
            return decorated
        return decorator

    def prompt(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a prompt on this server.
        Aligned with FastMCP @mcp.prompt decorator.

        Args:
            name: Prompt name (defaults to function name)
            description: Prompt description (defaults to docstring)
            tags: Optional set of tags for categorization
            meta: Optional metadata dictionary
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = prompt(name, description, tags, meta)(func)
            self._register_prompt_function(decorated)
            return decorated

        if callable(name):
            func = name
            name = None
            return decorator(func)

        return decorator

    def _get_capabilities(self):
        # type: () -> ServerCapabilities
        """Get server capabilities based on registered handlers."""
        extensions = {}
        if self._has_mcp_app_resources():
            # Advertise mcp-apps support so capable hosts know they can render
            # the UI resources attached to our tools.
            extensions[MCP_APPS_EXTENSION_ID] = {
                "mimeTypes": [MCP_APPS_MIME_TYPE]
            }
        if self._has_async_tools():
            extensions[MCP_TASKS_EXTENSION_ID] = {}
        return ServerCapabilities(
            tools=len(self._tools) > 0,
            resources=len(self._resources) > 0,
            prompts=len(self._prompts) > 0,
            logging=True,
            extensions=extensions,
        )

    def _has_mcp_app_resources(self):
        # type: () -> bool
        """True if any registered resource is an MCP App (ui:// HTML template)."""
        for entry in self._resources.values():
            definition = entry["definition"]
            mime = getattr(definition, "mimeType", None) or ""
            if "profile=mcp-app" in mime:
                return True
            if getattr(definition, "uri", "").startswith("ui://"):
                return True
        return False

    def _has_async_tools(self):
        # type: () -> bool
        """True if the server has any long-running async tools."""
        return any(t.get("is_async") for t in self._tools.values())

    def _context_param_name(self, func):
        # type: (Callable) -> Optional[str]
        """Return the context parameter name on `func`, or None.

        Single source of truth used by both _needs_context and _call_handler so
        we don't pay for inspect.signature twice on every call.
        """
        try:
            params = inspect.signature(func).parameters
        except (ValueError, TypeError):
            return None
        for candidate in ("ctx", "context"):
            if candidate in params:
                return candidate
        return None

    def _needs_context(self, func):
        # type: (Callable) -> bool
        """Check if a function accepts a context parameter."""
        return self._context_param_name(func) is not None

    def _call_handler(self, handler, request_id, arguments=None, session_id=None,
                      progress_token=None, meta=None):
        # type: (Callable, Optional[str], Optional[Dict[str, Any]], Optional[str], Optional[Any], Optional[Dict[str, Any]]) -> Any
        """Call a handler, injecting context if needed."""
        # Copy so we don't pollute the caller's params dict with a Context
        # object — that would break later JSON serialization or logging.
        arguments = dict(arguments) if arguments else {}

        ctx_param = self._context_param_name(handler)
        if ctx_param is not None:
            arguments[ctx_param] = Context(
                server=self,
                request_id=request_id,
                session_id=session_id,
                progress_token=progress_token,
                meta=meta,
            )

        return handler(**arguments)

    @staticmethod
    def _extract_progress_token(params):
        # type: (Dict[str, Any]) -> Optional[Any]
        """Pull progressToken out of params._meta per the MCP spec."""
        if not isinstance(params, dict):
            return None
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            return None
        return meta.get("progressToken")

    def _pending_key(self, session_id, request_id):
        # type: (str, Any) -> str
        """Build a lookup key for a pending server-to-client request."""
        return "{}:{}".format(session_id, request_id)

    def _set_session_capabilities(self, session_id, capabilities):
        # type: (Optional[str], Dict[str, Any]) -> None
        """Remember negotiated client capabilities for a session."""
        if not session_id:
            return
        with self._sessions_lock:
            session = self._sessions.setdefault(session_id, {})
            session["capabilities"] = capabilities or {}

    def _client_capabilities(self, session_id):
        # type: (Optional[str]) -> Dict[str, Any]
        """Return negotiated client capabilities for a session."""
        if not session_id:
            return {}
        with self._sessions_lock:
            session = self._sessions.get(session_id, {})
            return session.get("capabilities", {})

    def _client_supports(self, session_id, capability, mode=None):
        # type: (Optional[str], str, Optional[str]) -> bool
        """Check whether the client declared a capability."""
        capabilities = self._client_capabilities(session_id)
        value = capabilities.get(capability)
        if value is None:
            return False
        if mode is None:
            return True
        if capability == "elicitation" and value == {} and mode == "form":
            return True
        if isinstance(value, dict):
            return mode in value
        return False

    def _client_supports_tasks(self, session_id=None, params=None):
        # type: (Optional[str], Optional[Dict[str, Any]]) -> bool
        """Return True when a client opted into the MCP Tasks extension."""
        extension_sets = []

        if isinstance(params, dict):
            meta = params.get("_meta")
            if isinstance(meta, dict):
                request_caps = meta.get("io.modelcontextprotocol/clientCapabilities")
                if isinstance(request_caps, dict):
                    extension_sets.append(request_caps.get("extensions"))

        capabilities = self._client_capabilities(session_id)
        extension_sets.append(capabilities.get("extensions"))
        experimental = capabilities.get("experimental")
        if isinstance(experimental, dict):
            extension_sets.append(experimental)

        for extensions in extension_sets:
            if isinstance(extensions, dict) and MCP_TASKS_EXTENSION_ID in extensions:
                return True
        return False

    def _negotiate_protocol_version(self, requested):
        # type: (Optional[str]) -> str
        """Return the best supported protocol version for initialization."""
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            return requested
        return SUPPORTED_PROTOCOL_VERSIONS[0]

    def _receive_client_response(self, response, session_id):
        # type: (Dict[str, Any], Optional[str]) -> bool
        """Store a client response for a pending server-to-client request."""
        if not session_id or "id" not in response:
            return False
        key = self._pending_key(session_id, response.get("id"))
        with self._pending_lock:
            pending = self._pending_client_requests.get(key)
            if not pending:
                return False
            pending["response"] = response
            pending["event"].set()
            return True

    def _session_has_stream(self, session_id):
        # type: (Optional[str]) -> bool
        """Return True if the session has at least one active SSE queue."""
        if not session_id:
            return False
        with self._clients_lock:
            return bool(self._clients.get(session_id))

    def _request_client(self, session_id, method, params=None, timeout=60):
        # type: (str, str, Optional[Dict[str, Any]], int) -> Dict[str, Any]
        """Send a JSON-RPC request to one client and wait for its response."""
        if not session_id:
            raise RuntimeError("Client session is required for {}".format(method))

        # Server-to-client requests are delivered over the session's SSE stream.
        # If no stream is connected, fail fast instead of blocking until timeout.
        if not self._session_has_stream(session_id):
            raise RuntimeError(
                "No active client stream for session {} — cannot send {}".format(
                    session_id, method
                )
            )

        request_id = "server-{}".format(uuid.uuid4())
        key = self._pending_key(session_id, request_id)
        event = threading.Event()
        with self._pending_lock:
            self._pending_client_requests[key] = {
                "event": event,
                "response": None
            }

        # Re-check after registration: if the stream disconnected between the
        # initial check and now, _unregister_client may have already swept
        # pending entries — but our entry was inserted *after* its sweep, so it
        # would never be cleaned up. Verify we still have a stream and bail if
        # not, otherwise we'd block until timeout.
        if not self._session_has_stream(session_id):
            with self._pending_lock:
                self._pending_client_requests.pop(key, None)
            raise RuntimeError(
                "Client stream closed before {} could be sent".format(method)
            )

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {}
        }
        self._broadcast(request, session_id=session_id)

        try:
            if not event.wait(timeout):
                raise RuntimeError("Timed out waiting for client response to {}".format(method))
            with self._pending_lock:
                pending = self._pending_client_requests.get(key, {})
                response = pending.get("response")
            if not response:
                # Woken without a response — the SSE stream disconnected
                # (see _unregister_client) or the entry was already cleared.
                raise RuntimeError(
                    "Client stream closed before responding to {}".format(method)
                )
            if "error" in response:
                error = response["error"]
                message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise RuntimeError(message)
            return response.get("result", {})
        finally:
            with self._pending_lock:
                self._pending_client_requests.pop(key, None)

    def request_elicitation(self, session_id, message, requested_schema=None, mode="form",
                            url=None, elicitation_id=None, timeout=60):
        # type: (str, str, Optional[Dict[str, Any]], str, Optional[str], Optional[str], int) -> Dict[str, Any]
        """Request user input through the MCP client."""
        if not self._client_supports(session_id, "elicitation", mode):
            raise RuntimeError("Client does not support {} elicitation".format(mode))

        params = {
            "mode": mode,
            "message": message
        }
        if mode == "form":
            params["requestedSchema"] = requested_schema or {
                "type": "object",
                "properties": {},
                "required": []
            }
        elif mode == "url":
            if not url:
                raise ValueError("url is required for URL mode elicitation")
            params["url"] = url
            params["elicitationId"] = elicitation_id or str(uuid.uuid4())
        else:
            raise ValueError("Unsupported elicitation mode: {}".format(mode))

        return self._request_client(
            session_id=session_id,
            method="elicitation/create",
            params=params,
            timeout=timeout
        )

    def request_sampling(self, session_id, params, timeout=60):
        # type: (str, Dict[str, Any], int) -> Dict[str, Any]
        """Request LLM sampling from a capable client."""
        if not self._client_supports(session_id, "sampling"):
            raise RuntimeError("Client does not support sampling")
        return self._request_client(session_id, "sampling/createMessage", params, timeout=timeout)

    def request_roots(self, session_id, timeout=60):
        # type: (str, int) -> Dict[str, Any]
        """Request roots from a capable client."""
        if not self._client_supports(session_id, "roots"):
            raise RuntimeError("Client does not support roots")
        return self._request_client(session_id, "roots/list", {}, timeout=timeout)

    def _invalid_request(self, msg_id=None, message="Invalid Request"):
        # type: (Optional[Any], str) -> Dict[str, Any]
        """Build a JSON-RPC Invalid Request response."""
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32600, "message": message},
        }

    def _handle_jsonrpc_message(self, message, session_id=None):
        # type: (Any, Optional[str]) -> Optional[Dict[str, Any]]
        """Handle one JSON-RPC message after top-level shape validation."""
        if not isinstance(message, dict):
            return self._invalid_request(None, "JSON-RPC message must be an object")
        return self._handle_request(message, session_id=session_id)

    def _handle_jsonrpc_payload(self, payload, session_id=None):
        # type: (Any, Optional[str]) -> Optional[Any]
        """Handle a JSON-RPC message or batch payload."""
        if isinstance(payload, list):
            if not payload:
                return self._invalid_request(None, "JSON-RPC batch must not be empty")
            responses = []
            for message in payload:
                response = self._handle_jsonrpc_message(message, session_id=session_id)
                if response is not None:
                    responses.append(response)
            return responses or None
        return self._handle_jsonrpc_message(payload, session_id=session_id)

    def _handle_request(self, request, session_id=None):
        # type: (Dict[str, Any], Optional[str]) -> Optional[Dict[str, Any]]
        """Handle a JSON-RPC request and return response."""
        if not isinstance(request, dict):
            return self._invalid_request(None, "JSON-RPC message must be an object")

        method = request.get("method", "")
        msg_id = request.get("id")
        is_notification = msg_id is None
        params = request.get("params")
        if params is None:
            params = {}

        if not method and self._receive_client_response(request, session_id):
            return None

        # Per JSON-RPC 2.0, params (when present) must be an object or array.
        # We only accept objects since every method consumes named params.
        if not isinstance(params, dict):
            if is_notification:
                # Notifications must never receive a reply, even on error.
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "params must be a JSON object"},
            }

        # Per spec, an empty/missing method is an Invalid Request, not a
        # missing method. Surface that distinction so clients see -32600.
        if not method:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32600, "message": "Missing or empty method"},
            }

        progress_token = self._extract_progress_token(params)
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
        self._prune_expired_jobs()

        result = None
        error = None

        try:
            if method == "initialize":
                self._set_session_capabilities(session_id, params.get("capabilities", {}))
                result = {
                    "protocolVersion": self._negotiate_protocol_version(
                        params.get("protocolVersion")
                    ),
                    "serverInfo": ServerInfo(self.name, self.version).to_dict(),
                    "capabilities": self._get_capabilities().to_dict()
                }

            elif method == "initialized":
                # Notification, no response needed
                return None

            elif method == "ping":
                result = {}

            elif method == "tasks/get":
                task_id = params.get("taskId")
                if not isinstance(task_id, str) or not task_id:
                    error = {"code": -32602, "message": "tasks/get requires taskId"}
                elif not self._client_supports_tasks(session_id=session_id, params=params):
                    error = {
                        "code": -32003,
                        "message": "Missing required client capability",
                        "data": {
                            "requiredCapabilities": {
                                "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                            }
                        },
                    }
                else:
                    with self._jobs_lock:
                        job = self._jobs.get(task_id)
                        access_error = self._task_access_error(task_id, job, session_id)
                        task = (
                            self._job_to_task(task_id, job)
                            if access_error is None
                            else None
                        )
                    if access_error is not None:
                        error = access_error
                    else:
                        task["resultType"] = "complete"
                        result = task

            elif method == "tasks/update":
                task_id = params.get("taskId")
                if not isinstance(task_id, str) or not task_id:
                    error = {"code": -32602, "message": "tasks/update requires taskId"}
                elif not self._client_supports_tasks(session_id=session_id, params=params):
                    error = {
                        "code": -32003,
                        "message": "Missing required client capability",
                        "data": {
                            "requiredCapabilities": {
                                "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                            }
                        },
                    }
                else:
                    with self._jobs_lock:
                        job = self._jobs.get(task_id)
                        access_error = self._task_access_error(task_id, job, session_id)
                    if access_error is not None:
                        error = access_error
                    else:
                        # This server's async tools do not currently pause for
                        # MRTR input, so valid update requests are acknowledged.
                        result = {"resultType": "complete"}

            elif method == "tasks/cancel":
                task_id = params.get("taskId")
                if not isinstance(task_id, str) or not task_id:
                    error = {"code": -32602, "message": "tasks/cancel requires taskId"}
                elif not self._client_supports_tasks(session_id=session_id, params=params):
                    error = {
                        "code": -32003,
                        "message": "Missing required client capability",
                        "data": {
                            "requiredCapabilities": {
                                "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                            }
                        },
                    }
                else:
                    with self._jobs_lock:
                        job = self._jobs.get(task_id)
                        access_error = self._task_access_error(task_id, job, session_id)
                        if access_error is None and job.get("status") == "running":
                            job["status"] = "cancelled"
                            job["lastUpdatedAt"] = self._utc_now()
                    if access_error is not None:
                        error = access_error
                    else:
                        result = {"resultType": "complete"}

            elif method == "tools/list":
                result = {
                    "tools": [t["definition"].to_dict() for t in self._tools.values()]
                }

            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})

                if not isinstance(arguments, dict):
                    error = {"code": -32602,
                             "message": "tools/call arguments must be a JSON object"}
                elif tool_name not in self._tools:
                    error = {"code": -32601, "message": "Tool not found: {}".format(tool_name)}
                else:
                    tool_entry = self._tools[tool_name]
                    handler = tool_entry["handler"]

                    if tool_entry.get("is_async"):
                        # Return a task to clients that opted into the MCP
                        # Tasks extension; older clients keep the existing
                        # get_job_result polling-tool flow.
                        supports_tasks = self._client_supports_tasks(
                            session_id=session_id, params=params
                        )
                        if supports_tasks and not session_id:
                            error = {
                                "code": -32003,
                                "message": "Task-capable async tool calls require an MCP session",
                            }
                        else:
                            job_id = self._start_async_tool_job(
                                handler, msg_id, arguments,
                                session_id=session_id,
                                progress_token=progress_token,
                                meta=meta,
                            )
                        if supports_tasks and not error:
                            with self._jobs_lock:
                                job = self._jobs[job_id]
                                result = self._job_to_task(
                                    job_id, job, include_terminal_payload=False
                                )
                            result["resultType"] = "task"
                        elif not error:
                            result = {
                                "content": [{"type": "text", "text": json.dumps({
                                    "status": "running",
                                    "job_id": job_id,
                                    "message": "Job started. Poll with get_job_result(job_id=\"{}\")".format(job_id)
                                })}],
                                "isError": False
                            }
                    else:
                        try:
                            call_result = self._call_handler(
                                handler, msg_id, arguments,
                                session_id=session_id,
                                progress_token=progress_token,
                                meta=meta,
                            )

                            # Wrap result in proper format
                            if isinstance(call_result, ToolResult):
                                result = call_result.to_dict()
                            elif isinstance(call_result, dict):
                                # structuredContent (MCP spec): required when the
                                # tool declares outputSchema, and lets mcp-apps
                                # widgets consume data without re-parsing text.
                                result = {
                                    "content": [{"type": "text", "text": json.dumps(call_result)}],
                                    "structuredContent": call_result,
                                    "isError": False
                                }
                            else:
                                result = {
                                    "content": [{"type": "text", "text": str(call_result)}],
                                    "isError": False
                                }
                        except Exception as e:
                            traceback.print_exc()
                            result = {
                                "content": [{"type": "text", "text": str(e)}],
                                "isError": True
                            }

            elif method == "resources/list":
                resources = []
                for r in self._resources.values():
                    resource_dict = r["definition"].to_dict()
                    resources.append(resource_dict)
                result = {"resources": resources}

            elif method == "resources/read":
                uri = params.get("uri")

                # Strip proxy prefix from URI for lookup
                lookup_uri = self._strip_proxy_prefix(uri) if uri else uri

                if lookup_uri not in self._resources:
                    error = {"code": -32601, "message": "Resource not found: {}".format(uri)}
                else:
                    entry = self._resources[lookup_uri]
                    handler = entry["handler"]
                    definition = entry["definition"]
                    res_mime = getattr(definition, "mimeType", None)
                    res_meta = getattr(definition, "meta", None) or {}
                    try:
                        content = self._call_handler(
                            handler, msg_id,
                            session_id=session_id,
                            progress_token=progress_token,
                            meta=meta,
                        )

                        if isinstance(content, ResourceResult):
                            result = content.to_dict()
                        elif isinstance(content, dict):
                            result = {
                                "contents": [{
                                    "uri": uri,
                                    "text": json.dumps(content)
                                }]
                            }
                        else:
                            result = {
                                "contents": [{
                                    "uri": uri,
                                    "text": str(content)
                                }]
                            }

                        # Decorate each content entry with the registered
                        # mimeType and `_meta` (mcp-apps CSP, permissions,
                        # etc.). Don't override values the handler already
                        # supplied via ResourceContent.
                        for item in result.get("contents", []):
                            if not isinstance(item, dict):
                                continue
                            if res_mime and "mimeType" not in item:
                                item["mimeType"] = res_mime
                            if res_meta and "_meta" not in item:
                                item["_meta"] = res_meta
                    except Exception as e:
                        traceback.print_exc()
                        error = {"code": -32603, "message": str(e)}

            elif method == "prompts/list":
                result = {
                    "prompts": [p["definition"].to_dict() for p in self._prompts.values()]
                }

            elif method == "prompts/get":
                prompt_name = params.get("name")
                arguments = params.get("arguments", {})

                if not isinstance(arguments, dict):
                    error = {"code": -32602,
                             "message": "prompts/get arguments must be a JSON object"}
                elif prompt_name not in self._prompts:
                    error = {"code": -32601, "message": "Prompt not found: {}".format(prompt_name)}
                else:
                    handler = self._prompts[prompt_name]["handler"]
                    try:
                        prompt_result = self._call_handler(
                            handler, msg_id, arguments,
                            session_id=session_id,
                            progress_token=progress_token,
                            meta=meta,
                        )

                        if isinstance(prompt_result, PromptResult):
                            result = prompt_result.to_dict()
                        elif isinstance(prompt_result, list):
                            # Assume list of message dicts
                            result = {"messages": prompt_result}
                        else:
                            result = {"messages": [{"role": "user", "content": {"type": "text", "text": str(prompt_result)}}]}
                    except Exception as e:
                        traceback.print_exc()
                        error = {"code": -32603, "message": str(e)}

            elif method == "logging/setLevel":
                result = {}

            else:
                error = {"code": -32601, "message": "Method not found: {}".format(method)}

        except Exception as e:
            error = {"code": -32603, "message": str(e)}
            traceback.print_exc()

        # Build response
        if msg_id is None:
            # Notification, no response
            return None

        response = {"jsonrpc": "2.0", "id": msg_id}
        if error:
            response["error"] = error
        else:
            response["result"] = result

        return response

    def _register_client(self, session_id, client_queue):
        # type: (str, _SSEQueue) -> None
        """Register an SSE queue for a client session."""
        with self._clients_lock:
            self._clients.setdefault(session_id, []).append(client_queue)

    def _unregister_client(self, session_id, client_queue):
        # type: (str, _SSEQueue) -> None
        """Remove an SSE queue for a client session.

        When the last queue for a session disconnects, drop the session's
        negotiated capabilities and any pending server-to-client requests so
        long-lived processes don't leak memory across reconnects.
        """
        session_empty = False
        with self._clients_lock:
            queues = self._clients.get(session_id, [])
            if client_queue in queues:
                queues.remove(client_queue)
            if not queues and session_id in self._clients:
                del self._clients[session_id]
                session_empty = True

        if not session_empty:
            return

        with self._sessions_lock:
            self._sessions.pop(session_id, None)

        prefix = "{}:".format(session_id)
        with self._pending_lock:
            stale_keys = [k for k in self._pending_client_requests if k.startswith(prefix)]
            for key in stale_keys:
                pending = self._pending_client_requests.pop(key, None)
                if pending and "event" in pending:
                    # Wake any waiter so it raises instead of timing out
                    pending["event"].set()

    def _client_count(self):
        # type: () -> int
        """Return the total number of active SSE queues."""
        with self._clients_lock:
            return sum(len(queues) for queues in self._clients.values())

    def _broadcast(self, message, session_id=None):
        # type: (Dict[str, Any], Optional[str]) -> None
        """Send message to SSE clients for one session."""
        if not session_id:
            return
        json_str = json.dumps(message)
        with self._clients_lock:
            queues = list(self._clients.get(session_id, []))
        for client_queue in queues:
            client_queue.append(json_str)
            # Wake any reader blocked on this queue
            event = getattr(client_queue, "_wake_event", None)
            if event is not None:
                event.set()

    def run(self, host="0.0.0.0", port=8000, path_prefix="",
            require_session_header=False, max_request_bytes=MAX_REQUEST_BYTES):
        # type: (str, int, str, bool, int) -> None
        """Start the MCP server.

        Args:
            host: Host to bind to.
            port: Port to listen on.
            path_prefix: URL path prefix (e.g. '/weber/.../') for proxy environments.
                         Routes will be matched with or without this prefix.
            require_session_header: If True, only accept session ids from the
                ``Mcp-Session-Id`` header on POSTs and omit ``session_id`` from
                the SSE endpoint URL. Use this in production deployments where
                URLs may be logged by proxies — query-string session ids are
                otherwise treated as authoritative and could be replayed by
                anyone who sees them. Defaults to False for compatibility with
                clients that only support query-string sessions.
            max_request_bytes: Largest POST body accepted before a 413 is
                returned. Defaults to ``MAX_REQUEST_BYTES`` (16 MiB). Raise
                this if your tools accept large payloads (file uploads,
                inlined images), or lower it to harden against memory abuse.
        """
        server_instance = self
        _prefix = path_prefix.rstrip("/") if path_prefix else ""
        self._path_prefix = _prefix
        _require_header = bool(require_session_header)
        _max_bytes = int(max_request_bytes)

        class MCPRequestHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                print("[{}] {}".format(self.log_date_time_string(), format % args))

            def _strip_prefix(self):
                """Strip the path prefix to get the local route."""
                path = self.path
                if _prefix and path.startswith(_prefix):
                    path = path[len(_prefix):]
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
                if _require_header:
                    return None
                query = parse_qs(urlparse(self.path).query)
                values = query.get("session_id") or query.get("sessionId")
                if values:
                    return values[0]
                return None

            def _new_session_id(self):
                """Create a session id for an SSE stream."""
                return str(uuid.uuid4())

            def _endpoint_path(self, path, session_id):
                """Build a relative endpoint path for the SSE stream.

                In header-only mode the session id is *not* embedded in the URL —
                clients must echo it back via the ``Mcp-Session-Id`` header so
                it doesn't leak through proxy access logs.
                """
                base = "{}{}".format(_prefix, path) if _prefix else path
                if _require_header:
                    return base
                separator = "&" if "?" in base else "?"
                return "{}{}session_id={}".format(base, separator, session_id)

            def do_GET(self):
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
                        "name": server_instance.name,
                        "version": server_instance.version,
                        "status": "running",
                        "tools": len(server_instance._tools),
                        "resources": len(server_instance._resources),
                        "prompts": len(server_instance._prompts),
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
                self.end_headers()

                client_queue = _SSEQueue()
                server_instance._register_client(session_id, client_queue)
                print("SSE client connected. Session: {} Total: {}".format(
                    session_id, server_instance._client_count()
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
                    server_instance._unregister_client(session_id, client_queue)

            def do_POST(self):
                try:
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
                    if content_length > _max_bytes:
                        self.send_error(413, "Request body exceeds {} bytes".format(_max_bytes))
                        return

                    post_data = self.rfile.read(content_length)

                    # Decode + parse JSON in their own scope so malformed input
                    # surfaces as a 400, not a 500 with a noisy traceback.
                    try:
                        request = json.loads(post_data.decode("utf-8"))
                    except UnicodeDecodeError as e:
                        self.send_error(400, "Request body must be UTF-8: {}".format(e))
                        return
                    except json.JSONDecodeError as e:
                        self.send_error(400, "Malformed JSON: {}".format(e))
                        return

                    session_id = self._session_id()

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
                        # Fast methods (initialize, tools/list, ping, etc.) are
                        # handled synchronously so proxy clients get the
                        # response on the HTTP reply. Async tool calls
                        # (@async_tool) return a job_id wrapper immediately
                        # (HTTP 202 + the job_id broadcast on SSE); the actual
                        # result lands in the job table and the client polls
                        # via get_job_result.
                        method = request.get("method", "") if isinstance(request, dict) else ""
                        request_params = request.get("params", {}) if isinstance(request, dict) else {}
                        tool_name = request_params.get("name", "") if isinstance(request_params, dict) else ""
                        tool_entry = server_instance._tools.get(tool_name, {})
                        is_slow = method == "tools/call" and tool_entry.get("is_async", False)
                        returns_task = (
                            is_slow and
                            server_instance._client_supports_tasks(
                                session_id=session_id,
                                params=request_params if isinstance(request_params, dict) else {},
                            )
                        )

                        if is_slow and session_id and not returns_task:
                            def async_handler():
                                try:
                                    resp = server_instance._handle_request(
                                        request, session_id=session_id
                                    )
                                    if resp:
                                        server_instance._broadcast(resp, session_id=session_id)
                                except Exception as e:
                                    print("Error in async handler: {}".format(e))
                                    traceback.print_exc()

                            t = threading.Thread(target=async_handler)
                            t.daemon = True
                            t.start()

                            body = b'{"status":"accepted"}'
                            self.send_response(202)
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                        else:
                            response = server_instance._handle_jsonrpc_payload(
                                request, session_id=session_id
                            )
                            # Streamable HTTP: deliver the response on the HTTP
                            # reply *and* over the SSE stream so clients can
                            # subscribe to either channel. Clients listening on
                            # both should dedupe by JSON-RPC id.
                            if response:
                                server_instance._broadcast(response, session_id=session_id)
                                body = json.dumps(response).encode("utf-8")
                                self.send_response(200)
                            else:
                                body = b'{"status":"accepted"}'
                                self.send_response(202)

                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                        return

                    # Legacy HTTP+SSE transport: POST / pairs with GET /sse.
                    # Per the legacy spec, responses are delivered via the SSE
                    # stream; we also echo on the HTTP reply for REST-style
                    # clients that don't open an SSE channel.
                    response = server_instance._handle_jsonrpc_payload(
                        request, session_id=session_id
                    )

                    if response:
                        server_instance._broadcast(response, session_id=session_id)
                        body = json.dumps(response).encode("utf-8")
                        self.send_response(200)
                    else:
                        body = b'{"status":"accepted"}'
                        self.send_response(202)

                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                except Exception as e:
                    print("Error handling POST: {}".format(e))
                    traceback.print_exc()
                    self.send_error(500, str(e))

            def _handle_direct_tool_call(self, tool_name, arguments):
                """Handle direct REST-style tool call (OpenAPI compatible)."""
                if tool_name not in server_instance._tools:
                    self.send_error(404, "Tool not found: {}".format(tool_name))
                    return

                # The body must be a JSON object whose keys are the tool's
                # named arguments. Arrays, strings, null, etc. are 400.
                if not isinstance(arguments, dict):
                    self._send_json_error(
                        400, "Request body must be a JSON object of tool arguments"
                    )
                    return

                handler = server_instance._tools[tool_name]["handler"]

                # Direct REST has no SSE channel, so server-to-client requests
                # (elicit/sample/list_roots/progress) cannot work here. Refuse
                # context-bearing tools with 409 and steer callers to /mcp.
                if server_instance._needs_context(handler):
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
                    result = server_instance._call_handler(handler, None, arguments)
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

            def _handle_streamable_http_get(self):
                """Handle Streamable HTTP GET - returns SSE stream for async responses."""
                session_id = self._session_id() or self._new_session_id()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Mcp-Session-Id", session_id)
                self.end_headers()

                client_queue = _SSEQueue()
                server_instance._register_client(session_id, client_queue)
                print("Streamable HTTP client connected. Session: {} Total: {}".format(
                    session_id, server_instance._client_count()
                ))

                try:
                    # Send endpoint event per MCP Streamable HTTP spec
                    self.wfile.write(b"event: open\ndata: {}\n\n")
                    endpoint = self._endpoint_path("/mcp", session_id)
                    self.wfile.write("event: endpoint\ndata: {}\n\n".format(endpoint).encode("utf-8"))
                    self.wfile.flush()

                    self._sse_pump_loop(client_queue)
                except (BrokenPipeError, ConnectionResetError) as e:
                    print("Streamable HTTP client disconnected: {}".format(e))
                except Exception:
                    traceback.print_exc()
                finally:
                    server_instance._unregister_client(session_id, client_queue)

            def _handle_openapi(self):
                """Return OpenAPI schema for tool discovery."""
                tools_paths = {}
                for tool_name, tool_info in server_instance._tools.items():
                    # Skip context-requiring tools — they need an MCP session
                    # (server-to-client requests over SSE) and cannot be called
                    # via the stateless /tools/<name> REST endpoint.
                    if server_instance._needs_context(tool_info["handler"]):
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
                        "title": server_instance.name,
                        "version": server_instance.version,
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
                discovery = {
                    "mcpVersion": "2024-11-05",
                    "serverInfo": {
                        "name": server_instance.name,
                        "version": server_instance.version
                    },
                    "capabilities": server_instance._get_capabilities().to_dict(),
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

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer((host, port), MCPRequestHandler)
        print("MCP Server '{}' v{} listening on {}:{}".format(self.name, self.version, host, port))
        print("  Tools: {}".format(len(self._tools)))
        print("  Resources: {}".format(len(self._resources)))
        print("  Prompts: {}".format(len(self._prompts)))
        print("Endpoints:")
        print("  SSE transport:        http://{}:{}/sse".format(host, port))
        print("  Streamable HTTP:      http://{}:{}/mcp".format(host, port))
        print("  OpenAPI schema:       http://{}:{}/openapi.json".format(host, port))
        print("  MCP discovery:        http://{}:{}/.well-known/mcp.json".format(host, port))
        print("  Direct tool calls:    http://{}:{}/tools/<name>".format(host, port))

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")
            server.shutdown()
