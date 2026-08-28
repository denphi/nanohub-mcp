# Compatibility report — async task control, task metadata, and status notifications

Covers the three features added to `nanohubmcp` in this cycle, the compatibility
surface each one touches, and what was verified rather than assumed.

| | |
|---|---|
| **Version** | 0.4.0 |
| **Protocol** | `2026-07-28`, `2025-11-25`, `2025-06-18`, `2024-11-05` (dual-stack) |
| **Tests** | 107 passing (`pytest tests/ -q`), up from 59 |
| **Compatibility checks** | 10 scenarios, all passing |
| **Concurrency** | 7 threads × 25 iterations, no deadlock, no errors |
| **Conformance** | `validate_server.py` 0 errors, `check_conformance.py` 0 failed checks |


## What was added

0. **Protocol revision `2026-07-28`**, negotiated per request alongside every
   earlier revision. Adds `server/discover`, per-request `_meta` version and
   capabilities, `resultType` on every result, `CacheableResult` hints,
   deterministic `tools/list`, the renumbered error codes, stateless operation,
   and Multi Round-Trip Requests. Handshake-era clients are untouched.
1. **Real cancellation.** `tasks/cancel` now sets a per-job `threading.Event`
   and fires registered callbacks. Handlers poll `ctx.is_cancelled()`, wait on
   `ctx.cancel_event`, and register `ctx.on_cancel(cb)` to terminate a process
   group. `server.cancel_job(job_id)` exposes the same path for legacy clients.
2. **Task metadata at dispatch.** An optional `prepare=` hook on
   `@async_tool` runs synchronously before the worker starts;
   `ctx.set_task_metadata()` publishes durable handles that surface as `_meta`
   on the task handle and every `tasks/get`.
3. **Status notifications.** `subscriptions/listen` (with the Tasks `taskIds`
   filter) opts a session into `notifications/tasks` pushes.

## Compatibility matrix

### Existing servers and tools — no action required

| Check | Result |
|---|---|
| Async tool with no `ctx` parameter | Cancels without error; worker unaffected |
| Async tool with no `prepare` hook | Task handle is byte-identical to before — no `_meta`, no new fields |
| Legacy (non-Tasks) client body | Unchanged: `{status, job_id, message}`, no added keys |
| Job records created by older code | `cancel_job` and `_notify_task_status` tolerate missing `cancel_event` / `task_meta` |
| Tool entry gains a `prepare` key | Nothing iterates tool-entry keys; `tools/list` and OpenAPI unaffected |
| `Context.__init__` gains `job_id` | Appended last; every call site in the codebase and tests uses keywords |
| Job dict gains a `threading.Event` | No code path JSON-serializes a job record — verified by grep |
| `get_job_result` gains a `meta` key | The tool declares no `output_schema`, so no contract is broken |

Every existing async tool keeps working untouched. A tool that ignores the
cancel event still runs to completion on cancel — exactly the old behaviour.

### Client compatibility

| Client class | Behaviour |
|---|---|
| Tasks-capable, subscribes | Gets `_meta` on the handle, plus `notifications/tasks` pushes |
| Tasks-capable, no subscription | Gets `_meta`; **receives no notifications** — required by spec |
| Legacy, no Tasks extension | Gets `job_id` polling as before, with handles under `meta` in the body |
| Older protocol (`2025-06-18`) | Negotiates normally; `_meta` on results is valid in every supported revision |
| Doesn't know `subscriptions/listen` | Never calls it, keeps polling — no degradation |

Notifications and polling are complementary by design. The spec is explicit:
servers **MAY** push `notifications/tasks` *in addition to* servicing
`tasks/get`, and clients **MAY** keep polling. Nothing here removes polling.

### Protocol conformance

Verified against the `ext-tasks` schema and the 2026-07-28 base schema:

- `Task` has no `_meta`, but `CreateTaskResult = Result & Task` and
  `GetTaskResult = Result & …`, so metadata rides the **response envelope**.
- `_meta` keys follow the naming rules; unqualified keys get `org.nanohub/`,
  and MCP-reserved prefixes (second label `modelcontextprotocol` or `mcp`)
  raise `ValueError` instead of emitting non-conformant traffic.
- The acknowledgement is the first message carrying a subscription id, and no
  notification precedes it.
- Only tasks that exist and belong to the calling session are acknowledged.
- Cancellation is treated as cooperative; a cancelled task may still reach a
  non-`cancelled` terminal state.

### Protocol revision 2026-07-28

Supported **alongside** the older revisions, chosen per request. A request
declaring `io.modelcontextprotocol/protocolVersion: "2026-07-28"` in `_meta`
gets the new behaviour; everything else is served exactly as before.

| Change | Status |
|---|---|
| `server/discover` (MUST) | implemented; returns versions, capabilities, `instructions` |
| Per-request `_meta` version + capabilities | implemented; `_client_supports` consults them first |
| `UnsupportedProtocolVersionError` (`-32022`) | implemented with `{requested, supported}` |
| `resultType` on every result | implemented at the single dispatch exit |
| `_meta.serverInfo` on results | implemented (SHOULD) |
| Stateless operation | implemented: no `initialize`, no session id required |
| Error renumbering `-32001/3/4 → -32020/21/22` | implemented, version-gated |
| Resource not found `-32601 → -32602` | implemented, version-gated |
| `CacheableResult` (`ttlMs`, `cacheScope`) | implemented on `tools/list`, `resources/list`, `prompts/list` |
| Deterministic `tools/list` order | implemented (sorted by name) |
| `ping`, `logging/setLevel` removed | version-gated `-32601`; still served to older clients |
| Per-request `io.modelcontextprotocol/logLevel` | read via `ctx` |
| MRTR (`InputRequiredResult`) | implemented for sync tool calls |
| MRTR in tasks (`InputRequiredTask` + `tasks/update`) | implemented; worker parks and resumes |
| `Mcp-Method` / `Mcp-Name` headers | validated always; required via `run(require_route_headers=True)` |
| HTTP 400 for header/version errors | implemented |
| `resources/templates/list` | implemented (empty list) |
| `notifications/message` + per-request `logLevel` | implemented, gated per spec |
| `notifications/cancelled` | cancels the task or closes the listen stream |
| SSE resumability removed | never implemented — nothing to do |

**MRTR changes the execution model, and this is the part to understand before
writing new tools.** A server no longer pushes `elicitation/create` and blocks.
`ctx.elicit()` returns an answer if the client already supplied one; otherwise
it raises `InputRequired`, and `tools/call` answers with an
`InputRequiredResult` naming what it needs. The client retries the *same* call
with `inputResponses` + `requestState`, and **the handler runs again from the
top**. Two consequences:

- A handler must be **idempotent up to each ask**. Anything it does before
  eliciting happens once per round trip. Compute previews before asking; do
  side-effectful work after.
- Answers are keyed by ask order (`input-1`, `input-2`, …), so a handler must
  reach the same asks in the same order on a retry. Accumulated answers are
  held server-side against `requestState` for
  `MRTR_STATE_TTL_SECONDS` (10 minutes) and discarded when the call finishes.

The same tool source works on both stacks: older clients keep the blocking
path, and nothing in the tool body changes.

**MRTR works differently inside a task, and better.** A sync call has nothing
to block on, so the client re-drives it. An async tool's worker thread is alive
and holding its state, so instead the task moves to `input_required`, publishes
its `inputRequests`, and the thread parks until `tasks/update` delivers the
answer — then resumes in place. Nothing re-runs, so **an async handler needs no
idempotency**: expensive prep before an ask happens exactly once. Subscribers
are notified on entry to `input_required`, so a pushing client learns of the ask
without polling. `tasks/update` refuses responses for keys that are not
outstanding, and refuses a task that is not waiting.

**Routing headers are enforced where the protocol requires them.** A header
that *contradicts* the body is always rejected (`HeaderMismatchError`,
`-32020`, HTTP 400) — that is the whole point of a header a gateway routes on.
A *missing* header is rejected by default only for a request declaring
`2026-07-28`; earlier revisions never defined these headers, so requiring them
there would reject conformant clients. Enforcement is further limited to
transports that actually carry headers, so in-process calls, SSE, and stdio are
untouched. `run(require_route_headers=True|False)` forces it on or off for
every revision.

`com_mcp` now supplies them. `buildRouteHeaders()` forwards `Mcp-Method` /
`Mcp-Name` when the client sends them and otherwise derives them from the body
it has already parsed, so a client that knows nothing about these headers still
reaches a server running with enforcement on. Batches derive nothing, since no
single method or name describes them. Both were added to the CORS
`Access-Control-Allow-Headers` list so browser hosts may send them.

**Not implemented from this revision:** `resourceSubscriptions` (per-URI
resource update notifications), `x-mcp-header` (custom headers sourced
from tool parameters), and list pagination (`nextCursor`). Sessions remain
available because the nanoHUB gateway and every current client still use them.

### Schema validation (2026-07-28 tasks extension)

Server output was validated against the **machine-readable JSON Schema** from
`modelcontextprotocol/ext-tasks` (`schema/2026-07-28/schema.json`) and the base
`2026-07-28` schema — not against a reading of the prose:

| Shape | Definition | Result |
|---|---|---|
| `tools/call` task handle | `CreateTaskResult` | pass |
| `tasks/get` working / completed / cancelled / failed | `GetTaskResult` | pass |
| `notifications/tasks` (whole notification) | `TaskStatusNotification` | pass |
| `notifications/tasks` params | `TaskStatusNotificationParams` | pass |
| acknowledgement filter | `TaskSubscriptionAcknowledgedNotifications` | pass |
| acknowledgement notification | `SubscriptionsAcknowledgedNotification` (base) | pass |
| `tasks/cancel` result | `CancelTaskResult` | pass |

The validator was proven non-vacuous: 8 deliberately malformed instances
(missing `taskId`, missing `ttlMs`, missing `resultType`, `resultType:"complete"`
on a task handle, bad `status` value, wrong types, wrong notification method)
were all correctly rejected.

**Two limits on what that proves:**

- `additionalProperties` is unset throughout the generated schema, so validation
  checks required fields and types — **not** that output carries no extra keys.
  Passing is not proof of a byte-exact shape.
- It covers only the **tasks extension slice** that is implemented. See below.

### Concurrency

Lock order is **jobs → subscriptions → clients**, and no path holds two of them
across a call that takes the third:

- `_notify_task_status` releases `_subs_lock` *before* `_broadcast` takes
  `_clients_lock`.
- `_unregister_client` releases `_clients_lock` *before* `_drop_subscriptions`
  takes `_subs_lock`.
- `_fire_cancel_callbacks` runs user callbacks outside every lock, so a
  callback that kills a process group cannot block other jobs' bookkeeping.

Stressed with 6 churn threads (create → subscribe → cancel → get) against a
thread repeatedly disconnecting and reattaching the SSE client: no deadlock,
no exceptions. **This invariant is easy to break** — keep broadcasts and user
callbacks outside all locks.

### Runtime

Python 3.7+ per `pyproject.toml`; the additions use `.format()`, type comments,
and no 3.8+ syntax, matching the existing style. `isinstance(key, str)` checks
are safe on the supported range.

## Known deviations

Deliberate, and each one degrades to "client keeps polling":

1. **Requests are accepted without `_meta`.** 2026-07-28 makes
   `params._meta` mandatory, carrying `protocolVersion` and
   `clientCapabilities` on every request. This server reads both when present
   but never rejects a request for omitting them — a request with no declared
   version is simply served as `2025-11-25`. That permissiveness is what makes
   dual-stack possible; a strict-mode switch could be added if a deployment
   wants to refuse under-specified requests.
2. **Notifications ride the existing SSE channel**, not a
   `subscriptions/listen` response stream. A client that expects them on that
   POST's response body gets `202 {"status":"accepted"}` and must be on SSE.
3. **`tasks/update` is acknowledged but inert.** The server never produces an
   `InputRequiredTask` or `inputRequests`, so that half of the extension is
   unexercised and unvalidated.
4. **Only the `taskIds` filter is implemented.** The base filters
   (`toolsListChanged`, `promptsListChanged`, `resourcesListChanged`,
   `resourceSubscriptions`) are accepted but never honoured — safe, because the
   server simply never sends those notification types.
5. **No graceful `SubscriptionsListenResult`.** Subscriptions are dropped on
   disconnect, which the spec treats as an abrupt close carrying no response.
   A graceful shutdown should send one; it does not yet.
6. **`_meta` may be dropped by hosts.** A durable handle delivered only that way
   can fail to reach the model, so a tool that lists live handles remains the
   reliable path. This is a property of the ecosystem, not a defect.

## Deployed servers assessed

Every server in `com_mcp/` and `examples/` was loaded and validated against
0.4.0 with `scripts/validate_server.py`:

| Server | Lines | Result | Notes |
|---|---|---|---|
| `mcpdemo` | 716 | 0 errors | no `ctx`; unaffected |
| `mcp4mp` | 612 | 0 errors | no `ctx`; unaffected |
| `auramcp` | 1,814 | 0 errors | reads `server._jobs` (see below) |
| `rappturemcp` | 12,560 | 0 errors | 4 `ctx.elicit` sites, reads `server._sessions` |
| `examples/simple` | — | 0 errors | |
| `examples/simulator` | — | 0 errors | |
| `examples/data_analysis` | — | 0 errors | |

Warnings are pre-existing description/annotation nits, not errors. `mcpdemo`
was additionally driven end-to-end with both a 2026-07-28 client and a
2025-11-25 client on the same process: the modern client got `resultType`,
`ttlMs`, and sorted tools; the legacy client got byte-identical output to
before.

Two servers reach into framework internals. Both are safe:

- **`auramcp`** peeks `server._jobs` under `_jobs_lock` to poll a job without
  consuming it, reading only `status` and `lastUpdatedAt`. The keys added in
  0.4.0 (`cancel_event`, `cancel_callbacks`, `task_meta`) are ignored, and it
  never serializes a whole record — which matters, because `cancel_event` is a
  `threading.Event` and would not survive `json.dumps`.
- **`rappturemcp`** scans `server._sessions` for any session that declared MCP
  Apps, working around the gateway dropping `Mcp-Session-Id`. For a stateless
  2026-07-28 client that registry is empty, so the heuristic returns nothing —
  but it degrades to "unknown", which that code already treats as "don't claim
  the host has no UI". The per-request `_meta` capabilities now supported are
  the proper replacement for this workaround.

`rappturemcp`'s own `_jobs` is a module-level dict of its own, unrelated to the
framework registry — no collision.

**`rappturemcp` is MRTR-safe — an earlier draft of this report said otherwise
and was wrong.** All four of its `ctx.elicit` sites are read-only up to the ask
and catch narrowly (`AttributeError`, `RuntimeError`, `TypeError`), so an ask
propagates rather than being swallowed, and a client that cannot elicit fails
closed. The one state write that precedes an ask is guarded by
`if state is None` under an `experiment_id` derived from `plan_sha256`, so a
retry reloads the record instead of resetting every point to `pending`.
Verified by driving both confirmation helpers with a 2026-07-28 context. The
invariants are now recorded as comments at each site so a future edit does not
quietly break them.

## Issues found and fixed

| Issue | Fix |
|---|---|
| MRTR ignored client capabilities: a 2026-07-28 client that never declared `elicitation` was sent an `InputRequiredResult` it could not answer, so a handler's fail-closed `except RuntimeError` never fired — it neither acted nor refused | `ctx.elicit`/`sample`/`list_roots` now raise `RuntimeError` on both stacks when the capability is absent |
| A handler with a broad `except Exception` around an ask silently swallowed it and returned a fabricated answer | `InputRequired` derives from `BaseException` |
| `logging` capability advertised as `{"listChanged": false}` and never delivered a message | shape is `{}`, and `notifications/message` is emitted, gated on the request's `logLevel` |
| A cancelled job polled through `get_job_result` reported `{"status": "done", "result": null}` — it fell past the `error` check into the success branch | Returns `cancelled`; test added |
| `SUPPORTED_PROTOCOL_VERSIONS[0]` was `"2026-01-26"`, the **MCP Apps extension** revision, not a core one. Introduced in `6ce70ca`. `_negotiate_protocol_version` falls back to element zero, so unrecognized clients were told the server speaks a nonexistent protocol | Removed, with a comment recording the namespace rule. Fallback is now `2025-11-25` |
| `references/versioning.md` published the same wrong list | Corrected, with a note on the two version namespaces |
| `docs/api.rst` stated the wrong negotiation ceiling | Corrected |

Every *correct* use of `2026-01-26` was left alone — it is the `ui/initialize`
`protocolVersion` in `mcp_conformance.py` (`LATEST_UI_PROTOCOL`),
`check_conformance.py`, `references/mcp-apps.md`, and the simulator example.

## Open risks

- **Gateway path unverified.** Notifications require an SSE stream held open
  through `wrwroxy` and `com_mcp`. If any hop buffers or drops it, notifications
  silently never arrive while polling keeps working — a failure mode that looks
  like nothing is wrong. Confirm in a live nanoHUB session before relying on it.
- **2026-07-28 removes sessions**, and task ownership is currently enforced by
  session (`_task_access_error`). Any future migration needs a new ownership
  basis; the run-handle pattern is the spec's prescribed replacement.
- **Notifications are best-effort.** They are not queued for a disconnected
  client; a client that misses one must fall back to `tasks/get`. Treat pushes
  as an optimization, never as the only way a client learns a terminal state.

## Reproducing the checks

```sh
python3 -m pytest tests/ -q                                   # 79 tests
python3 build-nanohub-mcp/scripts/validate_server.py bin/yourtool.py
start_mcp --app bin/yourtool.py --port 8000
python3 build-nanohub-mcp/scripts/check_conformance.py http://localhost:8000
```

The compatibility and stress scripts were run ad hoc; their scenarios are
covered permanently by `tests/test_mcp_server.py` except the deadlock stress,
which is timing-dependent and deliberately not in the suite.
