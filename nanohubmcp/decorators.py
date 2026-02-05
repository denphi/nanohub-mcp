"""
Decorators for defining MCP tools, resources, and prompts.
Compatible with Python 3.6+ and aligned with FastMCP API.
"""

from __future__ import print_function

import inspect
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Set

# get_type_hints may fail on some Python 3.6 edge cases
try:
    from typing import get_type_hints
except ImportError:
    get_type_hints = None


def _python_type_to_json_schema(py_type):
    # type: (Any) -> Dict[str, Any]
    """Convert Python type hints to JSON Schema types."""
    if py_type is None or py_type is type(None):
        return {"type": "null"}
    if py_type is str:
        return {"type": "string"}
    if py_type is int:
        return {"type": "integer"}
    if py_type is float:
        return {"type": "number"}
    if py_type is bool:
        return {"type": "boolean"}
    if py_type is list or (hasattr(py_type, "__origin__") and py_type.__origin__ is list):
        return {"type": "array"}
    if py_type is dict or (hasattr(py_type, "__origin__") and py_type.__origin__ is dict):
        return {"type": "object"}
    # Default to string for unknown types
    return {"type": "string"}


def _generate_input_schema(func, exclude_params=None):
    # type: (Callable, Optional[Set[str]]) -> Dict[str, Any]
    """Generate JSON Schema from function signature."""
    exclude = exclude_params or set()
    exclude.update({"self", "cls", "ctx", "context"})

    sig = inspect.signature(func)

    hints = {}
    if get_type_hints is not None:
        try:
            hints = get_type_hints(func)
        except Exception:
            hints = {}

    properties = {}
    required = []

    for name, param in sig.parameters.items():
        if name in exclude:
            continue

        prop = {}

        # Get type from hints or annotation
        if name in hints:
            prop = _python_type_to_json_schema(hints[name])
        elif param.annotation is not inspect.Parameter.empty:
            prop = _python_type_to_json_schema(param.annotation)
        else:
            prop = {"type": "string"}

        properties[name] = prop

        # Check if required (no default value)
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required
    }


def tool(
    name=None,  # type: Optional[str]
    description=None,  # type: Optional[str]
    tags=None,  # type: Optional[Set[str]]
    meta=None,  # type: Optional[Dict[str, Any]]
    input_schema=None  # type: Optional[Dict[str, Any]]
):
    # type: (...) -> Callable
    """
    Decorator to register a function as an MCP tool.
    Aligned with FastMCP @mcp.tool decorator.

    Args:
        name: Tool name (defaults to function name)
        description: Tool description (defaults to docstring)
        tags: Optional set of tags for categorization
        meta: Optional metadata dictionary
        input_schema: JSON Schema for inputs (auto-generated if not provided)

    Example:
        @tool
        def add(a: int, b: int) -> int:
            '''Add two numbers'''
            return a + b

        @tool(name="multiply", tags={"math"})
        def mult(a: int, b: int) -> int:
            '''Multiply two numbers'''
            return a * b
    """
    def decorator(func):
        # type: (Callable) -> Callable
        # Store metadata on the function
        func._mcp_tool = True
        func._mcp_tool_name = name or func.__name__
        func._mcp_tool_description = description or (func.__doc__ or "").strip()
        func._mcp_tool_input_schema = input_schema or _generate_input_schema(func)
        func._mcp_tool_tags = tags or set()
        func._mcp_tool_meta = meta or {}

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        # Copy metadata to wrapper
        wrapper._mcp_tool = func._mcp_tool
        wrapper._mcp_tool_name = func._mcp_tool_name
        wrapper._mcp_tool_description = func._mcp_tool_description
        wrapper._mcp_tool_input_schema = func._mcp_tool_input_schema
        wrapper._mcp_tool_tags = func._mcp_tool_tags
        wrapper._mcp_tool_meta = func._mcp_tool_meta

        return wrapper

    # Handle @tool vs @tool()
    if callable(name):
        func = name
        name = None
        return decorator(func)

    return decorator


def resource(
    uri,  # type: str
    name=None,  # type: Optional[str]
    description=None,  # type: Optional[str]
    mime_type=None,  # type: Optional[str]
    tags=None,  # type: Optional[Set[str]]
    meta=None  # type: Optional[Dict[str, Any]]
):
    # type: (...) -> Callable
    """
    Decorator to register a function as an MCP resource.
    Aligned with FastMCP @mcp.resource decorator.

    Args:
        uri: Resource URI (e.g., "file:///path" or "config://settings")
             Supports templates: "weather://{city}/current"
        name: Resource name (defaults to function name)
        description: Resource description (defaults to docstring)
        mime_type: MIME type of the resource content
        tags: Optional set of tags for categorization
        meta: Optional metadata dictionary

    Example:
        @resource("config://app/settings")
        def get_settings():
            '''Application settings'''
            return {"theme": "dark"}

        @resource("data://{id}", mime_type="application/json")
        def get_data(id: str):
            return {"id": id, "value": 42}
    """
    def decorator(func):
        # type: (Callable) -> Callable
        func._mcp_resource = True
        func._mcp_resource_uri = uri
        func._mcp_resource_name = name or func.__name__
        func._mcp_resource_description = description or (func.__doc__ or "").strip()
        func._mcp_resource_mime_type = mime_type
        func._mcp_resource_tags = tags or set()
        func._mcp_resource_meta = meta or {}

        # Check if URI has template parameters
        func._mcp_resource_is_template = "{" in uri

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper._mcp_resource = func._mcp_resource
        wrapper._mcp_resource_uri = func._mcp_resource_uri
        wrapper._mcp_resource_name = func._mcp_resource_name
        wrapper._mcp_resource_description = func._mcp_resource_description
        wrapper._mcp_resource_mime_type = func._mcp_resource_mime_type
        wrapper._mcp_resource_tags = func._mcp_resource_tags
        wrapper._mcp_resource_meta = func._mcp_resource_meta
        wrapper._mcp_resource_is_template = func._mcp_resource_is_template

        return wrapper

    return decorator


def prompt(
    name=None,  # type: Optional[str]
    description=None,  # type: Optional[str]
    tags=None,  # type: Optional[Set[str]]
    meta=None  # type: Optional[Dict[str, Any]]
):
    # type: (...) -> Callable
    """
    Decorator to register a function as an MCP prompt.
    Aligned with FastMCP @mcp.prompt decorator.

    Args:
        name: Prompt name (defaults to function name)
        description: Prompt description (defaults to docstring)
        tags: Optional set of tags for categorization
        meta: Optional metadata dictionary

    Example:
        @prompt
        def ask_about_topic(topic: str) -> str:
            '''Generates a question about a topic'''
            return "Can you explain {}?".format(topic)

        @prompt
        def code_review(code: str) -> list:
            '''Generate a code review prompt'''
            return [
                Message("Please review this code:\\n{}".format(code)),
                Message("I'll analyze it.", role="assistant")
            ]
    """
    def decorator(func):
        # type: (Callable) -> Callable
        func._mcp_prompt = True
        func._mcp_prompt_name = name or func.__name__
        func._mcp_prompt_description = description or (func.__doc__ or "").strip()
        func._mcp_prompt_tags = tags or set()
        func._mcp_prompt_meta = meta or {}
        func._mcp_prompt_arguments = []

        # Auto-generate arguments from signature
        sig = inspect.signature(func)
        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls", "ctx", "context"):
                continue
            arg = {
                "name": param_name,
                "required": param.default is inspect.Parameter.empty
            }
            func._mcp_prompt_arguments.append(arg)

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper._mcp_prompt = func._mcp_prompt
        wrapper._mcp_prompt_name = func._mcp_prompt_name
        wrapper._mcp_prompt_description = func._mcp_prompt_description
        wrapper._mcp_prompt_arguments = func._mcp_prompt_arguments
        wrapper._mcp_prompt_tags = func._mcp_prompt_tags
        wrapper._mcp_prompt_meta = func._mcp_prompt_meta

        return wrapper

    # Handle @prompt vs @prompt()
    if callable(name):
        func = name
        name = None
        return decorator(func)

    return decorator
