# nanohub-mcp

A Python library for creating Model Context Protocol (MCP) servers that integrate with nanoHUB/HubZero tools infrastructure.

## Installation

```bash
pip install nanohub-mcp
```

## Quick Start

### Using Decorators

```python
from nanohubmcp import MCPServer

server = MCPServer("my-tool")

@server.tool()
def add(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b

@server.tool()
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""
    return a * b

@server.resource("config://settings")
def get_settings():
    """Get application settings."""
    return {"theme": "dark", "precision": 2}

server.run(port=8000)
```

### Using Folder Structure

Create a project structure:

```
my-tool/
├── mcp.json
├── tools/
│   ├── calculator.py
│   └── simulator.py
├── resources/
│   └── config.py
└── prompts/
    └── templates.py
```

**mcp.json:**
```json
{
    "name": "my-tool",
    "version": "1.0.0",
    "tools_dir": "./tools",
    "resources_dir": "./resources",
    "prompts_dir": "./prompts"
}
```

**tools/calculator.py:**
```python
from nanohubmcp import tool

@tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b

@tool()
def subtract(a: float, b: float) -> float:
    """Subtract b from a."""
    return a - b
```

**Run the server:**
```python
from nanohubmcp import MCPServer

server = MCPServer.from_config("mcp.json")
server.run()
```

### Using CLI

```bash
# Initialize a new project
nanohub-mcp init --name my-tool

# Run the server
cd my-tool
nanohub-mcp run --port 8000
```

## API Reference

### MCPServer

The main server class.

```python
from nanohubmcp import MCPServer

# Basic initialization
server = MCPServer("my-server", version="1.0.0")

# With auto-discovery directories
server = MCPServer(
    "my-server",
    tools_dir="./tools",
    resources_dir="./resources",
    prompts_dir="./prompts"
)

# From config file
server = MCPServer.from_config("mcp.json")

# Run the server
server.run(host="0.0.0.0", port=8000)
```

### @tool Decorator

Register a function as an MCP tool.

```python
from nanohubmcp import tool

@tool()
def my_function(arg1: str, arg2: int = 10) -> str:
    """Tool description from docstring."""
    return f"Result: {arg1}, {arg2}"

# With explicit configuration
@tool(
    name="custom_name",
    description="Custom description",
    input_schema={
        "type": "object",
        "properties": {
            "arg1": {"type": "string", "description": "First argument"}
        },
        "required": ["arg1"]
    }
)
def another_function(arg1: str):
    return arg1
```

### @resource Decorator

Register a function as an MCP resource.

```python
from nanohubmcp import resource

@resource("file:///data/config.json")
def read_config():
    """Read configuration file."""
    return {"setting": "value"}

@resource("config://app/settings", mime_type="application/json")
def get_settings():
    return {"theme": "dark"}
```

### @prompt Decorator

Register a function as an MCP prompt template.

```python
from nanohubmcp import prompt

@prompt()
def explain(topic: str):
    """Generate an explanation prompt."""
    return [
        {"role": "user", "content": {"type": "text", "text": f"Explain {topic} in simple terms."}}
    ]
```

## MCP Protocol

This library implements the Model Context Protocol (MCP) using HTTP + Server-Sent Events (SSE) transport:

- **GET /sse** - SSE endpoint for receiving responses
- **POST /** - Send JSON-RPC requests
- **GET /** - Server info

### Supported Methods

- `initialize` - Initialize the connection
- `ping` - Health check
- `tools/list` - List available tools
- `tools/call` - Call a tool
- `resources/list` - List available resources
- `resources/read` - Read a resource
- `prompts/list` - List available prompts
- `prompts/get` - Get a prompt

## Integration with nanoHUB

This library is designed to work with nanoHUB's tool infrastructure. When deployed as a nanoHUB tool, the MCP server is automatically proxied through the HubZero API:

```
GET  /api/mcp/{toolname}/sse    - Connect to SSE stream
POST /api/mcp/{toolname}        - Send messages
```

## License

MIT License - see LICENSE file for details.
