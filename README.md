# nanohub-mcp

A zero-dependency Python library for creating [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers that integrate with nanoHUB/HubZero tools infrastructure.

**Features:**
- Zero external dependencies (stdlib only)
- Python 3.6+ compatible
- SSE and Streamable HTTP transports
- OpenAPI schema auto-generation
- Direct REST-style tool calls
- nanoHUB proxy integration out of the box
- Context injection for logging and progress reporting

## Installation

```bash
pip install nanohub-mcp
```

## Quick Start

Create a file called `start_mcp.py`:

```python
from nanohubmcp import MCPServer

server = MCPServer("my-calculator", version="1.0.0")

@server.tool()
def add(a, b):
    # type: (float, float) -> float
    """Add two numbers together."""
    return float(a) + float(b)

@server.tool()
def multiply(a, b):
    # type: (float, float) -> float
    """Multiply two numbers."""
    return float(a) * float(b)

@server.resource("config://settings")
def get_settings():
    """Get application settings."""
    return {"precision": 10}

@server.prompt()
def calculate(expression):
    # type: (str) -> list
    """Generate a calculation prompt."""
    return [
        {
            "role": "user",
            "content": {"type": "text", "text": "Please calculate: {}".format(expression)}
        }
    ]

if __name__ == "__main__":
    server.run(port=8000)
```

Run it:

```bash
python start_mcp.py
```

The server starts and prints all available endpoints:

```
MCP Server 'my-calculator' v1.0.0 listening on 0.0.0.0:8000
  Tools: 2
  Resources: 1
  Prompts: 1
Endpoints:
  SSE transport:        http://0.0.0.0:8000/sse
  Streamable HTTP:      http://0.0.0.0:8000/mcp
  OpenAPI schema:       http://0.0.0.0:8000/openapi.json
  MCP discovery:        http://0.0.0.0:8000/.well-known/mcp.json
  Direct tool calls:    http://0.0.0.0:8000/tools/<name>
```

---

## Server Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Server info (name, version, tool/resource/prompt counts) |
| `/sse` | GET | SSE transport — streams responses as Server-Sent Events |
| `/mcp` | GET | Streamable HTTP — SSE stream with `endpoint` event |
| `/mcp` | POST | Streamable HTTP — accepts JSON-RPC requests |
| `/` | POST | JSON-RPC endpoint (same as `/mcp` POST) |
| `/tools/<name>` | POST | Direct REST-style tool call (OpenAPI compatible) |
| `/openapi.json` | GET | Auto-generated OpenAPI 3.1 schema |
| `/.well-known/mcp.json` | GET | MCP discovery document |

---

## Testing Your Server

### 1. Server Info

```bash
curl http://localhost:8000/
```

```json
{
  "name": "my-calculator",
  "version": "1.0.0",
  "status": "running",
  "tools": 2,
  "resources": 1,
  "prompts": 1,
  "endpoints": {"sse": "/sse", "mcp": "/mcp", "openapi": "/openapi.json"}
}
```

### 2. MCP JSON-RPC (Standard Protocol)

**Initialize:**

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "serverInfo": {"name": "my-calculator", "version": "1.0.0"},
    "capabilities": {"tools": {}, "resources": {}, "prompts": {}, "logging": {}}
  }
}
```

**List tools:**

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

**Call a tool:**

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add","arguments":{"a":2,"b":3}}}'
```

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "5.0"}],
    "isError": false
  }
}
```

**List resources:**

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"resources/list","params":{}}'
```

**Read a resource:**

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":5,"method":"resources/read","params":{"uri":"config://settings"}}'
```

**List prompts:**

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":6,"method":"prompts/list","params":{}}'
```

**Get a prompt:**

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":7,"method":"prompts/get","params":{"name":"calculate","arguments":{"expression":"2+2"}}}'
```

**Ping:**

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":8,"method":"ping","params":{}}'
```

### 3. Direct Tool Calls (REST/OpenAPI Style)

Call tools directly without JSON-RPC wrapping:

```bash
curl -X POST http://localhost:8000/tools/add \
  -H "Content-Type: application/json" \
  -d '{"a": 7, "b": 3}'
```

```json
{"result": "10.0"}
```

### 4. SSE Transport

Connect to the SSE stream to receive real-time responses:

```bash
curl -N http://localhost:8000/sse
```

In a separate terminal, send a request:

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
```

The SSE stream will emit:

```
event: open
data: {}

event: message
data: {"jsonrpc":"2.0","id":1,"result":{}}
```

### 5. OpenAPI Schema

```bash
curl http://localhost:8000/openapi.json
```

Returns a full OpenAPI 3.1 schema with all tools exposed as POST endpoints.

### 6. MCP Discovery

```bash
curl http://localhost:8000/.well-known/mcp.json
```

```json
{
  "mcpVersion": "2024-11-05",
  "serverInfo": {"name": "my-calculator", "version": "1.0.0"},
  "capabilities": {"tools": {}, "resources": {}, "prompts": {}, "logging": {}},
  "transports": [
    {"type": "sse", "endpoint": "/sse"},
    {"type": "streamable-http", "endpoint": "/mcp"}
  ]
}
```

### 7. Python Test Client

```python
import json
try:
    from http.client import HTTPConnection
except ImportError:
    from httplib import HTTPConnection

conn = HTTPConnection("localhost", 8000, timeout=5)

def call(method, params=None):
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {}
    }).encode("utf-8")
    conn.request("POST", "/", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    return json.loads(resp.read().decode("utf-8"))

# Initialize
print(call("initialize"))

# List tools
print(call("tools/list"))

# Call a tool
print(call("tools/call", {"name": "add", "arguments": {"a": 10, "b": 20}}))

# List resources
print(call("resources/list"))

# Read a resource
print(call("resources/read", {"uri": "config://settings"}))

# List prompts
print(call("prompts/list"))

# Get a prompt
print(call("prompts/get", {"name": "calculate", "arguments": {"expression": "2+2"}}))

conn.close()
```

---

## API Reference

### MCPServer

```python
from nanohubmcp import MCPServer

server = MCPServer("my-server", version="1.0.0")
server.run(host="0.0.0.0", port=8000, path_prefix="")
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Server name |
| `version` | str | `"1.0.0"` | Server version |

**`server.run()` parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `host` | str | `"0.0.0.0"` | Host to bind to |
| `port` | int | `8000` | Port to listen on |
| `path_prefix` | str | `""` | URL prefix for proxy environments |

### @server.tool()

Register a function as an MCP tool. The function name becomes the tool name, and the docstring becomes the description. Parameters are auto-detected from the function signature.

```python
@server.tool()
def add(a, b):
    # type: (float, float) -> float
    """Add two numbers together."""
    return float(a) + float(b)
```

**With options:**

```python
@server.tool(name="custom_name", description="Custom description", tags={"math"})
def my_func(a, b):
    return a + b
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | str | function name | Tool name |
| `description` | str | docstring | Tool description |
| `tags` | set | `None` | Tags for categorization |
| `meta` | dict | `None` | Metadata dictionary |
| `input_schema` | dict | auto-generated | JSON Schema for inputs |

### @server.resource()

Register a function as an MCP resource.

```python
@server.resource("config://calculator/settings")
def get_settings():
    """Get calculator settings."""
    return {"precision": 10}
```

**With MIME type:**

```python
@server.resource("data://samples/temperatures", mime_type="application/json")
def temperature_data():
    """Monthly average temperatures."""
    return {"data": [2.1, 3.5, 7.2, 12.1]}
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `uri` | str | required | Resource URI |
| `name` | str | function name | Resource name |
| `description` | str | docstring | Resource description |
| `mime_type` | str | `None` | MIME type of content |
| `tags` | set | `None` | Tags for categorization |
| `meta` | dict | `None` | Metadata dictionary |

### @server.prompt()

Register a function as an MCP prompt template.

```python
@server.prompt()
def calculate(expression):
    # type: (str) -> list
    """Generate a calculation prompt."""
    return [
        {
            "role": "user",
            "content": {"type": "text", "text": "Please calculate: {}".format(expression)}
        }
    ]
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | str | function name | Prompt name |
| `description` | str | docstring | Prompt description |
| `tags` | set | `None` | Tags for categorization |
| `meta` | dict | `None` | Metadata dictionary |

### Context

Tools can receive a `Context` object for logging and progress reporting. Add a `ctx` (or `context`) parameter as the first argument:

```python
from nanohubmcp import MCPServer, Context

server = MCPServer("my-server")

@server.tool()
def power(ctx, base, exponent):
    # type: (Context, float, float) -> float
    """Raise base to the power of exponent."""
    ctx.info("Computing {}^{}".format(base, exponent))
    ctx.report_progress(0.5, total=1.0, message="Computing...")
    return float(base) ** float(exponent)
```

**Context methods:**

| Method | Description |
|---|---|
| `ctx.debug(msg)` | Log debug message |
| `ctx.info(msg)` | Log info message |
| `ctx.warning(msg)` | Log warning message |
| `ctx.error(msg)` | Log error message |
| `ctx.report_progress(progress, total, message)` | Report progress to clients |

---

## Examples

### Simple Calculator

A basic calculator with arithmetic operations, a settings resource, and a calculation prompt.

```python
from nanohubmcp import MCPServer, Context

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
            "content": {"type": "text", "text": "Please calculate: {}".format(expression)}
        }
    ]

if __name__ == "__main__":
    server.run(port=8000)
```

**Test it:**

```bash
# Call add
curl -X POST http://localhost:8000/tools/add \
  -H "Content-Type: application/json" -d '{"a": 2, "b": 3}'

# Call power
curl -X POST http://localhost:8000/tools/power \
  -H "Content-Type: application/json" -d '{"base": 2, "exponent": 10}'

# Call divide (error case)
curl -X POST http://localhost:8000/tools/divide \
  -H "Content-Type: application/json" -d '{"a": 1, "b": 0}'
```

See full source: [examples/simple/start_mcp.py](examples/simple/start_mcp.py)

---

### Data Analysis

Statistical analysis tools with sample datasets for data exploration.

```python
import math
from nanohubmcp import MCPServer

server = MCPServer("data-analysis", version="1.0.0")

@server.tool()
def descriptive_stats(data):
    # type: (str) -> dict
    """
    Calculate descriptive statistics for a dataset.

    Args:
        data: Comma-separated list of numeric values (e.g., "1,2,3,4,5")
    """
    values = [float(x.strip()) for x in data.split(",")]
    n = len(values)
    sorted_data = sorted(values)
    mean = sum(values) / n

    if n % 2 == 0:
        median = (sorted_data[n//2 - 1] + sorted_data[n//2]) / 2
    else:
        median = sorted_data[n//2]

    variance = sum((x - mean) ** 2 for x in values) / n
    std = math.sqrt(variance)

    return {
        "count": n, "mean": round(mean, 6), "median": round(median, 6),
        "min": min(values), "max": max(values), "std": round(std, 6)
    }

@server.tool()
def correlation(x_data, y_data):
    # type: (str, str) -> dict
    """Calculate Pearson correlation coefficient between two datasets."""
    x = [float(v.strip()) for v in x_data.split(",")]
    y = [float(v.strip()) for v in y_data.split(",")]
    # ... (see full source for implementation)

@server.tool()
def linear_regression(x_data, y_data):
    # type: (str, str) -> dict
    """Perform simple linear regression (y = mx + b)."""
    # ...

@server.tool()
def normalize(data, method="minmax"):
    # type: (str, str) -> dict
    """Normalize a dataset using 'minmax' or 'zscore' method."""
    # ...

@server.resource("data://samples/temperatures", mime_type="application/json")
def temperature_data():
    """Monthly average temperatures (Celsius) for a year."""
    return {
        "data": [2.1, 3.5, 7.2, 12.1, 17.3, 21.5, 24.2, 23.8, 19.4, 13.2, 7.1, 3.2],
        "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    }

@server.resource("data://samples/scatter", mime_type="application/json")
def scatter_data():
    """Sample data for scatter plot / correlation analysis."""
    return {
        "x": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "y": [52, 58, 65, 68, 72, 78, 82, 85, 90, 95]
    }

@server.prompt()
def analyze_data(data):
    # type: (str) -> list
    """Generate a prompt to analyze a dataset."""
    return [{"role": "user", "content": {"type": "text", "text": "Please analyze: {}".format(data)}}]

if __name__ == "__main__":
    server.run(port=8000)
```

**Test it:**

```bash
# Descriptive statistics
curl -X POST http://localhost:8000/tools/descriptive_stats \
  -H "Content-Type: application/json" \
  -d '{"data": "10,20,30,40,50"}'

# Correlation
curl -X POST http://localhost:8000/tools/correlation \
  -H "Content-Type: application/json" \
  -d '{"x_data": "1,2,3,4,5", "y_data": "2,4,6,8,10"}'

# Linear regression
curl -X POST http://localhost:8000/tools/linear_regression \
  -H "Content-Type: application/json" \
  -d '{"x_data": "1,2,3,4,5", "y_data": "2.1,3.9,6.2,7.8,10.1"}'

# Normalize
curl -X POST http://localhost:8000/tools/normalize \
  -H "Content-Type: application/json" \
  -d '{"data": "10,20,30,40,50", "method": "zscore"}'

# Read temperature dataset
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"data://samples/temperatures"}}'
```

See full source: [examples/data_analysis/start_mcp.py](examples/data_analysis/start_mcp.py)

---

### Physics Simulator

Physics simulation tools with physical constants as resources.

```python
import math
from nanohubmcp import MCPServer, Context

server = MCPServer("physics-simulator", version="1.0.0")

GRAVITY = 9.81
SPEED_OF_LIGHT = 299792458

@server.tool()
def projectile_motion(v0, angle, h0=0):
    # type: (float, float, float) -> dict
    """
    Calculate projectile motion parameters.

    Args:
        v0: Initial velocity (m/s)
        angle: Launch angle (degrees)
        h0: Initial height (m), default 0
    """
    v0 = float(v0)
    angle_rad = math.radians(float(angle))
    vx = v0 * math.cos(angle_rad)
    vy = v0 * math.sin(angle_rad)
    t_flight = (vy + math.sqrt(vy**2 + 2 * GRAVITY * float(h0))) / GRAVITY
    return {
        "range": round(vx * t_flight, 3),
        "max_height": round(float(h0) + vy**2 / (2 * GRAVITY), 3),
        "time_of_flight": round(t_flight, 3)
    }

@server.tool()
def harmonic_oscillator(mass, spring_constant, amplitude, time):
    # type: (float, float, float, float) -> dict
    """Calculate simple harmonic motion parameters."""
    # ...

@server.tool()
def wave_properties(frequency, wavelength=None, medium_speed=None):
    # type: (float, float, float) -> dict
    """Calculate wave properties (period, speed, wavelength, photon energy)."""
    # ...

@server.tool()
def ideal_gas(pressure=None, volume=None, n_moles=None, temperature=None):
    # type: (float, float, float, float) -> dict
    """Ideal gas law calculator (PV = nRT). Provide 3 of 4 variables."""
    # ...

@server.tool(tags={"advanced"})
def relativistic_energy(ctx, rest_mass, velocity):
    # type: (Context, float, float) -> dict
    """Calculate relativistic energy and momentum."""
    ctx.info("Calculating relativistic properties for v = {} m/s".format(velocity))
    # ...

@server.resource("constants://physics", mime_type="application/json")
def physical_constants():
    """Fundamental physical constants."""
    return {
        "speed_of_light": {"value": 299792458, "unit": "m/s"},
        "gravitational_acceleration": {"value": 9.81, "unit": "m/s^2"},
        "planck_constant": {"value": 6.62607015e-34, "unit": "J*s"},
        "boltzmann_constant": {"value": 1.380649e-23, "unit": "J/K"}
    }

@server.prompt()
def physics_problem(problem_description):
    # type: (str) -> list
    """Generate a prompt to solve a physics problem."""
    return [{"role": "user", "content": {"type": "text", "text": "Solve: {}".format(problem_description)}}]

if __name__ == "__main__":
    server.run(port=8000)
```

**Test it:**

```bash
# Projectile motion (50 m/s at 45 degrees)
curl -X POST http://localhost:8000/tools/projectile_motion \
  -H "Content-Type: application/json" \
  -d '{"v0": 50, "angle": 45}'

# Harmonic oscillator
curl -X POST http://localhost:8000/tools/harmonic_oscillator \
  -H "Content-Type: application/json" \
  -d '{"mass": 0.5, "spring_constant": 20, "amplitude": 0.1, "time": 1.0}'

# Ideal gas law (find pressure given V, n, T)
curl -X POST http://localhost:8000/tools/ideal_gas \
  -H "Content-Type: application/json" \
  -d '{"volume": 0.0224, "n_moles": 1, "temperature": 273.15}'

# Read physical constants
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"constants://physics"}}'
```

See full source: [examples/simulator/start_mcp.py](examples/simulator/start_mcp.py)

---

## Running on nanoHUB

On nanoHUB, use the `start_mcp` CLI command:

```bash
start_mcp --app start_mcp.py
```

**With a conda environment:**

```bash
start_mcp --app start_mcp.py --python-env AIIDA
```

**How it works on nanoHUB:**

1. The CLI detects the nanoHUB environment (`SESSION`, `SESSIONDIR` variables)
2. It looks for an available `wrwroxy` reverse proxy using the `use` command
3. If wrwroxy is found: MCP runs on port 8001, wrwroxy proxies port 8000
4. If wrwroxy is not found: MCP runs directly on port 8000 with the weber path prefix

The proxy URL is printed at startup:

```
Proxy URL : https://proxy.nanohub.org/weber/{session}/{cookie}/{port}/
MCP Server ready! Access it at: https://proxy.nanohub.org/weber/...
SSE endpoint: https://proxy.nanohub.org/weber/.../sse
```

**Testing through the nanoHUB proxy:**

```bash
PROXY_URL="https://proxy.nanohub.org/weber/{session}/{cookie}/{port}"
COOKIE="weber-auth-nanohub-org={session}%3A{auth_token}"

# Server info
curl -b "$COOKIE" "$PROXY_URL/"

# Initialize
curl -b "$COOKIE" -X POST "$PROXY_URL/" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# Call a tool
curl -b "$COOKIE" -X POST "$PROXY_URL/" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"add","arguments":{"a":2,"b":3}}}'
```

---

## Running Tests

The test suite starts a server in a background thread and exercises all endpoints:

```bash
pip install pytest
pytest tests/
```

```
tests/test_mcp_server.py ...................... [100%]
22 passed
```

---

## MCP Protocol Reference

### Supported JSON-RPC Methods

| Method | Description |
|---|---|
| `initialize` | Initialize connection, returns protocol version and capabilities |
| `ping` | Health check, returns `{}` |
| `tools/list` | List all registered tools with schemas |
| `tools/call` | Call a tool by name with arguments |
| `resources/list` | List all registered resources |
| `resources/read` | Read a resource by URI |
| `prompts/list` | List all registered prompts |
| `prompts/get` | Get a prompt by name with arguments |

### Notifications

JSON-RPC requests without an `id` field are treated as notifications and receive a `202 Accepted` response:

```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialized","params":{}}'
```

```json
{"status": "accepted"}
```

### Error Handling

Tool errors return `isError: true` in the MCP response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{"type": "text", "text": "Cannot divide by zero"}],
    "isError": true
  }
}
```

Unknown methods return a JSON-RPC error:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {"code": -32601, "message": "Method not found: unknown/method"}
}
```

Direct tool call errors (`/tools/<name>`) return HTTP 500:

```json
{"error": "Cannot divide by zero"}
```

---

## License

MIT License - see LICENSE file for details.
