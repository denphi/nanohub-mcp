# Writing the server: tools, schemas, resources, prompts

## Contents

- Anatomy of a tool
- Input and output schemas
- Annotations
- Create → run → get
- Resources, prompts, and context
- Naming and self-diagnosis

Framework: `nanohubmcp` (repo `nanohub-mcp`). Reference servers:
`padremcp/bin/padremcp.py` (clean create→run→get simulation wrapper),
`rappturemcp/bin/rappturemcp.py` (large: apps, tasks, projects).

## Anatomy of a tool

```python
from nanohubmcp import MCPServer, ToolResult

server = MCPServer("mysolver", version="0.1.0")   # `server` name is the contract

_CREATE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "run_handle": {"type": "string"},
        "device": {"type": "string"},
    },
    "required": ["run_handle", "device"],
}

_CREATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "length": {"type": "number", "minimum": 0.01, "maximum": 1000,
                   "default": 1.0, "description": "Device length in microns."},
        "temperature": {"type": "number", "minimum": 1, "maximum": 2000,
                        "default": 300, "description": "Temperature in kelvin."},
    },
    "additionalProperties": False,
}

@server.tool(
    input_schema=_CREATE_INPUT_SCHEMA,
    output_schema=_CREATE_OUTPUT_SCHEMA,
    annotations={"title": "Create PN diode simulation",
                 "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
def create_pn_diode_sim(length=1.0, p_doping=1e17, n_doping=1e17,
                        temperature=300, sweep_electrode=1):
    """Create a PN junction diode simulation.

    Args:
        length: Total device length in microns (default 1.0)
        p_doping: P-side acceptor concentration in cm^-3 (default 1e17)
        ...
        sweep_electrode: Electrode to sweep (default 1 = P side/anode, so the
            forward sweep forward-biases the diode; 2 = N side/cathode)

    Returns:
        An opaque run_handle to pass to run_simulation and get_* tools.
    """
    ...
    return {"run_handle": run_handle, "device": "pn_diode"}
```

- `name` defaults to the function name; `description` defaults to the
  **docstring** — the docstring IS the prompt the model reads. State when to
  call the tool, what each argument means *with units and defaults*, what the
  result contains, and which tool to call next. Be imperative about what NOT
  to do ("do not emit iframe markup", "pass run_handle, not a path you
  invent").
- Returning a **dict** automatically produces both a JSON text block and
  `structuredContent` (spec-required when `outputSchema` is declared).
- Errors: return a short, fixed `ToolResult(content="what went wrong",
  is_error=True)` for
  expected failures (unknown tool name, missing file). Raised exceptions are
  caught and become `isError: true` with the message — acceptable for
  unexpected failures, but a deliberate ToolResult gives the model a better
  recovery hint. Never reflect arbitrary arguments, file contents, commands,
  or secrets in an error.

Read [security.md](security.md) before implementing filesystem access,
subprocess calls, solver-deck generation, result text, or computational limits.

## Input and output schemas — always explicit

If you omit `input_schema` the framework generates one from the signature.
Do it anyway, explicitly, because:

- **The schema is the model's steering.** Types, enums, defaults, and
  descriptions in the schema shape what the model sends. An auto-schema from
  an untyped Python 2-style signature is `{"type": "object"}` — no guidance.
- **Internal parameters leak.** A `ctx` parameter or loop-captured variable
  in a closure surfaces in an auto-schema and the model will try to fill it.
  Explicit schemas advertise exactly the public surface
  (`{"type": "object", "properties": {}, "required": []}` for zero-arg tools).
- **`output_schema` is a contract clients enforce.** claude.ai validates
  `structuredContent` against your declared `outputSchema` and REJECTS the
  call on mismatch. Two consequences:
  - keep a small **required envelope** and leave the rest open
    (`additionalProperties` unset) so you can add fields later:

    ```python
    def _object_output_schema(required, properties=None):
        return {"type": "object", "properties": properties or {},
                "required": list(required)}
    ```
  - mind `[]` vs `{}` — an empty list that gets re-encoded as an object
    somewhere in the proxy chain fails validation. Never round-trip your own
    JSON through lossy fixups.
- Validate your schemas at import time so a bad schema fails loudly:
  `Draft202012Validator.check_schema(SCHEMA)` (jsonschema package).
- Validating **inputs** against a JSON schema inside the tool
  (`jsonschema.validate`) turns malformed model calls into precise error
  messages the model can self-correct from.

## Annotations (behavior hints)

Emitted in `tools/list`; validated at decoration time (unknown keys raise).

| Key | Set it when |
|---|---|
| `title` | always — human-readable name in host UIs |
| `readOnlyHint: True` | pure reads — hosts may skip confirmation, cache, and allow calls from app widgets in strict mode |
| `destructiveHint: True` | deletes/cancels — hosts confirm with the user |
| `idempotentHint: True` | safe to retry (create-with-same-args, setters) |
| `openWorldHint` | tool reaches outside the session (network etc.) |

Shared constants keep this consistent across dozens of tools:

```python
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}
_WRITE_IDEMPOTENT = {"readOnlyHint": False, "idempotentHint": True}
```

## Design pattern for scientific codes: create → run → get

Split by duration and side effects; never one mega-tool:

1. `create_<device>_sim(params…) → {run_handle, inputs, estimate}` — fast,
   validates inputs and derived work, then creates confined run state.
2. `run_simulation(run_handle)` — `@server.async_tool` (see
   [tasks.md](tasks.md)); the only slow tool.
3. `get_iv_curve(run_handle)`, `get_band_diagram(...)`, `describe_outputs(...)`
   — read-only result extractors returning plot-ready arrays with units.
4. Optional helpers the model loves: parameter estimators
   (`estimate_threshold_voltage`) that return suggested sweep ranges, and
   `describe_devices` catalogs (name, params, defaults, units per device).

The `run_handle` is an opaque random identifier resolved beneath an
application-owned root. Never return or accept a caller-selected filesystem
path. If users need the generated deck for reproducibility, return it through a
bounded reader as structured data and label external/user-provided text as
untrusted data, not model instructions.

**Defaults are physics.** The zero-argument call of every `create_*` tool
must produce a correct, meaningful simulation — that is both the model's
first call and your acceptance test. (Field-tested: a PN diode whose default
swept the cathode made every default "forward sweep" reverse-biased; users
report that as a bug, correctly.)

**Warn, don't silently succeed:** if estimated physics says the run is
degenerate (Vt outside the sweep window, dark current below solver
resolution), return the result WITH a `warnings` field the model will relay.

## Resources and prompts

```python
@server.resource("config://mysolver/devices", mime_type="application/json")
def device_catalog():
    """Supported devices, parameters, units, defaults."""
    return {...}

@server.prompt()
def characterize_diode():
    """Guide the model through a full IV characterization."""
    return "Run create_pn_diode_sim, then ..."
```

Resources suit static/slowly-changing catalogs the model should browse
without a tool call; `ui://` resources are the MCP Apps surface
([mcp-apps.md](mcp-apps.md)). Handlers returning dicts are JSON-encoded;
registered `mimeType` and `_meta` are attached automatically. Proxied URI
prefixes (the hub's weber path) are stripped by the framework before lookup.

## Context injection (`ctx`)

Declare a `ctx=None` parameter (kept OUT of the input schema) and the
framework injects a `Context` with `session_id`, `request_id`, `server`,
logging (`ctx.info(...)`) and progress helpers. Uses:

- per-session state (which app instance is open, negotiated capabilities),
- gating model-facing notes on host capabilities — never claim host-side
  outcomes ("the UI is rendered") the declared capabilities don't prove,
- progress notifications from long non-async work.

Key optional per-session state by `ctx.session_id` with locking, expiry, and a
size cap. Never use the session ID as authentication and never store `ctx`
itself. See [state-model.md](state-model.md).

For tools registered in loops/closures, bind loop variables to locals
(`_captured = tool_name`) so they neither leak into schemas nor late-bind.

## Naming

- Tools: `verb_noun`, lowercase snake_case, consistent prefix per family
  (`create_*_sim`, `get_*`, `list_*`, `open_*`). The model picks tools by
  name first, description second.
- Keep names stable — clients cache tool lists; renames are breaking changes.

## Self-diagnosis tools

Ship a couple of read-only introspection tools (`validate_mcp_app`,
`get_app_size`, `get_cache_status`) — they cost little and let you debug a
**deployed** instance entirely through the MCP connection. Pair with the
offline validator in this skill's `scripts/validate_server.py` for pre-deploy
checks.
