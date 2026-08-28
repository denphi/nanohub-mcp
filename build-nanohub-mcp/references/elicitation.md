# Elicitation: asking the user for input mid-tool

Sometimes the tool — not the model — needs the user: confirming a destructive
step, filling in required parameters the conversation never provided, or
completing an external flow. MCP elicitation is a server→client request that
pops a structured form (or a URL) in the host UI. nanohub-mcp supports both
modes through the injected `ctx`.

## Form mode

```python
@server.tool(input_schema=..., output_schema=...)
def submit_batch(run_handle, ctx=None):
    """Submit the run to the cluster after user confirmation."""
    try:
        answer = ctx.elicit(
            "Submit this 4-hour batch run?",
            requested_schema={
                "type": "object",
                "properties": {
                    "queue": {"type": "string", "enum": ["standby", "long"],
                              "default": "standby"},
                    "notify": {"type": "boolean", "default": True},
                },
                "required": ["queue"],
            },
        )
    except RuntimeError:
        # Client declared no elicitation capability — degrade to conversation.
        return ToolResult(
            content="This client cannot show forms. Ask the user which queue "
                    "to use (standby or long), then call submit_batch_confirmed.",
            is_error=True)

    if answer.get("action") != "accept":
        return {"status": "cancelled_by_user"}
    queue = (answer.get("content") or {}).get("queue", "standby")
    ...
```

Facts that matter:

- **Handle all three actions.** The result is `{"action": "accept" |
  "decline" | "cancel", "content": {...}}` — treat decline and cancel as
  first-class outcomes, not errors.
- **Capability-gated.** The framework raises `RuntimeError` when the client
  didn't declare the `elicitation` capability (form mode also accepts the
  bare `{}` declaration). Always wrap and provide a conversational fallback —
  many MCP clients still don't implement it.
- **Fail closed when the prompt is a safety gate.** The fallback above is a
  degradation for *parameter* questions. When the elicitation is confirming an
  irreversible or expensive action, `RuntimeError`, `decline`, `cancel`, and
  timeout all mean do not proceed — see
  [security.md](security.md) for which actions require a prompt and why the
  model must not relay consent on them.
- **Keep the schema flat and small.** Hosts render it as a form; nested
  objects and long enums make bad forms. Provide `default`s — they prefill.
- **Default timeout is 60 s** (`ctx.elicit(..., timeout=…)`); a user who
  walked away raises a timeout error you should catch and report calmly.

## URL mode

```python
result = ctx.elicit_url(
    "Authorize access to the external data source",
    url="https://example.org/authorize?state=...",
)
```

The host opens/offers the URL and reports completion (`elicitationId`
correlates the flow). Use for third-party authorization or anything requiring
a real browser context. Never use elicitation of either mode to collect
passwords or secrets into tool results — results enter the model context.

## Under protocol 2026-07-28: the server asks by *returning*

That revision removed server-initiated requests. A server can no longer push
`elicitation/create` down the wire and block, so the same `ctx.elicit()` call
takes a different route — **the framework picks; your tool body does not
change**. What changes is what happens around it.

**In a sync tool, the call is re-driven.** `ctx.elicit()` raises
`InputRequired`, the server answers with an `InputRequiredResult` naming what
it needs, and the client retries the *same* `tools/call` with `inputResponses`
plus a `requestState`. **Your handler then runs again from the top.** So:

- **Be idempotent up to each ask.** Everything before the ask happens once per
  round trip. Compute the preview, then ask, then do the work — never write
  files, submit jobs, or mutate state before a confirmation.
- **Ask in a stable order.** Answers are keyed by ask order (`input-1`,
  `input-2`, …), so the handler must reach the same asks in the same sequence
  on a retry. Don't branch on a random value or a clock before an ask.

**In an async tool, nothing re-runs.** The worker thread is alive and holding
its state, so the task moves to `input_required`, publishes its
`inputRequests`, and parks until the client sends `tasks/update`. It then
resumes in place. An async handler needs no idempotency at all — expensive prep
before an ask happens exactly once. This is the better shape for anything
costly: put the work in an `@async_tool` and the retry problem disappears.

What does not change:

- **The capability gate still raises `RuntimeError`** when the client never
  declared `elicitation`, on both stacks. The fail-closed pattern in
  [security.md](security.md) works unmodified.
- **Never wrap the ask in a bare `except Exception`.** `InputRequired` derives
  from `BaseException` precisely so a careless catch cannot swallow it and
  return a fabricated answer — but catching `BaseException` would still break
  the flow. Catch `RuntimeError`, which is what the gate raises.
- URL mode loses `elicitationId`: with no completion notification, the client
  reports the outcome by retrying. Encode your own correlation id in
  `requestState` if you need one.

## Transport constraints (nanoHUB-specific)

- Server→client requests ride the MCP session channel. They work through
  `POST /mcp` with an active session; the direct REST convenience route
  (`POST /tools/{name}`) **refuses context-bearing tools with 409** for
  exactly this reason. If you add `ctx` to a tool, script users lose the REST
  shortcut for it — that is the intended trade.
- Elicit **before** starting long work, from a fast sync tool (a
  `confirm_and_submit` step), not from inside an async task: the model and
  user may be several poll cycles away when a background thread asks.

## When to elicit vs when to let the model ask

| Situation | Use |
|---|---|
| destructive/expensive confirmation | `ctx.elicit` — required, and fail closed ([security.md](security.md)) |
| a required parameter with a small closed choice set | elicit — beats a wrong guess |
| open-ended scientific intent ("what sweep range?") | let the model ask in chat — it has context |
| external browser flow (OAuth to a data source) | `ctx.elicit_url` |
| clients without the capability, cheap/reversible action | ToolResult telling the model what to ask |
| clients without the capability, irreversible/expensive action | refuse and say so — the model must not relay consent |
| a confirmation in a tool that already did expensive work | move the work after the ask, or make the tool `@async_tool` — a sync handler re-runs from the top on 2026-07-28 |
