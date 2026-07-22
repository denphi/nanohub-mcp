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
app  → host   ui/initialize            (request: appCapabilities, clientInfo)
host → app    ← result                 (hostCapabilities, hostInfo, hostContext)
app  → host   ui/notifications/initialized
```

Hosts hold back all data until they see `ui/notifications/initialized` —
an app that skips it renders but never receives tool input.

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

## Size and self-diagnosis

Apps are typically one self-contained HTML page with inlined JS/CSS — sizes
of several MB are real (a Rappture-generated app runs ~6 MB). Keep a
configurable size limit (default 8 MB, `..._APP_SIZE_LIMIT_BYTES` env) and
ship introspection tools (`get_..._app_size`, `validate_..._mcp_app`) that
render the app, report bytes vs limit, and assert invariants (bridge script
present, lifecycle handlers wired). Hosts have their own caps; when a tile
doesn't mount on a host that *did* declare the capability, size is the first
suspect.

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
