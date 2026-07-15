# State model

Separate four kinds of state. Script-style “current simulation” globals often
pass local tests and fail when one nanoHUB session process serves concurrent MCP
sessions and background tasks.

## Module globals

Keep only immutable configuration, registries built at import time, and bounded
shared caches in module globals. Protect mutable caches with locks because
`async_tool` handlers run in background threads. Include cache size limits,
expiry, and invalidation. Never store a global current user, current run,
current working directory, request context, or access token.

## Run state

Persist scientific inputs and outputs beneath an application-owned run root.
Return an opaque `run_handle`; resolve it through the confinement helper in
`security.md` for every operation. Put status in run-local files or a durable
store so readers do not depend on which worker thread handled the request.

Record ownership separately when runs are user-specific. A handle is a locator,
not authorization. Expire old runs, cap total retained bytes, and make cleanup
idempotent.

## MCP-session state

Use `ctx.session_id` only for ephemeral negotiated or conversational state such
as host capabilities, a currently mounted app instance, progress channels, or
per-session defaults. Key a synchronized map by session ID and set an expiry.
Handle missing context because direct tests and REST convenience routes may not
provide it.

Do not put authoritative run results only in this map: Tasks may outlive a
request, clients reconnect, and old session processes are eventually replaced.
Do not use the session ID to decide which authenticated user may access data.

```python
_SESSION_STATE = {}
_SESSION_LOCK = threading.Lock()

def update_session_state(ctx, **changes):
    if ctx is None or not ctx.session_id:
        return
    with _SESSION_LOCK:
        state = _SESSION_STATE.setdefault(ctx.session_id, {})
        state.update(changes)
        state["expires_at"] = time.time() + SESSION_TTL_SECONDS
```

Prune expired entries and cap the map. Avoid storing `ctx` itself because it is
request-scoped.

## Authenticated-user state

Derive identity and authorization from validated gateway credentials or a
trusted server-side identity supplied by the platform. Bind user-owned runs,
quotas, and permissions to that identity. Never infer identity from
`Mcp-Session-Id`, a run handle, a model-provided username, or a directory name.

## Concurrency rules

- Make create operations atomic and return a handle only after inputs are
  completely written.
- Write results to a temporary file and atomically replace the final filename.
- Prevent two runs from mutating the same handle simultaneously with a lock,
  lock file, or durable state transition.
- Make read tools tolerate `created`, `running`, `completed`, `failed`, and
  `cancelled` states explicitly.
- Make deletion refuse active work or cancel and join it before removal.
- Make retries idempotent in behavior, not only in annotations.

Test two sessions using the same process, two concurrent calls against one
handle, reconnection with a new session ID, stale-state expiry, and access to a
run owned by a different authenticated identity.
