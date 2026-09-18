"""
MCP Server implementation with HTTP + SSE transport.
Compatible with Python 3.7+.

Usage:
    server = MCPServer("my-tool")

    @server.tool()
    def add(a, b):
        '''Add two numbers'''
        return a + b

    server.run()
"""

from __future__ import print_function

import base64
import hashlib
import inspect
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import uuid
import warnings
from datetime import datetime
from pathlib import Path

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:
    # Python 2 fallback (not officially supported but helps with syntax)
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn

try:
    from urllib.parse import parse_qs, unquote, urlparse
except ImportError:
    from urlparse import parse_qs, urlparse
    from urllib import unquote

from typing import Any, Callable, Dict, List, Optional, Set

from .types import (
    Tool, Resource, Prompt, Skill, TextContent, ImageContent, InputRequired,
    ToolResult, ResourceResult, ResourceContent,
    PromptResult, Message, Role,
    ServerCapabilities, ServerInfo
)
from .decorators import tool, async_tool, resource, prompt
from .context import Context


# Core MCP revisions only, newest first. Do not add extension revisions here:
# the MCP Apps spec date ("2026-01-26") is the `ui/initialize` protocolVersion,
# a different namespace, and advertising it as a core version names a revision
# that does not exist.
SUPPORTED_PROTOCOL_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2024-11-05"]

# 2026-07-28 removes sessions and the initialize handshake, so a client that
# never declares a version must not be handed the stateless behaviour by
# accident. Handshake-era clients fall back here; stateless clients opt in
# explicitly through `_meta`.
PROTOCOL_2026_07_28 = "2026-07-28"
DEFAULT_NEGOTIATED_VERSION = "2025-11-25"

# `_meta` keys defined by the base protocol (2026-07-28).
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# 2026-07-28 partitions the JSON-RPC server-error range: -32000..-32019 stays
# implementation-defined, -32020..-32099 belongs to the spec. The codes below
# moved, so each is emitted in the numbering the requesting revision expects.
ERROR_CODE_REMAP_2026_07_28 = {
    -32001: -32020,   # HeaderMismatch
    -32003: -32021,   # MissingRequiredClientCapability
    -32004: -32022,   # UnsupportedProtocolVersion
}
ERR_UNSUPPORTED_PROTOCOL_VERSION = -32004   # remapped to -32022 for modern clients

# Freshness hints for CacheableResult (tools/list, prompts/list, resources/*).
CACHEABLE_TTL_MS = 60 * 1000
CACHEABLE_SCOPE = "private"

# How long a partially-answered MRTR request is remembered between retries.
MRTR_STATE_TTL_SECONDS = 10 * 60

# A client picks its own subscription ids (they are its JSON-RPC request ids),
# so the count has to be bounded somewhere. Far above any real use.
MAX_SUBSCRIPTIONS_PER_SESSION = 64


class InvalidParams(Exception):
    """A request the caller got wrong, reported as JSON-RPC -32602.

    Distinct from ValueError on purpose: the server raises ValueError for its
    own programming errors (a reserved `_meta` prefix, an unsupported
    elicitation mode), and those are -32603. Catching ValueError broadly here
    would blame the caller for a server bug.
    """


# MCP Apps extension (https://github.com/modelcontextprotocol/ext-apps).
# Servers advertising this extension can attach UI resources to tools via
# `_meta.ui.resourceUri` and serve `text/html;profile=mcp-app` resources.
MCP_APPS_EXTENSION_ID = "io.modelcontextprotocol/ui"
MCP_APPS_MIME_TYPE = "text/html;profile=mcp-app"

# MCP Tasks extension (https://github.com/modelcontextprotocol/experimental-ext-tasks).
# Async tools can return a task handle to clients that opt into this extension,
# while older clients continue to receive the existing get_job_result flow.
MCP_TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
MCP_TASK_TTL_MS = 60 * 60 * 1000
MCP_TASK_POLL_INTERVAL_MS = 1000

# The Tasks `Task` object is a closed shape with no `_meta`, but
# `CreateTaskResult = Result & Task` and `GetTaskResult = Result & ...`, so the
# *response envelope* carries the base-protocol `_meta`. That is where a tool's
# durable handles ride. Unqualified keys set by tools get this prefix; MCP
# reserves any prefix whose second label is `modelcontextprotocol` or `mcp`.
TASK_META_PREFIX = "org.nanohub/"
MCP_SUBSCRIPTION_ID_KEY = "io.modelcontextprotocol/subscriptionId"
_RESERVED_META_LABELS = ("modelcontextprotocol", "mcp")

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

# Seconds between SSE heartbeats — keeps idle connections alive through proxies
# (nginx, wrwroxy, etc.) that drop connections after ~30-60s of inactivity.
SSE_HEARTBEAT_INTERVAL = 20.0

# Cap on request bodies. Anything larger gets a 413 without being read into
# memory. Generous default for tool payloads but bounds worst-case allocation.
MAX_REQUEST_BYTES = 16 * 1024 * 1024  # 16 MiB


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


class _SSEQueue(list):
    """A list with an attached threading.Event for wake-on-append semantics."""

    def __init__(self):
        list.__init__(self)
        self._wake_event = threading.Event()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server for handling multiple SSE connections."""
    daemon_threads = True


class MCPServer(object):
    """
    Model Context Protocol server for nanoHUB/HubZero tools.

    Usage:
        server = MCPServer("my-tool")

        @server.tool()
        def add(a, b):
            '''Add two numbers'''
            return a + b

        @server.resource("config://settings")
        def get_settings():
            '''Get settings'''
            return {"theme": "dark"}

        @server.prompt()
        def ask(topic):
            '''Ask about a topic'''
            return "Tell me about {}".format(topic)

        server.run()

    Proxy Support:
        When running behind a reverse proxy that rewrites URIs (like weber),

        server = MCPServer("my-tool")

    """

    def __init__(
        self,
        name,  # type: str
        version="1.0.0",  # type: str
        instructions=None,  # type: Optional[str]
        list_page_size=None  # type: Optional[int]
    ):
        # type: (...) -> None
        """
        Initialize an MCP server.

        Args:
            name: Server name
            version: Server version
            instructions: Optional natural-language guidance about the server,
                returned by ``server/discover`` so hosts can prime a model.
                Describe when to reach for this server; don't restate tool
                descriptions.
        """
        self.name = name
        self.version = version
        self.instructions = instructions
        # None means "return everything in one response", which is what every
        # release before 0.4.4 did. Set it and the list endpoints paginate.
        self.list_page_size = list_page_size

        self._tools = {}  # type: Dict[str, Dict[str, Any]]
        self._resources = {}  # type: Dict[str, Dict[str, Any]]
        self._prompts = {}  # type: Dict[str, Dict[str, Any]]
        # SEP-2640 skills. Keyed by the skill's SKILL.md URI.
        self._skills = {}  # type: Dict[str, Dict[str, Any]]
        # Every file of every registered skill, keyed by its own resource URI,
        # for O(1) resources/read lookups without scanning each skill.
        self._skill_resources = {}  # type: Dict[str, Dict[str, Any]]
        # Every directory level of every registered skill (skill root and each
        # subdirectory), keyed by its URI, mapping to its direct children in
        # the shape resources/directory/read returns.
        self._skill_directories = {}  # type: Dict[str, List[Dict[str, Any]]]
        # Guards *mutation* of the three registries above and any iteration
        # over them. Single-key lookups stay lock-free: dict.get is atomic
        # under the GIL, and tools/call is the hot path. Re-entrant because
        # registering an async tool registers get_job_result underneath.
        self._registry_lock = threading.RLock()
        # Flipped the first time something is registered or removed after the
        # server starts serving. Until then listChanged is advertised False,
        # because a static server never sends one.
        self._dynamic_registry = False  # type: bool
        self._serving = False  # type: bool
        self._clients = {}  # type: Dict[str, List[_SSEQueue]]
        self._clients_lock = threading.Lock()
        self._sessions = {}  # type: Dict[str, Dict[str, Any]]
        self._sessions_lock = threading.Lock()
        self._pending_client_requests = {}  # type: Dict[str, Dict[str, Any]]
        self._pending_lock = threading.Lock()
        # session_id -> {subscription_id: {"task_ids": set()}} for
        # subscriptions/listen. Task status notifications are opt-in: the spec
        # forbids sending a notification type the client did not request.
        self._subscriptions = {}  # type: Dict[str, Dict[Any, Dict[str, Any]]]
        self._subs_lock = threading.Lock()
        # Multi Round-Trip Requests: answers accumulated across retries of one
        # logical request, keyed by the opaque requestState handed to the client.
        self._mrtr_states = {}  # type: Dict[str, Dict[str, Any]]
        self._mrtr_lock = threading.Lock()
        # "auto" | True | False, set by run(). "auto" enforces the routing
        # headers only for requests that declare 2026-07-28, the revision that
        # requires them — see run() for why that is the default.
        self._require_route_headers = "auto"  # type: Any
        # None means "no Origin policy configured"; see run(allowed_origins=).
        self._allowed_origins = None  # type: Optional[set]
        self._path_prefix = ""  # type: str
        # job_id -> {"status": "running"|"done"|"error", "result": Any}
        self._jobs = {}  # type: Dict[str, Dict[str, Any]]
        self._jobs_lock = threading.Lock()
        # get_job_result is registered lazily the first time an async tool is
        # registered, so servers without any async tools don't advertise it.
        self._job_polling_registered = False  # type: bool

    def _register_get_job_result(self):
        # type: () -> None
        """Auto-register the built-in get_job_result polling tool."""
        server_instance = self

        def get_job_result(job_id):
            # type: (str) -> Dict[str, Any]
            """Poll the result of a long-running async tool call.

            Returns status 'running' while the job is in progress, or the final
            result/error once it completes. The first successful poll consumes
            the job — subsequent polls return ``not_found`` — so the server
            doesn't accumulate finished-job state for the lifetime of the
            process.

            Args:
                job_id: The job ID returned by an async tool call.
            """
            with server_instance._jobs_lock:
                job = server_instance._jobs.get(job_id)
                if job is None:
                    return {"status": "not_found", "job_id": job_id}
                task_meta = dict(job.get("task_meta") or {})
                if job["status"] == "running":
                    running = {"status": "running", "job_id": job_id}
                    if task_meta:
                        running["meta"] = task_meta
                    return running
                # Terminal state — remove so memory doesn't grow unbounded.
                server_instance._jobs.pop(job_id, None)

            if job["status"] == "cancelled":
                payload = {"status": "cancelled", "job_id": job_id}
            elif job["status"] == "error":
                payload = {"status": "error", "job_id": job_id, "error": job["result"]}
            else:
                payload = {"status": "done", "job_id": job_id, "result": job["result"]}
            if task_meta:
                payload["meta"] = task_meta
            return payload

        decorated = tool(
            name="get_job_result",
            description=(
                "Poll the result of a long-running async tool call. "
                "Pass the job_id returned by an async tool. "
                "Returns {\"status\": \"running\"} until complete, then the final result."
            ),
            input_schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"]
            }
        )(get_job_result)
        self._register_tool_function(decorated)

    @staticmethod
    def _normalize_task_meta(metadata):
        # type: (Dict[str, Any]) -> Dict[str, Any]
        """Namespace and validate task `_meta` keys per the MCP naming rules.

        Bare keys (``jobHandle``) get :data:`TASK_META_PREFIX`; already-qualified
        keys pass through. Prefixes reserved by MCP raise ``ValueError`` rather
        than emitting non-conformant traffic.
        """
        normalized = {}
        for key, value in (metadata or {}).items():
            if not isinstance(key, str) or not key:
                raise ValueError("task metadata keys must be non-empty strings")
            if "/" in key:
                prefix, _, name = key.partition("/")
                labels = prefix.split(".")
                if len(labels) >= 2 and labels[1] in _RESERVED_META_LABELS:
                    raise ValueError(
                        "task metadata key {!r} uses an MCP-reserved prefix".format(key)
                    )
                if not name:
                    raise ValueError("task metadata key {!r} has no name".format(key))
                normalized[key] = value
            else:
                normalized[TASK_META_PREFIX + key] = value
        return normalized

    def _notify_task_status(self, job_id):
        # type: (str) -> None
        """Push `notifications/tasks` to sessions subscribed to this task.

        Complements polling rather than replacing it: the spec says servers MAY
        push status updates *in addition to* servicing `tasks/get`, and clients
        MAY keep polling. Sending is strictly opt-in — only sessions that named
        this task in a `subscriptions/listen` request receive anything.
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            session_id = job.get("session_id")
            task = self._job_to_task(job_id, job)

        if not session_id:
            return

        with self._subs_lock:
            subscriptions = [
                sub_id
                for sub_id, sub in (self._subscriptions.get(session_id) or {}).items()
                if job_id in sub.get("task_ids", ())
            ]

        for sub_id in subscriptions:
            params = dict(task)
            meta = dict(params.get("_meta") or {})
            meta[MCP_SUBSCRIPTION_ID_KEY] = sub_id
            params["_meta"] = meta
            self._broadcast({
                "jsonrpc": "2.0",
                "method": "notifications/tasks",
                "params": params,
            }, session_id=session_id)

    def _drop_subscriptions(self, session_id):
        # type: (str) -> None
        """Forget a session's subscriptions.

        An abrupt transport close carries no `subscriptions/listen` response
        per the spec, so this just releases the state.
        """
        with self._subs_lock:
            self._subscriptions.pop(session_id, None)

    def _fire_cancel_callbacks(self, job_id, callbacks):
        # type: (str, List[Callable]) -> None
        """Run cancel callbacks outside the jobs lock, isolating failures."""
        for callback in callbacks:
            try:
                callback()
            except Exception:
                print("Cancel callback for job {} failed".format(job_id))
                traceback.print_exc()

    def cancel_job(self, job_id):
        # type: (str) -> bool
        """Request cancellation of a running async job.

        Sets the job's cancel event (so handlers polling ``ctx.is_cancelled()``
        stop) and fires any ``ctx.on_cancel`` callbacks (so a supervisor can
        terminate a process group). Returns True if a running job was
        signalled.

        This is the same path ``tasks/cancel`` takes, exposed so servers can
        offer cancellation to legacy clients that lack the Tasks extension —
        register your own tool that calls it, with whatever authorization your
        server requires. Cancellation is cooperative: the job may still reach a
        non-cancelled terminal state.
        """
        callbacks = []
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("status") not in ("running", "input_required"):
                return False
            job["status"] = "cancelled"
            job["lastUpdatedAt"] = self._utc_now()
            event = job.get("cancel_event")
            if event is not None:
                event.set()
            # A worker parked in input_required is waiting on this instead.
            waiting = job.get("input_event")
            if waiting is not None:
                waiting.set()
            callbacks = list(job.get("cancel_callbacks") or [])
        self._fire_cancel_callbacks(job_id, callbacks)
        self._notify_task_status(job_id)
        return True

    def _start_async_tool_job(self, handler, msg_id, arguments, session_id=None,
                              progress_token=None, meta=None, prepare=None,
                              protocol_version=None):
        # type: (Any, Any, Dict[str, Any], Optional[str], Optional[Any], Optional[Dict[str, Any]], Optional[Callable]) -> str
        """Spawn a background thread for an async tool; return a job_id immediately.

        ``prepare`` runs synchronously on the request thread *before* the
        worker starts, so anything it publishes via ``ctx.set_task_metadata()``
        (or returns as a dict) is already on the job record when the initial
        task handle is built. That is the only way for a durable handle to
        reach the caller at dispatch — the handler itself has not run yet. A
        raising ``prepare`` aborts the call and starts no job.
        """
        # Snapshot the arguments dict — the closure runs on a background thread
        # and we don't want later mutations of the caller's dict to leak in.
        arguments = dict(arguments) if arguments else {}

        job_id = str(uuid.uuid4())
        now = self._utc_now()
        with self._jobs_lock:
            self._jobs[job_id] = {
                "status": "running",
                "result": None,
                "createdAt": now,
                "lastUpdatedAt": now,
                "ttlMs": MCP_TASK_TTL_MS,
                "pollIntervalMs": MCP_TASK_POLL_INTERVAL_MS,
                "session_id": session_id,
                "request_id": msg_id,
                "expires_at": time.time() + (MCP_TASK_TTL_MS / 1000.0),
                "cancel_event": threading.Event(),
                "cancel_callbacks": [],
                "task_meta": {},
                # MRTR for tasks: the worker parks here while the client
                # answers, rather than re-running like a sync call does.
                "input_requests": {},
                "input_responses": {},
                "input_event": threading.Event(),
            }

        if prepare is not None:
            try:
                prepared = self._call_handler(
                    prepare, msg_id, arguments,
                    session_id=session_id,
                    progress_token=progress_token,
                    meta=meta,
                    job_id=job_id,
                    protocol_version=protocol_version,
                )
            except Exception:
                # No worker was started, so drop the record rather than leave a
                # phantom "running" task the client would poll forever.
                with self._jobs_lock:
                    self._jobs.pop(job_id, None)
                raise
            if isinstance(prepared, dict) and prepared:
                normalized = self._normalize_task_meta(prepared)
                with self._jobs_lock:
                    job = self._jobs.get(job_id)
                    if job is not None:
                        job.setdefault("task_meta", {}).update(normalized)

        server_instance = self

        def _run():
            try:
                call_result = self._call_handler(
                    handler, msg_id, arguments,
                    session_id=session_id,
                    progress_token=progress_token,
                    meta=meta,
                    job_id=job_id,
                    protocol_version=protocol_version,
                )

                # If the tool signalled failure via ToolResult(isError=True),
                # surface that as a job error rather than a successful result.
                if isinstance(call_result, ToolResult):
                    payload = call_result.to_dict()
                    if payload.get("isError"):
                        items = payload.get("content", [])
                        message = (
                            items[0]["text"]
                            if len(items) == 1 and "text" in items[0]
                            else payload
                        )
                        with server_instance._jobs_lock:
                            if server_instance._jobs[job_id].get("status") == "cancelled":
                                return
                            server_instance._jobs[job_id]["status"] = "error"
                            server_instance._jobs[job_id]["result"] = message
                            server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
                        # Outside the lock: notifying takes other locks.
                        server_instance._notify_task_status(job_id)
                        return
                    result = payload
                elif isinstance(call_result, dict):
                    result = call_result
                else:
                    result = str(call_result)
                with server_instance._jobs_lock:
                    if server_instance._jobs[job_id].get("status") == "cancelled":
                        return
                    server_instance._jobs[job_id]["status"] = "done"
                    server_instance._jobs[job_id]["result"] = result
                    server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
                server_instance._notify_task_status(job_id)
            except Exception as e:
                with server_instance._jobs_lock:
                    if server_instance._jobs[job_id].get("status") == "cancelled":
                        return
                    server_instance._jobs[job_id]["status"] = "error"
                    server_instance._jobs[job_id]["result"] = str(e)
                    server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
                server_instance._notify_task_status(job_id)
                traceback.print_exc()

        t = threading.Thread(target=_run)
        t.daemon = True
        t.start()
        return job_id

    @staticmethod
    def _utc_now():
        # type: () -> str
        """Return an MCP-friendly UTC timestamp."""
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    def _job_to_task(self, task_id, job, include_terminal_payload=True):
        # type: (str, Dict[str, Any], bool) -> Dict[str, Any]
        """Convert an internal async-job record into an MCP Task object."""
        status_map = {
            "running": "working",
            "input_required": "input_required",
            "done": "completed",
            "error": "failed",
            "cancelled": "cancelled",
        }
        status = status_map.get(job.get("status"), "working")
        now = self._utc_now()
        task = {
            "taskId": task_id,
            "status": status,
            "createdAt": job.get("createdAt", now),
            "lastUpdatedAt": job.get("lastUpdatedAt", now),
            "ttlMs": job.get("ttlMs", MCP_TASK_TTL_MS),
            "pollIntervalMs": job.get("pollIntervalMs", MCP_TASK_POLL_INTERVAL_MS),
        }
        task_meta = job.get("task_meta") or {}
        if task_meta:
            # Task itself has no _meta; the Result half of the intersection does.
            task["_meta"] = dict(task_meta)
        if status == "input_required":
            # InputRequiredTask requires the outstanding asks.
            task["inputRequests"] = dict(job.get("input_requests") or {})
            task["statusMessage"] = "Waiting for input from the client."
        elif status == "working":
            task["statusMessage"] = "The operation is in progress."
        elif status == "cancelled":
            task["statusMessage"] = "Cancellation was requested."
        elif include_terminal_payload and status == "completed":
            task["result"] = self._tool_result_payload(job.get("result"))
        elif include_terminal_payload and status == "failed":
            task["error"] = {
                "code": -32603,
                "message": str(job.get("result", "Task failed")),
            }
        return task

    def _tool_result_payload(self, value):
        # type: (Any) -> Dict[str, Any]
        """Wrap a stored async-tool value as a CallToolResult payload."""
        if isinstance(value, ToolResult):
            return value.to_dict()
        if self._is_tool_result_payload(value):
            return value
        if isinstance(value, dict):
            return {
                "content": [{"type": "text", "text": json.dumps(value)}],
                "structuredContent": value,
                "isError": False,
            }
        return {
            "content": [{"type": "text", "text": str(value)}],
            "isError": False,
        }

    @staticmethod
    def _is_tool_result_payload(value):
        # type: (Any) -> bool
        """Return True for dicts that already look like CallToolResult."""
        return (
            isinstance(value, dict)
            and isinstance(value.get("content"), list)
            and "isError" in value
        )

    def _task_access_error(self, task_id, job, session_id):
        # type: (str, Optional[Dict[str, Any]], Optional[str]) -> Optional[Dict[str, Any]]
        """Return a JSON-RPC error when the caller cannot access a task.

        Handshake-era clients are confined to tasks their own session created.
        A stateless (2026-07-28) task has no owning session: the server-minted
        `taskId` is itself the unguessable handle, which is the cross-call
        state mechanism that revision prescribes.
        """
        if job is None:
            return {"code": -32602, "message": "Unknown taskId: {}".format(task_id)}
        owner = job.get("session_id")
        if owner is None:
            return None
        if owner != session_id:
            return {
                "code": -32003,
                "message": "Task is not available in this session",
            }
        return None

    def _handle_cancelled(self, params, session_id):
        # type: (Dict[str, Any], Optional[str]) -> None
        """Act on notifications/cancelled.

        The requestId identifies the call the client is withdrawing. Two of
        ours outlive their response and can still be stopped: an async task
        started by that call, and a subscriptions/listen stream opened by it
        (2026-07-28 uses this notification to close one).
        """
        request_id = params.get("requestId")
        if request_id is None:
            return

        closed = False
        with self._subs_lock:
            session_subs = self._subscriptions.get(session_id) or {}
            if request_id in session_subs:
                session_subs.pop(request_id, None)
                closed = True
                if not session_subs:
                    self._subscriptions.pop(session_id, None)
        if closed:
            return

        # Otherwise treat it as cancelling the task that call created.
        with self._jobs_lock:
            targets = [
                job_id for job_id, job in self._jobs.items()
                if job.get("request_id") == request_id
                and job.get("status") in ("running", "input_required")
            ]
        for job_id in targets:
            self.cancel_job(job_id)

    def origin_allowed(self, origin):
        # type: (Optional[str]) -> bool
        """Whether a browser Origin may talk to this server.

        No ``Origin`` means a non-browser client, which this cannot protect
        and does not try to. No configured allowlist means no policy, so
        everything passes — a library cannot guess a deployment's origins.
        """
        if not origin or self._allowed_origins is None:
            return True
        return origin.rstrip("/").lower() in self._allowed_origins

    def _route_headers_required(self, version):
        # type: (str) -> bool
        """Whether a *missing* routing header should be rejected.

        Default is "auto": required exactly where the spec requires them, i.e.
        for a request that declares 2026-07-28. Earlier revisions never defined
        these headers, so demanding them there would reject conformant clients.
        """
        setting = self._require_route_headers
        if setting == "auto":
            return self._is_stateless(version)
        return bool(setting)

    # Methods whose Mcp-Name header is required, and where its value lives.
    _MCP_NAME_SOURCES = {
        "tools/call": "name",
        "prompts/get": "name",
        "resources/read": "uri",
    }

    # A header value the client could not render as plain ASCII arrives
    # wrapped in this sentinel; servers MUST decode before comparing.
    _B64_PREFIX = "=?base64?"
    _B64_SUFFIX = "?="

    @classmethod
    def _decode_header_value(cls, value):
        # type: (Optional[str]) -> Optional[str]
        """Undo the Base64 sentinel encoding, if present.

        Clients MUST use it for any value that is not plain visible ASCII —
        which includes most resource URIs the moment they carry non-ASCII —
        and for any literal value that would look like the sentinel.
        """
        if not isinstance(value, str):
            return value
        if not (value.startswith(cls._B64_PREFIX) and value.endswith(cls._B64_SUFFIX)):
            return value
        payload = value[len(cls._B64_PREFIX):-len(cls._B64_SUFFIX)]
        try:
            return base64.b64decode(payload, validate=True).decode("utf-8")
        except Exception:
            # Malformed encoding is a header validation failure, not a value.
            return None

    @staticmethod
    def _header_values_match(declared, body_value):
        # type: (Optional[str], Any) -> bool
        """Compare a decoded header against the body value it mirrors.

        Numbers compare numerically, so "42" and 42 agree — the spec calls
        this out because a header is always a string on the wire.
        """
        if declared is None:
            return False
        if isinstance(body_value, bool):
            return declared == ("true" if body_value else "false")
        if isinstance(body_value, (int, float)):
            try:
                return float(declared) == float(body_value)
            except (TypeError, ValueError):
                return False
        return declared == body_value

    def _header_mismatch(self, request, headers, version=None):
        # type: (Any, Any, Optional[str]) -> Optional[Dict[str, Any]]
        """Validate the mirrored HTTP headers against the request body.

        The transport mirrors selected body fields into headers so a gateway
        can route and authorize without parsing the body. That only works if
        the two agree — a load balancer acting on the header while the server
        acts on the body is the vulnerability this check exists to close. So a
        contradicting header is always rejected, whatever the revision.

        A *missing* header is rejected only where the declaring revision
        requires one — see :meth:`_route_headers_required`. ``run()`` can force
        that on for every revision, or off entirely, for a deployment whose
        gateway cannot be relied on to forward them.
        """
        if not isinstance(request, dict):
            return None

        def header(name):
            # RFC 9110: field names are case-insensitive, and both sides MUST
            # compare them that way. http.client's message object already does;
            # a plain dict (tests, other transports) does not.
            getter = getattr(headers, "get", None) if headers else None
            if getter is None:
                return None
            found = getter(name)
            if found is not None:
                return found
            wanted = name.lower()
            try:
                items = headers.items()
            except AttributeError:
                return None
            for key, value in items:
                if isinstance(key, str) and key.lower() == wanted:
                    return value
            return None

        def bad(message):
            return {"code": -32020, "message": message}

        # Only an HTTP POST carries a header collection. A direct in-process
        # call, the SSE channel, and stdio have nowhere to put these, so a
        # missing header there means "not applicable", not "omitted".
        transport_has_headers = getattr(headers, "get", None) is not None
        if not transport_has_headers:
            return None

        method = request.get("method")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        name_field = self._MCP_NAME_SOURCES.get(method)
        body_name = params.get(name_field) if name_field else None

        required = self._route_headers_required(version or DEFAULT_NEGOTIATED_VERSION)

        # ── MCP-Protocol-Version must agree with the body's _meta ───────────
        # Compared against the *declared* value only. `version` falls back to
        # the session's negotiation and then to a default, and a 2025-06-18
        # client — whose revision introduced this header but not `_meta` —
        # would otherwise be rejected whenever the gateway dropped its
        # session id, which is a known failure mode rather than a rare one.
        declared_version = header("MCP-Protocol-Version")
        body_meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        body_version = body_meta.get(META_PROTOCOL_VERSION)
        if (declared_version is not None and isinstance(body_version, str)
                and body_version and declared_version != body_version):
            return bad(
                "MCP-Protocol-Version header {!r} does not match body version "
                "{!r}".format(declared_version, body_version))

        # ── Mcp-Method ─────────────────────────────────────────────────────
        declared_method = header("Mcp-Method")
        if declared_method is None:
            if required and method:
                return bad("Missing required Mcp-Method header")
        elif declared_method != method:
            return bad("Mcp-Method header {!r} does not match body method {!r}".format(
                declared_method, method))

        # ── Mcp-Name: params.name for tools/prompts, params.uri for resources ─
        declared_name = header("Mcp-Name")
        if declared_name is None:
            if required and body_name is not None:
                return bad("Missing required Mcp-Name header")
        elif body_name is not None:
            decoded = self._decode_header_value(declared_name)
            if decoded is None:
                return bad("Mcp-Name header is not valid Base64 sentinel encoding")
            if decoded != body_name:
                return bad("Mcp-Name header {!r} does not match body {} {!r}".format(
                    decoded, name_field, body_name))

        # ── Mcp-Param-{Name}: arguments mirrored via x-mcp-header ──────────
        if method == "tools/call":
            mirrored = self._mirrored_params(params.get("name"))
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            for header_name, path in mirrored.items():
                present, body_value = self._value_at_path(arguments, path)
                declared = header("Mcp-Param-" + header_name)
                if not present:
                    # The client MUST omit the header when the value is absent.
                    if declared is not None:
                        return bad("Mcp-Param-{} sent but {} is absent from "
                                   "arguments".format(header_name, ".".join(path)))
                    continue
                if declared is None:
                    if required:
                        return bad("Missing required Mcp-Param-{} header".format(
                            header_name))
                    continue
                decoded = self._decode_header_value(declared)
                if decoded is None:
                    return bad("Mcp-Param-{} is not valid Base64 sentinel "
                               "encoding".format(header_name))
                if not self._header_values_match(decoded, body_value):
                    return bad("Mcp-Param-{} header {!r} does not match argument "
                               "{!r}".format(header_name, decoded, body_value))

        return None

    # RFC 9110 tchar: the characters an HTTP field name may contain.
    _TCHAR = set("!#$%&'*+-.^_`|~0123456789"
                 "abcdefghijklmnopqrstuvwxyz"
                 "ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    # x-mcp-header may only mirror a primitive. `number` is excluded by the
    # spec — a float has no single canonical header representation.
    _MIRRORABLE_TYPES = ("string", "integer", "boolean")

    @classmethod
    def _validate_mirrored_params(cls, schema, tool_name):
        # type: (Any, str) -> Dict[str, List[str]]
        """Check a tool's x-mcp-header annotations, or raise ValueError.

        This is validated at registration because the failure is otherwise
        silent and total: a conforming client MUST exclude a tool with an
        invalid annotation from `tools/list`, so the tool simply vanishes for
        the user with nothing logged anywhere.
        """
        if not isinstance(schema, dict):
            return {}

        reachable = {}   # header name (lowercased) -> property path
        seen_at = {}     # header name -> the path that claimed it first

        def collect(node, path):
            properties = node.get("properties")
            if not isinstance(properties, dict):
                return
            for key, prop in properties.items():
                if not isinstance(prop, dict):
                    continue
                here = path + [key]
                annotation = prop.get("x-mcp-header")
                if annotation is not None:
                    cls._check_mirror_annotation(
                        annotation, prop, here, tool_name, seen_at)
                    reachable[annotation.lower()] = here
                collect(prop, here)

        collect(schema, [])

        # An annotation anywhere unreachable — behind items, a composition or
        # conditional keyword, or a $ref — invalidates the whole definition.
        stray = cls._stray_mirror_annotations(schema, reachable)
        if stray:
            raise ValueError(
                "Tool {!r}: x-mcp-header on {} is not statically reachable; the "
                "path must be a chain of 'properties' keys only".format(
                    tool_name, ", ".join(sorted(stray))))
        return reachable

    @classmethod
    def _check_mirror_annotation(cls, annotation, prop, path, tool_name, seen_at):
        # type: (Any, Dict[str, Any], List[str], str, Dict[str, List[str]]) -> None
        """Validate one x-mcp-header value against the spec's constraints."""
        where = ".".join(path)
        if not isinstance(annotation, str) or not annotation:
            raise ValueError("Tool {!r}: x-mcp-header on {!r} must be a "
                             "non-empty string".format(tool_name, where))
        if any(ch not in cls._TCHAR for ch in annotation):
            raise ValueError(
                "Tool {!r}: x-mcp-header {!r} on {!r} is not a valid HTTP "
                "field-name token".format(tool_name, annotation, where))

        lowered = annotation.lower()
        if lowered in seen_at:
            raise ValueError(
                "Tool {!r}: x-mcp-header {!r} on {!r} duplicates the one on "
                "{!r} (names are case-insensitive)".format(
                    tool_name, annotation, where, ".".join(seen_at[lowered])))
        seen_at[lowered] = path

        prop_type = prop.get("type")
        if prop_type not in cls._MIRRORABLE_TYPES:
            raise ValueError(
                "Tool {!r}: x-mcp-header on {!r} needs type one of {}; got "
                "{!r}".format(tool_name, where,
                              ", ".join(cls._MIRRORABLE_TYPES), prop_type))

    @staticmethod
    def _stray_mirror_annotations(schema, reachable):
        # type: (Dict[str, Any], Dict[str, List[str]]) -> set
        """Find x-mcp-header annotations outside the reachable property tree.

        Reachable means: every step from the schema root was a `properties`
        key. An annotation under `items`, `oneOf`/`anyOf`/`allOf`/`not`,
        `if`/`then`/`else`, or `$ref` is invalid, and invalidates the tool.
        """
        allowed = set(reachable)
        stray = set()

        def walk(node, reachable_here):
            if isinstance(node, list):
                for item in node:
                    walk(item, False)
                return
            if not isinstance(node, dict):
                return

            annotation = node.get("x-mcp-header")
            if isinstance(annotation, str) and annotation:
                if not reachable_here or annotation.lower() not in allowed:
                    stray.add(annotation)

            for key, value in node.items():
                if key == "x-mcp-header":
                    continue
                if key == "properties" and isinstance(value, dict):
                    # Each property is one more reachable step.
                    for child in value.values():
                        walk(child, reachable_here)
                else:
                    walk(value, False)

        walk(schema, True)
        return stray

    def _mirrored_params(self, tool_name):
        # type: (Optional[str]) -> Dict[str, List[str]]
        """Header name (lowercased) -> property path, for one tool.

        Validated and computed when the tool was registered, so a `tools/call`
        does not re-walk the schema.
        """
        if not tool_name:
            return {}
        entry = self._tools.get(tool_name)
        if not entry:
            return {}
        return entry.get("mirrored_params") or {}

    @staticmethod
    def _value_at_path(arguments, path):
        # type: (Dict[str, Any], List[str]) -> tuple
        """Read the instance value at an exact property path."""
        node = arguments
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return False, None
            node = node[key]
        # A null value is treated as absent: the client MUST omit the header.
        return (node is not None), node

    def _task_await_input(self, job_id, requests, timeout):
        # type: (str, Dict[str, Any], float) -> Dict[str, Any]
        """Park an async worker in `input_required` until the client answers.

        Unlike a sync call — which the client re-drives from the top — the
        worker thread is still alive and holding its state, so it waits here
        and resumes in place. Nothing needs to be idempotent.
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise RuntimeError("Task {} no longer exists".format(job_id))
            if job.get("status") != "running":
                raise RuntimeError("Task {} is not running".format(job_id))
            job["input_requests"] = dict(requests)
            job["status"] = "input_required"
            job["lastUpdatedAt"] = self._utc_now()
            event = job.get("input_event")
            if event is None:
                event = threading.Event()
                job["input_event"] = event
            event.clear()
            cancel_event = job.get("cancel_event")

        # Tell subscribers the task now needs something, so a client that
        # pushes rather than polls still learns about the ask.
        self._notify_task_status(job_id)

        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Task {} was cancelled while awaiting input".format(job_id))
            if event.wait(0.1):
                break
        else:
            with self._jobs_lock:
                job = self._jobs.get(job_id)
                if job is not None and job.get("status") == "input_required":
                    job["status"] = "running"
                    job["input_requests"] = {}
                    job["lastUpdatedAt"] = self._utc_now()
            raise RuntimeError("Timed out waiting for client input on task {}".format(job_id))

        # Cancellation wakes this same event, so re-check it before reading:
        # otherwise a cancelled task reports "no response supplied", and a
        # handler failing closed on RuntimeError logs the wrong cause.
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError(
                "Task {} was cancelled while awaiting input".format(job_id))

        with self._jobs_lock:
            job = self._jobs.get(job_id) or {}
            answers = dict(job.get("input_responses") or {})
        return answers

    def _task_deliver_input(self, job_id, responses):
        # type: (str, Dict[str, Any]) -> Optional[Dict[str, Any]]
        """Hand tasks/update answers to a parked worker and wake it."""
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"code": -32602, "message": "Unknown taskId: {}".format(job_id)}
            if job.get("status") != "input_required":
                return {
                    "code": -32602,
                    "message": "Task {} is not awaiting input".format(job_id),
                }
            outstanding = set(job.get("input_requests") or {})
            unknown = [k for k in responses if k not in outstanding]
            if unknown:
                return {
                    "code": -32602,
                    "message": "inputResponses keys not outstanding: {}".format(
                        ", ".join(sorted(unknown))),
                }
            job.setdefault("input_responses", {}).update(responses)
            job["input_requests"] = {}
            job["status"] = "running"
            job["lastUpdatedAt"] = self._utc_now()
            event = job.get("input_event")
        if event is not None:
            event.set()
        self._notify_task_status(job_id)
        return None

    def _mrtr_load(self, request_state, session_id=None):
        # type: (Optional[str], Optional[str]) -> Dict[str, Any]
        """Answers already collected for this logical request.

        Bound to the session that created the state, exactly as a task is.
        Without that, one session presenting another's `requestState` inherits
        answers a different user gave — including an approval it never asked
        for. Returning empty rather than erroring means the worst case is being
        asked again, which is the safe direction.
        """
        if not request_state:
            return {}
        with self._mrtr_lock:
            entry = self._mrtr_states.get(request_state)
            if entry is None or entry.get("expires_at", 0) <= time.time():
                self._mrtr_states.pop(request_state, None)
                return {}
            if entry.get("session_id") != session_id:
                return {}
            return dict(entry.get("responses") or {})

    def _mrtr_save(self, request_state, responses, session_id=None):
        # type: (Optional[str], Dict[str, Any], Optional[str]) -> str
        """Persist accumulated answers; return the state id to hand the client.

        A multi-step handler asks once per round trip, so answers must survive
        between retries even though the client may only resend the newest one.
        """
        state_id = request_state or ("mrtr_" + uuid.uuid4().hex)
        with self._mrtr_lock:
            existing = self._mrtr_states.get(state_id)
            if existing is not None and existing.get("session_id") != session_id:
                # Someone else's state. Never rebind it — overwriting the owner
                # would let any session permanently break another's in-flight
                # request just by naming its id. Start a fresh one instead.
                state_id = "mrtr_" + uuid.uuid4().hex
            self._mrtr_states[state_id] = {
                "responses": dict(responses or {}),
                "session_id": session_id,
                "expires_at": time.time() + MRTR_STATE_TTL_SECONDS,
            }
        return state_id

    def _mrtr_discard(self, request_state):
        # type: (Optional[str]) -> None
        """Drop state once the request has finally completed or failed."""
        if not request_state:
            return
        with self._mrtr_lock:
            self._mrtr_states.pop(request_state, None)

    def _prune_expired_mrtr_states(self):
        # type: () -> None
        """Expire abandoned round-trips so memory can't grow unbounded."""
        now = time.time()
        with self._mrtr_lock:
            for state_id in [
                k for k, v in self._mrtr_states.items()
                if v.get("expires_at", 0) <= now
            ]:
                self._mrtr_states.pop(state_id, None)

    def _prune_expired_jobs(self):
        # type: () -> None
        """Remove task/job records whose retention window has elapsed."""
        now = time.time()
        with self._jobs_lock:
            expired = [
                job_id for job_id, job in self._jobs.items()
                if job.get("expires_at") is not None and job.get("expires_at") <= now
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)

    def _strip_proxy_prefix(self, uri):
        # type: (str) -> str
        """
        Normalize proxied resource URIs to registered resource keys.

        Some proxy/client stacks may pass a full proxied URL/path instead of the
        raw MCP resource URI. This method attempts a safe normalization by
        matching known resource URIs.
        """
        if not uri or not isinstance(uri, str):
            return uri

        # Exact-match fast path.
        if uri in self._resources:
            return uri

        # Try common normalizations before suffix matching.
        candidates = [uri.strip()]
        if self._path_prefix:
            prefix = self._path_prefix.rstrip("/")
            if prefix and candidates[0].startswith(prefix):
                stripped = candidates[0][len(prefix):]
                candidates.append(stripped if stripped.startswith("/") else "/" + stripped)

        decoded = unquote(candidates[0])
        if decoded not in candidates:
            candidates.append(decoded)

        # Remove query/fragment and leading slashes variants.
        normalized_candidates = []
        for candidate in candidates:
            base = candidate.split("?", 1)[0].split("#", 1)[0]
            for value in (candidate, base, base.lstrip("/")):
                if value and value not in normalized_candidates:
                    normalized_candidates.append(value)

        # Exact match after normalization.
        for candidate in normalized_candidates:
            if candidate in self._resources:
                return candidate

        # Fallback: match by registered URI suffix (prefer longest match).
        resource_uris = sorted(self._resources.keys(), key=len, reverse=True)
        for candidate in normalized_candidates:
            for resource_uri in resource_uris:
                if candidate.endswith(resource_uri):
                    return resource_uri
                if candidate.endswith("/" + resource_uri):
                    return resource_uri

        return uri

    def _register_tool_function(self, func):
        # type: (Callable) -> None
        """Register a decorated tool function.

        Usable at any time. Called at import (the usual case) it just fills the
        registry; called while the server is serving it also emits
        `notifications/tools/list_changed`.
        """
        name = func._mcp_tool_name
        is_async = getattr(func, "_mcp_async_tool", False)
        # Raises before the tool is registered, so a definition a conforming
        # client would silently drop never reaches tools/list.
        mirrored = self._validate_mirrored_params(
            func._mcp_tool_input_schema, name)
        with self._registry_lock:
            self._tools[name] = {
                "definition": Tool(
                    name=name,
                    description=func._mcp_tool_description,
                    inputSchema=func._mcp_tool_input_schema,
                    meta=getattr(func, "_mcp_tool_meta", None) or {},
                    outputSchema=getattr(func, "_mcp_tool_output_schema", None),
                    annotations=getattr(func, "_mcp_tool_annotations", None),
                ),
                "handler": func,
                "is_async": is_async,
                "prepare": getattr(func, "_mcp_async_prepare", None),
                # Computed once here rather than re-walking the schema on
                # every tools/call that carries headers.
                "mirrored_params": mirrored,
            }
        self._mark_dynamic("tools")
        if is_async:
            # Lazily install get_job_result so servers without async tools
            # don't advertise it in tools/list. Guarded so concurrent decorator
            # evaluations can't both call _register_get_job_result.
            with self._jobs_lock:
                already_registered = self._job_polling_registered
                self._job_polling_registered = True
            if not already_registered:
                self._register_get_job_result()

    def _register_resource_function(self, func):
        # type: (Callable) -> None
        """Register a decorated resource function, at import or while serving."""
        uri = func._mcp_resource_uri
        with self._registry_lock:
            replaced = uri in self._resources
            self._resources[uri] = {
                "definition": Resource(
                    uri=uri,
                    name=func._mcp_resource_name,
                    description=func._mcp_resource_description,
                    mimeType=func._mcp_resource_mime_type,
                    meta=getattr(func, "_mcp_resource_meta", None) or {},
                ),
                "handler": func
            }
        self._mark_dynamic("resources")
        # Only a *replacement* is an update; a first registration is a
        # creation, which list_changed already reports.
        if replaced:
            self.resource_updated(uri)

    def _register_prompt_function(self, func):
        # type: (Callable) -> None
        """Register a decorated prompt function, at import or while serving."""
        name = func._mcp_prompt_name
        with self._registry_lock:
            self._prompts[name] = {
                "definition": Prompt(
                    name=name,
                    description=func._mcp_prompt_description,
                    arguments=func._mcp_prompt_arguments
                ),
                "handler": func
            }
        self._mark_dynamic("prompts")

    def _register_skill_directory(self, skill_path, directory):
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

        with self._registry_lock:
            self._skills[skill_md_uri] = {"definition": skill}
            self._skill_resources.update(skill_files)
            for dir_uri, children in directories.items():
                self._skill_directories[dir_uri] = sorted(
                    children.values(), key=lambda c: c["name"])
            # Merged, not replaced: two skills under one prefix (acme/billing/
            # refunds and acme/billing/invoices) are both children of it, and
            # whichever registered second would otherwise hide the first.
            for dir_uri, children in prefix_dirs.items():
                merged = {c["name"]: c for c in self._skill_directories.get(dir_uri, [])}
                merged.update(children)
                self._skill_directories[dir_uri] = sorted(
                    merged.values(), key=lambda c: c["name"])
        # Deliberately no `_mark_dynamic`: the extension defines no
        # skills/list_changed notification, so there is nothing to send, and
        # flipping the dynamic-registry flag would make tools, resources and
        # prompts start advertising listChanged over a registry that did not
        # change.

    def skill(self, skill_path):
        # type: (str) -> Callable
        """
        Decorator to register a skill (SEP-2640 Skills Extension), served
        from a directory containing a SKILL.md.

        The decorated function is called once, at registration, and must
        return the skill's directory (a path or pathlib.Path):

            @server.skill("git-workflow")        # -> skill://git-workflow/SKILL.md
            def git_workflow():
                return Path(__file__).parent / "skills" / "git-workflow"

            @server.skill("acme/billing/refunds")  # -> skill://acme/billing/refunds/SKILL.md
            def refunds():
                return "./skills/refunds"

        Every file in the directory is served as an MCP resource under
        skill://<skill_path>/<relative-file-path>, readable via the
        standard resources/read. A SHA-256 digest and size are computed for
        each file up front so skills/list and skills/get can answer from
        the registry alone.

        Args:
            skill_path: The skill's path within this server's skill
                namespace (e.g. "git-workflow" or "acme/billing/refunds").
                Its final segment must equal the `name` field of the
                skill's SKILL.md frontmatter.
        """
        def decorator(func):
            # type: (Callable) -> Callable
            directory = func()
            self._register_skill_directory(skill_path, directory)
            return func
        return decorator

    def tool(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None,  # type: Optional[Dict[str, Any]]
        input_schema=None,  # type: Optional[Dict[str, Any]]
        output_schema=None,  # type: Optional[Dict[str, Any]]
        annotations=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a tool on this server.
        Aligned with FastMCP @mcp.tool decorator.

        Args:
            name: Tool name (defaults to function name)
            description: Tool description (defaults to docstring)
            tags: Optional set of tags for categorization
            meta: Optional metadata dictionary
            input_schema: JSON Schema for inputs (auto-generated if not provided)
            output_schema: JSON Schema describing the dict the tool returns.
                Emitted as `outputSchema` in tools/list; dict results always
                carry `structuredContent` per the MCP spec.
            annotations: MCP ToolAnnotations hints emitted in tools/list —
                readOnlyHint / destructiveHint / idempotentHint /
                openWorldHint (bool) and title (str). Unknown keys or wrong
                types raise ValueError at decoration time. Hints only: mark
                pure reads readOnlyHint=True so clients may cache results and
                allow the tool from embedded app widgets in strict mode.
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = tool(name, description, tags, meta, input_schema, output_schema,
                             annotations=annotations)(func)
            self._register_tool_function(decorated)
            return decorated

        if callable(name):
            func = name
            name = None
            return decorator(func)

        return decorator

    def async_tool(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None,  # type: Optional[Dict[str, Any]]
        input_schema=None,  # type: Optional[Dict[str, Any]]
        output_schema=None,  # type: Optional[Dict[str, Any]]
        annotations=None,  # type: Optional[Dict[str, Any]]
        prepare=None  # type: Optional[Callable]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a long-running tool that returns a job_id immediately.

        The server runs the function in a background thread and the client polls
        for the result using the built-in ``get_job_result`` tool.

        Usage::

            @server.async_tool()
            def run_openlane(verilog_code: str, design_name: str) -> str:
                # takes minutes — won't block the HTTP response
                ...
                return result
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = async_tool(name, description, tags, meta, input_schema,
                                   output_schema=output_schema, annotations=annotations,
                                   prepare=prepare)(func)
            self._register_tool_function(decorated)
            return decorated

        if callable(name):
            func = name
            name = None
            return decorator(func)

        return decorator

    def resource(
        self,
        uri,  # type: str
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        mime_type=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a resource on this server.
        Aligned with FastMCP @mcp.resource decorator.

        Args:
            uri: Resource URI (e.g., "file:///path" or "config://settings")
            name: Resource name (defaults to function name)
            description: Resource description (defaults to docstring)
            mime_type: MIME type of the resource content
            tags: Optional set of tags for categorization
            meta: Optional metadata dictionary
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = resource(uri, name, description, mime_type, tags, meta)(func)
            self._register_resource_function(decorated)
            return decorated
        return decorator

    def prompt(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None  # type: Optional[Dict[str, Any]]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a prompt on this server.
        Aligned with FastMCP @mcp.prompt decorator.

        Args:
            name: Prompt name (defaults to function name)
            description: Prompt description (defaults to docstring)
            tags: Optional set of tags for categorization
            meta: Optional metadata dictionary
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = prompt(name, description, tags, meta)(func)
            self._register_prompt_function(decorated)
            return decorated

        if callable(name):
            func = name
            name = None
            return decorator(func)

        return decorator

    # ── Dynamic registration ─────────────────────────────────────────────
    # Tools, resources, and prompts may be registered or removed after the
    # server is running. The decorators work unchanged at import time; using
    # them (or the remove_* methods) later additionally notifies clients.

    _LIST_CHANGED_METHODS = {
        "tools": "notifications/tools/list_changed",
        "resources": "notifications/resources/list_changed",
        "prompts": "notifications/prompts/list_changed",
    }
    _LIST_CHANGED_FILTERS = {
        "tools": "toolsListChanged",
        "resources": "resourcesListChanged",
        "prompts": "promptsListChanged",
    }

    def _notify_list_changed(self, kind):
        # type: (str) -> None
        """Tell clients a registry changed, by whichever route each expects.

        2026-07-28 made these notifications opt-in: a server MUST NOT send a
        type the client did not request through `subscriptions/listen`. Earlier
        revisions have no such request — the server simply advertises
        `listChanged` and sends. Both are honoured here, so one registration
        reaches old and new clients alike.
        """
        method = self._LIST_CHANGED_METHODS.get(kind)
        if method is None or not self._serving:
            return
        filter_name = self._LIST_CHANGED_FILTERS[kind]

        with self._subs_lock:
            subscribed = {
                session_id: [
                    sub_id for sub_id, sub in (subs or {}).items()
                    if (sub.get("filters") or {}).get(filter_name)
                ]
                for session_id, subs in self._subscriptions.items()
            }
        with self._clients_lock:
            sessions = list(self._clients.keys())

        for session_id in sessions:
            subs = subscribed.get(session_id) or []
            if subs:
                for sub_id in subs:
                    self._broadcast({
                        "jsonrpc": "2.0", "method": method,
                        "params": {"_meta": {MCP_SUBSCRIPTION_ID_KEY: sub_id}},
                    }, session_id=session_id)
            elif not self._session_is_stateless(session_id):
                # Handshake-era session: no subscription exists to opt in with.
                self._broadcast({"jsonrpc": "2.0", "method": method, "params": {}},
                                session_id=session_id)

    def _session_is_stateless(self, session_id):
        # type: (Optional[str]) -> bool
        """True when this session negotiated a revision that requires opt-in."""
        with self._sessions_lock:
            session = self._sessions.get(session_id) or {}
        return self._is_stateless(session.get("protocol_version") or "")

    def _mark_dynamic(self, kind):
        # type: (str) -> None
        """Record that the registry can change, then announce this change."""
        if self._serving and not self._dynamic_registry:
            self._dynamic_registry = True
        self._notify_list_changed(kind)

    def resource_updated(self, uri):
        # type: (str) -> int
        """Tell subscribers a resource's content changed; returns how many.

        The counterpart to `subscriptions/listen` with `resourceSubscriptions`.
        Nothing infers this — a resource handler is just a function, and the
        server cannot know when whatever it reads from has moved underneath.
        Call this when you know it has.
        """
        sent = 0
        with self._subs_lock:
            targets = [
                (session_id, sub_id)
                for session_id, subs in self._subscriptions.items()
                for sub_id, sub in (subs or {}).items()
                if uri in (sub.get("resource_uris") or ())
            ]
        for session_id, sub_id in targets:
            self._broadcast({
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": uri,
                           "_meta": {MCP_SUBSCRIPTION_ID_KEY: sub_id}},
            }, session_id=session_id)
            sent += 1
        return sent

    def remove_tool(self, name):
        # type: (str) -> bool
        """Unregister a tool by name. Returns True if one was removed.

        Safe while the server is serving: an in-flight call already holds its
        handler, so removal affects only subsequent lookups.
        """
        with self._registry_lock:
            removed = self._tools.pop(name, None) is not None
        if removed:
            self._mark_dynamic("tools")
        return removed

    def remove_resource(self, uri):
        # type: (str) -> bool
        """Unregister a resource by URI. Returns True if one was removed."""
        with self._registry_lock:
            removed = self._resources.pop(uri, None) is not None
        if removed:
            self._mark_dynamic("resources")
        return removed

    def remove_prompt(self, name):
        # type: (str) -> bool
        """Unregister a prompt by name. Returns True if one was removed."""
        with self._registry_lock:
            removed = self._prompts.pop(name, None) is not None
        if removed:
            self._mark_dynamic("prompts")
        return removed

    def _get_capabilities(self):
        # type: () -> ServerCapabilities
        """Get server capabilities based on registered handlers."""
        extensions = {}
        if self._has_mcp_app_resources():
            # Advertise mcp-apps support so capable hosts know they can render
            # the UI resources attached to our tools.
            extensions[MCP_APPS_EXTENSION_ID] = {
                "mimeTypes": [MCP_APPS_MIME_TYPE]
            }
        if self._has_async_tools():
            extensions[MCP_TASKS_EXTENSION_ID] = {}
        if self._skills:
            # This server always implements resources/directory/read for its
            # skill namespaces once any skill is registered.
            extensions[MCP_SKILLS_EXTENSION_ID] = {"directoryRead": True}
        return ServerCapabilities(
            tools=len(self._tools) > 0,
            # A server declaring the skills extension MUST also declare the
            # base resources capability (SEP-2640), even if no plain
            # @server.resource() was ever registered.
            resources=len(self._resources) > 0 or len(self._skills) > 0,
            prompts=len(self._prompts) > 0,
            logging=True,
            extensions=extensions,
            list_changed=self._dynamic_registry,
            subscribe=len(self._resources) > 0,
        )

    def _has_mcp_app_resources(self):
        # type: () -> bool
        """True if any registered resource is an MCP App (ui:// HTML template)."""
        with self._registry_lock:
            entries = list(self._resources.values())
        for entry in entries:
            definition = entry["definition"]
            mime = getattr(definition, "mimeType", None) or ""
            if "profile=mcp-app" in mime:
                return True
            if getattr(definition, "uri", "").startswith("ui://"):
                return True
        return False

    def _has_async_tools(self):
        # type: () -> bool
        """True if the server has any long-running async tools."""
        with self._registry_lock:
            return any(t.get("is_async") for t in list(self._tools.values()))

    def _context_param_name(self, func):
        # type: (Callable) -> Optional[str]
        """Return the context parameter name on `func`, or None.

        Single source of truth used by both _needs_context and _call_handler so
        we don't pay for inspect.signature twice on every call.
        """
        try:
            params = inspect.signature(func).parameters
        except (ValueError, TypeError):
            return None
        for candidate in ("ctx", "context"):
            if candidate in params:
                return candidate
        return None

    def _needs_context(self, func):
        # type: (Callable) -> bool
        """Check if a function accepts a context parameter."""
        return self._context_param_name(func) is not None

    def _call_handler(self, handler, request_id, arguments=None, session_id=None,
                      progress_token=None, meta=None, job_id=None,
                      protocol_version=None, input_responses=None, request_state=None):
        # type: (Callable, Optional[str], Optional[Dict[str, Any]], Optional[str], Optional[Any], Optional[Dict[str, Any]], Optional[str], Optional[str], Optional[Dict[str, Any]], Optional[str]) -> Any
        """Call a handler, injecting context if needed."""
        # Copy so we don't pollute the caller's params dict with a Context
        # object — that would break later JSON serialization or logging.
        arguments = dict(arguments) if arguments else {}

        ctx_param = self._context_param_name(handler)
        if ctx_param is not None:
            arguments[ctx_param] = Context(
                server=self,
                request_id=request_id,
                session_id=session_id,
                progress_token=progress_token,
                meta=meta,
                job_id=job_id,
                protocol_version=protocol_version,
                input_responses=input_responses,
                request_state=request_state,
            )

        return handler(**arguments)

    @staticmethod
    def _extract_progress_token(params):
        # type: (Dict[str, Any]) -> Optional[Any]
        """Pull progressToken out of params._meta per the MCP spec."""
        if not isinstance(params, dict):
            return None
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            return None
        return meta.get("progressToken")

    def _pending_key(self, session_id, request_id):
        # type: (str, Any) -> str
        """Build a lookup key for a pending server-to-client request."""
        return "{}:{}".format(session_id, request_id)

    def _set_session_capabilities(self, session_id, capabilities):
        # type: (Optional[str], Dict[str, Any]) -> None
        """Remember negotiated client capabilities for a session."""
        if not session_id:
            return
        with self._sessions_lock:
            session = self._sessions.setdefault(session_id, {})
            session["capabilities"] = capabilities or {}

    def _set_session_protocol_version(self, session_id, version):
        # type: (Optional[str], str) -> None
        """Remember the revision a handshake-era session negotiated."""
        if not session_id:
            return
        with self._sessions_lock:
            session = self._sessions.setdefault(session_id, {})
            session["protocol_version"] = version

    def _client_capabilities(self, session_id):
        # type: (Optional[str]) -> Dict[str, Any]
        """Return negotiated client capabilities for a session."""
        if not session_id:
            return {}
        with self._sessions_lock:
            session = self._sessions.get(session_id, {})
            return session.get("capabilities", {})

    def _client_supports(self, session_id, capability, mode=None, params=None):
        # type: (Optional[str], str, Optional[str], Optional[Dict[str, Any]]) -> bool
        """Check whether the client declared a capability.

        2026-07-28 clients declare capabilities on every request rather than
        once at initialize, so a per-request declaration is consulted first.
        """
        value = None
        if isinstance(params, dict):
            meta = params.get("_meta")
            if isinstance(meta, dict):
                request_caps = meta.get(META_CLIENT_CAPABILITIES)
                if isinstance(request_caps, dict):
                    value = request_caps.get(capability)
        if value is None:
            capabilities = self._client_capabilities(session_id)
            value = capabilities.get(capability)
        if value is None:
            return False
        if mode is None:
            return True
        if capability == "elicitation" and value == {} and mode == "form":
            return True
        if isinstance(value, dict):
            return mode in value
        return False

    def _client_supports_tasks(self, session_id=None, params=None):
        # type: (Optional[str], Optional[Dict[str, Any]]) -> bool
        """Return True when a client opted into the MCP Tasks extension."""
        extension_sets = []

        if isinstance(params, dict):
            meta = params.get("_meta")
            if isinstance(meta, dict):
                request_caps = meta.get("io.modelcontextprotocol/clientCapabilities")
                if isinstance(request_caps, dict):
                    extension_sets.append(request_caps.get("extensions"))

        capabilities = self._client_capabilities(session_id)
        extension_sets.append(capabilities.get("extensions"))
        experimental = capabilities.get("experimental")
        if isinstance(experimental, dict):
            extension_sets.append(experimental)

        for extensions in extension_sets:
            if isinstance(extensions, dict) and MCP_TASKS_EXTENSION_ID in extensions:
                return True
        return False

    def _negotiate_protocol_version(self, requested):
        # type: (Optional[str]) -> str
        """Return the best supported protocol version for initialization.

        An unrecognized request falls back to the newest *handshake* revision,
        never to 2026-07-28: a client old enough to call `initialize` cannot
        cope with the stateless behaviour that version implies.
        """
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            return requested
        return DEFAULT_NEGOTIATED_VERSION

    def _request_protocol_version(self, params, session_id=None):
        # type: (Dict[str, Any], Optional[str]) -> str
        """Resolve the protocol revision a single request is speaking.

        2026-07-28 is stateless: each request declares its own version in
        `_meta`. Earlier revisions declare it once at `initialize`, so fall
        back to whatever the session negotiated, then to the handshake default.
        """
        if isinstance(params, dict):
            meta = params.get("_meta")
            if isinstance(meta, dict):
                declared = meta.get(META_PROTOCOL_VERSION)
                if isinstance(declared, str) and declared:
                    return declared
        if session_id:
            with self._sessions_lock:
                session = self._sessions.get(session_id) or {}
            negotiated = session.get("protocol_version")
            if negotiated:
                return negotiated
        return DEFAULT_NEGOTIATED_VERSION

    @staticmethod
    def _is_stateless(version):
        # type: (str) -> bool
        """True for revisions with no sessions and no initialize handshake."""
        return bool(version) and version >= PROTOCOL_2026_07_28

    def _request_client_info(self, params):
        # type: (Dict[str, Any]) -> Optional[Dict[str, Any]]
        """Per-request client identity (2026-07-28 replaces initialize's)."""
        meta = params.get("_meta") if isinstance(params, dict) else None
        if isinstance(meta, dict):
            info = meta.get(META_CLIENT_INFO)
            if isinstance(info, dict):
                return info
        return None

    def _request_log_level(self, params):
        # type: (Dict[str, Any]) -> Optional[str]
        """Per-request log level. 2026-07-28 removed logging/setLevel, and
        servers MUST NOT emit notifications/message without this field."""
        meta = params.get("_meta") if isinstance(params, dict) else None
        if isinstance(meta, dict):
            level = meta.get(META_LOG_LEVEL)
            if isinstance(level, str) and level:
                return level
        return None

    @staticmethod
    def _http_status_for(response):
        # type: (Any) -> int
        """HTTP status for a JSON-RPC response.

        2026-07-28 requires 400 for HeaderMismatch and
        UnsupportedProtocolVersion — a gateway should be able to see those
        without parsing the body. Everything else stays 200.
        """
        candidates = response if isinstance(response, list) else [response]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            error = item.get("error")
            if isinstance(error, dict) and error.get("code") in (
                    -32020, -32022, ERROR_CODE_REMAP_2026_07_28.get(-32001),
                    ERROR_CODE_REMAP_2026_07_28.get(-32004)):
                return 400
        return 200

    def _unsupported_version_error(self, requested):
        # type: (str) -> Dict[str, Any]
        """The 2026-07-28 UnsupportedProtocolVersionError payload."""
        return {
            "code": ERR_UNSUPPORTED_PROTOCOL_VERSION,
            "message": "Unsupported protocol version: {}".format(requested),
            "data": {
                "requested": requested,
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
            },
        }

    def _finalize_result(self, result, version):
        # type: (Any, str) -> Any
        """Apply the 2026-07-28 result contract.

        Every result carries `resultType`, and the server SHOULD identify
        itself in `_meta`. Older revisions get the result untouched, so
        existing clients see byte-identical payloads.
        """
        if not self._is_stateless(version) or not isinstance(result, dict):
            return result
        result.setdefault("resultType", "complete")
        meta = result.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
            result["_meta"] = meta
        meta.setdefault(META_SERVER_INFO, ServerInfo(self.name, self.version).to_dict())
        return result

    def _finalize_error(self, error, version):
        # type: (Dict[str, Any], str) -> Dict[str, Any]
        """Emit each error in the numbering the requesting revision expects."""
        if not self._is_stateless(version) or not isinstance(error, dict):
            return error
        remapped = ERROR_CODE_REMAP_2026_07_28.get(error.get("code"))
        if remapped is not None:
            error = dict(error)
            error["code"] = remapped
        return error

    def _paginate(self, items, params, result_key, result, key_field):
        # type: (List[Any], Dict[str, Any], str, Dict[str, Any], str) -> Dict[str, Any]
        """Apply cursor paging to a list result, if a page size is configured.

        Off by default: with no ``list_page_size`` the whole list is returned
        and no ``nextCursor`` appears, exactly as before 0.4.4.

        The cursor carries the last key returned, not an offset. Offsets are
        wrong here because these registries can change while a client is
        walking them — removing an entry ahead of the cursor shifts everything
        down and an entry is skipped entirely, never seen. Resuming *after a
        key* is stable under both insertion and removal, and works even when
        the keyed entry is itself gone. It relies on the list being sorted by
        that key, which tools, resources and prompts all are.
        """
        page_size = self.list_page_size
        if not page_size or page_size <= 0:
            result[result_key] = items
            return result

        start = 0
        cursor = params.get("cursor") if isinstance(params, dict) else None
        if cursor is not None:
            if not isinstance(cursor, str):
                raise InvalidParams("Invalid cursor")
            try:
                # Decoded with validate=True, which `urlsafe_b64decode` has no
                # parameter for — hence the manual translation. Without it
                # base64 *discards* every character outside its alphabet, so a
                # junk cursor like "!!!" decodes to b"" rather than raising and
                # silently restarts paging at page one, instead of rejecting a
                # token this server never minted.
                after = base64.b64decode(
                    cursor.replace("-", "+").replace("_", "/").encode("ascii"),
                    validate=True).decode("utf-8")
            except Exception:
                # `from None`: the base64 failure is noise. What matters is
                # that the token is not one this server minted.
                raise InvalidParams("Invalid cursor") from None
            start = len(items)
            for index, item in enumerate(items):
                if str(item.get(key_field, "")) > after:
                    start = index
                    break

        page = items[start:start + page_size]
        result[result_key] = page
        if page and start + len(page) < len(items):
            result["nextCursor"] = base64.urlsafe_b64encode(
                str(page[-1].get(key_field, "")).encode("utf-8")
            ).decode("ascii")
        return result

    def _cacheable(self, result, version):
        # type: (Dict[str, Any], str) -> Dict[str, Any]
        """Add the CacheableResult freshness hints required by 2026-07-28."""
        if self._is_stateless(version) and isinstance(result, dict):
            result.setdefault("ttlMs", CACHEABLE_TTL_MS)
            result.setdefault("cacheScope", CACHEABLE_SCOPE)
        return result

    def _receive_client_response(self, response, session_id):
        # type: (Dict[str, Any], Optional[str]) -> bool
        """Store a client response for a pending server-to-client request."""
        if not session_id or "id" not in response:
            return False
        key = self._pending_key(session_id, response.get("id"))
        with self._pending_lock:
            pending = self._pending_client_requests.get(key)
            if not pending:
                return False
            pending["response"] = response
            pending["event"].set()
            return True

    def _session_has_stream(self, session_id):
        # type: (Optional[str]) -> bool
        """Return True if the session has at least one active SSE queue."""
        if not session_id:
            return False
        with self._clients_lock:
            return bool(self._clients.get(session_id))

    def _request_client(self, session_id, method, params=None, timeout=60):
        # type: (str, str, Optional[Dict[str, Any]], int) -> Dict[str, Any]
        """Send a JSON-RPC request to one client and wait for its response."""
        if not session_id:
            raise RuntimeError("Client session is required for {}".format(method))

        # Server-to-client requests are delivered over the session's SSE stream.
        # If no stream is connected, fail fast instead of blocking until timeout.
        if not self._session_has_stream(session_id):
            raise RuntimeError(
                "No active client stream for session {} — cannot send {}".format(
                    session_id, method
                )
            )

        request_id = "server-{}".format(uuid.uuid4())
        key = self._pending_key(session_id, request_id)
        event = threading.Event()
        with self._pending_lock:
            self._pending_client_requests[key] = {
                "event": event,
                "response": None
            }

        # Re-check after registration: if the stream disconnected between the
        # initial check and now, _unregister_client may have already swept
        # pending entries — but our entry was inserted *after* its sweep, so it
        # would never be cleaned up. Verify we still have a stream and bail if
        # not, otherwise we'd block until timeout.
        if not self._session_has_stream(session_id):
            with self._pending_lock:
                self._pending_client_requests.pop(key, None)
            raise RuntimeError(
                "Client stream closed before {} could be sent".format(method)
            )

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {}
        }
        self._broadcast(request, session_id=session_id)

        try:
            if not event.wait(timeout):
                raise RuntimeError("Timed out waiting for client response to {}".format(method))
            with self._pending_lock:
                pending = self._pending_client_requests.get(key, {})
                response = pending.get("response")
            if not response:
                # Woken without a response — the SSE stream disconnected
                # (see _unregister_client) or the entry was already cleared.
                raise RuntimeError(
                    "Client stream closed before responding to {}".format(method)
                )
            if "error" in response:
                error = response["error"]
                message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise RuntimeError(message)
            return response.get("result", {})
        finally:
            with self._pending_lock:
                self._pending_client_requests.pop(key, None)

    def request_elicitation(self, session_id, message, requested_schema=None, mode="form",
                            url=None, elicitation_id=None, timeout=60):
        # type: (str, str, Optional[Dict[str, Any]], str, Optional[str], Optional[str], int) -> Dict[str, Any]
        """Request user input through the MCP client."""
        if not self._client_supports(session_id, "elicitation", mode):
            raise RuntimeError("Client does not support {} elicitation".format(mode))

        params = {
            "mode": mode,
            "message": message
        }
        if mode == "form":
            params["requestedSchema"] = requested_schema or {
                "type": "object",
                "properties": {},
                "required": []
            }
        elif mode == "url":
            if not url:
                raise ValueError("url is required for URL mode elicitation")
            params["url"] = url
            params["elicitationId"] = elicitation_id or str(uuid.uuid4())
        else:
            raise ValueError("Unsupported elicitation mode: {}".format(mode))

        return self._request_client(
            session_id=session_id,
            method="elicitation/create",
            params=params,
            timeout=timeout
        )

    def request_sampling(self, session_id, params, timeout=60):
        # type: (str, Dict[str, Any], int) -> Dict[str, Any]
        """Request LLM sampling from a capable client."""
        if not self._client_supports(session_id, "sampling"):
            raise RuntimeError("Client does not support sampling")
        return self._request_client(session_id, "sampling/createMessage", params, timeout=timeout)

    def request_roots(self, session_id, timeout=60):
        # type: (str, int) -> Dict[str, Any]
        """Request roots from a capable client."""
        if not self._client_supports(session_id, "roots"):
            raise RuntimeError("Client does not support roots")
        return self._request_client(session_id, "roots/list", {}, timeout=timeout)

    def _invalid_request(self, msg_id=None, message="Invalid Request"):
        # type: (Optional[Any], str) -> Dict[str, Any]
        """Build a JSON-RPC Invalid Request response."""
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32600, "message": message},
        }

    def _handle_jsonrpc_message(self, message, session_id=None, headers=None):
        # type: (Any, Optional[str], Optional[Any]) -> Optional[Dict[str, Any]]
        """Handle one JSON-RPC message after top-level shape validation."""
        if not isinstance(message, dict):
            return self._invalid_request(None, "JSON-RPC message must be an object")
        return self._handle_request(message, session_id=session_id, headers=headers)

    def _handle_jsonrpc_payload(self, payload, session_id=None, headers=None):
        # type: (Any, Optional[str], Optional[Any]) -> Optional[Any]
        """Handle a JSON-RPC message or batch payload."""
        if isinstance(payload, list):
            if not payload:
                return self._invalid_request(None, "JSON-RPC batch must not be empty")
            responses = []
            for message in payload:
                response = self._handle_jsonrpc_message(
                    message, session_id=session_id, headers=headers)
                if response is not None:
                    responses.append(response)
            return responses or None
        return self._handle_jsonrpc_message(
            payload, session_id=session_id, headers=headers)

    def _handle_request(self, request, session_id=None, headers=None):
        # type: (Dict[str, Any], Optional[str], Optional[Any]) -> Optional[Dict[str, Any]]
        """Handle a JSON-RPC request and return response."""
        if not isinstance(request, dict):
            return self._invalid_request(None, "JSON-RPC message must be an object")



        method = request.get("method", "")
        msg_id = request.get("id")
        is_notification = msg_id is None
        params = request.get("params")
        if params is None:
            params = {}

        if not method and self._receive_client_response(request, session_id):
            return None

        # Per JSON-RPC 2.0, params (when present) must be an object or array.
        # We only accept objects since every method consumes named params.
        if not isinstance(params, dict):
            if is_notification:
                # Notifications must never receive a reply, even on error.
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "params must be a JSON object"},
            }

        # Per spec, an empty/missing method is an Invalid Request, not a
        # missing method. Surface that distinction so clients see -32600.
        if not method:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32600, "message": "Missing or empty method"},
            }

        progress_token = self._extract_progress_token(params)
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
        self._prune_expired_jobs()
        self._prune_expired_mrtr_states()

        # Which revision is this single request speaking? 2026-07-28 declares
        # it per request; older revisions inherit the session's negotiation.
        version = self._request_protocol_version(params, session_id)
        modern = self._is_stateless(version)
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                # Emitted in the new numbering: only a 2026-07-28-aware client
                # declares a version we could fail to recognize.
                "error": self._finalize_error(
                    self._unsupported_version_error(version), PROTOCOL_2026_07_28
                ),
            }

        # Routing headers are checked once the declared revision is known:
        # a contradicting header is always rejected, a missing one only where
        # that revision requires it.
        mismatch = self._header_mismatch(request, headers, version)
        if mismatch is not None:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "error": mismatch}

        result = None
        error = None

        try:
            if method == "initialize":
                self._set_session_capabilities(session_id, params.get("capabilities", {}))
                negotiated = self._negotiate_protocol_version(
                    params.get("protocolVersion")
                )
                self._set_session_protocol_version(session_id, negotiated)
                result = {
                    "protocolVersion": negotiated,
                    "serverInfo": ServerInfo(self.name, self.version).to_dict(),
                    "capabilities": self._get_capabilities().to_dict()
                }

            elif method == "initialized":
                # Notification, no response needed
                return None

            elif method == "notifications/cancelled":
                # A client withdrawing an in-flight request. Map it onto the
                # work it actually started: a running task, or a listen stream.
                self._handle_cancelled(params, session_id)
                return None

            elif method == "server/discover":
                # Mandatory in 2026-07-28: one call returns identity, versions,
                # and capabilities, replacing what initialize used to carry.
                result = self._cacheable({
                    "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": self._get_capabilities().to_dict(),
                }, PROTOCOL_2026_07_28)
                if self.instructions:
                    result["instructions"] = self.instructions
                # Answer in the new shape regardless of the caller's declared
                # version — the method only exists in 2026-07-28.
                result = self._finalize_result(result, PROTOCOL_2026_07_28)

            elif method == "ping":
                if modern:
                    error = {"code": -32601,
                             "message": "Method not found: ping (removed in {})".format(version)}
                else:
                    result = {}

            elif method == "subscriptions/listen":
                if msg_id is None:
                    error = {"code": -32600,
                             "message": "subscriptions/listen must be a request"}
                elif not session_id:
                    error = {"code": -32003,
                             "message": "subscriptions/listen requires an MCP session"}
                else:
                    requested = params.get("notifications")
                    task_ids = requested.get("taskIds") if isinstance(requested, dict) else None
                    if not isinstance(requested, dict):
                        error = {"code": -32602,
                                 "message": "subscriptions/listen requires a notifications filter"}
                    elif task_ids is not None and (
                        not isinstance(task_ids, list)
                        or not all(isinstance(t, str) and t for t in task_ids)
                    ):
                        error = {"code": -32602,
                                 "message": "notifications.taskIds must be an array of task ids"}
                    else:
                        # Acknowledge only tasks that exist and belong to this
                        # session — the ack reports what the server agreed to.
                        accepted = []
                        with self._jobs_lock:
                            for task_id in (task_ids or []):
                                job = self._jobs.get(task_id)
                                if job is None:
                                    continue
                                if self._task_access_error(task_id, job, session_id) is None:
                                    accepted.append(task_id)

                        # The base filters are opt-in flags; remember which
                        # the client asked for so list_changed can honour them.
                        filters = {
                            key: bool(requested.get(key))
                            for key in ("toolsListChanged", "promptsListChanged",
                                        "resourcesListChanged")
                            if requested.get(key)
                        }
                        # Per-URI subscriptions: acknowledge only URIs that
                        # exist, so the ack states what will actually be watched.
                        wanted_uris = requested.get("resourceSubscriptions")
                        resource_uris = []
                        if isinstance(wanted_uris, list):
                            with self._registry_lock:
                                known = set(self._resources)
                            resource_uris = [
                                u for u in wanted_uris
                                if isinstance(u, str) and u in known
                            ]
                        with self._subs_lock:
                            session_subs = self._subscriptions.setdefault(session_id, {})
                            if (msg_id not in session_subs
                                    and len(session_subs) >= MAX_SUBSCRIPTIONS_PER_SESSION):
                                error = {
                                    "code": -32602,
                                    "message": "Too many open subscriptions for this "
                                               "session (max {})".format(
                                                   MAX_SUBSCRIPTIONS_PER_SESSION),
                                }
                                session_subs = None
                            else:
                                session_subs[msg_id] = {"task_ids": set(accepted),
                                                    "filters": filters,
                                                    "resource_uris": set(resource_uris)}
                        if session_subs is None:
                            # Over the cap: fall through to the error response.
                            return {"jsonrpc": "2.0", "id": msg_id, "error": error}

                        # MUST be the first message carrying this subscription
                        # id, and MUST precede any notification on the stream.
                        self._broadcast({
                            "jsonrpc": "2.0",
                            "method": "notifications/subscriptions/acknowledged",
                            "params": {
                                "notifications": dict(
                                    filters, taskIds=accepted,
                                    resourceSubscriptions=resource_uris),
                                "_meta": {MCP_SUBSCRIPTION_ID_KEY: msg_id},
                            },
                        }, session_id=session_id)

                        # The listen stream is long-lived: its response is sent
                        # only on graceful teardown, so return nothing now.
                        return None

            elif method == "tasks/get":
                task_id = params.get("taskId")
                if not isinstance(task_id, str) or not task_id:
                    error = {"code": -32602, "message": "tasks/get requires taskId"}
                elif not self._client_supports_tasks(session_id=session_id, params=params):
                    error = {
                        "code": -32003,
                        "message": "Missing required client capability",
                        "data": {
                            "requiredCapabilities": {
                                "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                            }
                        },
                    }
                else:
                    with self._jobs_lock:
                        job = self._jobs.get(task_id)
                        access_error = self._task_access_error(task_id, job, session_id)
                        task = (
                            self._job_to_task(task_id, job)
                            if access_error is None
                            else None
                        )
                    if access_error is not None:
                        error = access_error
                    else:
                        task["resultType"] = "complete"
                        result = task

            elif method == "tasks/update":
                task_id = params.get("taskId")
                if not isinstance(task_id, str) or not task_id:
                    error = {"code": -32602, "message": "tasks/update requires taskId"}
                elif not self._client_supports_tasks(session_id=session_id, params=params):
                    error = {
                        "code": -32003,
                        "message": "Missing required client capability",
                        "data": {
                            "requiredCapabilities": {
                                "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                            }
                        },
                    }
                else:
                    with self._jobs_lock:
                        job = self._jobs.get(task_id)
                        access_error = self._task_access_error(task_id, job, session_id)
                    if access_error is not None:
                        error = access_error
                    else:
                        responses = params.get("inputResponses")
                        if not isinstance(responses, dict) or not responses:
                            error = {
                                "code": -32602,
                                "message": "tasks/update requires inputResponses",
                            }
                        else:
                            error = self._task_deliver_input(task_id, responses)
                            if error is None:
                                result = {"resultType": "complete"}

            elif method == "tasks/cancel":
                task_id = params.get("taskId")
                if not isinstance(task_id, str) or not task_id:
                    error = {"code": -32602, "message": "tasks/cancel requires taskId"}
                elif not self._client_supports_tasks(session_id=session_id, params=params):
                    error = {
                        "code": -32003,
                        "message": "Missing required client capability",
                        "data": {
                            "requiredCapabilities": {
                                "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                            }
                        },
                    }
                else:
                    callbacks = []
                    with self._jobs_lock:
                        job = self._jobs.get(task_id)
                        access_error = self._task_access_error(task_id, job, session_id)
                        if access_error is None and job.get("status") in (
                                "running", "input_required"):
                            job["status"] = "cancelled"
                            job["lastUpdatedAt"] = self._utc_now()
                            event = job.get("cancel_event")
                            if event is not None:
                                event.set()
                            waiting = job.get("input_event")
                            if waiting is not None:
                                waiting.set()
                            callbacks = list(job.get("cancel_callbacks") or [])
                    if access_error is not None:
                        error = access_error
                    else:
                        # Outside the lock: callbacks kill process groups and
                        # must not block every other job's bookkeeping.
                        self._fire_cancel_callbacks(task_id, callbacks)
                        if callbacks or job is not None:
                            self._notify_task_status(task_id)
                        result = {"resultType": "complete"}

            elif method == "tools/list":
                # Deterministic order: 2026-07-28 asks for it so clients can
                # cache and so LLM prompt-cache hit rates stay high.
                with self._registry_lock:
                    tool_dicts = [
                        self._tools[name]["definition"].to_dict()
                        for name in sorted(self._tools)
                    ]
                result = self._cacheable(
                    self._paginate(tool_dicts, params, "tools", {}, "name"), version)

            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})

                if not isinstance(arguments, dict):
                    error = {"code": -32602,
                             "message": "tools/call arguments must be a JSON object"}
                elif tool_name not in self._tools:
                    error = {"code": -32601, "message": "Tool not found: {}".format(tool_name)}
                else:
                    tool_entry = self._tools[tool_name]
                    handler = tool_entry["handler"]

                    if tool_entry.get("is_async"):
                        # Return a task to clients that opted into the MCP
                        # Tasks extension; older clients keep the existing
                        # get_job_result polling-tool flow.
                        supports_tasks = self._client_supports_tasks(
                            session_id=session_id, params=params
                        )
                        if supports_tasks and not session_id and not modern:
                            error = {
                                "code": -32003,
                                "message": "Task-capable async tool calls require an MCP session",
                            }
                        else:
                            job_id = self._start_async_tool_job(
                                handler, msg_id, arguments,
                                session_id=session_id,
                                progress_token=progress_token,
                                meta=meta,
                                prepare=tool_entry.get("prepare"),
                                protocol_version=version,
                            )
                        if supports_tasks and not error:
                            with self._jobs_lock:
                                job = self._jobs[job_id]
                                result = self._job_to_task(
                                    job_id, job, include_terminal_payload=False
                                )
                            result["resultType"] = "task"
                        elif not error:
                            legacy_body = {
                                "status": "running",
                                "job_id": job_id,
                                "message": "Job started. Poll with get_job_result(job_id=\"{}\")".format(job_id)
                            }
                            with self._jobs_lock:
                                started = self._jobs.get(job_id) or {}
                                task_meta = dict(started.get("task_meta") or {})
                            if task_meta:
                                # Legacy clients never read `_meta`; give them
                                # the same handles in the body they do read.
                                legacy_body["meta"] = task_meta
                            result = {
                                "content": [{"type": "text", "text": json.dumps(legacy_body)}],
                                "isError": False
                            }
                    else:
                        # MRTR: fold in answers the client already gave, from
                        # this retry and from earlier rounds of the same request.
                        request_state = params.get("requestState")
                        if not isinstance(request_state, str):
                            request_state = None
                        collected = self._mrtr_load(request_state, session_id)
                        supplied = params.get("inputResponses")
                        if isinstance(supplied, dict):
                            collected.update(supplied)
                        try:
                            call_result = self._call_handler(
                                handler, msg_id, arguments,
                                session_id=session_id,
                                progress_token=progress_token,
                                meta=meta,
                                protocol_version=version,
                                input_responses=collected,
                                request_state=request_state,
                            )
                            self._mrtr_discard(request_state)

                            # Wrap result in proper format
                            if isinstance(call_result, ToolResult):
                                result = call_result.to_dict()
                            elif isinstance(call_result, dict):
                                # structuredContent (MCP spec): required when the
                                # tool declares outputSchema, and lets mcp-apps
                                # widgets consume data without re-parsing text.
                                result = {
                                    "content": [{"type": "text", "text": json.dumps(call_result)}],
                                    "structuredContent": call_result,
                                    "isError": False
                                }
                            else:
                                result = {
                                    "content": [{"type": "text", "text": str(call_result)}],
                                    "isError": False
                                }
                        except InputRequired as needed:
                            # Not an error: the handler is telling the client
                            # what it needs. The client answers by retrying this
                            # same call with inputResponses + requestState.
                            result = {
                                "resultType": "input_required",
                                "inputRequests": needed.requests,
                                "requestState": self._mrtr_save(
                                    request_state, collected, session_id
                                ),
                            }
                        except Exception as e:
                            self._mrtr_discard(request_state)
                            traceback.print_exc()
                            result = {
                                "content": [{"type": "text", "text": str(e)}],
                                "isError": True
                            }

            elif method == "resources/list":
                resources = []
                with self._registry_lock:
                    # Sorted for the same reasons tools/list is: a stable order
                    # lets clients cache, and offset paging can only be walked
                    # safely when the order does not shuffle between pages.
                    entries = [self._resources[uri] for uri in sorted(self._resources)]
                for r in entries:
                    resource_dict = r["definition"].to_dict()
                    resources.append(resource_dict)
                result = self._cacheable(
                    self._paginate(resources, params, "resources", {}, "uri"),
                    version)

            elif method == "resources/templates/list":
                # No URI-template resources are registered by this framework;
                # answering with an empty list beats a "method not found" for
                # a client that walks every resources/* endpoint.
                result = self._cacheable({"resourceTemplates": []}, version)

            elif method == "resources/read":
                uri = params.get("uri")

                # Validated before use: a non-string uri reached a dict lookup
                # and raised TypeError, which surfaced as an internal error
                # with a traceback — blaming the server for the caller's
                # malformed request, and logging a stack trace per bad request.
                if not isinstance(uri, str) or not uri:
                    error = {"code": -32602,
                             "message": "resources/read requires a string uri"}
                else:
                    # Strip proxy prefix from URI for lookup
                    lookup_uri = self._strip_proxy_prefix(uri)

                    if lookup_uri in self._skill_resources:
                        # SEP-2640 skill file. No handler to call: the bytes
                        # come straight off disk, read fresh on every call
                        # (never cached ahead of need, per the SEP).
                        skill_entry = self._skill_resources[lookup_uri]
                        try:
                            data = skill_entry["path"].read_bytes()
                            content_item = {
                                "uri": uri,
                                "mimeType": skill_entry["mimeType"],
                            }
                            try:
                                content_item["text"] = data.decode("utf-8")
                            except UnicodeDecodeError:
                                content_item["blob"] = base64.b64encode(
                                    data).decode("ascii")
                            result = self._cacheable(
                                {"contents": [content_item]}, version)
                        except Exception as e:
                            traceback.print_exc()
                            error = {"code": -32603, "message": str(e)}
                    elif lookup_uri not in self._resources:
                        # 2026-07-28 aligns resource-not-found with JSON-RPC's
                        # Invalid Params; older clients keep the code they know.
                        error = {
                            "code": -32602 if modern else -32601,
                            "message": "Resource not found: {}".format(uri),
                        }
                    else:
                        entry = self._resources[lookup_uri]
                        handler = entry["handler"]
                        definition = entry["definition"]
                        res_mime = getattr(definition, "mimeType", None)
                        res_meta = getattr(definition, "meta", None) or {}
                        try:
                            content = self._call_handler(
                                handler, msg_id,
                                session_id=session_id,
                                progress_token=progress_token,
                                meta=meta,
                            )

                            if isinstance(content, ResourceResult):
                                result = content.to_dict()
                            elif isinstance(content, dict):
                                result = {
                                    "contents": [{
                                        "uri": uri,
                                        "text": json.dumps(content)
                                    }]
                                }
                            else:
                                result = {
                                    "contents": [{
                                        "uri": uri,
                                        "text": str(content)
                                    }]
                                }

                            # Decorate each content entry with the registered
                            # mimeType and `_meta` (mcp-apps CSP, permissions,
                            # etc.). Don't override values the handler already
                            # supplied via ResourceContent.
                            for item in result.get("contents", []):
                                if not isinstance(item, dict):
                                    continue
                                if res_mime and "mimeType" not in item:
                                    item["mimeType"] = res_mime
                                if res_meta and "_meta" not in item:
                                    item["_meta"] = res_meta

                            # A read is a CacheableResult too, exactly like the
                            # list methods above. Hosts that validate the result
                            # schema reject a resources/read with no ttlMs and no
                            # cacheScope, and for an MCP Apps server that rejection
                            # means the app resource never loads at all.
                            result = self._cacheable(result, version)
                        except Exception as e:
                            traceback.print_exc()
                            error = {"code": -32603, "message": str(e)}

            elif method == "prompts/list":
                with self._registry_lock:
                    prompt_dicts = [self._prompts[name]["definition"].to_dict()
                                    for name in sorted(self._prompts)]
                result = self._cacheable(
                    self._paginate(prompt_dicts, params, "prompts", {}, "name"),
                    version)

            elif method == "prompts/get":
                prompt_name = params.get("name")
                arguments = params.get("arguments", {})

                if not isinstance(prompt_name, str) or not prompt_name:
                    error = {"code": -32602,
                             "message": "prompts/get requires a string name"}
                elif not isinstance(arguments, dict):
                    error = {"code": -32602,
                             "message": "prompts/get arguments must be a JSON object"}
                elif prompt_name not in self._prompts:
                    error = {"code": -32601, "message": "Prompt not found: {}".format(prompt_name)}
                else:
                    handler = self._prompts[prompt_name]["handler"]
                    try:
                        prompt_result = self._call_handler(
                            handler, msg_id, arguments,
                            session_id=session_id,
                            progress_token=progress_token,
                            meta=meta,
                        )

                        if isinstance(prompt_result, PromptResult):
                            result = prompt_result.to_dict()
                        elif isinstance(prompt_result, list):
                            # Assume list of message dicts
                            result = {"messages": prompt_result}
                        else:
                            result = {"messages": [{"role": "user", "content": {"type": "text", "text": str(prompt_result)}}]}
                    except Exception as e:
                        traceback.print_exc()
                        error = {"code": -32603, "message": str(e)}

            elif method == "skills/list":
                # SEP-2640. Every entry is already a complete manifest
                # (frontmatter verbatim, every file's digest and size), so
                # there is nothing to compute here beyond sorting and paging.
                with self._registry_lock:
                    skill_dicts = [self._skills[uri]["definition"].to_dict()
                                   for uri in sorted(self._skills)]
                result = self._cacheable(
                    self._paginate(skill_dicts, params, "skills", {}, "uri"),
                    version)

            elif method == "skills/get":
                skill_uri = params.get("uri")
                if not isinstance(skill_uri, str) or not skill_uri:
                    error = {"code": -32602,
                             "message": "skills/get requires a string uri"}
                else:
                    with self._registry_lock:
                        entry = self._skills.get(skill_uri)
                    if entry is None:
                        # Same code resources/read uses for an unknown URI,
                        # per SEP-2640.
                        error = {"code": -32602,
                                 "message": "Skill not found: {}".format(skill_uri)}
                    else:
                        result = self._cacheable(
                            {"skill": entry["definition"].to_dict()}, version)

            elif method == "resources/directory/read":
                dir_uri = params.get("uri")
                if not isinstance(dir_uri, str) or not dir_uri:
                    error = {"code": -32602,
                             "message": "resources/directory/read requires a string uri"}
                else:
                    with self._registry_lock:
                        children = self._skill_directories.get(dir_uri)
                        children = list(children) if children is not None else None
                    if children is None:
                        error = {"code": -32602,
                                 "message": "Not a directory resource: {}".format(dir_uri)}
                    else:
                        result = self._cacheable(
                            self._paginate(children, params, "resources", {}, "uri"),
                            version)

            elif method == "logging/setLevel":
                if modern:
                    # Removed: log level is per-request via _meta instead.
                    error = {"code": -32601,
                             "message": "Method not found: logging/setLevel "
                                        "(removed in {}; use _meta {})".format(
                                            version, META_LOG_LEVEL)}
                else:
                    result = {}

            else:
                error = {"code": -32601, "message": "Method not found: {}".format(method)}

        except InvalidParams as e:
            # The caller's input was wrong — not the server failing.
            error = {"code": -32602, "message": str(e)}
        except Exception as e:
            error = {"code": -32603, "message": str(e)}
            traceback.print_exc()

        # Build response
        if msg_id is None:
            # Notification, no response
            return None

        response = {"jsonrpc": "2.0", "id": msg_id}
        if error:
            response["error"] = self._finalize_error(error, version)
        else:
            response["result"] = self._finalize_result(result, version)

        return response

    def _register_client(self, session_id, client_queue):
        # type: (str, _SSEQueue) -> None
        """Register an SSE queue for a client session."""
        with self._clients_lock:
            self._clients.setdefault(session_id, []).append(client_queue)

    def _unregister_client(self, session_id, client_queue):
        # type: (str, _SSEQueue) -> None
        """Remove an SSE queue for a client session.

        When the last queue for a session disconnects, drop the session's
        negotiated capabilities and any pending server-to-client requests so
        long-lived processes don't leak memory across reconnects.
        """
        session_empty = False
        with self._clients_lock:
            queues = self._clients.get(session_id, [])
            if client_queue in queues:
                queues.remove(client_queue)
            if not queues and session_id in self._clients:
                del self._clients[session_id]
                session_empty = True

        if not session_empty:
            return

        with self._sessions_lock:
            self._sessions.pop(session_id, None)
        # Outside the sessions lock on purpose. Everywhere else acquires
        # subscriptions *before* sessions; nesting them the other way here
        # would make one future edit that spans both a deadlock.
        self._drop_subscriptions(session_id)

        prefix = "{}:".format(session_id)
        with self._pending_lock:
            stale_keys = [k for k in self._pending_client_requests if k.startswith(prefix)]
            for key in stale_keys:
                pending = self._pending_client_requests.pop(key, None)
                if pending and "event" in pending:
                    # Wake any waiter so it raises instead of timing out
                    pending["event"].set()

    def _client_count(self):
        # type: () -> int
        """Return the total number of active SSE queues."""
        with self._clients_lock:
            return sum(len(queues) for queues in self._clients.values())

    def _broadcast(self, message, session_id=None):
        # type: (Dict[str, Any], Optional[str]) -> None
        """Send message to SSE clients for one session."""
        if not session_id:
            return
        json_str = json.dumps(message)
        with self._clients_lock:
            queues = list(self._clients.get(session_id, []))
        for client_queue in queues:
            client_queue.append(json_str)
            # Wake any reader blocked on this queue
            event = getattr(client_queue, "_wake_event", None)
            if event is not None:
                event.set()

    def run(self, host="0.0.0.0", port=8000, path_prefix="",
            require_session_header=False, max_request_bytes=MAX_REQUEST_BYTES,
            require_route_headers="auto", allowed_origins=None):
        # type: (str, int, str, bool, int, Any, Optional[List[str]]) -> None
        """Start the MCP server.

        Args:
            host: Host to bind to.
            port: Port to listen on.
            path_prefix: URL path prefix (e.g. '/weber/.../') for proxy environments.
                         Routes will be matched with or without this prefix.
            require_session_header: If True, only accept session ids from the
                ``Mcp-Session-Id`` header on POSTs and omit ``session_id`` from
                the SSE endpoint URL. Use this in production deployments where
                URLs may be logged by proxies — query-string session ids are
                otherwise treated as authoritative and could be replayed by
                anyone who sees them. Defaults to False for compatibility with
                clients that only support query-string sessions.
            max_request_bytes: Largest POST body accepted before a 413 is
                returned. Defaults to ``MAX_REQUEST_BYTES`` (16 MiB). Raise
                this if your tools accept large payloads (file uploads,
                inlined images), or lower it to harden against memory abuse.
            require_route_headers: When a POST must carry ``Mcp-Method`` (and
                ``Mcp-Name``, where the body names a tool or prompt), on pain
                of ``HeaderMismatchError`` and HTTP 400.

                ``"auto"`` (default) enforces them exactly where the protocol
                does: for a request declaring ``2026-07-28``, which requires
                them, and not for earlier revisions, which never defined them —
                demanding them there would reject a conformant client. ``True``
                requires them of every revision; ``False`` never does.

                Enforcement applies only to transports that carry headers at
                all. A direct in-process call, the SSE channel, and stdio have
                nowhere to put one, so absence there means "not applicable".

                A *contradicting* header is always rejected, whatever this is
                set to — that is the point of a header a gateway routes on.
            allowed_origins: Origins a browser may call this server from. The
                transport spec requires servers to validate ``Origin`` to block
                DNS rebinding — a page on any origin can otherwise script a
                request to a server reachable from the victim's browser.

                A request carrying an ``Origin`` outside this list is refused
                with HTTP 403. ``None`` (the default) accepts any origin,
                because a library cannot know which are legitimate; **set it in
                any deployment a browser can reach.** Requests with no
                ``Origin`` at all — every non-browser client — are unaffected.
        """
        server_instance = self
        self._serving = True
        _prefix = path_prefix.rstrip("/") if path_prefix else ""
        self._path_prefix = _prefix
        _require_header = bool(require_session_header)
        _max_bytes = int(max_request_bytes)
        self._require_route_headers = (
            "auto" if require_route_headers == "auto" else bool(require_route_headers))
        self._allowed_origins = (
            None if allowed_origins is None
            else {str(o).rstrip("/").lower() for o in allowed_origins})

        class MCPRequestHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                print("[{}] {}".format(self.log_date_time_string(), format % args))

            def _strip_prefix(self):
                """Strip the path prefix to get the local route."""
                path = self.path
                if _prefix and path.startswith(_prefix):
                    path = path[len(_prefix):]
                if not path.startswith("/"):
                    path = "/" + path
                return path

            def _session_id(self):
                """Get the client session id from headers or query parameters.

                When ``require_session_header`` is set, query-string session ids
                are ignored — only the ``Mcp-Session-Id`` header is honored.
                """
                session_id = self.headers.get("Mcp-Session-Id")
                if session_id:
                    return session_id
                if _require_header:
                    return None
                query = parse_qs(urlparse(self.path).query)
                values = query.get("session_id") or query.get("sessionId")
                if values:
                    return values[0]
                return None

            def _new_session_id(self):
                """Create a session id (for an SSE stream or an initialize POST)."""
                return str(uuid.uuid4())

            @staticmethod
            def _is_initialize(request):
                """Whether a JSON-RPC payload (or batch) carries `initialize`."""
                messages = request if isinstance(request, list) else [request]
                return any(isinstance(m, dict) and m.get("method") == "initialize"
                           for m in messages)

            def _send_session_headers(self, minted_session):
                """Return a session minted on this request, and let browsers see it.

                `Access-Control-Expose-Headers` is what lets a browser client
                read the header from a cross-origin response; the preflight's
                Allow-Headers only lets it be *sent*.
                """
                if minted_session:
                    self.send_header("Mcp-Session-Id", minted_session)
                self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")

            def _send_json(self, payload, status=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _endpoint_path(self, path, session_id):
                """Build a relative endpoint path for the SSE stream.

                In header-only mode the session id is *not* embedded in the URL —
                clients must echo it back via the ``Mcp-Session-Id`` header so
                it doesn't leak through proxy access logs.
                """
                base = "{}{}".format(_prefix, path) if _prefix else path
                if _require_header:
                    return base
                separator = "&" if "?" in base else "?"
                return "{}{}session_id={}".format(base, separator, session_id)

            def _reject_forbidden_origin(self):
                """Refuse a browser request from an origin outside the allowlist.

                Checked on every method, not just POST: the SSE stream is a
                long-lived channel a page can open cross-origin with
                EventSource, which is the more useful target of the two.
                """
                if server_instance.origin_allowed(self.headers.get("Origin")):
                    return False
                body = json.dumps({
                    "jsonrpc": "2.0",
                    "error": {"code": -32600, "message": "Origin not allowed"},
                }).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return True

            def do_GET(self):
                if self._reject_forbidden_origin():
                    return
                path = self._strip_prefix()
                # Remove query string for path matching
                path_only = path.split("?")[0]

                if path_only.rstrip("/") == "/sse" or path_only == "/sse":
                    self._handle_sse()
                elif path_only.rstrip("/") == "/mcp" or path_only == "/mcp":
                    # Streamable HTTP - GET returns SSE stream for responses
                    self._handle_streamable_http_get()
                elif path_only == "/openapi.json":
                    self._handle_openapi()
                elif path_only == "/.well-known/mcp.json":
                    self._handle_mcp_discovery()
                elif path_only == "/favicon.ico":
                    # Browsers probe for this; respond cheaply without payload.
                    self.send_response(204)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                elif path_only in ("/", ""):
                    # Root: server info / health page.
                    info = {
                        "name": server_instance.name,
                        "version": server_instance.version,
                        "status": "running",
                        "tools": len(server_instance._tools),
                        "resources": len(server_instance._resources),
                        "prompts": len(server_instance._prompts),
                        "endpoints": {
                            "sse": "/sse",
                            "mcp": "/mcp",
                            "openapi": "/openapi.json"
                        }
                    }
                    body = json.dumps(info).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404, "No GET endpoint at {}".format(path_only))

            def _sse_pump_loop(self, client_queue):
                """Drain queued messages and emit periodic heartbeats.

                Blocks on a threading.Event so the thread doesn't spin; sends a
                comment-line heartbeat every SSE_HEARTBEAT_INTERVAL seconds to
                keep proxies (nginx, wrwroxy) from closing idle connections.
                """
                event = client_queue._wake_event
                while True:
                    while client_queue:
                        msg = client_queue.pop(0)
                        self.wfile.write(
                            "event: message\ndata: {}\n\n".format(msg).encode("utf-8")
                        )
                        self.wfile.flush()

                    event.clear()
                    if event.wait(SSE_HEARTBEAT_INTERVAL):
                        # Woken by a new message; loop and drain
                        continue
                    # Timed out — emit a heartbeat comment to keep the link warm
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()

            def _handle_sse(self):
                session_id = self._session_id() or self._new_session_id()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Mcp-Session-Id", session_id)
                self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
                self.end_headers()

                client_queue = _SSEQueue()
                server_instance._register_client(session_id, client_queue)
                print("SSE client connected. Session: {} Total: {}".format(
                    session_id, server_instance._client_count()
                ))

                try:
                    # Send connection event
                    self.wfile.write(b"event: open\ndata: {}\n\n")
                    endpoint = self._endpoint_path("/", session_id)
                    self.wfile.write("event: endpoint\ndata: {}\n\n".format(endpoint).encode("utf-8"))
                    self.wfile.flush()

                    self._sse_pump_loop(client_queue)
                except (BrokenPipeError, ConnectionResetError) as e:
                    print("SSE client disconnected: {}".format(e))
                except Exception:
                    # Real bug in the pump — log a traceback so it's debuggable
                    # rather than masquerading as a clean disconnect.
                    traceback.print_exc()
                finally:
                    server_instance._unregister_client(session_id, client_queue)

            def do_POST(self):
                try:
                    # Before anything else, including reading the body: a
                    # rebinding attempt should cost nothing.
                    if self._reject_forbidden_origin():
                        return

                    path = self._strip_prefix()
                    path_only = path.split("?")[0]

                    # Validate the route *before* reading the body so we don't
                    # buffer a large payload only to 404 it.
                    is_direct_tool = path_only.startswith("/tools/")
                    if not is_direct_tool and path_only not in ("/", "/mcp", "/mcp/"):
                        self.send_error(404, "No JSON-RPC endpoint at {}".format(path_only))
                        return

                    # Parse Content-Length defensively — a non-integer header
                    # is bad input, not a server bug.
                    raw_len = self.headers.get("Content-Length", "0")
                    try:
                        content_length = int(raw_len)
                    except (TypeError, ValueError):
                        self.send_error(400, "Invalid Content-Length header")
                        return
                    if content_length < 0:
                        self.send_error(400, "Negative Content-Length")
                        return
                    if content_length > _max_bytes:
                        self.send_error(413, "Request body exceeds {} bytes".format(_max_bytes))
                        return

                    post_data = self.rfile.read(content_length)

                    # Decode + parse JSON in their own scope so malformed input
                    # surfaces as a structured error, not a 500 with a noisy
                    # traceback. On the JSON-RPC endpoint that error is a
                    # JSON-RPC Parse error (-32700) with a JSON body: an HTML
                    # 400 is not something a JSON-RPC client -- or the proxy in
                    # front of it -- can interpret, and was observed wrapped
                    # into a bogus "Proxy error" downstream.
                    try:
                        request = json.loads(post_data.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as e:
                        if is_direct_tool:
                            self.send_error(400, "Malformed JSON: {}".format(e))
                            return
                        self._send_json({
                            "jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700,
                                      "message": "Parse error: {}".format(e)},
                        }, status=400)
                        return

                    session_id = self._session_id()
                    # Streamable HTTP: a client initializes with a POST and
                    # expects the server to mint the session and return it in
                    # `Mcp-Session-Id` on that response. Creating sessions only
                    # when a GET/SSE stream was opened first left every
                    # POST-only client stateless: the capabilities it
                    # negotiated (elicitation, tasks) were stored under no
                    # session and were gone on the next request.
                    minted_session = None
                    if (not session_id and not is_direct_tool
                            and self._is_initialize(request)):
                        session_id = minted_session = self._new_session_id()

                    # Direct tool call via /tools/{name}
                    if is_direct_tool:
                        tool_name = path_only[len("/tools/"):]
                        # Reject nested paths like /tools/foo/bar — only the
                        # bare name is a valid tool identifier.
                        if "/" in tool_name or not tool_name:
                            self.send_error(404, "Tool not found: {}".format(tool_name))
                            return
                        self._handle_direct_tool_call(tool_name, request)
                        return

                    if isinstance(request, list):
                        print("Received: batch[{}]".format(len(request)))
                    elif isinstance(request, dict):
                        print("Received: {}".format(request.get("method", "unknown")))
                    else:
                        print("Received: invalid JSON-RPC payload")

                    if path_only in ("/mcp", "/mcp/"):
                        # Fast methods (initialize, tools/list, ping, etc.) are
                        # handled synchronously so proxy clients get the
                        # response on the HTTP reply. Async tool calls
                        # (@async_tool) return a job_id wrapper immediately
                        # (HTTP 202 + the job_id broadcast on SSE); the actual
                        # result lands in the job table and the client polls
                        # via get_job_result.
                        method = request.get("method", "") if isinstance(request, dict) else ""
                        request_params = request.get("params", {}) if isinstance(request, dict) else {}
                        tool_name = request_params.get("name", "") if isinstance(request_params, dict) else ""
                        tool_entry = server_instance._tools.get(tool_name, {})
                        is_slow = method == "tools/call" and tool_entry.get("is_async", False)
                        returns_task = (
                            is_slow and
                            server_instance._client_supports_tasks(
                                session_id=session_id,
                                params=request_params if isinstance(request_params, dict) else {},
                            )
                        )

                        if is_slow and session_id and not returns_task:
                            def async_handler():
                                try:
                                    resp = server_instance._handle_request(
                                        request, session_id=session_id
                                    )
                                    if resp:
                                        server_instance._broadcast(resp, session_id=session_id)
                                except Exception as e:
                                    print("Error in async handler: {}".format(e))
                                    traceback.print_exc()

                            t = threading.Thread(target=async_handler)
                            t.daemon = True
                            t.start()

                            body = b'{"status":"accepted"}'
                            self.send_response(202)
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                        else:
                            response = server_instance._handle_jsonrpc_payload(
                                request, session_id=session_id, headers=self.headers
                            )
                            # Streamable HTTP: deliver the response on the HTTP
                            # reply *and* over the SSE stream so clients can
                            # subscribe to either channel. Clients listening on
                            # both should dedupe by JSON-RPC id.
                            if response:
                                server_instance._broadcast(response, session_id=session_id)
                                body = json.dumps(response).encode("utf-8")
                                self.send_response(
                                    server_instance._http_status_for(response))
                            else:
                                body = b'{"status":"accepted"}'
                                self.send_response(202)

                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self._send_session_headers(minted_session)
                            self.end_headers()
                            self.wfile.write(body)
                        return

                    # Legacy HTTP+SSE transport: POST / pairs with GET /sse.
                    # Per the legacy spec, responses are delivered via the SSE
                    # stream; we also echo on the HTTP reply for REST-style
                    # clients that don't open an SSE channel.
                    response = server_instance._handle_jsonrpc_payload(
                        request, session_id=session_id, headers=self.headers
                    )

                    if response:
                        server_instance._broadcast(response, session_id=session_id)
                        body = json.dumps(response).encode("utf-8")
                        self.send_response(
                            server_instance._http_status_for(response))
                    else:
                        body = b'{"status":"accepted"}'
                        self.send_response(202)

                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self._send_session_headers(minted_session)
                    self.end_headers()
                    self.wfile.write(body)

                except Exception as e:
                    print("Error handling POST: {}".format(e))
                    traceback.print_exc()
                    self.send_error(500, str(e))

            def _handle_direct_tool_call(self, tool_name, arguments):
                """Handle direct REST-style tool call (OpenAPI compatible)."""
                if tool_name not in server_instance._tools:
                    self.send_error(404, "Tool not found: {}".format(tool_name))
                    return

                # The body must be a JSON object whose keys are the tool's
                # named arguments. Arrays, strings, null, etc. are 400.
                if not isinstance(arguments, dict):
                    self._send_json_error(
                        400, "Request body must be a JSON object of tool arguments"
                    )
                    return

                handler = server_instance._tools[tool_name]["handler"]

                # Direct REST has no SSE channel, so server-to-client requests
                # (elicit/sample/list_roots/progress) cannot work here. Refuse
                # context-bearing tools with 409 and steer callers to /mcp.
                if server_instance._needs_context(handler):
                    self._send_json_error(
                        409,
                        (
                            "Tool '{}' requires an MCP session. "
                            "Call it via POST /mcp with an active SSE connection "
                            "instead of /tools/{}."
                        ).format(tool_name, tool_name),
                    )
                    return

                # Invoke the tool. TypeError here is most commonly bad kwargs
                # from the caller — surface as 400. Anything else is a tool
                # failure: 500 with a logged traceback.
                try:
                    result = server_instance._call_handler(handler, None, arguments)
                except TypeError as e:
                    self._send_json_error(400, str(e))
                    return
                except Exception as e:
                    self._send_json_error(500, str(e))
                    return

                # Shape and serialize the response. Failures here are server
                # bugs (non-serializable return value) — always 500.
                try:
                    if isinstance(result, ToolResult):
                        payload = result.to_dict()
                        if payload.get("isError"):
                            # Tool-domain failure (the tool itself reported an
                            # error) — 422 Unprocessable Entity rather than 500
                            # so we don't log a traceback for an expected case.
                            items = payload.get("content", [])
                            if len(items) == 1 and isinstance(items[0].get("text"), str):
                                msg = items[0]["text"]
                            else:
                                msg = "Tool returned isError"
                            self._send_json_error(422, msg, details=payload)
                            return
                        items = payload.get("content", [])
                        unwrapped = items[0]["text"] if len(items) == 1 else payload
                        body = json.dumps(
                            unwrapped if isinstance(unwrapped, dict) else {"result": unwrapped}
                        ).encode("utf-8")
                    elif isinstance(result, dict):
                        body = json.dumps(result).encode("utf-8")
                    else:
                        body = json.dumps({"result": str(result)}).encode("utf-8")
                except (TypeError, ValueError) as e:
                    self._send_json_error(500, "Failed to serialize tool result: {}".format(e))
                    return

                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_json_error(self, status, message, details=None):
                """Write a JSON error body with the given HTTP status.

                Logs a traceback when status >= 500 so server errors leave a
                trail without each callsite needing to remember.
                """
                if status >= 500:
                    traceback.print_exc()
                payload = {"error": message}
                if details is not None:
                    payload["details"] = details
                try:
                    error_body = json.dumps(payload).encode("utf-8")
                except (TypeError, ValueError):
                    # `details` carried a non-JSON-serializable object (e.g. a
                    # ToolResult containing a custom class). Drop it rather
                    # than losing the whole error response to a serialize bug.
                    error_body = json.dumps({"error": message}).encode("utf-8")
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error_body)))
                self.end_headers()
                self.wfile.write(error_body)

            def _handle_streamable_http_get(self):
                """Handle Streamable HTTP GET - returns SSE stream for async responses."""
                session_id = self._session_id() or self._new_session_id()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Mcp-Session-Id", session_id)
                self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
                self.end_headers()

                client_queue = _SSEQueue()
                server_instance._register_client(session_id, client_queue)
                print("Streamable HTTP client connected. Session: {} Total: {}".format(
                    session_id, server_instance._client_count()
                ))

                try:
                    # Send endpoint event per MCP Streamable HTTP spec
                    self.wfile.write(b"event: open\ndata: {}\n\n")
                    endpoint = self._endpoint_path("/mcp", session_id)
                    self.wfile.write("event: endpoint\ndata: {}\n\n".format(endpoint).encode("utf-8"))
                    self.wfile.flush()

                    self._sse_pump_loop(client_queue)
                except (BrokenPipeError, ConnectionResetError) as e:
                    print("Streamable HTTP client disconnected: {}".format(e))
                except Exception:
                    traceback.print_exc()
                finally:
                    server_instance._unregister_client(session_id, client_queue)

            def _handle_openapi(self):
                """Return OpenAPI schema for tool discovery."""
                tools_paths = {}
                for tool_name, tool_info in server_instance._tools.items():
                    # Skip context-requiring tools — they need an MCP session
                    # (server-to-client requests over SSE) and cannot be called
                    # via the stateless /tools/<name> REST endpoint.
                    if server_instance._needs_context(tool_info["handler"]):
                        continue
                    tool_def = tool_info["definition"]
                    schema = tool_def.inputSchema if hasattr(tool_def, 'inputSchema') else {}

                    tools_paths["/tools/{}".format(tool_name)] = {
                        "post": {
                            "operationId": tool_name,
                            "summary": tool_def.description if hasattr(tool_def, 'description') else tool_name,
                            "requestBody": {
                                "required": True,
                                "content": {
                                    "application/json": {
                                        "schema": schema
                                    }
                                }
                            },
                            "responses": {
                                "200": {
                                    "description": "Tool result",
                                    "content": {
                                        "application/json": {
                                            "schema": {"type": "object"}
                                        }
                                    }
                                }
                            }
                        }
                    }

                openapi = {
                    "openapi": "3.1.0",
                    "info": {
                        "title": server_instance.name,
                        "version": server_instance.version,
                        "description": "MCP Server exposing tools as OpenAPI endpoints"
                    },
                    "paths": {
                        "/mcp": {
                            "get": {
                                "operationId": "mcp_sse",
                                "summary": "MCP Streamable HTTP SSE endpoint",
                                "responses": {"200": {"description": "SSE stream"}}
                            },
                            "post": {
                                "operationId": "mcp_message",
                                "summary": "Send MCP JSON-RPC message",
                                "requestBody": {
                                    "content": {
                                        "application/json": {
                                            "schema": {"type": "object"}
                                        }
                                    }
                                },
                                "responses": {"200": {"description": "JSON-RPC response"}}
                            }
                        },
                        **tools_paths
                    }
                }

                body = json.dumps(openapi).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle_mcp_discovery(self):
                """Return MCP discovery document."""
                discovery = {
                    "mcpVersion": "2024-11-05",
                    "serverInfo": {
                        "name": server_instance.name,
                        "version": server_instance.version
                    },
                    "capabilities": server_instance._get_capabilities().to_dict(),
                    "transports": [
                        {"type": "sse", "endpoint": "/sse"},
                        {"type": "streamable-http", "endpoint": "/mcp"}
                    ]
                }

                body = json.dumps(discovery).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id")
                self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer((host, port), MCPRequestHandler)
        print("MCP Server '{}' v{} listening on {}:{}".format(self.name, self.version, host, port))
        print("  Tools: {}".format(len(self._tools)))
        print("  Resources: {}".format(len(self._resources)))
        print("  Prompts: {}".format(len(self._prompts)))
        print("Endpoints:")
        print("  SSE transport:        http://{}:{}/sse".format(host, port))
        print("  Streamable HTTP:      http://{}:{}/mcp".format(host, port))
        print("  OpenAPI schema:       http://{}:{}/openapi.json".format(host, port))
        print("  MCP discovery:        http://{}:{}/.well-known/mcp.json".format(host, port))
        print("  Direct tool calls:    http://{}:{}/tools/<name>".format(host, port))

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")
            server.shutdown()
