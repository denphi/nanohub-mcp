# Vendored MCP schemas

Machine-readable JSON Schemas for protocol revision `2026-07-28`, used by
`tests/test_schema_conformance.py` to validate real server output against the
specification rather than against our reading of it.

| File | Source |
|---|---|
| `mcp-2026-07-28.schema.json` | `modelcontextprotocol/modelcontextprotocol` → `schema/2026-07-28/schema.json` |
| `ext-tasks-2026-07-28.schema.json` | `modelcontextprotocol/ext-tasks` → `schema/2026-07-28/schema.json` |

Vendored so the suite stays offline — CI must not depend on network access,
and `scripts/check_ci.py` fails the build on skipped tests.

Refresh with:

```sh
curl -sL https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/2026-07-28/schema.json \
  -o tests/schemas/mcp-2026-07-28.schema.json
curl -sL https://raw.githubusercontent.com/modelcontextprotocol/ext-tasks/main/schema/2026-07-28/schema.json \
  -o tests/schemas/ext-tasks-2026-07-28.schema.json
```

**These schemas leave `additionalProperties` unset**, so validation proves
required fields and types are right — not that output carries no extra keys.
