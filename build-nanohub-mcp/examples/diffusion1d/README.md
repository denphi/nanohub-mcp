# diffusion1d — worked example

A complete nanoHUB MCP tool around a real (tiny) scientific code: 1D
transient diffusion, explicit FTCS, no-flux boundaries. Pure stdlib, so it
runs anywhere Python does.

```sh
pip install nanohub-mcp jsonschema
python3 -m pytest tests/ -q                            # physics + contract tests
start_mcp --app bin/diffusion1d.py --port 8000         # serve it locally
python ../../scripts/validate_server.py bin/diffusion1d.py
```

Try it end to end with curl (or any MCP client):

```sh
curl -s localhost:8000 -X POST -H 'Content-Type: application/json' -d \
 '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"create_diffusion_sim","arguments":{}}}'
# take run_handle from the result, then run_simulation, then get_peak_history
```

## What each piece demonstrates

| Piece | Skill reference |
|---|---|
| `create_diffusion_sim` / `run_simulation` / `get_*` split | server-guide.md (create→run→get) |
| zero-arg defaults produce correct physics (and tests assert it) | server-guide.md |
| explicit input/output schemas, envelope pattern | server-guide.md |
| opaque `run_handle`, root confinement, fixed errors | security.md |
| derived `points × n_steps` ceiling and bounded files/results | security.md |
| `@server.async_tool` + `ctx.report_progress` | tasks.md |
| `max_points` decimation, summary-not-blob results, `tail`-style reads | quota-and-etiquette.md |
| `delete_run` with `destructiveHint` and the shared handle resolver | security.md, quota-and-etiquette.md |
| `config://diffusion1d/about` reporting the running version | versioning.md |
| tests that FAIL (not skip) on import errors; mass-conservation check | verification.md |
| `middleware/invoke` headless entrypoint | project-layout.md |

To adapt: replace `_initial_profile`/`_ftcs_solve` with calls into your code,
rename the tools for your domain, keep everything else.
