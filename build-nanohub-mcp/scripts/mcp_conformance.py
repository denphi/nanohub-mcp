#!/usr/bin/env python3
"""Shared MCP + extension conformance invariants for the nanohub-mcp skill.

One source of truth for the checks that both the offline validator
(`validate_server.py`) and the live conformance driver (`check_conformance.py`)
apply, so a rule can never drift between "pre-deploy" and "post-deploy".

Covers the core protocol plus the extensions nanohub-mcp implements:
  * MCP Apps      — io.modelcontextprotocol/ui     (interactive ui:// HTML apps)
  * MCP Tasks     — io.modelcontextprotocol/tasks  (async long-running tools)
  * MCP Skills    — io.modelcontextprotocol/skills (SEP-2640 skill:// content)
  * Elicitation   — core capability the server *requests* from the client

Everything here is stdlib-only and print_function-safe so it runs in the same
anaconda-7 / py3.8 environment the server runs in.
"""

from __future__ import print_function

import re

# ── Protocol / extension identifiers ────────────────────────────────────────
# Keep these byte-identical to nanohubmcp/server.py; the whole point of the
# checks is to catch a server that has drifted from the spec these name.
MCP_APP_MIME = "text/html;profile=mcp-app"
APPS_EXTENSION_ID = "io.modelcontextprotocol/ui"
TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
SKILLS_EXTENSION_ID = "io.modelcontextprotocol/skills"

# SEP-2640 skills. A skill is a directory of files (minimally SKILL.md) served
# as individually addressable resources; `skills/list` publishes a complete
# manifest (every file's sha256 digest and byte size) that a host verifies
# reads against, so a wrong manifest is not cosmetic — it makes the host
# refuse the file.
SKILL_MD_SUFFIX = "/SKILL.md"
SKILL_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Per-skill limits every conforming host must accept, and past which a server
# SHOULD NOT serve a skill (SEP-2640 "Limits").
SKILL_MAX_RESOURCES = 512
SKILL_MAX_TOTAL_BYTES = 16 * 1024 * 1024  # 16 MiB

# ext-apps (io.modelcontextprotocol/ui) app<->host postMessage handshake.
UI_INITIALIZE = "ui/initialize"
UI_INITIALIZED = "ui/notifications/initialized"
UI_SIZE_CHANGED = "ui/notifications/size-changed"
UI_TEARDOWN = "ui/resource-teardown"
# Current LATEST_PROTOCOL_VERSION in modelcontextprotocol/ext-apps (all releases
# v1.0.0..v1.7.4). Update when the ext-apps spec bumps.
LATEST_UI_PROTOCOL = "2026-01-26"

# Method an elicitation-using handler triggers (server -> client request).
ELICITATION_METHOD = "elicitation/create"

# Base protocol revision that made MCP stateless: per-request `_meta` version
# and capabilities, `server/discover`, `resultType` on every result, cacheable
# list results, and the renumbered error codes.
PROTOCOL_2026_07_28 = "2026-07-28"


def _strip_js_comments(text):
    """Drop `//` and `/* */` comments so windows see code, not prose.

    Without this a comment that documents the rule ("params are appInfo, not
    clientInfo") lands inside a window and is read as the code doing it — a
    false positive — while also pushing the real params past the window edge,
    which hides a real defect. Line comments are only stripped when `//` is not
    preceded by `:` so URLs (`https://`, `ui://`) survive intact.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", " ", text)


def _script_sources(text):
    """Return just the `<script>` bodies, or the whole text if there are none.

    Servers embed their bridge JS inside a Python (or PHP) source file, so the
    surrounding host-language comments are part of the file. Those comments
    routinely *document* the rule being checked ("appInfo, never clientInfo"),
    which lands in a window and reads as the code doing the wrong thing — a
    false positive on a correct app. Restricting to script bodies drops them,
    and drops `<style>` blocks too, where a leading `#` is a selector rather
    than a comment.
    """
    blocks = re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.DOTALL | re.IGNORECASE)
    return "\n".join(blocks) if blocks else text


def _initialize_windows(html, span=600):
    """Yield the text right after each literal `ui/initialize` occurrence.

    Windowing (rather than scanning the whole doc) lets us assert something
    about the *arguments of the initialize call* specifically, while tolerating
    an earlier mention of the string in a comment.
    """
    html = _strip_js_comments(_script_sources(html))
    windows = []
    start = 0
    while True:
        i = html.find(UI_INITIALIZE, start)
        if i < 0:
            break
        windows.append(html[i:i + span])
        start = i + len(UI_INITIALIZE)
    return windows


def check_app_handshake(html):
    """Validate an ext-apps app's HTML for the client-side handshake invariants.

    Returns ``(errors, warnings)`` — lists of strings.

    Why this exists: a spec-strict host (Claude) rejects a malformed
    ``ui/initialize`` and then keeps the iframe permanently ``visibility:hidden``
    — the app renders blank with no error in the chat. The bug that motivated
    the check sent the core-MCP ``capabilities`` + ``clientInfo`` shape instead
    of the ext-apps ``appInfo`` + ``appCapabilities`` shape, so the init promise
    rejected, ``.then()`` never ran, and ``ui/notifications/initialized`` was
    never sent. Lenient hosts (ChatGPT) rendered anyway, hiding the defect.

    A plain "does the HTML contain 'ui/initialize'?" check would have PASSED the
    broken code, so this asserts the *argument shape*, not just presence.
    """
    errors = []
    warnings = []

    if not isinstance(html, str):
        errors.append("app content is not a string; cannot inspect handshake")
        return errors, warnings

    if UI_INITIALIZE not in html:
        errors.append(
            "app never sends '{}': the ext-apps handshake is absent, so hosts "
            "render nothing (mount an app<->host postMessage bridge)".format(UI_INITIALIZE))
        return errors, warnings

    windows = _initialize_windows(html)

    # The ui/initialize params MUST be McpUiInitializeRequest: appInfo +
    # appCapabilities (+ protocolVersion). Satisfied if any occurrence's window
    # carries both keys (handles a mention in a preceding comment).
    if not any(("appInfo" in w and "appCapabilities" in w) for w in windows):
        errors.append(
            "ui/initialize is missing appInfo and/or appCapabilities. "
            "McpUiInitializeRequest (ext-apps {}) requires BOTH; sending the "
            "core-MCP shape makes spec-strict hosts (Claude) reject the "
            "handshake and the app renders blank.".format(LATEST_UI_PROTOCOL))

    # The classic wrong shape: core-MCP clientInfo instead of appInfo.
    if any("clientInfo" in w for w in windows):
        errors.append(
            "ui/initialize sends the core-MCP 'clientInfo' key; ext-apps uses "
            "'appInfo'. Replace clientInfo with appInfo.")

    if not any("protocolVersion" in w for w in windows):
        warnings.append(
            "ui/initialize omits protocolVersion; send \"{}\".".format(LATEST_UI_PROTOCOL))
    elif not any(LATEST_UI_PROTOCOL in w for w in windows):
        warnings.append(
            "ui/initialize protocolVersion is not the current \"{}\"; the host "
            "will negotiate down if it can.".format(LATEST_UI_PROTOCOL))

    if UI_INITIALIZED not in html:
        errors.append(
            "app never sends '{}': hosts hold the iframe hidden until they "
            "receive it. Send it once ui/initialize resolves.".format(UI_INITIALIZED))

    if UI_SIZE_CHANGED not in html:
        warnings.append(
            "app never sends '{}': the host cannot size the iframe to its "
            "content.".format(UI_SIZE_CHANGED))

    if UI_TEARDOWN not in html:
        warnings.append(
            "app does not handle '{}': fine for a stateless app, but a stateful "
            "one should persist on teardown before the host destroys the "
            "iframe.".format(UI_TEARDOWN))

    return errors, warnings


def check_skill_entry(entry):
    """Validate one `skills/list` / `skills/get` entry against SEP-2640.

    Returns ``(errors, warnings)``. The entry is the unit a host verifies
    every later read against, so each rule here has a consequence at the
    host: a malformed manifest means the skill does not load at all, and a
    digest that does not match the bytes served means the file is rejected
    as tampered.
    """
    errors = []
    warnings = []

    if not isinstance(entry, dict):
        return ["skill entry is {}, not an object".format(type(entry).__name__)], []

    uri = entry.get("uri")
    if not isinstance(uri, str) or not uri:
        errors.append("skill entry has no string 'uri'")
        uri = ""
    elif not uri.endswith(SKILL_MD_SUFFIX):
        errors.append(
            "skill uri {!r} must be the skill's SKILL.md — a skill is addressed "
            "by 'skill://<skill-path>/SKILL.md', not by its directory".format(uri))

    frontmatter = entry.get("frontmatter")
    if not isinstance(frontmatter, dict):
        errors.append("skill {!r}: 'frontmatter' must be the SKILL.md YAML "
                      "frontmatter as an object".format(uri))
        frontmatter = {}
    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        errors.append("skill {!r}: frontmatter.name is required".format(uri))
    elif uri.endswith(SKILL_MD_SUFFIX):
        # The final <skill-path> segment MUST equal the frontmatter name, which
        # is what lets a host recover the name from the URI alone.
        final_segment = uri[:-len(SKILL_MD_SUFFIX)].rsplit("/", 1)[-1]
        if final_segment != name:
            errors.append(
                "skill {!r}: frontmatter.name {!r} must equal the final path "
                "segment {!r}".format(uri, name, final_segment))
    if not frontmatter.get("description"):
        errors.append("skill {!r}: frontmatter.description is required — it is "
                      "what the host shows the model to decide relevance".format(uri))

    resources = entry.get("resources")
    if resources == "dynamic":
        warnings.append(
            "skill {!r} declares 'resources: dynamic': it offers no content "
            "integrity and cannot be content-bound, and some hosts decline to "
            "load such skills".format(uri))
        return errors, warnings
    if not isinstance(resources, list):
        errors.append("skill {!r}: 'resources' must be an array of "
                      "{{uri, digest, size}} or the string \"dynamic\"".format(uri))
        return errors, warnings

    total = 0
    seen = set()
    for item in resources:
        if not isinstance(item, dict):
            errors.append("skill {!r}: resources entry is not an object".format(uri))
            continue
        item_uri = item.get("uri")
        if not isinstance(item_uri, str) or not item_uri:
            errors.append("skill {!r}: a resources entry has no 'uri'".format(uri))
        elif item_uri in seen:
            errors.append("skill {!r}: {!r} listed twice; each file appears "
                          "exactly once".format(uri, item_uri))
        else:
            seen.add(item_uri)
        digest = item.get("digest")
        if not isinstance(digest, str) or not SKILL_DIGEST_RE.match(digest):
            errors.append(
                "skill {!r}: {!r} digest {!r} must be 'sha256:' + 64 lowercase "
                "hex chars".format(uri, item_uri, digest))
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            errors.append("skill {!r}: {!r} needs an integer byte 'size'".format(
                uri, item_uri))
        else:
            total += size

    if uri and uri not in seen:
        errors.append(
            "skill {!r}: 'resources' must be complete and include the SKILL.md "
            "itself; a file missing from it is unreadable to a conforming "
            "host".format(uri))
    if len(resources) > SKILL_MAX_RESOURCES:
        warnings.append("skill {!r} has {} files, over the {}-file limit hosts "
                        "are required to accept".format(
                            uri, len(resources), SKILL_MAX_RESOURCES))
    if total > SKILL_MAX_TOTAL_BYTES:
        warnings.append("skill {!r} totals {} bytes, over the {}-byte limit "
                        "hosts are required to accept".format(
                            uri, total, SKILL_MAX_TOTAL_BYTES))
    return errors, warnings


def expected_extensions(has_apps, has_async_tools, has_skills=False):
    """The extension IDs a server with these features MUST advertise at
    ``initialize`` (per nanohubmcp/server.py _get_capabilities)."""
    expected = {}
    if has_apps:
        expected[APPS_EXTENSION_ID] = {"mimeTypes": [MCP_APP_MIME]}
    if has_async_tools:
        expected[TASKS_EXTENSION_ID] = {}
    if has_skills:
        expected[SKILLS_EXTENSION_ID] = {"directoryRead": True}
    return expected
