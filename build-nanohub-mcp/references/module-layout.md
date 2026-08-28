# Module layout: splitting the server before it becomes a monolith

## Contents

- Why one file stops working
- Target layout
- The entrypoint contract (and the naming trap)
- Registration: `build_server()` and `register(server)`
- Keep import time pure
- Seams and ownership rules
- Test layout
- When to split
- What the split does not change

## Why one file stops working

The scaffold emits a single `bin/yourtool.py`, and for a first server that is
the right shape. It stops being the right shape the moment one file holds
configuration, path confinement, run state, process supervision, every schema
and handler, protocol glue, HTML/JavaScript, and shutdown side effects.

Servers reach that state quickly, one tool at a time: a 5,000-line
`bin/yourtool.py` with a 2,000-line `tests/test_offline.py` beside it is a
routine outcome, not an unusual one. At that size the problems are correctness
and security problems, not style preferences:

- **Invariants get near-duplicate implementations.** Two path resolvers drift
  apart and one of them forgets `realpath`; two workload checks disagree on the
  ceiling. A confinement bug you fixed once reappears through the copy.
- **Import does work.** The module creates roots, recovers jobs, registers
  tools, and installs hooks as it loads — so importing the server to test it
  mutates the filesystem, and the offline suite cannot run without a session.
- **Locks and state transitions have no owner.** Nothing tells you which lock
  guards which transition, so new handlers acquire the wrong one or none.
- **Unfinished paths hide.** A `TODO` that still returns a fabricated success
  is invisible in thousands of lines and reads as a working tool.
- **Unrelated changes collide.** UI, protocol, storage, and execution edits all
  land in the same file, so every change carries the whole file's review
  surface and every branch conflicts.

Split by responsibility, with schemas colocated with the tools that own them.

## Target layout

```text
yourtool/
├── bin/
│   ├── yourtool.py            # thin entrypoint: `server = build_server()` — under 20 lines
│   └── yourtool_mcp/          # the package — MUST NOT be named `yourtool` (see below)
│       ├── __init__.py
│       ├── app.py             # build_server(): registration only, under 200 lines
│       ├── config.py          # names, versions, limits, roots, env reads — no side effects
│       ├── errors.py          # one error shape every tool returns
│       ├── security.py        # handle minting + confinement — the ONLY path resolver
│       ├── state.py           # run locks, caches, session state and its expiry
│       ├── storage/
│       │   ├── runs.py        # run directories, input decks, metadata
│       │   └── artifacts.py   # bounded reads, pagination, decimation
│       ├── execution/
│       │   ├── supervisor.py  # subprocess launch, timeouts, kill trees
│       │   └── policies.py    # workload ceilings, quota, admission checks
│       ├── tools/             # one module per tool group; schemas colocated
│       │   ├── simulation.py  # create_*_sim, run_simulation
│       │   ├── results.py     # get_* readers
│       │   └── admin.py       # delete_run, about
│       ├── ui/
│       │   └── appname.html   # one file per ui:// app; assets as files, never Python strings
│       ├── resources.py
│       └── prompts.py
├── middleware/invoke
├── scripts/                   # validate_server.py, check_conformance.py, mcp_conformance.py, check_ci.py
├── tests/
│   ├── unit/                  # pure functions: schemas, confinement, policies
│   ├── integration/           # create → run → get against the real filesystem
│   ├── protocol/              # initialize, tools/list, error envelopes
│   └── scenarios/             # end-to-end scientific workflows
├── .github/workflows/ci.yml
├── doc/description.html
└── examples/  data/  src/
```

Adopt the directories you actually need. A server with six tools and no MCP App
needs `config.py`, `security.py`, `state.py`, and `tools/`; it does not need
`execution/` split in two.

## The entrypoint contract (and the naming trap)

`start_mcp` inserts the app file's directory into `sys.path`, then loads
`bin/yourtool.py` under the module name `yourtool` — registering it in
`sys.modules` **before** executing it (`nanohubmcp/cli.py`).

That ordering sets a trap. If the package is also named `yourtool`, the
half-initialized entrypoint shadows it and the import fails:

```text
ModuleNotFoundError: No module named 'yourtool.core'; 'yourtool' is not a package
```

Two rules follow:

- **Name the package something other than the entrypoint file.**
  `yourtool_mcp/` is the convention used here.
- **Put the package under `bin/`.** That is the directory `start_mcp` adds to
  `sys.path` inside the tool session. A package under `src/` is not importable
  unless the invoke file puts it on `PYTHONPATH` itself.

The entrypoint stays trivial, and `server` remains the module-level contract:

```python
"""yourtool — nanoHUB entrypoint. start_mcp imports `server` from this module."""
from yourtool_mcp.app import build_server

server = build_server()
```

## Registration: `build_server()` and `register(server)`

Tool decorators are bound to a server instance, so each tool module exposes a
`register(server)` function and `app.py` calls them in order:

```python
# yourtool_mcp/app.py
from nanohubmcp import MCPServer

from .config import APP_NAME, APP_VERSION
from .tools import admin, results, simulation


def build_server():
    server = MCPServer(APP_NAME, version=APP_VERSION)
    simulation.register(server)
    results.register(server)
    admin.register(server)
    return server
```

```python
# yourtool_mcp/tools/simulation.py
from ..config import MAX_CELL_UPDATES
from ..errors import invalid_params
from ..security import new_run_handle

_CREATE_INPUT_SCHEMA = {...}   # colocated with the tool that owns it
_CREATE_OUTPUT_SCHEMA = {...}


def register(server):
    @server.tool(
        name="create_simulation",
        description="Validate inputs and derived cost, then stage a run.",
        input_schema=_CREATE_INPUT_SCHEMA,
        output_schema=_CREATE_OUTPUT_SCHEMA,
        annotations={"title": "Create simulation", "readOnlyHint": False,
                     "destructiveHint": False, "idempotentHint": False,
                     "openWorldHint": False},
    )
    def create_simulation(mesh_points=101, time_steps=1000):
        ...
```

`app.py` reads as a table of contents. If it grows past ~200 lines, logic has
leaked back into it.

## Keep import time pure

`build_server()` should register tools and nothing else. Importing the server
must not create directories, scan run roots, recover jobs, spawn processes, or
install `atexit` hooks — offline tests, `validate_server.py`, and CI all import
the app file, and none of them should touch the filesystem to do it.

`MCPServer` has no startup hook, so do first-use work lazily behind the lock
that owns it:

```python
# yourtool_mcp/storage/runs.py
_ROOT_READY = False
_ROOT_GUARD = threading.Lock()


def ensure_run_root():
    global _ROOT_READY
    with _ROOT_GUARD:
        if not _ROOT_READY:
            os.makedirs(RUN_ROOT, mode=0o700, exist_ok=True)
            _ROOT_READY = True
    return RUN_ROOT
```

Handlers call `ensure_run_root()`; import does not.

## Seams and ownership rules

| Module | Owns | Rule |
|---|---|---|
| `config.py` | Names, versions, limits, roots, env reads | Constants only — importing it must be free of side effects |
| `security.py` | Handle minting and confinement | The **only** place that turns a handle into a path; every tool routes through it |
| `errors.py` | The error envelope | One shape, one place; no handler builds its own |
| `state.py` | Locks, caches, session state | One lock per resource, acquired in one documented order |
| `execution/` | Subprocess launch and ceilings | Argument arrays, `shell=False`, a timeout, and one kill path |
| `storage/` | Run dirs, decks, artifacts, bounded reads | Pagination and decimation live here, not in handlers |
| `tools/*.py` | Schemas + handlers for one tool group | Validate in the schema **and** in the handler; call into the modules above |
| `ui/*.html` | MCP App markup, one file per `ui://yourtool/appname` | Real files the resource handler reads on demand; never embedded Python strings |

The one-resolver rule matters most. [security.md](security.md) requires opaque
handles rooted beneath one application-owned directory; that requirement is only
enforceable while a single function implements it.

## Test layout

Mirror the split — a 2,000-line `test_offline.py` hides the same failures the
2,000-line server does:

| Directory | Covers | Needs |
|---|---|---|
| `tests/unit/` | Schemas, confinement, workload policies, error shapes | Nothing but the package |
| `tests/integration/` | create → run → get against a real temp root | Filesystem |
| `tests/protocol/` | `initialize`, `tools/list`, envelopes, annotations | The built server |
| `tests/scenarios/` | Complete scientific workflows and their numbers | Filesystem, maybe the solver |

Keep everything except `tests/scenarios/` runnable with no solver and no hub
session, so CI stays offline. `scripts/check_ci.py` still fails the build on
skipped or empty suites, so a suite that silently stops importing is caught.

## When to split

Split on the first trigger you hit, not on a line count alone:

- A second copy of a security invariant appears (a path resolve, a workload
  check, a handle parse).
- The server file passes ~800 lines, or any single file mixes three of
  {config, security, state, execution, schemas, handlers, UI, protocol}.
- An MCP App arrives — HTML and JavaScript never belong in the server file.
- Import-time work makes tests need a filesystem or a session.
- Two people edit the file at once and conflict on unrelated features.

Splitting is mechanical and safe to do incrementally: move `config.py` and
`security.py` out first (they have the fewest inbound dependencies), then
`tools/` one group at a time, re-running `validate_server.py` after each move.

## What the split does not change

Verified against this skill's own tooling on a split-layout server:

- `middleware/invoke` is unchanged — it still points at
  `start_mcp --app @tool/bin/yourtool.py`.
- `python scripts/validate_server.py bin/yourtool.py` reports the same results.
  It loads the entrypoint and inspects each **handler's** source with
  `inspect.getsource`, so its security scan follows handlers into package
  modules; nothing is skipped by moving them.
- `scripts/check_conformance.py` is unaffected — it drives the running server
  over the wire and never sees the file layout.
- The `server` module-level variable in `bin/yourtool.py` remains the contract.

Deployment, publishing, and the gateway path are identical
([project-layout.md](project-layout.md)).
