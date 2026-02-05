"""
nanoHUB MCP Server Library

A Python library for creating Model Context Protocol (MCP) servers
that integrate with nanoHUB/HubZero tools infrastructure.

Example usage:
    from nanohubmcp import MCPServer

    server = MCPServer("my-tool")

    @server.tool()
    def calculate(expression: str) -> str:
        '''Evaluate a mathematical expression'''
        return str(eval(expression))

    @server.resource("config://settings")
    def get_settings():
        '''Get application settings'''
        return {"theme": "dark", "version": "1.0"}

    server.run()

Run with:
    start_mcp --app my_server.py
    start_mcp --app my_server.py --python-env AIIDA
"""

from .server import MCPServer
from .decorators import tool, resource, prompt
from .context import Context
from .types import (
    Tool,
    Resource,
    Prompt,
    # Primary type names (FastMCP aligned)
    ToolResult,
    ResourceResult,
    ResourceContent,
    PromptResult,
    Message,
    TextContent,
    ImageContent,
    Image,
    Role,
    # Backwards compatibility aliases
    CallToolResult,
    ReadResourceResult,
    GetPromptResult,
    PromptMessage,
)

from ._version import __version__
__all__ = [
    "MCPServer",
    "tool",
    "resource",
    "prompt",
    "Context",
    "Tool",
    "Resource",
    "Prompt",
    # Primary type names (FastMCP aligned)
    "ToolResult",
    "ResourceResult",
    "ResourceContent",
    "PromptResult",
    "Message",
    "TextContent",
    "ImageContent",
    "Image",
    "Role",
    # Backwards compatibility aliases
    "CallToolResult",
    "ReadResourceResult",
    "GetPromptResult",
    "PromptMessage",
]
