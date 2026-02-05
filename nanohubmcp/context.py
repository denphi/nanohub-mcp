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
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> None
        self._server = server
        self._request_id = request_id
        self._meta = meta or {}
        self._log_messages = []  # type: List[Dict[str, Any]]

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

        # If server is available, could broadcast progress to clients
        if self._server and hasattr(self._server, "_broadcast"):
            self._server._broadcast({
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {
                    "requestId": self._request_id,
                    **progress_info
                }
            })
