"""
Context class for MCP tools and resources.
Provides access to server context within decorated handlers.
Aligned with FastMCP Context API.
"""

from __future__ import print_function

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .server import MCPServer


class Context(object):
    """
    Context object passed to tools and resources.
    Aligned with FastMCP Context API.

    Provides access to:
    - Server information
    - Logging capabilities
    - Request metadata

    Usage:
        @server.tool()
        def my_tool(ctx: Context, arg1: str) -> str:
            ctx.info("Processing arg1: {}".format(arg1))
            return "result"
    """

    def __init__(
        self,
        server=None,  # type: Optional[MCPServer]
        request_id=None,  # type: Optional[str]
        session_id=None,  # type: Optional[str]
        meta=None,  # type: Optional[Dict[str, Any]]
        progress_token=None  # type: Optional[Any]
    ):
        # type: (...) -> None
        self._server = server
        self._request_id = request_id
        self._session_id = session_id
        self._meta = meta or {}
        self._progress_token = progress_token
        self._log_messages = []  # type: List[Dict[str, Any]]

    @property
    def progress_token(self):
        # type: () -> Optional[Any]
        """Token associated with this request for progress notifications."""
        return self._progress_token

    @property
    def server(self):
        # type: () -> Optional[MCPServer]
        """Get the server instance."""
        return self._server

    @property
    def request_id(self):
        # type: () -> Optional[str]
        """Get the current request ID."""
        return self._request_id

    @property
    def session_id(self):
        # type: () -> Optional[str]
        """Get the current client session ID."""
        return self._session_id

    @property
    def meta(self):
        # type: () -> Dict[str, Any]
        """Get request metadata."""
        return self._meta

    def debug(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Log a debug message."""
        self._log("debug", message, kwargs)

    def info(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Log an info message."""
        self._log("info", message, kwargs)

    def warning(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Log a warning message."""
        self._log("warning", message, kwargs)

    def error(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Log an error message."""
        self._log("error", message, kwargs)

    def _log(self, level, message, data):
        # type: (str, str, Dict[str, Any]) -> None
        """Internal logging method."""
        log_entry = {
            "level": level,
            "message": message,
            "data": data
        }
        self._log_messages.append(log_entry)
        # Also print to console
        print("[{}] {}".format(level.upper(), message))

    def get_log_messages(self):
        # type: () -> List[Dict[str, Any]]
        """Get all logged messages for this context."""
        return self._log_messages

    def elicit(self, message, requested_schema=None, timeout=60):
        # type: (str, Optional[Dict[str, Any]], int) -> Dict[str, Any]
        """Request structured user input from the connected MCP client."""
        if not self._server:
            raise RuntimeError("Context is not attached to a server")
        return self._server.request_elicitation(
            session_id=self._session_id,
            message=message,
            requested_schema=requested_schema,
            mode="form",
            timeout=timeout
        )

    def elicit_url(self, message, url, elicitation_id=None, timeout=60):
        # type: (str, str, Optional[str], int) -> Dict[str, Any]
        """Request URL-mode elicitation from the connected MCP client."""
        if not self._server:
            raise RuntimeError("Context is not attached to a server")
        return self._server.request_elicitation(
            session_id=self._session_id,
            message=message,
            mode="url",
            url=url,
            elicitation_id=elicitation_id,
            timeout=timeout
        )

    def sample(self, params, timeout=60):
        # type: (Dict[str, Any], int) -> Dict[str, Any]
        """Request sampling from the connected MCP client."""
        if not self._server:
            raise RuntimeError("Context is not attached to a server")
        return self._server.request_sampling(self._session_id, params, timeout=timeout)

    def list_roots(self, timeout=60):
        # type: (int) -> Dict[str, Any]
        """Request roots from the connected MCP client."""
        if not self._server:
            raise RuntimeError("Context is not attached to a server")
        return self._server.request_roots(self._session_id, timeout=timeout)

    def report_progress(self, progress, total=None, message=None):
        # type: (float, Optional[float], Optional[str]) -> None
        """
        Report progress for long-running operations.
        Aligned with FastMCP progress reporting.

        Args:
            progress: Current progress value
            total: Total expected value (optional)
            message: Progress message (optional)
        """
        progress_info = {"progress": progress}
        if total is not None:
            progress_info["total"] = total
        if message:
            progress_info["message"] = message

        self.info("Progress: {}".format(progress_info))

        # MCP spec: notifications/progress must carry the progressToken that
        # the client originally sent in the request's _meta.progressToken so
        # the client can correlate the notification with its in-flight call.
        # If no token was provided, suppress the broadcast — emitting an
        # uncorrelated notification would just be noise to the client.
        if (
            self._progress_token is not None
            and self._server
            and hasattr(self._server, "_broadcast")
        ):
            params = {"progressToken": self._progress_token}
            params.update(progress_info)
            self._server._broadcast({
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": params
            }, session_id=self._session_id)
