# Connecting real MCP clients

## Contents

- Claude Code
- Claude and Claude Desktop
- ChatGPT custom apps
- MCP Inspector and nanoHUB chat
- Scripts and capability checks

Host behavior changes faster than server code. These notes were verified on
2026-07-15; recheck the linked vendor documentation before publishing a support
claim. Use the deployed Streamable HTTP endpoint:

```text
https://nanohub.org/api/mcp/{tool}/mcp
```

## Claude Code

Add the remote server and complete OAuth from `/mcp`:

```sh
claude mcp add --transport http mysolver \
  https://nanohub.org/api/mcp/mysolver/mcp
claude mcp get mysolver
claude mcp list
```

Start Claude Code, run `/mcp`, select the server, and authenticate in the
browser. Use `--scope local` for a project-local private configuration or the
current documented project/user scope when sharing is intentional. Do not put a
static token in a committed `.mcp.json`.

Reference: <https://docs.anthropic.com/en/docs/claude-code/mcp>

## Claude and Claude Desktop

For a deployed remote server, open Settings → Connectors, add a custom
connector, paste the endpoint, then connect/authenticate it. Team and Enterprise
organizations require an owner to add the organization connector; users then
authorize individually. Current paid-plan and mobile behavior is documented by
Anthropic and may change.

Use this host to test Tools, Prompts, Resources, text/image results, OAuth, and
ordinary remote workflows. Do not assume support for draft features such as
resource subscriptions, sampling, Tasks, Apps, or elicitation without observing
the initialize capabilities from the exact host version.

Reference: <https://support.anthropic.com/en/articles/11175166-about-custom-integrations-using-remote-mcp>

## ChatGPT custom app

ChatGPT connects to a remote MCP server, not directly to an unauthenticated
localhost development server. Enable developer mode/custom apps in the relevant
workspace settings, create the app with the nanoHUB endpoint, complete OAuth,
test privately, then have an authorized admin publish it for the workspace.
Plan, role, web-only, write-action, and approval rules are product-dependent.

ChatGPT can freeze the approved tool/input snapshot. After changing tools or
schemas, refresh/review the app actions or republish as required; a live server
change alone may not update the workspace copy. For a server on a private or
developer network, use OpenAI's supported secure tunnel rather than exposing it
ad hoc.

Reference: <https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta>

## MCP Inspector

Start the Inspector:

```sh
npx -y @modelcontextprotocol/inspector
```

Select Streamable HTTP, enter the local URL
`http://127.0.0.1:8000/mcp` (or the exact endpoint printed by `start_mcp`) or
the deployed nanoHUB URL, then connect. Exercise initialize, Tools, Resources,
Prompts, notifications, invalid inputs, and OAuth. Inspector success proves
protocol behavior; it does not prove that a production host supports an
extension or renders an app.

Reference: <https://modelcontextprotocol.io/docs/tools/inspector>

## nanoHUB hub chat

Select the registered service in the `com_mcp` chat UI. The hub supplies its
own authentication and gateway route. Verify that the host's initialize request
declares every extension the server gates: Tasks, MCP Apps, and elicitation are
not implied by basic tool support.

## Scripts and automation

Use a nanoHUB developer token only through an environment variable or secret
store. Send it in the `Authorization: Bearer` header, capture the
`Mcp-Session-Id` response header from initialize, and reuse it for every
subsequent request in the session. Never print the token or place it in a tool
result, committed shell script, URL, or CI log.

## Capability checklist

Record observed behavior per host and version instead of relying on the product
name:

| Capability | How to prove it |
|---|---|
| Tools | `tools/list`, valid and invalid `tools/call` |
| Resources | `resources/list` then `resources/read` |
| Prompts | `prompts/list` then `prompts/get` |
| Tasks | initialize declaration, task-returning call, same-session poll/cancel |
| MCP Apps | UI extension + MIME declaration, resource read, full app handshake |
| Elicitation | client capability + server request/response over an active session |
| OAuth | 401 challenge, discovery, registration/client config, consent, refresh |

Do not advertise a capability until the target host proves it on the deployed
revision.
