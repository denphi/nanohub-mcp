#!/usr/bin/env python
"""
Simple Calculator MCP Server.

Demonstrates the core decorators:

    * ``@server.tool``        — synchronous tool
    * ``@server.tool`` w/ ctx — context-aware tool (logging, progress)
    * ``@server.async_tool``  — long-running tool (returns a job_id)
    * ``@server.resource``    — read-only resource
    * ``@server.prompt``      — prompt template

Run with::

    start_mcp --app start_mcp.py
    start_mcp --app start_mcp.py --python-env AIIDA

Or directly::

    python start_mcp.py 8000
"""

from __future__ import print_function

import os
import sys
import time

# Add package path for development
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from nanohubmcp import MCPServer, Context, ToolResult

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
    # type: (float, float) -> ToolResult
    """Divide a by b. Returns a ToolResult so div-by-zero is a tool error
    rather than a transport-level exception."""
    if float(b) == 0:
        return ToolResult(content="Cannot divide by zero", is_error=True)
    return ToolResult(content=str(float(a) / float(b)))


@server.tool()
def factorial_with_progress(ctx, n):
    # type: (Context, int) -> int
    """Compute n! while emitting progress notifications.

    The client must include ``_meta.progressToken`` in the call to receive the
    notifications; otherwise progress is silently suppressed.
    """
    n = int(n)
    if n < 0:
        raise ValueError("n must be non-negative")
    result = 1
    for i in range(1, n + 1):
        result *= i
        ctx.report_progress(progress=i, total=n, message="multiplied by {}".format(i))
    return result


@server.async_tool()
def slow_sum(values, delay=0.0):
    # type: (str, float) -> float
    """Sum a comma-separated list of numbers, optionally sleeping first.

    Demonstrates async tools: the call returns a job_id immediately and the
    client polls ``get_job_result(job_id=...)`` for the answer. Use this
    pattern for tools that take longer than a proxy timeout.
    """
    time.sleep(float(delay))
    return sum(float(v.strip()) for v in str(values).split(",") if v.strip())


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
