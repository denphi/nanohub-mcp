#!/usr/bin/env python
"""
Simple Calculator MCP Server

Run with:
    start_mcp --app start_mcp.py
    start_mcp --app start_mcp.py --python-env AIIDA

Connect to:
    - http://localhost:8000/sse for SSE stream
    - POST to http://localhost:8000 to send messages
"""

from __future__ import print_function

import os
import sys

# Add package path for development
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from nanohubmcp import MCPServer, Context

# Create server instance
server = MCPServer("simple-calculator", version="1.0.0")


@server.tool()
def add(a, b):
    # type: (float, float) -> float
    """Add two numbers together."""
    return float(a) + float(b)


@server.tool(tags={"math", "advanced"})
def power(ctx, base, exponent):
    # type: (Context, float, float) -> float
    """Raise base to the power of exponent. Demonstrates Context usage."""
    ctx.info("Computing {}^{}".format(base, exponent))
    return float(base) ** float(exponent)


@server.tool()
def subtract(a, b):
    # type: (float, float) -> float
    """Subtract b from a."""
    return float(a) - float(b)


@server.tool()
def multiply(a, b):
    # type: (float, float) -> float
    """Multiply two numbers."""
    return float(a) * float(b)


@server.tool()
def divide(a, b):
    # type: (float, float) -> float
    """Divide a by b."""
    if float(b) == 0:
        raise ValueError("Cannot divide by zero")
    return float(a) / float(b)


@server.resource("config://calculator/settings")
def get_settings():
    """Get calculator settings."""
    return {
        "precision": 10,
        "max_value": 1e308,
        "supported_operations": ["add", "subtract", "multiply", "divide", "power"]
    }


@server.prompt()
def calculate(expression):
    # type: (str) -> list
    """Generate a calculation prompt."""
    return [
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": "Please calculate: {}".format(expression)
            }
        }
    ]


def main():
    port = int(os.environ.get("MCP_PORT", 8000))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    server.run(port=port)


if __name__ == "__main__":
    main()
