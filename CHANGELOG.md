# Changelog

## 0.4.4

Completes the 2026-07-28 transport surface, fixes three header-validation
bugs introduced with it in 0.4.0, and adds the SEP-2640 Skills Extension.

### Fixed

- **`Mcp-Name` ignored `params.uri`.** The header's source is `params.name` for
  `tools/call` and `prompts/get` but `params.uri` for `resources/read`, so
  resource reads were never validated against their header.
- **Base64 sentinel values were rejected as mismatches.** A client MUST wrap any
  value that is not plain visible ASCII as `=?base64?…?=`, and a server MUST
  decode before comparing. Resource URIs are the common case.
- **Header names were compared case-sensitively.** RFC 9110 field names are
  case-insensitive and both sides MUST treat them so.
- **A blob resource emitted `"text": ""` alongside its blob.** The spec splits
  these into `TextResourceContents` (requires `text`) and
  `BlobResourceContents` (requires `blob`); sending both matched neither
  variant cleanly and read as empty text to a strict client.

### Added

- **SEP-2640 Skills Extension** — `@server.skill(skill_path)` registers a
  directory (a `SKILL.md` plus supporting files) served as individually
  addressable resources under `skill://<skill_path>/<file-path>`. Adds
  `skills/list` and `skills/get`, and `resources/directory/read` for
  navigating a skill's subdirectories; `resources/read` serves each file
  (text or, for binary content, a base64 `blob`). Every file's SHA-256
  digest and size are computed once at registration, so listings and
  `skills/get` answer without touching disk again. Dotfiles and
  dot-directories are excluded from the walk, so a skill directory that is a
  git checkout does not publish `.git/config` or `.env`. Declared under
  `capabilities.extensions["io.modelcontextprotocol/skills"]`, with
  `directoryRead: true`, only once a skill is registered; `resources` is
  advertised too, as the extension requires, even with no plain
  `@server.resource()` in use.
- **`MCP-Protocol-Version` header validation** — it MUST match the body's
  `_meta` protocol version; a disagreement is `HeaderMismatch` (`-32020`).
- **`x-mcp-header` / `Mcp-Param-{Name}`** — a tool may annotate an input-schema
  property so clients mirror that argument into a header. The server validates
  the mirrored value against the body, decoding the Base64 sentinel and
  comparing numbers numerically. Only statically reachable properties (a chain
  of `properties` keys) are honoured, as the spec requires.
- **Resource subscriptions** — `subscriptions/listen` accepts
  `resourceSubscriptions`, and `server.resource_updated(uri)` emits
  `notifications/resources/updated` to exactly the subscribers watching that
  URI. Re-registering a resource fires it too. `resources.subscribe` is now
  advertised when the server has resources. This was the last unimplemented
  filter.
- **Cursor pagination** — `MCPServer(..., list_page_size=N)` paginates
  `tools/list`, `resources/list` and `prompts/list` with an opaque
  `nextCursor`. **Off by default**: with no page size the whole list is
  returned exactly as before. A cursor the server did not mint is `-32602`.
- `prompts/list` is now returned in a deterministic order, as `tools/list`
  already was.

### Security and robustness (found in successive review passes)

- **MRTR request state is bound to its session**, as tasks already were. A
  `requestState` is presented by the client, so without an owner check a second
  session naming another's in-flight state inherited answers a different user
  gave — including an approval it never asked for. A save also never rebinds an
  existing state to a new owner: doing so would let any session permanently
  break another's request just by naming its id.
- **Subscriptions per session are capped.** Subscription ids are the client's
  own JSON-RPC request ids, so the count was client-controlled and unbounded
  until the session ended.
- **A cancelled task waiting on input now reports the cancellation.**
  Cancellation wakes the same event an answer does, so the handler saw "no
  response supplied" — a tool failing closed on `RuntimeError` recorded the
  wrong cause.
- **`Origin` is validated on every method**, not only POST: the SSE stream is a
  long-lived channel a page can open cross-origin with `EventSource`.
- **Cursor paging no longer skips entries.** The cursor carries the last key
  rather than an offset. With dynamic registration in the same release,
  removing an entry ahead of an offset cursor shifted the list down and one
  entry was never returned at all.
- **A latent lock inversion** in `_unregister_client`, which nested
  subscriptions inside sessions where every other path takes them the other way
  round.
- **Malformed `resources/read` / `prompts/get` params are `-32602`.** A
  non-string `uri` reached a dict lookup and raised `TypeError`, surfacing as
  an internal error with a stack trace per bad request.

### Hardening (found in a second review pass)

- **`x-mcp-header` annotations are validated at registration.** An invalid one
  is not a cosmetic problem: a conforming client **MUST** exclude the whole
  tool from `tools/list`, so the tool silently vanished for the user with
  nothing reported anywhere. The server now refuses to register it, checking
  every constraint the spec sets — non-empty, valid HTTP field-name token (so a
  CRLF injection attempt is rejected), case-insensitively unique, applied only
  to `string`/`integer`/`boolean`, and statically reachable through a chain of
  `properties` keys only. The validated map is cached, so `tools/call` no
  longer re-walks the schema.
- **`Origin` validation** for DNS-rebinding protection, which the transport
  spec requires: `run(allowed_origins=[...])` refuses a browser request from
  an origin outside the list with HTTP 403. Off by default — a library cannot
  know which origins are legitimate — and requests without an `Origin` (every
  non-browser client) are never affected. **Set it in any deployment a browser
  can reach.**

### Also fixed (found reviewing the above before release)

- A cursor error raised `ValueError`, and the handler caught `ValueError`
  broadly — so a genuine server-side error (a reserved `_meta` prefix, an
  unsupported elicitation mode) was reported to the caller as `-32602` Invalid
  params, blaming them for a server bug. Cursor errors now use a dedicated
  exception and everything else stays `-32603`.
- `MCP-Protocol-Version` was compared against the server's *resolved* version,
  which falls back to the session's negotiation and then to a default. A
  2025-06-18 client — whose revision defines the header but carries no `_meta`
  — was rejected whenever the gateway dropped its session id. It is now
  compared only against a version the body actually declares.
- Registering a resource for the first time emitted
  `notifications/resources/updated`; only replacing a handler is an update.
- `resources/list` is now returned in a deterministic order, like `tools/list`
  and `prompts/list` — required for offset paging to be walkable.

### Testing

- Coverage 75% → 83%; 112 → 192 tests.
- New `tests/test_schema_inference.py` covers type-expression inference and
  wire serialisation directly — both are contract surfaces a happy-path
  integration test never exercises. The blob bug above was found writing it.
- New tests for the MRTR sampling, roots and URL-elicitation routes, multi-round
  answer accumulation, the nanoHUB weber proxy path, and the CLI flag
  resolution.
- New `tests/test_protocol_branches.py` and `tests/test_context_and_annotations.py`
  cover the task/prompt error branches, tool-annotation validation (a misspelled
  hint is dropped silently by clients, so it raises at decoration time instead),
  and the Context surface off-session.

## 0.4.3

### Fixed

- The child process started by `start_mcp --python-env NAME` no longer receives
  the launcher's own package root on `PYTHONPATH`. 0.4.1 added it to guarantee
  that a generated runner and the framework it drives are the same version —
  a real failure, since 0.4.0 taught the runner to pass `require_route_headers`
  and an older `MCPServer.run()` rejects that keyword outright, killing the
  child before the port opens. But for the documented `pip install
  nanohub-mcp` that root is a shared site-packages, so the fix handed the child
  every library installed beside the framework, including a NumPy built for the
  launcher's Python rather than the one `--python-env` selected.

  0.4.2 made the entry redundant: `_pin_framework` imports the launcher's
  `nanohubmcp` from its own file, which delivers the same version guarantee
  without exposing a single sibling. Removing it restores the 0.3.x search
  path — the app directory, the session's own `PYTHONPATH`, and the selected
  environment's packages.

### Changed

- A package that exists *only* beside an installed launcher is no longer
  importable from a `--python-env` child. It now raises `ModuleNotFoundError`
  naming the package, instead of being satisfied by a copy built for the
  launcher's interpreter and failing further into the import in a way that
  does not name the real problem. Packages on the session's `PYTHONPATH` are
  unaffected.

## 0.4.2

### Fixed

- `start_mcp --python-env NAME` now runs the app against the packages of the
  environment it names. Two things had to change. Inherited `PYTHONPATH`
  entries, which the interpreter places ahead of its own site-packages, are
  demoted behind them, so a NumPy in the launcher's environment no longer
  shadows the one the app was started for. And the launcher's `nanohubmcp` is
  pinned by importing that package from its own file rather than by putting
  its parent directory on `sys.path`: for the documented `pip install
  nanohub-mcp` that parent is a shared site-packages, and pinning the whole
  directory — as 0.4.1 did through `PYTHONPATH` — reintroduced the launcher's
  NumPy at the head of the search path. The app directory stays first, and the
  framework version the child imports still matches the launcher's.

## 0.4.1

### Fixed

- `resources/read` now returns the `ttlMs` and `cacheScope` freshness hints
  that `2026-07-28` requires of every `CacheableResult`. The list methods had
  carried them since 0.4.0, but reads did not, so a host that validates the
  response against the published schema rejected every read outright. For an
  MCP Apps server that rejection is total: the app resource never loads, and
  the host reports a display failure with no request ever reaching the server.
- `start_mcp --python-env NAME` now places the launcher's own package root
  ahead of the selected environment on `PYTHONPATH`. This prevents a generated
  0.4.x runner from importing an older framework from the scientific
  environment and exiting before the MCP port opens. Environment-specific
  packages remain available, while the launcher and child server are
  guaranteed to use the same `nanohubmcp` version.

## 0.4.0

Adds protocol revision **`2026-07-28`**, negotiated per request alongside every
earlier revision. Nothing currently deployed changes behaviour: a client that
does not declare a version is served exactly as before.

### Protocol 2026-07-28

- `server/discover` — identity, supported versions, capabilities, and optional
  `instructions` in one call (new `MCPServer(..., instructions=...)` argument).
- Per-request `_meta`: `protocolVersion`, `clientCapabilities`, `clientInfo`,
  and `logLevel` are read per request rather than once at `initialize`.
- Stateless operation — neither `initialize` nor a session id is required. A
  task created without a session is reachable by its `taskId`, which is the
  server-minted handle that revision prescribes.
- `resultType` on every result, and `_meta.serverInfo` identifying the server.
- `UnsupportedProtocolVersionError` (`-32022`) carrying `{requested, supported}`.
- Error renumbering, version-gated: `-32001/-32003/-32004` →
  `-32020/-32021/-32022`; resource-not-found `-32601` → `-32602`.
- `CacheableResult` (`ttlMs`, `cacheScope`) on `tools/list`, `resources/list`,
  `prompts/list`, and `resources/templates/list`; `tools/list` is now ordered
  deterministically.
- `ping` and `logging/setLevel` are refused for 2026-07-28 clients and still
  served to older ones.
- `notifications/message` is emitted, gated on the request's `logLevel` — a
  request that asks for no logs receives none, as the spec requires.
- `notifications/cancelled` cancels the task the request started, or closes the
  `subscriptions/listen` stream it opened.
- `resources/templates/list` is answered instead of returning "method not found".
- `Mcp-Method` / `Mcp-Name`: a contradicting header is always
  `HeaderMismatchError` (`-32020`, HTTP 400). A *missing* one is rejected by
  default only for requests declaring `2026-07-28`, the revision that requires
  them — earlier revisions never defined these headers, so demanding them there
  would reject conformant clients. Enforcement applies only to transports that
  carry headers, so direct calls, SSE, and stdio are unaffected. Override with
  `run(require_route_headers=True|False)` or the `start_mcp`
  `--require-route-headers` / `--allow-missing-route-headers` flags.

### Multi Round-Trip Requests

`ctx.elicit()`, `ctx.sample()`, and `ctx.list_roots()` no longer push a request
to the client on 2026-07-28. **Tool bodies do not change** — the framework
picks the route:

- **Sync tools** return an `InputRequiredResult`; the client retries the call
  with `inputResponses` + `requestState`, and the handler **runs again from the
  top**. Handlers must be idempotent up to each ask.
- **Async tools** move the task to `input_required` and park the worker until
  `tasks/update` arrives, then resume in place. Nothing re-runs.

The capability gate still raises `RuntimeError` on both stacks, so existing
fail-closed code is unaffected. `InputRequired` derives from `BaseException` so
a broad `except Exception` in a handler cannot swallow an ask.

### Dynamic registration

- Tools, resources, and prompts can be registered while the server is running;
  the existing decorators work unchanged at import time and additionally emit
  `notifications/{tools,resources,prompts}/list_changed` when used later.
- New `remove_tool(name)`, `remove_resource(uri)`, `remove_prompt(name)`.
- Both client generations are served: a 2026-07-28 client opts in via
  `subscriptions/listen` and gets notifications tagged with its subscription
  id; an earlier-revision session receives them because `listChanged` is
  advertised. A modern client that did not subscribe receives nothing.
- `listChanged` is advertised `false` until the registry actually changes while
  serving, so a static server never claims a notification it will not send.
- Registration and list iteration are guarded by a re-entrant lock; hot-path
  single-key lookups stay lock-free.

### Deployment

- `start_mcp` gained `--require-route-headers` and `--require-session-header`,
  so both hardening options are reachable from `middleware/invoke` instead of
  requiring an edit to the app file.

### Task control

- `tasks/cancel` now sets a per-job cancel event and fires registered
  callbacks, so cancelling stops the work instead of relabelling the record.
  New: `ctx.is_cancelled()`, `ctx.cancel_event`, `ctx.on_cancel(cb)`, and
  `server.cancel_job(job_id)`.
- `@async_tool(prepare=...)` runs synchronously before the worker starts, so a
  durable handle published with `ctx.set_task_metadata()` rides the initial
  task handle's `_meta`. Legacy clients get the same values under `meta`.
- `notifications/tasks` status pushes for clients that opt in via
  `subscriptions/listen`; polling remains fully supported.

### Fixed

- A cancelled job polled through `get_job_result` reported
  `{"status": "done", "result": null}`; it now reports `cancelled`.
- `SUPPORTED_PROTOCOL_VERSIONS` listed `"2026-01-26"` — the **MCP Apps
  extension** revision, not a core one — and `_negotiate_protocol_version` fell
  back to it, telling clients the server spoke a protocol that does not exist.
- The `logging` capability was advertised as `{"listChanged": false}` and never
  delivered a message; it is now `{}` and actually emits.

### Testing

- `tests/test_schema_conformance.py` validates live server output against the
  vendored machine-readable schemas in `tests/schemas/` (base `2026-07-28` and
  `ext-tasks`), offline. `jsonschema` is now a dev dependency.
- `scripts/check_conformance.py` gained a `2026-07-28` section and can send
  per-call headers.

## 0.3.3

Fix `Optional[Dict[...]]` JSON-schema generation.
