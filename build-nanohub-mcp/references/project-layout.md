# Project layout, the invoke file, and how your server actually runs

## Contents

- Repository layout
- `middleware/invoke`
- `start_mcp` behavior
- Publishing and revisions

## Repository layout of a nanoHUB MCP tool

A nanoHUB MCP tool is a regular nanoHUB tool. Real examples: `padremcp`
(minimal) and `rappturemcp` (large).

```
yourtool/
├── bin/
│   └── yourtool.py          # THE server — defines module-level `server`
├── tests/
│   └── test_offline.py      # tests that need no solver/session (run anywhere)
├── middleware/
│   └── invoke               # how the hub launches the tool (see below)
├── scripts/                 # copied by the scaffold
│   ├── validate_server.py   # offline: contracts + extension conformance
│   ├── check_conformance.py # live: core + extensions over the wire
│   ├── mcp_conformance.py   # shared rule set the two validators import
│   └── check_ci.py          # fail CI on skipped/empty suites
├── .github/workflows/ci.yml # offline tests + validator on push/PR
├── doc/
│   └── description.html     # the tool's page on nanohub.org
├── examples/                # example inputs / notebooks
├── data/                    # static data the tools read
├── src/                     # your scientific library code, or a Makefile
└── README.md
```

Rules of thumb:

- Everything the server imports at runtime must be importable **inside the
  tool session** — either in `bin/` next to the server file (`start_mcp` adds
  that directory to `sys.path`), installed in the conda env named by the
  invoke file, or an installed hub app.
- Keep the server in ONE file if you can. Deployment, debugging, and the
  `@tool/bin/...` invoke reference all get simpler.
- Offline tests belong in the repo and must not require the solver binary or
  a hub session (test deck generation, schemas, tool registration — see
  `scripts/` in this skill).
- Keep CI offline. Do not run `smoke_live.sh` automatically: DCR and deployed
  solver calls may mutate production state and require explicit credentials.

## The `middleware/invoke` file, flag by flag

Minimal (padremcp):

```sh
#!/bin/sh
/usr/bin/invoke_app "$@" -t padremcp \
                         -C "start_mcp --app @tool/bin/padremcp.py" \
                         -u anaconda-6 \
                         -u padre-2.4E-r15 \
                         -r none \
                         -w headless
```

| Flag | Meaning |
|---|---|
| `-t yourtool` | tool name — must match the published tool (and the gateway URL `/api/mcp/{yourtool}/mcp`) |
| `-C "start_mcp --app @tool/bin/yourtool.py"` | the command to run; `@tool` expands to the installed tool directory |
| `-u <env>` | environment module(s) to load — repeatable; load your conda distribution AND your solver (e.g. `anaconda-6` + `padre-2.4E-r15`) |
| `-r none` | no Rappture wrapper |
| `-w headless` | no VNC desktop — an MCP server has no GUI; sessions are cheaper and start faster |
| `-e NAME=VALUE` | extra environment variables (e.g. `PYTHONNOUSERSITE=1` to keep user site-packages from shadowing the env) |

**The env-pinning gotcha (will bite you):** a bare `start_mcp` resolves
against whatever the loaded anaconda **base** provides. If your dependencies
live in a specific conda env, pin the absolute path:

```sh
-C "/apps/share64/debian7/anaconda/anaconda-6/envs/yourenv/bin/start_mcp --app @tool/bin/yourtool.py"
```

Symptom of getting this wrong: the server starts and lists tools fine, but
calls that import your library fail with `No module named '...'` — often
surfacing only inside `resources/read`.

## What `start_mcp` does (so you can debug it)

`start_mcp` (entry point of the `nanohub-mcp` package, `nanohubmcp/cli.py`):

1. Imports your `--app` file and takes its module-level **`server`** variable
   (must be an `MCPServer` instance — this name is the contract).
2. **On nanoHUB** (detected via `SESSION`/`SESSIONDIR` env): reads the session
   `resources` file, computes the proxy path
   `/weber/{session}/{cookie}/{port%1000}/`, runs the MCP server on port
   **8001** and the `wrwroxy` reverse proxy on port **8000**. The gateway
   reaches the server through that weber path — this is why resource URIs
   sometimes arrive with a proxied prefix (the framework strips it).
3. **Locally** (no session env): serves directly on `--port` (default 8000).
4. `--python-env NAME` re-executes under a named conda env's python — an
   alternative to hardcoding the env path in the invoke line.

Local development loop:

```sh
start_mcp --app bin/yourtool.py --port 8000
curl -s localhost:8000 -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python3 -m json.tool
```

This is byte-for-byte the server that runs on the hub; anything you can't
make work locally will not work deployed.

## Publishing on the hub

1. Create the tool at `nanohub.org/tools/create`; pick a short lowercase name
   (it becomes the URL and the `-t` flag).
2. Commit the layout above to the tool repo; drive the tool status pipeline
   (Uploaded → Installed → Approved → Published). Headless tools skip the
   screenshot/GUI review steps' complications.
3. Test the installed revision in a workspace/session before publishing:
   `invoke` should leave a running server you can curl through the session.
4. Ask the hub team to register the tool with **com_mcp** (the MCP gateway
   component). After that, `https://{hub}/api/mcp/{yourtool}/mcp` is live and
   gated by com_mcp's `allowed_groups` configuration.
5. Re-verify through the gateway with the smoke tests
   ([verification.md](verification.md) / `scripts/smoke_live.sh`) — the
   session-side server working does not guarantee the gateway path does.

Updating: publish a new tool revision; the gateway picks a fresh session. Old
sessions may keep running the previous revision — kill stale sessions when
testing an update, or you will debug the wrong code.
