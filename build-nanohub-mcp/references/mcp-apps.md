# MCP Apps (interactive ui:// resources)

## Contents

- Protocol keys and server pattern
- App lifecycle
- Host bridge requests
- Size and self-diagnosis
- Host-side notes

Optional level-up: MCP Apps let a tool render an interactive HTML app
(plots, forms, 3D viewers) inside the chat. High payoff for simulation tools,
but ship the plain tools first — apps only work on hosts that implement the
extension, and everything below assumes your tools already work.

Follow the **official ext-apps extension** (spec `2026-01-26`,
https://modelcontextprotocol.io/docs/extensions/apps) — not older mcp-ui or
OpenAI Apps-SDK conventions.

## The spec keys, precisely (people get these wrong)

- The **tool** declares `_meta.ui.resourceUri = "ui://yourtool/appname"` — a
  **bare `ui` key**. The namespaced string `io.modelcontextprotocol/ui` is
  the *host capability* name, NOT the tool `_meta` key. (A deprecated
  `_meta["ui/resourceUri"]` form exists; don't use it.)
- The **resource** is served at that `ui://` URI with
  `mimeType: "text/html;profile=mcp-app"` and its own `_meta.ui` object
  (`csp` domain lists, `permissions`, `prefersBorder`).
- **Tool results need no UI reference** — the `tools/list` declaration is
  sufficient for the host to mount the app.
- **Hosts declare support at `initialize`**:

  ```json
  {"capabilities": {"extensions": {
      "io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}}
  ```

  Servers should check this (including the mimeTypes list) and degrade
  gracefully when absent.

## Server-side pattern

Per app, register a resource + a zero-argument `open_<name>` tool:

```python
meta = {"ui": {
    "resourceUri": "ui://yourtool/appname",
    "type": "resource",
    "csp": {"connectDomains": [], "resourceDomains": [],
            "frameDomains": [], "baseUriDomains": []},
    "permissions": {}, "prefersBorder": True,
}}
server.tool(name="open_appname", meta=meta,
            input_schema={"type": "object", "properties": {}, "required": []},
            ...)(open_fn)
```

Inside `open_fn(ctx=None)`:

- If the host declared the UI capability → return
  `status: "opened"` and a note telling the model the UI is ready and how to
  push later parameter changes (`set_..._inputs`), and explicitly: "do NOT
  emit HTML, iframe markup, or ui:// links".
- Otherwise → `status: "ui_not_available"` and a note saying the UI was NOT
  rendered, the model must not claim it is visible, and offering the
  chat-driven fallback tools. **Never make the success note unconditional** —
  the server cannot see the host's screen.
- Do not put the `ui://` URI in the result body (the LLM will write its own
  iframe; CSP blocks it and the chat shows a broken box).

Tools that only the app itself should call (state polling, example loaders)
get `_meta.ui.visibility = ["app"]` so hosts hide them from the model.

## The app lifecycle (spec `2026-01-26`)

Your HTML app talks JSON-RPC to the host over `postMessage`. The app
initiates; implement every phase or hosts will consider the app broken.

**1. Handshake — app goes first:**

```
app  → host   ui/initialize            (request: appInfo, appCapabilities,
                                        protocolVersion — never clientInfo)
host → app    ← result                 (hostCapabilities, hostInfo, hostContext)
app  → host   ui/notifications/initialized
```

Hosts hold back all data until they see `ui/notifications/initialized` —
an app that skips it renders but never receives tool input. The rule is
symmetric and hosts enforce it: **send nothing before `initialized`** — no
`tools/call`, no `ui/notifications/size-changed`. Page startup code that calls
tools on `DOMContentLoaded` must await the handshake, or the host logs
`AppBridge received 'tools/call' before ui/notifications/initialized` and drops
the calls. Queue outbound notifications until the handshake resolves, and gate
`callTool` on it.

**2. Host pushes data (after initialized):**

| Notification | When |
|---|---|
| `ui/notifications/tool-input-partial` | streaming arguments, zero or more |
| `ui/notifications/tool-input` | complete arguments — always before the result |
| `ui/notifications/tool-result` | the tool call finished; render it |
| `ui/notifications/tool-cancelled` | stop spinners, keep state consistent |
| `ui/notifications/host-context-changed` | theme, locale, display mode changed |

**3. App requests to the host** (each is a normal JSON-RPC request):
`tools/call`, `resources/read`, `ui/message` (post into the chat),
`ui/open-link`, `ui/request-display-mode`, `ui/update-model-context` (feed
state back to the model for future turns), `notifications/message` (logging),
`ping`, and the `ui/notifications/size-changed` notification whenever your
content height changes (hosts size the iframe from it).

**4. Teardown:** the host sends a `ui/resource-teardown` *request* (it has an
`id`) before destroying the iframe. Reply — persist state fast if you must —
then expect to die. Don't rely on `unload` events inside a sandbox.

Lifecycle rules that follow from visibility: hosts refuse `tools/call` from
apps for tools whose `_meta.ui.visibility` lacks `"app"`, and hide
`["app"]`-only tools from the model. Default is `["model", "app"]`.

**Be a polite app.** Hosts throttle the app→server bridge hard (the com_mcp
host coalesces identical in-flight calls, caches identical results for ~5 s,
rate-limits per frame, and stretches poll reuse to minutes when the tab is
hidden). Design for it: poll with `visibilitychange` awareness and modest
timers, don't re-call the same tool with the same args in a render loop, and
prefer one `["app"]`-visibility polling tool with a `seq` cursor over many
chatty reads.

**Get the `ui/initialize` argument shape right — this is the #1 blank-app
cause.** The request params are `McpUiInitializeRequest`: `appInfo` +
`appCapabilities` + `protocolVersion` (`"2026-01-26"`) — **not** the core-MCP
`clientInfo` + `capabilities` shape. A spec-strict host (Claude) *rejects* a
malformed `ui/initialize`, so the promise rejects, `ui/notifications/initialized`
is never sent, and the host keeps the iframe `visibility:hidden` → the app
renders blank with no error in the chat. Lenient hosts (ChatGPT) render anyway,
which masks the bug. Send it as:

```js
rpc("ui/initialize", {
  appInfo: { name: "yourapp", version: "1.0.0" },
  appCapabilities: { availableDisplayModes: ["inline"] },
  protocolVersion: "2026-01-26",
}).then(() => notify("ui/notifications/initialized", {}))
  .catch((e) => console.error("ui/initialize failed:", e));
```

**Never fail the handshake silently, and retry it.** A bare `.catch(() => {})`
around `ui/initialize` turns every one of the failures above into a blank app
with an empty console — the single hardest MCP Apps bug to diagnose, because
the server side looks perfect end to end. Two rules:

- **Retry with backoff** (e.g. 400 ms → 5 s, ~30 s total) instead of one short
  timeout. Hosts answer at wildly different speeds: a host that renders the view
  in the same page replies synchronously, while one that routes it through a
  separate sandbox frame can drop the first attempts. Leave timed-out attempts
  registered in your pending-request map so a *late* reply to any of them still
  completes the handshake.
- **Surface the failure in the app's own DOM** when the retries are exhausted,
  and reject `callTool` with the reason. A visible banner in the iframe is the
  only channel you have — the host will not show one for you.

**Testable invariant (automated in this skill).** A presence check
(`"ui/initialize" in html`) is not enough — the broken shape above still
contains the string. The skill's validators assert the *argument shape* and the
full lifecycle, offline and live, sharing one rule set
(`scripts/mcp_conformance.py`):

```sh
python scripts/validate_server.py bin/yourtool.py     # offline: renders ui:// apps,
                                                      #   asserts appInfo+appCapabilities,
                                                      #   initialized, size-changed, and that
                                                      #   the server advertises the ui extension
python scripts/check_conformance.py http://localhost:8000   # live: ui:// ⇔ extension advertised,
                                                            #   resources/read handshake conformance
```

Also assert this in your tool's own test suite so a refactor fails fast in CI
(see mcp4mp `tests/test_mcp_app_handshake.py` for a self-contained example).

**Run these checks on rendered app HTML, not on the server source.** Both the
shared rule set and the project-local guards window on the text following the
literal `ui/initialize`, which makes them sensitive to prose:

- A host-language comment that *documents* the rule ("appInfo, never
  clientInfo") lands inside the window and reads as the code doing the wrong
  thing — a false positive on a correct app. `mcp_conformance.py` now scans only
  `<script>` bodies and strips JS comments, but a Python `#` comment sitting
  inside a `<script>` template string can still fool it.
- A string literal containing `<script` (for instance a check asserting a
  fragment ships no script) pairs with a later `</script>` and swallows whatever
  lies between.
- The project-local guards use the **first** occurrence with a fixed window, so
  keep the literal method name out of comments and log messages above the call —
  otherwise the real params fall outside the window and a correct app fails.

Rendering first avoids all three: the host language is gone and only the app's
own markup remains.

## Size and self-diagnosis

Apps are typically one self-contained HTML page with inlined JS/CSS — sizes
of several MB are real (a Rappture-generated app runs ~6 MB). Keep a
configurable size limit (default 8 MB, `..._APP_SIZE_LIMIT_BYTES` env) and
ship introspection tools (`get_..._app_size`, `validate_..._mcp_app`) that
render the app, report bytes vs limit, and assert invariants (bridge script
present, lifecycle handlers wired). Hosts have their own caps; when a tile
doesn't mount on a host that *did* declare the capability, size is the first
suspect.

## Your own host is not a conformance test

The most common reason an app works locally and dies on a third-party host is
that the local host is more permissive. Check these before blaming the host:

- **CSP.** A conformant host *builds* a CSP from your resource `_meta.ui.csp`
  and applies it to the iframe; a host that just sets `iframe.srcdoc` (com_mcp
  does) enforces nothing. With the secure default of empty domain lists you get
  roughly `default-src 'none'; script-src 'self' 'unsafe-inline'; connect-src
  'self'; img-src 'self' data:; font-src 'self'; media-src 'self' data:`. Note
  what is absent: **no `blob:` anywhere** (so no blob workers, blob scripts, or
  blob object URLs) and **no `data:` in `font-src`** (so inlined `@font-face`
  data URIs fail). `csp` accepts *domains only* — there is no way to re-add a
  scheme, so don't depend on those. Inline `<script>` and `<style>` are fine;
  `<script src="data:...">` is not.
- **Sandbox.** Assume `allow-scripts` only. No top-level navigation, no popups,
  no form posts to anything real.
- **Handshake latency.** A same-page host replies to `ui/initialize` in
  microseconds; a sandbox-frame host does not. See the retry rule above.
- **Viewport-height CSS silently pins the frame.** If your page came from a
  standalone web app it probably has `body { height: 100vh/100dvh;
  overflow: hidden }`. Inside an iframe that closes a feedback loop:
  `scrollHeight` equals whatever height the host granted, you report that back
  via `ui/notifications/size-changed`, and the host keeps the frame at its
  initial size forever. The app renders *correctly* but as an unusable sliver,
  and it only looks right in fullscreen — the one mode where it finally gets a
  real viewport. Measured on a Rappture app in a 200 px frame: the document laid
  out at 232 px; with `html, body { height: auto; overflow: visible }` plus a
  `min-height` floor it laid out at 800 px. Override the height in app mode and
  report `max(documentElement.scrollHeight, body.scrollHeight, floor)`.
- **Transport.** claude.ai's client also opens the optional `GET` SSE stream and
  can treat a failure as fatal for the whole connector; make sure the gateway
  answers `GET /mcp` with `text/event-stream`, not 405.

## Host-side notes (if you also build the host)

- Render in a sandboxed iframe with a postMessage JSON-RPC bridge; the app
  initiates with `ui/initialize`.
- Declare the capability at `initialize` (see above) — servers now gate on it.
- Canonicalize resource URIs before comparing: the nanoHUB gateway prefixes
  its origin onto `ui://` URIs (`https://nanohub.org/ui://tool/app`); compare
  from the last non-http(s) scheme marker onward or app frames are never
  matched and data pushes are dropped.
- Empty CSP domain lists must survive as `[]` end-to-end (see the `{}` vs `[]`
  passthrough rule in gateway-cors.md).
