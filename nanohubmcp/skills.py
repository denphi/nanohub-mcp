"""SEP-2640 Skills: the skill registry, and the SKILL.md frontmatter parser.

A skill is a directory (minimally a ``SKILL.md``) served as individually
addressable ``skill://`` resources. This module owns registering one — walking
it, parsing its frontmatter, and computing the digests and sizes that make up
the manifest a host verifies every later read against — plus the three methods
the extension adds.

Frontmatter is parsed by the hand-rolled YAML subset below rather than PyYAML,
because this library has no dependencies and gains none here. That subset is
the risk the feature carries: the specification requires the published
frontmatter be *identical in content* to the file, so a construct parsed
differently is a silent conformance failure rather than a crash. What it
covers, and what it deliberately does not, is pinned by
tests/test_skill_frontmatter.py.
"""

from __future__ import print_function

import hashlib
import mimetypes
import os
import re
import warnings
from pathlib import Path

from typing import Any, Dict, List

from .types import Skill

# MCP Skills extension (SEP-2640: https://modelcontextprotocol.io/seps/2640).
# Skills are directories (a SKILL.md plus supporting files) served as
# individually addressable resources under skill://<skill-path>/<file-path>.
# This server always implements resources/directory/read once any skill is
# registered, so directoryRead is unconditionally true whenever we declare
# the extension at all.
MCP_SKILLS_EXTENSION_ID = "io.modelcontextprotocol/skills"
SKILL_URI_SCHEME = "skill"
SKILL_MAX_RESOURCES = 512
SKILL_MAX_TOTAL_BYTES = 16 * 1024 * 1024  # 16 MiB, per SEP-2640 Limits

# Python's stdlib `mimetypes` has no `.md` entry, but skill content is
# overwhelmingly Markdown (SKILL.md itself, references/, examples/), and
# SEP-2640 says a skill's SKILL.md resource `mimeType` SHOULD be
# text/markdown. Applied to every skill file by extension, not just
# SKILL.md, so a plain `references/GUIDE.md` gets it too.
_SKILL_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}

def _guess_skill_mime_type(rel_path):
    # type: (str) -> str
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in _SKILL_MIME_OVERRIDES:
        return _SKILL_MIME_OVERRIDES[ext]
    return mimetypes.guess_type(rel_path)[0] or "application/octet-stream"

def _yaml_split_flow(inner):
    # type: (str) -> List[str]
    """Split the inside of a flow collection (`[...]` or `{...}`) on
    top-level commas, respecting quotes and nested brackets."""
    parts = []
    depth = 0
    quote = None
    current = ""
    for ch in inner:
        if quote:
            current += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            current += ch
        elif ch in "[{":
            depth += 1
            current += ch
        elif ch in "]}":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts

# A YAML number, which is narrower than what Python's int()/float() accept.
_YAML_NUMBER_RE = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")


def _yaml_strip_comment(raw):
    # type: (str) -> str
    """Drop a trailing `#` comment from an unquoted scalar.

    YAML starts a comment only at the beginning of a line or after
    whitespace, so `a#b` is the string `a#b` while `a # b` is `a`. A `#`
    inside quotes is literal.
    """
    quote = None
    for index, ch in enumerate(raw):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#" and (index == 0 or raw[index - 1] in " \t"):
            return raw[:index].rstrip()
    return raw

def _yaml_flow_scalar(raw):
    # type: (str) -> Any
    """Parse one YAML scalar, flow list (`[a, b]`), or flow mapping
    (`{a: b}`). No external YAML dependency: this library ships with zero
    dependencies, and SKILL.md frontmatter (per the Agent Skills spec) only
    ever needs this subset — quoted/plain strings, booleans, null, numbers,
    and simple flow collections."""
    raw = raw.strip()
    if not raw:
        return None
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    raw = _yaml_strip_comment(raw)
    if not raw:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw in ("null", "~"):
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_yaml_flow_scalar(part) for part in _yaml_split_flow(inner)]
    if raw.startswith("{") and raw.endswith("}"):
        inner = raw[1:-1].strip()
        if not inner:
            return {}
        mapping = {}
        for part in _yaml_split_flow(inner):
            key, _, value = part.partition(":")
            mapping[key.strip().strip("\"'")] = _yaml_flow_scalar(value)
        return mapping
    # Python accepts digit separators (`1_000`) and the bare words `nan`/`inf`
    # that YAML does not: the first would silently change an author's string
    # into a number, and the second two produce floats that `json.dumps`
    # renders as bare NaN/Infinity — not JSON, and rejected outright by strict
    # parsers on the client side.
    if _YAML_NUMBER_RE.match(raw):
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
    return raw

def _yaml_block_scalar(lines, start, indent, style):
    # type: (List[str], int, int, str) -> Any
    """Parse a `|` (literal) or `>` (folded) block scalar's body: every
    following line indented more than `indent`. Returns `(text, next_index)`.
    """
    i = start
    body = []
    body_indent = None
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            body.append("")
            i += 1
            continue
        cur_indent = len(line) - len(line.lstrip(" "))
        if cur_indent <= indent:
            break
        if body_indent is None:
            body_indent = cur_indent
        body.append(line[body_indent:])
        i += 1
    while body and body[-1] == "":
        body.pop()
    joiner = " " if style.startswith(">") else "\n"
    text = joiner.join(body)
    if not style.endswith("-"):
        text += "\n"
    return text, i

def _yaml_parse_block_with_end(lines, start, indent):
    # type: (List[str], int, int) -> Any
    """Parse a YAML mapping or block list at `indent`, starting at `lines[start]`.

    Returns `(value, next_index)`. Recurses into nested blocks by
    indentation, the same subset used by `_yaml_flow_scalar`'s caller.
    """
    if start < len(lines) and lines[start].strip().startswith("- "):
        i = start
        items = []
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            cur_indent = len(line) - len(line.lstrip(" "))
            if cur_indent < indent or not stripped.startswith("- "):
                break
            items.append(_yaml_flow_scalar(stripped[2:]))
            i += 1
        return items, i

    i = start
    mapping = {}
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        cur_indent = len(line) - len(line.lstrip(" "))
        if cur_indent < indent:
            break
        key, _, rest = stripped.partition(":")
        key = key.strip().strip("\"'")
        rest = rest.strip()
        i += 1
        if rest in ("|", "|-", "|+", ">", ">-", ">+"):
            mapping[key], i = _yaml_block_scalar(lines, i, cur_indent, rest)
            continue
        if rest:
            # A plain scalar continues onto any following line indented more
            # than its key, folded with spaces. Wrapping a long `description:`
            # this way is ordinary in SKILL.md, and reading only the first
            # line both truncates the value and turns each continuation into
            # a bogus top-level key.
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    break
                nxt_indent = len(nxt) - len(nxt.lstrip(" "))
                if nxt_indent <= cur_indent:
                    break
                rest += " " + nxt.strip()
                i += 1
            mapping[key] = _yaml_flow_scalar(rest)
            continue
        j = i
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            mapping[key] = None
            continue
        next_indent = len(lines[j]) - len(lines[j].lstrip(" "))
        # A block sequence may sit at its key's own indent, not only deeper:
        #     allowed-tools:
        #     - Bash
        # is valid YAML and parsed each item as a separate key before.
        if next_indent == cur_indent and lines[j].strip().startswith("- "):
            mapping[key], i = _yaml_parse_block_with_end(lines, j, next_indent)
            continue
        if next_indent <= cur_indent:
            mapping[key] = None
            continue
        mapping[key], i = _yaml_parse_block_with_end(lines, j, next_indent)
    return mapping, i

def _parse_skill_frontmatter(markdown_text):
    # type: (str) -> Dict[str, Any]
    """Parse a SKILL.md file's YAML frontmatter into a dict.

    Raises ValueError if the file has no `---`-delimited frontmatter block,
    per the Agent Skills spec that SEP-2640 defers the skill format to.

    Covers scalars, quoted strings, booleans/null, numbers, flow and block
    lists, flow and block mappings, and `|`/`>` block scalars — everything
    seen in the Agent Skills examples. Not covered: YAML anchors/aliases,
    multi-document streams, and `>` folding's blank-line-becomes-newline
    rule (blank lines fold to a space here rather than a paragraph break).
    A frontmatter field using one of these is not rejected; it is parsed
    into something other than what a full YAML parser would produce, which
    is a real divergence from "verbatim" for those fields specifically.
    """
    text = markdown_text
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(
            "SKILL.md must begin with YAML frontmatter delimited by '---'")
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end is None:
        raise ValueError("SKILL.md frontmatter is not terminated with '---'")
    frontmatter, _ = _yaml_parse_block_with_end(lines[1:end], 0, 0)
    if not isinstance(frontmatter, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")
    return frontmatter


def register_skill_directory(server, skill_path, directory):
    # type: (str, Any) -> None
    """Register a skill (SEP-2640) served from a directory on disk.

    Walks the directory once, parses SKILL.md's frontmatter, and
    computes a SHA-256 digest and size for every file up front, so
    skills/list, skills/get, and resources/directory/read all answer
    from the registry with no further disk access. `resources/read`
    still reads each file's bytes lazily, on demand.
    """
    directory = Path(directory)
    skill_path = skill_path.strip("/")
    if not skill_path:
        raise ValueError("Skill path must not be empty")
    skill_name = skill_path.rsplit("/", 1)[-1]

    skill_md_path = directory / "SKILL.md"
    if not skill_md_path.is_file():
        raise ValueError(
            "Skill '{}' has no SKILL.md at {}".format(skill_path, skill_md_path))

    frontmatter = _parse_skill_frontmatter(
        skill_md_path.read_text(encoding="utf-8"))
    if frontmatter.get("name") != skill_name:
        raise ValueError(
            "Skill '{}': SKILL.md frontmatter name '{}' must equal the "
            "final path segment '{}'".format(
                skill_path, frontmatter.get("name"), skill_name))
    if not frontmatter.get("description"):
        raise ValueError(
            "Skill '{}': SKILL.md frontmatter is missing "
            "'description'".format(skill_path))

    root_uri = "{}://{}".format(SKILL_URI_SCHEME, skill_path)
    skill_md_uri = root_uri + "/SKILL.md"

    files = []  # type: List[tuple]
    subdirs = []  # type: List[str]
    for dirpath, dirnames, filenames in os.walk(str(directory)):
        # Dotfiles are editor state, VCS metadata, and secrets — a skill
        # directory that is a git checkout would otherwise publish
        # `.git/config` and `.env` in its manifest, readable by every
        # connected client. Pruning `dirnames` in place also stops the
        # walk from descending into them.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for dirname in dirnames:
            rel_dir = (Path(dirpath) / dirname).relative_to(directory).as_posix()
            subdirs.append(rel_dir)
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            abs_path = Path(dirpath) / filename
            rel_path = abs_path.relative_to(directory).as_posix()
            files.append((rel_path, abs_path))

    if len(files) > SKILL_MAX_RESOURCES:
        warnings.warn(
            "Skill '{}' has {} files, exceeding the SEP-2640 limit of "
            "{}; some hosts may decline to load it".format(
                skill_path, len(files), SKILL_MAX_RESOURCES))

    resource_entries = []
    skill_files = {}
    total_bytes = 0
    for rel_path, abs_path in files:
        data = abs_path.read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        size = len(data)
        total_bytes += size
        uri = "{}/{}".format(root_uri, rel_path)
        mime_type = _guess_skill_mime_type(rel_path)
        resource_entries.append({"uri": uri, "digest": digest, "size": size})
        skill_files[uri] = {
            "path": abs_path,
            "mimeType": mime_type,
        }

    if total_bytes > SKILL_MAX_TOTAL_BYTES:
        warnings.warn(
            "Skill '{}' totals {} bytes, exceeding the SEP-2640 limit "
            "of {}; some hosts may decline to load it".format(
                skill_path, total_bytes, SKILL_MAX_TOTAL_BYTES))

    resource_entries.sort(key=lambda e: e["uri"])

    # Direct children per directory level, for resources/directory/read.
    # Sorting by name also sorts by uri, since uri is a fixed prefix (the
    # parent directory) plus name.
    directories = {}  # type: Dict[str, Dict[str, Dict[str, Any]]]
    directories[root_uri] = {}
    # Seeded from the walk's directory names, not inferred from file
    # paths: an empty subdirectory has no files to infer it from, and the
    # spec requires the method to answer for *every* directory in the
    # namespace — an empty one with an empty `resources` array.
    for rel_dir in subdirs:
        directories.setdefault(root_uri + "/" + rel_dir, {})
    for rel_path, abs_path in files:
        parts = rel_path.split("/")
        parent_uri = root_uri
        for depth, part in enumerate(parts):
            directories.setdefault(parent_uri, {})
            child_uri = parent_uri + "/" + part
            if depth == len(parts) - 1:
                child = {
                    "uri": child_uri,
                    "name": part,
                    "mimeType": skill_files[child_uri]["mimeType"],
                }
                if child_uri == skill_md_uri:
                    # The SKILL.md resource's name and description SHOULD
                    # come from its frontmatter, so a host can build its
                    # skill registry without fetching the file.
                    child["name"] = frontmatter.get("name") or part
                    description = frontmatter.get("description")
                    if isinstance(description, str) and description:
                        child["description"] = description
                directories[parent_uri][part] = child
            else:
                directories.setdefault(child_uri, {})
                directories[parent_uri][part] = {
                    "uri": child_uri,
                    "name": part,
                    "mimeType": "inode/directory",
                }
            parent_uri = child_uri

    # Organizational prefix segments (`acme`, `acme/billing` for a skill at
    # `acme/billing/refunds`) are directories in the namespace too, even
    # though nothing on disk corresponds to them. A host walking a virtual
    # mount with `ls` reaches the skill through them.
    prefix_dirs = {}  # type: Dict[str, Dict[str, Dict[str, Any]]]
    segments = skill_path.split("/")
    for depth in range(len(segments) - 1):
        parent = "{}://{}".format(SKILL_URI_SCHEME, "/".join(segments[:depth + 1]))
        child_name = segments[depth + 1]
        prefix_dirs.setdefault(parent, {})[child_name] = {
            "uri": parent + "/" + child_name,
            "name": child_name,
            "mimeType": "inode/directory",
        }

    skill = Skill(uri=skill_md_uri, frontmatter=frontmatter,
                   resources=resource_entries)

    with server._registry_lock:
        server._skills[skill_md_uri] = {"definition": skill}
        server._skill_resources.update(skill_files)
        for dir_uri, children in directories.items():
            server._skill_directories[dir_uri] = sorted(
                children.values(), key=lambda c: c["name"])
        # Merged, not replaced: two skills under one prefix (acme/billing/
        # refunds and acme/billing/invoices) are both children of it, and
        # whichever registered second would otherwise hide the first.
        for dir_uri, children in prefix_dirs.items():
            merged = {c["name"]: c for c in server._skill_directories.get(dir_uri, [])}
            merged.update(children)
            server._skill_directories[dir_uri] = sorted(
                merged.values(), key=lambda c: c["name"])


def rpc_skills_list(server, ctx):
    # type: (_RequestContext) -> tuple
    """Handle the JSON-RPC `skills/list` method."""
    params, version = ctx.params, ctx.version
    result = None

    with server._registry_lock:
        skill_dicts = [server._skills[uri]["definition"].to_dict()
                       for uri in sorted(server._skills)]
    result = server._cacheable(
        server._paginate(skill_dicts, params, "skills", {}, "uri"),
        version)
    return result, None


def rpc_skills_get(server, ctx):
    # type: (_RequestContext) -> tuple
    """Handle the JSON-RPC `skills/get` method."""
    params, version = ctx.params, ctx.version
    result = None
    error = None

    skill_uri = params.get("uri")
    if not isinstance(skill_uri, str) or not skill_uri:
        error = {"code": -32602,
                 "message": "skills/get requires a string uri"}
    else:
        with server._registry_lock:
            entry = server._skills.get(skill_uri)
        if entry is None:
            # Same code resources/read uses for an unknown URI,
            # per SEP-2640.
            error = {"code": -32602,
                     "message": "Skill not found: {}".format(skill_uri)}
        else:
            result = server._cacheable(
                {"skill": entry["definition"].to_dict()}, version)
    return result, error


def rpc_resources_directory_read(server, ctx):
    # type: (_RequestContext) -> tuple
    """Handle the JSON-RPC `resources/directory/read` method."""
    params, version = ctx.params, ctx.version
    result = None
    error = None

    dir_uri = params.get("uri")
    if not isinstance(dir_uri, str) or not dir_uri:
        error = {"code": -32602,
                 "message": "resources/directory/read requires a string uri"}
    else:
        with server._registry_lock:
            children = server._skill_directories.get(dir_uri)
            children = list(children) if children is not None else None
        if children is None:
            error = {"code": -32602,
                     "message": "Not a directory resource: {}".format(dir_uri)}
        else:
            result = server._cacheable(
                server._paginate(children, params, "resources", {}, "uri"),
                version)
    return result, error
