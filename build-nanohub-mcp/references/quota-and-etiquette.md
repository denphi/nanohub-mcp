# Quota and etiquette: tokens, API calls, sessions

Your tool results live in three budgets at once: the model's context window
(tokens), the hub's API quota (every call is a proxied POST), and the hub's
compute (tool sessions are real containers). Designs that ignore any of the
three feel fine in a demo and degrade in real conversations.

## Token economics — results are forever

Every tool result enters the model's context and is re-read on every
subsequent turn of the conversation. A 100 kB array returned once costs
tokens on *every* turn after it.

- **Decimate plot data.** A human-facing curve needs ~200–500 points, not
  50 000. Downsample server-side; offer `max_points` in the schema if power
  users need more.
- **Summaries + handles, not blobs.** Return `run_handle`, artifact names,
  and a summary (`n_points`, ranges, peak values, warnings); let dedicated
  read-only tools fetch specific slices on demand. This is the create→run→get
  pattern's other payoff.
- **Return the deck once.** Input decks/configs are great for reproducibility
  — from `create_*`. Don't re-attach them to every run/status/result payload.
- **Paginate lists** (`limit` + `offset`/cursor in the schema) and default
  the limit low (10–25). The model will page when it needs to.
- **Logs: tail, don't dump.** A `get_log(run_handle, tail_lines=50)` beats
  returning a solver log wholesale; failed-run diagnosis rarely needs more.
- **Images/binaries**: return them only when asked, as resources or artifact
  reader tools — never embedded by default in a result the conversation will
  drag along.
- **Error messages are context too.** Return a fixed code and one actionable
  line, not a reflected argument, stack trace, command, or file content.

## API-call economics — the gateway meters you

Every `tools/call`, poll, and resource read from any client goes through
`/api/mcp/{tool}/...` and counts against hub API rate limits.

- **Async polling is the big consumer.** Honor the task's `pollIntervalMs`;
  in your own clients/apps use backoff, not a tight loop.
- **Apps multiply calls.** An app polling on a 3 s timer is 1 200 calls/hour
  per open tab. Hosts defend themselves (the com_mcp host coalesces identical
  in-flight calls, short-caches identical results, rate-limits per frame, and
  stretches reuse to minutes for hidden tabs) — design apps that stay under
  those defenses: `seq`-cursor polling tools, `visibilitychange` awareness,
  and no identical-args calls in render loops.
- **DCR endpoints are rate-capped** (per-IP hourly and global daily caps) —
  register clients once and store them; don't re-register per run.

## Session economics — containers aren't free

- Each tool's gateway traffic runs in a real hub session (headless = cheap,
  not free). The gateway reuses a live session; your server should tolerate
  serving many conversations from one process — no global "current user"
  state outside confined run handles and bounded per-session `ctx` state.
- Clean up after runs: cap run retention and total bytes, offer a
  `delete_run`-style tool (marked `destructiveHint`), and don't fill the
  session disk with every parameter sweep.
- After publishing a new revision, stale sessions keep running old code —
  kill them (see versioning.md).

## A quick self-audit

For each tool ask: *if the model called this five times in one conversation,
what did it cost?* Five 400-point curves: fine. Five full IV logs with the
deck attached: you just spent the context window. Five async polls per
second: you're the reason the quota exists.
