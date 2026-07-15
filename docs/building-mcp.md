# Building MCP servers (the `build-nanohub-mcp` skill)

This section reproduces the **`build-nanohub-mcp`** skill that ships in this
repository. It walks through turning a simulation, solver, or analysis library
into a secure, bounded nanoHUB MCP tool — designing explicit tools, hardening
inputs, testing offline and in CI, packaging as a headless nanoHUB tool,
publishing through HubZero/`com_mcp`, connecting real MCP clients, and
diagnosing gateway, OAuth, session, CORS, Tasks, Resources, and MCP Apps
failures.

:::{admonition} Get the skill
:class: tip

The skill is a directory of Markdown, scripts, and a worked example, not a
single file. To use it with Claude Code or Claude:

- **Browse on GitHub:**
  [build-nanohub-mcp/](https://github.com/denphi/nanohub-mcp/tree/main/build-nanohub-mcp)
- **Download the whole repo (contains the skill) as a zip:**
  [nanohub-mcp main.zip](https://github.com/denphi/nanohub-mcp/archive/refs/heads/main.zip)
  — after unzipping, the skill is the `build-nanohub-mcp/` folder.
- **Or clone it:**

  ```sh
  git clone https://github.com/denphi/nanohub-mcp.git
  # skill lives at nanohub-mcp/build-nanohub-mcp/
  ```

Install it as a Claude skill by copying the `build-nanohub-mcp/` directory into
your project's `.claude/skills/` (or your personal skills directory).
:::

```{include} skill/_skill_body.md
```

## Reference material

The steps above link into these reference pages, included here in full:

```{toctree}
:maxdepth: 1

skill/references/security
skill/references/state-model
skill/references/server-guide
skill/references/tasks
skill/references/project-layout
skill/references/connecting-clients
skill/references/troubleshooting
skill/references/verification
skill/references/mcp-apps
skill/references/elicitation
skill/references/quota-and-etiquette
skill/references/oauth-dcr
skill/references/gateway-cors
skill/references/versioning
```
