#!/usr/bin/env python3
"""Shared MCP + extension conformance invariants for the nanohub-mcp skill.

One source of truth for the checks that both the offline validator
(`validate_server.py`) and the live conformance driver (`check_conformance.py`)
apply, so a rule can never drift between "pre-deploy" and "post-deploy".

Covers the core protocol plus the extensions nanohub-mcp implements:
  * MCP Apps      — io.modelcontextprotocol/ui   (interactive ui:// HTML apps)
  * MCP Tasks     — io.modelcontextprotocol/tasks (async long-running tools)
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


def _initialize_windows(html, span=600):
    """Yield the text right after each literal `ui/initialize` occurrence.

    Windowing (rather than scanning the whole doc) lets us assert something
    about the *arguments of the initialize call* specifically, while tolerating
    an earlier mention of the string in a comment.
    """
    html = _strip_js_comments(html)
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


def expected_extensions(has_apps, has_async_tools):
    """The extension IDs a server with these features MUST advertise at
    ``initialize`` (per nanohubmcp/server.py _get_capabilities)."""
    expected = {}
    if has_apps:
        expected[APPS_EXTENSION_ID] = {"mimeTypes": [MCP_APP_MIME]}
    if has_async_tools:
        expected[TASKS_EXTENSION_ID] = {}
    return expected
