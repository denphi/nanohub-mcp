---
name: build-nanohub-mcp
description: Transform scientific Python code into a secure, bounded MCP server built with nanohub-mcp, test it locally and in CI, package it as a nanoHUB headless tool, publish it through HubZero/com_mcp, connect real MCP clients, and diagnose gateway, OAuth, session, CORS, Tasks, Resources, and MCP Apps failures. Use when wrapping a simulation, solver, analysis routine, or scientific workflow for conversational use on nanoHUB, or when repairing a nanoHUB MCP deployment.
---

# Build a scientific MCP for nanoHUB

Turn a simulation, solver, or analysis library into a nanoHUB tool that an MCP
host can drive conversationally. Produce one server with small, explicit tools,
offline scientific tests, a headless `middleware/invoke`, CI, and live gateway
verification.

## Target architecture

```text
MCP host (Claude, ChatGPT, Inspector, hub chat)
        │  OAuth bearer token; the hub provides discovery and routing
        ▼
https://nanohub.org/api/mcp/{tool}/mcp
        ▼
com_mcp → nanoHUB session → middleware/invoke → start_mcp
        ▼
bin/{tool}.py → server = MCPServer(...)
```

Start from the safe scaffold:

```sh
python scripts/new_tool.py mysolver
```

Use [examples/diffusion1d/](examples/diffusion1d/) as the complete worked
example. Replace its numerical kernel while retaining its schemas, run-handle
confinement, workload checks, create→run→get decomposition, and tests.

## 1. Establish the security and state boundaries

Read [references/security.md](references/security.md) before exposing any
function. Treat every tool argument and every file read by a tool as untrusted.
Use opaque run handles rooted beneath one application-owned directory; never
accept a caller-selected filesystem path. Bound derived work, not just each
parameter. Invoke solvers with argument arrays, `shell=False`, and a timeout.
Never return credentials or present external text as model instructions.

Read [references/state-model.md](references/state-model.md) before caching.
Keep globals limited to immutable configuration and synchronized caches. Store
run state behind handles; key optional conversation state by `ctx.session_id`
with expiry, but never use an MCP session ID as authorization.

## 2. Design explicit tools

Split simulation workflows into:

1. `create_<case>_sim(params) → {run_handle, inputs, estimate}`: validate
   scientific inputs and derived cost, then write inputs beneath the run root.
2. `run_simulation(run_handle)`: make only this slow step an async tool.
3. `get_*` readers: return bounded, plot-ready arrays, units, warnings, and
   pagination/decimation controls.
4. `delete_run(run_handle)`: resolve the handle through the same confinement
   helper and mark the tool destructive.

Declare `input_schema` and `output_schema` on every tool. Put types, units,
descriptions, defaults, enums, and numeric bounds in the input schema; repeat
critical validation inside the handler for direct calls and defense in depth.
Keep zero-argument defaults physically meaningful.

Use [references/server-guide.md](references/server-guide.md) for tool contracts
and [references/tasks.md](references/tasks.md) for async execution.

## 3. Test and validate locally

Install the same dependencies used by the published environment, then run:

```sh
python3 -m pytest tests/ -q
python scripts/validate_server.py bin/yourtool.py
start_mcp --app bin/yourtool.py --port 8000
```

Exercise `initialize`, the initialized notification, `tools/list`, a complete
create→run→get workflow, invalid schemas, traversal handles, workload limits,
and cleanup. Require at least one passing test and make skipped offline tests a
CI failure. The scaffold includes `.github/workflows/ci.yml` and copies the
validator into the generated repository.

## 4. Package and publish

Use this repository layout:

```text
yourtool/
├── bin/yourtool.py
├── middleware/invoke
├── scripts/validate_server.py
├── tests/
├── .github/workflows/ci.yml
├── doc/description.html
└── src/  data/  examples/
```

Follow [references/project-layout.md](references/project-layout.md) for the
environment-pinned `invoke` command and nanoHUB workflow. Drive the tool through
Uploaded → Installed → Approved → Published, then ask the hub team to register
it with `com_mcp`. Terminate stale sessions before validating a new revision.

## 5. Connect real clients

Do not stop at curl. Follow [references/connecting-clients.md](references/connecting-clients.md)
for Claude Code, Claude, ChatGPT, MCP Inspector, hub chat, and token-authenticated
scripts. Check the dated host-capability notes before claiming support for
Resources, Prompts, Tasks, Apps, or elicitation.

Run the non-mutating live checks first:

```sh
HUB=https://nanohub.org TOOL=yourtool scripts/smoke_live.sh
```

Add `TOKEN=...` to test an authenticated MCP session and resource reads. Set
`TASK_TOOL=run_simulation` plus `TASK_ARGS_JSON='{"run_handle":"..."}'` to
exercise Tasks. Set `RUN_DCR=1` only when intentionally creating a throwaway
OAuth client; remove that client afterwards.

Use [references/troubleshooting.md](references/troubleshooting.md) to map common
symptoms to the failing layer. Use [references/verification.md](references/verification.md)
for the manual equivalents.

## 6. Add optional protocol features

- Use [references/mcp-apps.md](references/mcp-apps.md) for an interactive
  `ui://` application after plain tools work.
- Use [references/elicitation.md](references/elicitation.md) for capability-
  gated forms, confirmations, and URL flows.
- Use [references/quota-and-etiquette.md](references/quota-and-etiquette.md)
  before shipping to constrain context, API calls, disk, and sessions.
- Use [references/versioning.md](references/versioning.md) before changing a
  published schema, tool name, framework version, or hub revision.

## Bundled resources

| Resource | Use |
|---|---|
| `scripts/new_tool.py NAME` | Scaffold a secure server, tests, invoke file, validator, docs, and CI |
| `scripts/validate_server.py APP` | Validate contracts and flag common security hazards before deployment |
| `scripts/smoke_live.sh` | Verify discovery, CORS, OAuth metadata, sessions, resources, and optional Tasks/DCR |
| [references/security.md](references/security.md) | Apply input, path, process, result, secret, and resource controls |
| [references/state-model.md](references/state-model.md) | Separate global, run, session, and authenticated-user state |
| [references/server-guide.md](references/server-guide.md) | Design tools, schemas, annotations, resources, prompts, and context use |
| [references/tasks.md](references/tasks.md) | Choose sync/async behavior and test Tasks |
| [references/project-layout.md](references/project-layout.md) | Package, invoke, install, and publish the nanoHUB tool |
| [references/connecting-clients.md](references/connecting-clients.md) | Configure real MCP hosts and compare capabilities |
| [references/troubleshooting.md](references/troubleshooting.md) | Diagnose symptom → cause → fix |
| [references/verification.md](references/verification.md) | Run local and deployed checks manually |
| [references/mcp-apps.md](references/mcp-apps.md) | Add an MCP App |
| [references/elicitation.md](references/elicitation.md) | Ask for user input through the MCP session |
| [references/quota-and-etiquette.md](references/quota-and-etiquette.md) | Control context, calls, storage, and session costs |
| [references/oauth-dcr.md](references/oauth-dcr.md) | Understand hub-provided OAuth and DCR |
| [references/gateway-cors.md](references/gateway-cors.md) | Diagnose gateway, CORS, SSE, and header forwarding |
| [references/versioning.md](references/versioning.md) | Evolve protocol, framework, tools, and hub revisions safely |
