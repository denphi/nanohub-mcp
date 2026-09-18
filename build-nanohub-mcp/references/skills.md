# Serve a skill from your tool (SEP-2640)

A **skill** is a directory of instructions — minimally a `SKILL.md` — that
teaches a model how to drive your tool. [SEP-2640][sep] serves that directory
over MCP as ordinary resources, so a host can load it the same way it loads a
skill from its own filesystem.

Ship a skill when your tools need *procedure* the schemas cannot carry: which
tool to call first, what a physically sensible parameter range is, how to read
the output, when to refuse. Server `instructions` is the wrong place for this —
it is delivered on every connection and is practically size-bounded. A skill is
fetched only when the model decides it is relevant.

Do **not** ship a skill to restate tool descriptions. If the content is one
sentence per tool, it belongs in the tool's `description`.

[sep]: https://modelcontextprotocol.io/seps/2640-skills-extension

## Register one

```python
from pathlib import Path

@server.skill("diffusion1d")          # -> skill://diffusion1d/SKILL.md
def diffusion1d_skill():
    return Path(__file__).parent / "skills" / "diffusion1d"
```

The decorated function is called **once, at registration**, and returns the
directory. Everything under it is walked immediately: each file is published at
`skill://<skill-path>/<relative-path>` with a SHA-256 digest and byte size, so
`skills/list` and `skills/get` answer from memory. File *contents* are read
lazily, per `resources/read`.

Nest the path to organize a family of skills — `@server.skill("nanohub/diffusion1d")`
publishes `skill://nanohub/diffusion1d/SKILL.md`. The **final segment must equal
the `name` in the frontmatter**; registration raises `ValueError` if it does not,
because that rule is what lets a host recover the skill's name from its URI.

## Lay the directory out

```text
bin/yourtool_mcp/skills/diffusion1d/
├── SKILL.md                    # required, with YAML frontmatter
├── references/
│   └── PARAMETERS.md           # physical ranges, units, failure modes
└── examples/
    └── session.md              # a worked create -> run -> get transcript
```

`SKILL.md` must open with frontmatter carrying at least `name` and
`description`:

```markdown
---
name: diffusion1d
description: Run and interpret 1-D diffusion simulations with this server's
  create_diffusion_sim / run_simulation / get_profile tools
---

# 1-D diffusion

Call `create_diffusion_sim` first; it validates the grid and returns a
`run_handle`. See `references/PARAMETERS.md` for physical ranges.
```

The `description` is what the host shows the model when deciding whether the
skill is relevant — write it as a trigger, not a title.

Keep a skill under **512 files and 16 MiB total**. Those are the limits every
conforming host must accept; past them your skill is not guaranteed to load
anywhere. Registration warns rather than failing, so check the warning.

Relative links inside `SKILL.md` resolve against the skill root, exactly as on
a filesystem — `references/PARAMETERS.md` becomes
`skill://diffusion1d/references/PARAMETERS.md`. Write them relative; never
hardcode the scheme.

## What the framework then serves

| Method | Behavior |
|---|---|
| `skills/list` | Every registered skill: `uri`, verbatim `frontmatter`, and a complete `resources` manifest of `{uri, digest, size}` |
| `skills/get` | One skill's entry, by its `SKILL.md` URI. Unknown URI → `-32602` |
| `resources/read` | Any skill file. UTF-8 decodable content is returned as `text`, anything else as a base64 `blob` |
| `resources/directory/read` | The direct children of a skill directory; subdirectories appear with `mimeType: inode/directory` |

Capability advertisement is automatic and conditional: register at least one
skill and `initialize` declares
`extensions["io.modelcontextprotocol/skills"] = {"directoryRead": true}`, plus
the base `resources` capability the SEP requires alongside it. Register none and
the extension is absent entirely.

Skill files are deliberately **not** listed in `resources/list`. They are
discovered through `skills/list` and read by URI, which keeps a large skill
catalog from flooding a host's resource browser.

## Frontmatter is parsed by a YAML subset

nanohub-mcp has zero dependencies, so frontmatter is parsed by a hand-rolled
subset rather than PyYAML. It covers scalars, quoted strings, booleans, null,
numbers, flow and block lists, nested mappings, flow mappings, and `|`/`>`
block scalars. It does **not** implement YAML anchors/aliases, and `>` folding
does not turn a blank line into a paragraph break.

This matters because the SEP requires the published `frontmatter` to be
identical in content to the file: a construct outside the subset is parsed into
something *other* than what a full YAML parser would produce, and a host that
compares the two rejects the skill. Keep frontmatter boring — flat keys, quoted
strings, `|` for multi-line text — and run `validate_server.py`, which checks
the published entry.

## The content is untrusted, and the host treats it that way

A skill is server-authored text that lands in a model's context, so hosts
treat it as a prompt-injection surface and gate it: origin is shown to the
model, loading may require user approval, approval is bound to the exact
digests in the manifest, and `allowed-tools` in frontmatter grants a
remote skill nothing. Write to that reality:

- **Never** put credentials, tokens, or internal hostnames in skill content.
  It is published to every client that connects.
- Do not assume the skill was loaded. It is optional and may be declined; your
  tools must still validate every argument themselves
  ([references/security.md](security.md)). A skill is guidance, never an
  enforcement boundary.
- Changing any file changes its digest, which **revokes** a user's prior
  approval and re-prompts them. Edit skills deliberately, and version them with
  the tool ([references/versioning.md](versioning.md)).

Digests are unsigned and come from the same server as the bytes — they prove
listing and content agree, nothing more.

## Verify

```sh
python scripts/validate_server.py bin/yourtool.py     # entry shape, limits, capability
start_mcp --app bin/yourtool.py --port 8000
python scripts/check_conformance.py http://localhost:8000
```

The live checker re-hashes every file it reads and compares against the
published digest and size — the same comparison a host makes before it will use
the content, so a manifest that has drifted from the files on disk fails here
rather than silently at a user's machine.
