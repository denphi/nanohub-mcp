# Vendored MCP schemas

Machine-readable JSON Schemas for every protocol revision this server
advertises, used by `tests/test_schema_conformance.py` to validate real
server output against the specification rather than against our reading
of it. One per entry in `SUPPORTED_PROTOCOL_VERSIONS`: a revision the
server accepts but cannot be checked against is a revision it only claims
to speak.

| File | Source |
|---|---|
| `mcp-2024-11-05.schema.json` | `modelcontextprotocol/modelcontextprotocol` → `schema/2024-11-05/schema.json` |
| `mcp-2025-06-18.schema.json` | `modelcontextprotocol/modelcontextprotocol` → `schema/2025-06-18/schema.json` |
| `mcp-2025-11-25.schema.json` | `modelcontextprotocol/modelcontextprotocol` → `schema/2025-11-25/schema.json` |
| `mcp-2026-07-28.schema.json` | `modelcontextprotocol/modelcontextprotocol` → `schema/2026-07-28/schema.json` |
| `ext-tasks-2026-07-28.schema.json` | `modelcontextprotocol/ext-tasks` → `schema/2026-07-28/schema.json` |

Vendored so the suite stays offline — CI must not depend on network access,
and `scripts/check_ci.py` fails the build on skipped tests.

Refresh with:

```sh
for v in 2024-11-05 2025-06-18 2025-11-25 2026-07-28; do
  curl -sL "https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/schema/$v/schema.json" \
    -o "tests/schemas/mcp-$v.schema.json"
done
curl -sL https://raw.githubusercontent.com/modelcontextprotocol/ext-tasks/main/schema/2026-07-28/schema.json \
  -o tests/schemas/ext-tasks-2026-07-28.schema.json
```

**These schemas leave `additionalProperties` unset**, so validation proves
required fields and types are right — not that output carries no extra keys.
