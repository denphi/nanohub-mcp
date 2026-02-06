"""
MCP Server implementation with HTTP + SSE transport.
Compatible with Python 3.6+.

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
import time
import traceback

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:
    # Python 2 fallback (not officially supported but helps with syntax)
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn

from typing import Any, Callable, Dict, List, Optional, Set

from .types import (
    Tool, Resource, Prompt, TextContent, ImageContent,
    ToolResult, ResourceResult, ResourceContent,
    PromptResult, Message, Role,
    ServerCapabilities, ServerInfo
)
from .decorators import tool, resource, prompt
from .context import Context


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
        self._clients = []  # type: List[List]

    def _register_tool_function(self, func):
        # type: (Callable) -> None
        """Register a decorated tool function."""
        name = func._mcp_tool_name
        self._tools[name] = {
            "definition": Tool(
                name=name,
                description=func._mcp_tool_description,
                inputSchema=func._mcp_tool_input_schema
            ),
            "handler": func
        }

    def _register_resource_function(self, func):
        # type: (Callable) -> None
        """Register a decorated resource function."""
        uri = func._mcp_resource_uri
        self._resources[uri] = {
            "definition": Resource(
                uri=uri,
                name=func._mcp_resource_name,
                description=func._mcp_resource_description,
                mimeType=func._mcp_resource_mime_type
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
        input_schema=None  # type: Optional[Dict[str, Any]]
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
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = tool(name, description, tags, meta, input_schema)(func)
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
        return ServerCapabilities(
            tools=len(self._tools) > 0,
            resources=len(self._resources) > 0,
            prompts=len(self._prompts) > 0,
            logging=True
        )

    def _needs_context(self, func):
        # type: (Callable) -> bool
        """Check if a function accepts a context parameter."""
        try:
            sig = inspect.signature(func)
            return "ctx" in sig.parameters or "context" in sig.parameters
        except (ValueError, TypeError):
            return False

    def _call_handler(self, handler, request_id, arguments=None):
        # type: (Callable, Optional[str], Optional[Dict[str, Any]]) -> Any
        """Call a handler, injecting context if needed."""
        if arguments is None:
            arguments = {}

        if self._needs_context(handler):
            ctx = Context(server=self, request_id=request_id)
            # Determine which parameter name to use
            sig = inspect.signature(handler)
            if "ctx" in sig.parameters:
                arguments["ctx"] = ctx
            elif "context" in sig.parameters:
                arguments["context"] = ctx

        return handler(**arguments)

    def _handle_request(self, request):
        # type: (Dict[str, Any]) -> Optional[Dict[str, Any]]
        """Handle a JSON-RPC request and return response."""
        method = request.get("method", "")
        msg_id = request.get("id")
        params = request.get("params", {})

        result = None
        error = None

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": ServerInfo(self.name, self.version).to_dict(),
                    "capabilities": self._get_capabilities().to_dict()
                }

            elif method == "initialized":
                # Notification, no response needed
                return None

            elif method == "ping":
                result = {}

            elif method == "tools/list":
                result = {
                    "tools": [t["definition"].to_dict() for t in self._tools.values()]
                }

            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})

                if tool_name not in self._tools:
                    error = {"code": -32601, "message": "Tool not found: {}".format(tool_name)}
                else:
                    handler = self._tools[tool_name]["handler"]
                    try:
                        call_result = self._call_handler(handler, msg_id, arguments)

                        # Wrap result in proper format
                        if isinstance(call_result, ToolResult):
                            result = call_result.to_dict()
                        elif isinstance(call_result, dict):
                            result = {
                                "content": [{"type": "text", "text": json.dumps(call_result)}],
                                "isError": False
                            }
                        else:
                            result = {
                                "content": [{"type": "text", "text": str(call_result)}],
                                "isError": False
                            }
                    except Exception as e:
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
                    handler = self._resources[lookup_uri]["handler"]
                    try:
                        content = self._call_handler(handler, msg_id)

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
                    except Exception as e:
                        error = {"code": -32603, "message": str(e)}

            elif method == "prompts/list":
                result = {
                    "prompts": [p["definition"].to_dict() for p in self._prompts.values()]
                }

            elif method == "prompts/get":
                prompt_name = params.get("name")
                arguments = params.get("arguments", {})

                if prompt_name not in self._prompts:
                    error = {"code": -32601, "message": "Prompt not found: {}".format(prompt_name)}
                else:
                    handler = self._prompts[prompt_name]["handler"]
                    try:
                        prompt_result = self._call_handler(handler, msg_id, arguments)

                        if isinstance(prompt_result, PromptResult):
                            result = prompt_result.to_dict()
                        elif isinstance(prompt_result, list):
                            # Assume list of message dicts
                            result = {"messages": prompt_result}
                        else:
                            result = {"messages": [{"role": "user", "content": {"type": "text", "text": str(prompt_result)}}]}
                    except Exception as e:
                        error = {"code": -32603, "message": str(e)}

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

    def _broadcast(self, message):
        # type: (Dict[str, Any]) -> None
        """Send message to all connected SSE clients."""
        json_str = json.dumps(message)
        for client_queue in self._clients:
            client_queue.append(json_str)

    def run(self, host="0.0.0.0", port=8000, path_prefix=""):
        # type: (str, int, str) -> None
        """Start the MCP server.

        Args:
            host: Host to bind to.
            port: Port to listen on.
            path_prefix: URL path prefix (e.g. '/weber/.../') for proxy environments.
                         Routes will be matched with or without this prefix.
        """
        server_instance = self
        _prefix = path_prefix.rstrip("/") if path_prefix else ""

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
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
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
                    self.wfile.write(json.dumps(info).encode("utf-8"))

            def _handle_sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                client_queue = []
                server_instance._clients.append(client_queue)
                print("SSE client connected. Total: {}".format(len(server_instance._clients)))

                try:
                    # Send connection event
                    self.wfile.write(b"event: open\ndata: {}\n\n")
                    self.wfile.flush()

                    while True:
                        if client_queue:
                            msg = client_queue.pop(0)
                            self.wfile.write("event: message\ndata: {}\n\n".format(msg).encode("utf-8"))
                            self.wfile.flush()
                        time.sleep(0.05)
                except Exception as e:
                    print("SSE client disconnected: {}".format(e))
                finally:
                    if client_queue in server_instance._clients:
                        server_instance._clients.remove(client_queue)

            def do_POST(self):
                try:
                    path = self._strip_prefix()
                    path_only = path.split("?")[0]
                    content_length = int(self.headers.get("Content-Length", 0))
                    post_data = self.rfile.read(content_length)
                    request = json.loads(post_data.decode("utf-8"))

                    # Check for direct tool call via /tools/{name}
                    if path_only.startswith("/tools/"):
                        tool_name = path_only[7:]  # Strip "/tools/"
                        self._handle_direct_tool_call(tool_name, request)
                        return

                    # Standard MCP JSON-RPC handling (for /mcp and root POST)
                    print("Received: {}".format(request.get("method", "unknown")))

                    response = server_instance._handle_request(request)

                    # Broadcast to SSE clients
                    if response:
                        server_instance._broadcast(response)

                    # Return response synchronously (for non-SSE clients)
                    if response:
                        body = json.dumps(response).encode("utf-8")
                        self.send_response(200)
                    else:
                        body = b"{\"status\":\"accepted\"}"
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

                try:
                    handler = server_instance._tools[tool_name]["handler"]
                    result = server_instance._call_handler(handler, None, arguments)

                    # Format result
                    if isinstance(result, dict):
                        body = json.dumps(result).encode("utf-8")
                    else:
                        body = json.dumps({"result": str(result)}).encode("utf-8")

                    self.send_response(200)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                except Exception as e:
                    error_body = json.dumps({"error": str(e)}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(error_body)))
                    self.end_headers()
                    self.wfile.write(error_body)

            def _handle_streamable_http_get(self):
                """Handle Streamable HTTP GET - returns SSE stream for async responses."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                client_queue = []
                server_instance._clients.append(client_queue)

                try:
                    # Send endpoint event per MCP Streamable HTTP spec
                    self.wfile.write(b"event: endpoint\ndata: /mcp\n\n")
                    self.wfile.flush()

                    while True:
                        if client_queue:
                            msg = client_queue.pop(0)
                            self.wfile.write("event: message\ndata: {}\n\n".format(msg).encode("utf-8"))
                            self.wfile.flush()
                        time.sleep(0.05)
                except Exception as e:
                    print("Streamable HTTP client disconnected: {}".format(e))
                finally:
                    if client_queue in server_instance._clients:
                        server_instance._clients.remove(client_queue)

            def _handle_openapi(self):
                """Return OpenAPI schema for tool discovery."""
                tools_paths = {}
                for tool_name, tool_info in server_instance._tools.items():
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
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id")
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
