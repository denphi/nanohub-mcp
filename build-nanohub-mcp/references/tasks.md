# Long-running work: async tools and the MCP Tasks extension

## Contents

- Choosing sync or async
- Server-side pattern
- Task-capable and legacy client flows
- Design guidance and testing

Any tool that can exceed a few seconds — a solver run, a mesh sweep, a
training job — must NOT block the JSON-RPC response. The gateway, proxies,
and clients all have timeouts; a blocking 3-minute tool call looks like a
dead server. The framework gives you one decorator that handles both modern
and legacy clients.

## Sync or async? Decide per tool, by worst case

Judge by the **worst plausible case** (largest mesh, most bias points), not
the demo case — the model will find the worst case for you.

| Worst-case duration | Use | Why |
|---|---|---|
| < ~2 s | sync | polling costs more than it saves — every poll is a full client→gateway→session round trip |
| 2–15 s | sync, usually | still inside every timeout; consider progress notifications via `ctx` so hosts show life |
| > ~15 s, or unbounded | `@server.async_tool` | you are now racing PHP/nginx/gateway/client timeouts you don't control — any hop killing the request loses the result |
| interactive/apps polling | sync + `["app"]` visibility | apps poll on timers; make the read cheap and idempotent instead of async |

Rules of thumb that fall out:

- **Duration is a schema property.** If a parameter changes the runtime class
  (`nsteps=10` vs `nsteps=10000`), either bound it in the input schema or
  make the tool async — don't ship a sync tool that is "usually fast".
- **Never async cheap reads.** An async `get_results` turns one round trip
  into three (call → poll → fetch) and floods the transcript with poll noise.
- **One async tool per family** is a smell worth aiming for: the create→run→
  get split ([server-guide.md](server-guide.md)) concentrates all slowness in
  `run_simulation`, so everything else stays snappy and cacheable.
- **Sync tools must be safe to retry.** Clients and gateways retry timed-out
  requests; a sync tool with side effects needs `idempotentHint: True`
  behavior for real, not just as an annotation.
- **If you're tempted to `sleep()` in a sync tool waiting for a file to
  appear — that's an async tool.**

## Server side: one decorator

```python
@server.async_tool(
    input_schema=_HANDLE_INPUT_SCHEMA,
    output_schema=_RUN_OUTPUT_SCHEMA,
    annotations={"title": "Run simulation", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def run_simulation(run_handle):
    """Run a confined handle. Long-running; poll, then call get_results."""
    ...                        # runs in a background thread
    return {"status": "finished", "outputs": [...]}   # stored as the task result
```

That's it. The function runs in a background thread; its return value (or
exception) is stored as the job result. Everything below happens
automatically.

## What clients experience (two flows, negotiated)

**Task-capable clients** — those that declared the Tasks extension
(`io.modelcontextprotocol/tasks`) in `initialize` capabilities, or per
request via `_meta["io.modelcontextprotocol/clientCapabilities"]`:

- `tools/call` returns immediately with `resultType: "task"` and a task
  object: `taskId`, `status` (`working` → `completed` / `failed` /
  `cancelled`), `pollIntervalMs`.
- The client polls `tasks/get` (terminal states carry the tool result or the
  error), and may `tasks/cancel`.
- Tasks are **owned by the MCP session**: a `tasks/get` from another session
  gets "Task is not available in this session", and a task-capable call
  without a session id is rejected with *"Task-capable async tool calls
  require an MCP session"*. In practice this means the `Mcp-Session-Id`
  header must flow through every hop — if async tools break through the
  gateway but work locally, a proxy is dropping that header.
- Results are retained for a limited window and then pruned; clients must
  collect them, not come back an hour later.

**Legacy clients** (no Tasks declaration): the same `tools/call` returns a
small JSON body — `{"status": "running", "job_id": "...", "message": "Poll
with get_job_result(job_id=…)"}` — and the model polls the built-in
**`get_job_result`** tool. You get graceful degradation for free; don't
remove or shadow `get_job_result`.

## Design guidance

- **Only the run is async.** Keep `create_*` (deck generation) and `get_*`
  (result extraction) synchronous and fast; the async tool should do nothing
  but execute the solver (see the create→run→get pattern in
  [server-guide.md](server-guide.md)).
- **Return a summary, not the data.** The task result should say what was
  produced (`outputs`, warnings, timings); the model then calls the cheap
  `get_*` readers for arrays. Giant task results bloat every poll response.
- **Progress**: for long phases, a `ctx` parameter gives you
  progress/logging helpers tied to the request's progress token.
- **Cancellation** maps to `tasks/cancel`, which sets a per-job cancel event
  and fires the callbacks your handler registered — so a cancel stops the
  solver instead of just relabelling the record. Poll `ctx.is_cancelled()`
  between iterations, wait on `ctx.cancel_event` instead of `sleep`, and
  register `ctx.on_cancel(...)` to kill a process group you cannot poll out
  of:

  ```python
  proc = subprocess.Popen(argv, start_new_session=True)
  ctx.on_cancel(lambda: os.killpg(proc.pid, signal.SIGTERM))
  while proc.poll() is None:
      if ctx.cancel_event.wait(0.5):
          break
  ```

  Cancellation is cooperative by spec — a cancelled task may still reach a
  non-`cancelled` terminal state — so decide what a half-finished run leaves
  behind. `server.cancel_job(job_id)` is the same path, for offering
  cancellation to legacy clients through a tool of your own.
- **Push instead of poll, if the client asks.** A client may subscribe with
  `subscriptions/listen` (`params.notifications.taskIds`); the server
  acknowledges with `notifications/subscriptions/acknowledged` and then pushes
  `notifications/tasks` on every status change, carrying the full task and its
  terminal result. This *supplements* polling — the spec says servers MAY push
  in addition to servicing `tasks/get`, and clients MAY keep polling — and it
  is strictly opt-in: a session that never subscribes receives nothing. You
  get it for free; there is nothing to add to a tool. Do not design a tool
  that only works with notifications, since most hosts still just poll.
- **Durable handles at dispatch.** If the caller needs an identifier to find
  the work again — a scheduler job id, a run handle — mint it in a `prepare`
  hook. It runs synchronously before the worker starts, so it lands on the
  task handle the call returns; the tool body has not run yet and cannot:

  ```python
  def _submit(deck, ctx=None):
      ctx.set_task_metadata(jobHandle=cluster.submit(deck))

  @server.async_tool(prepare=_submit)
  def run_simulation(deck, ctx=None):
      return cluster.wait(ctx.task_metadata["org.nanohub/jobHandle"])
  ```

  Values surface as `_meta` on the initial handle and on every `tasks/get`
  (the `Task` object has no `_meta`, but `CreateTaskResult` is `Result &
  Task`, so the envelope carries it). Bare keys get an `org.nanohub/` prefix;
  MCP-reserved prefixes raise. Hosts may ignore `_meta` entirely, so keep a
  tool that lists live handles as the reliable path.
- **Docstring**: say explicitly "long-running; returns a task/job — poll for
  the result, then call get_… tools". Models handle the flow well when told.

## Testing

- Locally: call the async tool, assert you get a `job_id`/task immediately,
  then poll `get_job_result` (or `tasks/get` with a session) until terminal.
- A fake solver (`sleep 2; write output`) exercises the whole lifecycle
  without scientific dependencies — good offline-test material.
- Cancel mid-run and assert the *work* stopped, not just the status: check the
  `on_cancel` callback fired and the handler returned early. A test that only
  asserts `status == "cancelled"` passes even when the solver runs to
  completion.
