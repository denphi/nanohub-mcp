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
| destructive/expensive confirmation with options | `ctx.elicit` (form with enum + defaults) |
| a required parameter with a small closed choice set | elicit — beats a wrong guess |
| open-ended scientific intent ("what sweep range?") | let the model ask in chat — it has context |
| external browser flow (OAuth to a data source) | `ctx.elicit_url` |
| clients without the capability | ToolResult telling the model what to ask |
