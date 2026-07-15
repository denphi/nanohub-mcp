# Versioning: protocol, server, tools, and hub revisions

Four version axes exist; know which one you're changing.

## 1. MCP protocol version (framework-handled)

nanohub-mcp negotiates from `SUPPORTED_PROTOCOL_VERSIONS` in the installed
`nanohubmcp/server.py`. The repository inspected for this skill listed
`2026-01-26, 2025-11-25, 2025-06-18, 2024-11-05`; verify the installed
environment instead of treating that list as permanent. If the client
requests one of those, it wins; otherwise the server answers with the newest.
You write no code for this — but don't assume a newer-protocol feature is
available just because the framework supports it: check the **negotiated
capabilities** (Tasks, Apps, elicitation), not the version string.

## 2. What nanohub-mcp does NOT support: live list changes

There are no `listChanged` notifications. Tools and resources are whatever
was registered when your module imported; if you register more at runtime (a
catalog-refresh tool, like rappturemcp's), connected clients are NOT
notified — they see additions only on their next `tools/list` /
`resources/list`. If you build such a refresh, return
`"..._list_notifications_supported": false` in its result so the model knows
to re-list rather than wait, and treat this as an admin affordance, not a
client-facing feature.

## 3. Tool surface evolution (your responsibility)

Clients cache tool lists; hosts pin behavior to tool names and schemas.

- **Names are forever.** A rename is a breaking change; the model-facing name
  is an API. Breaking change ⇒ new tool name, keep the old one delegating,
  mark its docstring "Deprecated: use X — kept for existing clients."
- **Schemas grow, never shrink.** Keep the small required envelope + open
  `additionalProperties` pattern (server-guide.md): adding output fields is
  then safe; removing or retyping a `required` field breaks claude.ai's
  structuredContent validation immediately.
- **Defaults are behavior.** Changing a default (even fixing one, like a
  sweep electrode) changes what every zero-argument call does — call it out
  in the tool description and release notes.
- Surface your versions at runtime for debugging: `MCPServer(name,
  version=APP_VERSION)` goes out in `initialize`'s serverInfo, and an
  `about`/`config://` resource that reports `APP_VERSION` plus key dependency
  versions lets anyone check WHAT is actually deployed through the MCP
  connection itself.

## 4. Hub tool revisions (where stale code hides)

The published nanoHUB tool revision is the deployment unit, and it has a
trap: **existing tool sessions keep running the old revision** after you
publish. The gateway happily reuses them. Symptoms: "I deployed the fix but
the behavior didn't change."

- After publishing, terminate the tool's active sessions (or wait them out)
  before re-testing.
- Bump `APP_VERSION` on every publish and check it via the about resource /
  serverInfo — that's your proof of which revision answered.
- The live hub may also lag your working tree at the *platform* level
  (gateway, OAuth components) — the verification.md prime directive applies
  to every axis of this file.
