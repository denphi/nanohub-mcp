# Troubleshooting playbook

Start at the narrowest failing layer: server import → local MCP → nanoHUB
session → gateway → OAuth/CORS → real host. Verify the deployed revision before
reading the working tree.

| Symptom | Likely cause | Fix / proof |
|---|---|---|
| Server will not import | Missing dependency, syntax error, or no module-level `server` | Run `python scripts/validate_server.py bin/tool.py` inside the invoke environment; fix the first import error |
| Tools work but `resources/read` says `No module named ...` | `start_mcp` runs from Anaconda base while the resource handler imports a package from another env | Pin the full `start_mcp` path or use the correct `--python-env`; reproduce inside a nanoHUB session |
| Local `tools/list` works but gateway returns 404 | Tool is not registered with `com_mcp`, tool name differs, or revision is not published | Confirm the published tool name, gateway registration, and `/api/mcp/{tool}/mcp` URL |
| `409` says an MCP session is required | A `ctx`-bearing tool was called through the REST convenience route | Call it through Streamable HTTP MCP after `initialize`; keep a context-free companion only if REST support is required |
| Async tools work locally but fail through the gateway | `Mcp-Session-Id` is dropped on a request or response | Capture the initialize response header, send it on later calls, and verify gateway expose/forward rules |
| `Task is not available in this session` | Poll uses a different or missing session ID | Reuse the exact session ID for `tasks/get`/`tasks/cancel`; do not start a fresh client between calls |
| SSE connects but no endpoint event arrives | PHP/nginx buffering or a framework-generated empty body | Use the raw SSE emit path, disable buffering, and inspect `Content-Length` plus the first complete event |
| Browser shows an opaque CORS error | CORS is present only on OPTIONS or required headers are not exposed | Check actual 200/401/403 responses; emit ACAO everywhere and expose `WWW-Authenticate, Mcp-Session-Id` |
| OAuth discovery never starts | Gateway returned 403 or a 401 without a valid `WWW-Authenticate` resource metadata challenge | Missing/invalid authentication must return 401 with the challenge; reserve 403 for authenticated denials |
| `invalid_client` mentions a client secret | The `client_id` is unknown/unpublished, or a secret was incorrectly sent | Re-register once, use the returned ID exactly, and interpret the precise OAuth error table in `oauth-dcr.md` |
| Bogus-code exchange returns `invalid_grant` | Client authentication succeeded; only the fake/expired code failed | Treat this as the intended DCR smoke-test result |
| Deployed fix changed nothing | Gateway reused a session running an old tool revision | Bump the app version, terminate stale sessions, reconnect, and verify `serverInfo`/about resource |
| `structuredContent` is rejected by a host | Output does not match `outputSchema`, commonly `[]` rewritten as `{}` | Validate the exact wire response; preserve raw JSON bytes through the gateway |
| App tool succeeds but no UI appears | Host did not declare the Apps capability, URI/mime metadata is wrong, or the resource is oversized | Inspect initialize capabilities, `resourceUri`, MIME type, resource read, and rendered byte size |
| Elicitation works locally but fails through REST | Elicitation requires the bidirectional MCP session channel | Invoke through a session-aware host and provide a conversational fallback |
| Solver is killed or session becomes unresponsive | Derived work, memory, disk, or result size was not bounded | Estimate total work before launch; enforce time, concurrency, disk, and output ceilings |
| CI is green with no useful coverage | Tests skipped because an optional import failed | Install test dependencies explicitly and fail CI on skipped offline tests or zero passes |

## Layered command order

1. Run offline physics and contract tests.
2. Run `validate_server.py` in the deployment environment.
3. Start locally and test initialize plus create→run→get.
4. Invoke the installed nanoHUB revision inside a session.
5. Run `smoke_live.sh` without DCR.
6. Add a token, preserve the returned session header, and test Resources/Tasks.
7. Connect the real target host using `connecting-clients.md`.

When a report contradicts local results, reproduce its exact host, tool
revision, redirect URI, client ID, arguments, and session flow before drawing a
conclusion.
