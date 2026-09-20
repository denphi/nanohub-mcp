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
import inspect
import json
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime

try:
    from urllib.parse import unquote
except ImportError:  # pragma: no cover - Python 2 fallback
    from urllib import unquote

from typing import Any, Callable, Dict, List, Optional, Set

from .types import (
    Tool, Resource, ResourceTemplate, Prompt, TextContent, ImageContent,
    InputRequired,
    ToolResult, ResourceResult, ResourceContent,
    PromptResult, Message, Role,
    ServerCapabilities, ServerInfo
)
from .decorators import tool, async_tool, resource, prompt
from .context import Context
# Re-exported, not merely imported: `_SSEQueue`, `ThreadingHTTPServer` and
# `SSE_HEARTBEAT_INTERVAL` were defined here before the transport moved to its
# own module, and existing code imports them from `nanohubmcp.server`.
from .transport import (  # noqa: F401
    MCPRequestHandler,
    ThreadingHTTPServer,
    SSE_HEARTBEAT_INTERVAL,
    _SSEQueue,
)
from .protocol import MCP_SUBSCRIPTION_ID_KEY  # noqa: F401
from . import skills as _skills
from . import tasks as _tasks
# Used by _get_capabilities, which stays here; the rest of the tasks
# constants are read only by the module that now owns them.
from .tasks import MCP_TASKS_EXTENSION_ID  # noqa: F401
# Re-exported for the same reason as the transport names above: these were
# defined here before the skills registry moved out, and are imported from
# `nanohubmcp.server` by existing code and tests.
from .skills import (  # noqa: F401
    MCP_SKILLS_EXTENSION_ID,
    SKILL_MAX_RESOURCES,
    SKILL_MAX_TOTAL_BYTES,
    SKILL_URI_SCHEME,
    _guess_skill_mime_type,
    _parse_skill_frontmatter,
)


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

# "The thing you named does not exist" is Invalid Params, not Method Not Found:
# `tools/call` exists, it is the `name` inside it that does not. The spec says
# so outright for tools ("Unknown tool: ..." with -32602) and, since
# 2026-07-28, for resources too.
ERR_NOT_FOUND = -32602
# Resources kept their own code until 2026-07-28 renumbered them onto -32602.
# Pre-2026 clients match on -32002, so each revision gets the one it knows.
ERR_RESOURCE_NOT_FOUND_LEGACY = -32002

# RFC 5424 severities, lowest first. `logging/setLevel` rejects anything else,
# and the order is the threshold comparison.
LOG_LEVELS = ("debug", "info", "notice", "warning", "error",
              "critical", "alert", "emergency")

# How long a session with no open stream survives before it is collected.
# A client that only ever POSTs never disconnects anything, so without this
# its negotiated capabilities would sit in `_sessions` for the life of the
# process. Refreshed on every request the session makes.
SESSION_IDLE_TIMEOUT_SECONDS = 60 * 60

# Freshness hints for CacheableResult (tools/list, prompts/list, resources/*).
CACHEABLE_TTL_MS = 60 * 1000
CACHEABLE_SCOPE = "private"


# A client picks its own subscription ids (they are its JSON-RPC request ids),
# so the count has to be bounded somewhere. Far above any real use.
MAX_SUBSCRIPTIONS_PER_SESSION = 64


class _Sentinel(object):
    """A distinguishable stand-in for 'this is not a result'."""

    def __init__(self, name):
        # type: (str) -> None
        self.name = name

    def __repr__(self):
        # type: () -> str
        return self.name


# Returned by a handler that answers nothing at all: a notification, or the
# long-lived listen stream whose response is sent on teardown instead.
_NO_RESPONSE = _Sentinel("_NO_RESPONSE")
# Returned by a handler that has already built the whole JSON-RPC envelope and
# must not have the usual result/error finalization applied on top of it.
_RAW_RESPONSE = _Sentinel("_RAW_RESPONSE")
# Distinguishes "this tool produced no structured result" from one that
# produced `null`, which a 2026-07-28 outputSchema may well permit.
_NO_STRUCTURED = _Sentinel("_NO_STRUCTURED")


class _RequestContext(object):
    """Everything one JSON-RPC request resolved to before dispatch.

    These were locals of `_handle_request` while every method was handled in
    a single 664-line if/elif chain. They are passed to the per-method
    handlers unchanged, so each handler reads the same values the chain did.
    """

    __slots__ = ("request", "method", "msg_id", "params", "session_id",
                 "headers", "version", "modern", "is_notification", "meta",
                 "progress_token")

    def __init__(self, request=None, method="", msg_id=None, params=None,
                 session_id=None, headers=None, version="", modern=False,
                 is_notification=False, meta=None, progress_token=None):
        # type: (...) -> None
        self.request = request
        self.method = method
        self.msg_id = msg_id
        self.params = params if params is not None else {}
        self.session_id = session_id
        self.headers = headers
        self.version = version
        self.modern = modern
        self.is_notification = is_notification
        self.meta = meta
        self.progress_token = progress_token


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






# Cap on request bodies. Anything larger gets a 413 without being read into
# memory. Generous default for tool payloads but bounds worst-case allocation.
MAX_REQUEST_BYTES = 16 * 1024 * 1024  # 16 MiB





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
        # Resources whose URI carries {placeholders}. Kept apart from
        # `_resources` because a template names no single resource: it
        # belongs in resources/templates/list, and resources/read has to
        # match against it rather than look it up.
        self._resource_templates = {}  # type: Dict[str, Dict[str, Any]]
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
        # session_id -> {uri} for the handshake-era `resources/subscribe`.
        # Kept apart from `_subscriptions` because it carries no subscription
        # id: those revisions send `notifications/resources/updated` bare,
        # with nothing in `_meta` to correlate it against.
        self._resource_subs = {}  # type: Dict[str, Set[str]]
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
        # Both are overwritten by run() from its arguments. They are defaulted
        # here because MCPRequestHandler reads them off the server, and the
        # handler is now constructible without run() ever being called.
        self._require_session_header = False  # type: bool
        self._max_request_bytes = MAX_REQUEST_BYTES  # type: int
        # job_id -> {"status": "running"|"done"|"error", "result": Any}
        self._jobs = {}  # type: Dict[str, Dict[str, Any]]
        self._jobs_lock = threading.Lock()
        # get_job_result is registered lazily the first time an async tool is
        # registered, so servers without any async tools don't advertise it.
        self._job_polling_registered = False  # type: bool

    def _drop_subscriptions(self, session_id):
        # type: (str) -> None
        """Forget a session's subscriptions.

        An abrupt transport close carries no `subscriptions/listen` response
        per the spec, so this just releases the state.
        """
        with self._subs_lock:
            self._subscriptions.pop(session_id, None)
            self._resource_subs.pop(session_id, None)

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


    @staticmethod
    def _utc_now():
        # type: () -> str
        """Return an MCP-friendly UTC timestamp."""
        return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


    def _declared_output_schema(self, tool_name):
        # type: (Any) -> Optional[Dict[str, Any]]
        """The `outputSchema` a tool published, or None."""
        entry = self._tools.get(tool_name) if isinstance(tool_name, str) else None
        schema = getattr((entry or {}).get("definition"), "outputSchema", None)
        return schema if isinstance(schema, dict) and schema else None

    @staticmethod
    def _structured_content_fits(value, version):
        # type: (Any, str) -> bool
        """Whether this revision's `structuredContent` can carry `value`.

        Through 2025-11-25 the field is typed as an object; SEP-2106
        (2026-07-28) loosened it to any JSON value. Sending an array to an
        older client produces a `CallToolResult` that revision's own schema
        rejects, so the data rides in the serialized-JSON text block instead.
        """
        return isinstance(value, dict) or MCPServer._is_stateless(version)

    def _structured_candidate(self, value):
        # type: (Any) -> Any
        """The value a tool's `outputSchema` describes, or `_NO_STRUCTURED`.

        Read from the handler's return value *before* shaping, so a tool is
        held to its contract even where the negotiated revision cannot carry
        the result in `structuredContent`.
        """
        if isinstance(value, ToolResult):
            return (value.structured_content if value.has_structured_content
                    else _NO_STRUCTURED)
        if self._is_tool_result_payload(value):
            return value.get("structuredContent", _NO_STRUCTURED)
        # A plain return value *is* the structured result.
        return value

    def _shape_tool_result(self, tool_name, value, version):
        # type: (Optional[str], Any, str) -> Dict[str, Any]
        """Turn a handler's return value into a CallToolResult payload.

        A dict becomes `structuredContent` plus the serialized JSON the spec
        asks for as a backwards-compatibility text block. Anything else is
        text — *unless* the tool published an `outputSchema`, in which case
        the spec requires structured content, and withholding it would leave
        the result unable to satisfy the schema.

        `structuredContent` is attached only where the revision can carry it;
        see :meth:`_structured_content_fits`.
        """
        declares_schema = self._declared_output_schema(tool_name) is not None

        if isinstance(value, dict) or declares_schema:
            try:
                text = json.dumps(value)
            except (TypeError, ValueError):
                # Not JSON at all: fall through to the text form, where
                # `_apply_output_schema` will report the contract breach.
                return {"content": [{"type": "text", "text": str(value)}],
                        "isError": False}
            shaped = {"content": [{"type": "text", "text": text}],
                      "isError": False}
            if self._structured_content_fits(value, version):
                shaped["structuredContent"] = value
            return shaped
        return {
            "content": [{"type": "text", "text": str(value)}],
            "isError": False,
        }

    def _fit_structured_content(self, payload, version):
        # type: (Dict[str, Any], str) -> Dict[str, Any]
        """Drop a `structuredContent` this revision cannot represent."""
        if ("structuredContent" in payload
                and not self._structured_content_fits(
                    payload["structuredContent"], version)):
            payload = dict(payload)
            payload.pop("structuredContent", None)
        return payload

    def _tool_result_payload(self, value, tool_name=None, version=None):
        # type: (Any, Optional[str], Optional[str]) -> Dict[str, Any]
        """Wrap a stored async-tool value as a CallToolResult payload."""
        version = version or DEFAULT_NEGOTIATED_VERSION
        if isinstance(value, ToolResult):
            payload = self._fit_structured_content(value.to_dict(), version)
        elif self._is_tool_result_payload(value):
            payload = self._fit_structured_content(value, version)
        else:
            payload = self._shape_tool_result(tool_name, value, version)
        return self._apply_output_schema(tool_name, payload, value)

    # The JSON Schema keywords `_schema_violation` understands. Anything else
    # in an outputSchema is ignored rather than guessed at — see below.
    _JSON_TYPES = {
        "object": dict, "array": list, "string": str,
        "boolean": bool, "null": type(None),
    }

    @classmethod
    def _schema_violation(cls, schema, value, path="$"):
        # type: (Any, Any, str) -> Optional[str]
        """Describe how `value` fails `schema`, or None if it passes.

        A deliberately small subset of JSON Schema: `type`, `required`,
        `properties`, `items`, and `enum`. That is what tool output schemas
        are made of in practice, and it is enough to catch the failure this
        exists for — a handler whose return value does not match the shape
        the server published in `tools/list`.

        Unknown keywords are *ignored*, never guessed at. A validator that
        invents semantics for `allOf` would reject conforming results, which
        is worse than letting an exotic schema through unchecked. Servers
        wanting full 2020-12 validation should validate inside the handler.
        """
        if not isinstance(schema, dict):
            return None

        expected = schema.get("type")
        if isinstance(expected, str):
            expected = [expected]
        if isinstance(expected, list) and expected:
            if not any(cls._matches_type(name, value) for name in expected):
                return "{} should be {}, got {}".format(
                    path, "/".join(expected), type(value).__name__)

        if isinstance(value, dict):
            for key in schema.get("required") or ():
                if isinstance(key, str) and key not in value:
                    return "{} is missing required property {!r}".format(path, key)
            properties = schema.get("properties")
            if isinstance(properties, dict):
                for key, subschema in properties.items():
                    if key in value:
                        found = cls._schema_violation(
                            subschema, value[key], "{}.{}".format(path, key))
                        if found:
                            return found

        if isinstance(value, list):
            items = schema.get("items")
            # A list-form `items` is the 2019-09 tuple syntax; skip it rather
            # than apply the first subschema to every element.
            if isinstance(items, dict):
                for index, item in enumerate(value):
                    found = cls._schema_violation(
                        items, item, "{}[{}]".format(path, index))
                    if found:
                        return found

        allowed = schema.get("enum")
        if isinstance(allowed, list) and allowed:
            if not any(cls._json_equal(item, value) for item in allowed):
                return "{} is not one of the permitted values".format(path)

        return None

    @staticmethod
    def _json_equal(a, b):
        # type: (Any, Any) -> bool
        """JSON equality, which is not Python's.

        `True == 1` in Python and not in JSON, so booleans only ever equal
        booleans. Numbers otherwise compare by value, so `1` matches `1.0`
        the way an `enum` says it should.
        """
        if isinstance(a, bool) != isinstance(b, bool):
            return False
        return bool(a == b)

    @staticmethod
    def _matches_type(name, value):
        # type: (str, Any) -> bool
        """Whether `value` is of the named JSON Schema type."""
        if name == "integer":
            # `True` is an int in Python and is not an integer in JSON.
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        expected = MCPServer._JSON_TYPES.get(name)
        if expected is None:
            # An unrecognized type name constrains nothing we can check.
            return True
        if expected is not bool and isinstance(value, bool):
            # Same asymmetry the other way: a bool is not a "string",
            # and `isinstance(True, int)` would have said otherwise.
            return False
        return isinstance(value, expected)

    def _missing_prompt_arguments(self, prompt_name, arguments):
        # type: (Any, Dict[str, Any]) -> List[str]
        """Required prompt arguments the caller left out."""
        entry = self._prompts.get(prompt_name) or {}
        definition = entry.get("definition")
        declared = getattr(definition, "arguments", None) or []
        return [spec["name"] for spec in declared
                if isinstance(spec, dict) and spec.get("required")
                and spec.get("name") not in arguments]

    def _input_schema_violation(self, tool_name, arguments):
        # type: (Any, Dict[str, Any]) -> Optional[str]
        """Check a call's arguments against the tool's published inputSchema.

        "Servers MUST validate all tool inputs." Without this the arguments
        went straight to the handler, and a missing or mistyped one came back
        as a Python ``TypeError`` wrapped in ``isError`` — which tells the
        model the tool *ran and failed*, when in fact the call never happened.
        That is a protocol error (-32602), and the message it used to carry
        leaked the handler's signature.

        Deliberately narrow: only the keywords `_schema_violation` handles,
        which are the ones the schema generator emits. A hand-written schema
        using richer JSON Schema is checked for what can be checked and
        otherwise left to the handler.
        """
        entry = self._tools.get(tool_name) or {}
        definition = entry.get("definition")
        schema = getattr(definition, "inputSchema", None)
        if not isinstance(schema, dict) or not schema:
            return None
        return self._schema_violation(schema, arguments, path="arguments")

    def _apply_output_schema(self, tool_name, result, call_result):
        # type: (Optional[str], Dict[str, Any], Any) -> Dict[str, Any]
        """Hold a tool's result to the `outputSchema` it published.

        The spec is a MUST: "Servers MUST provide structured results that
        conform to this schema." The server cannot invent a conforming result,
        so a handler that breaks its own contract is reported the way any
        other tool failure is — `isError`, with the reason — rather than
        shipping a payload that contradicts what `tools/list` promised.

        Validation reads the handler's own value rather than the shaped
        `structuredContent`, because that field is dropped when the
        negotiated revision cannot represent it. The tool is still held to
        its contract there; the result just carries the data as serialized
        JSON instead of as a field the client would reject.
        """
        schema = self._declared_output_schema(tool_name)
        if schema is None or not isinstance(result, dict) or result.get("isError"):
            return result

        structured = self._structured_candidate(call_result)
        if structured is _NO_STRUCTURED:
            return self._output_schema_error(
                tool_name,
                "returned no structured content, but declares an outputSchema")

        violation = self._schema_violation(schema, structured)
        if violation:
            return self._output_schema_error(
                tool_name, "returned structured content that violates its "
                           "outputSchema: {}".format(violation))
        return result

    @staticmethod
    def _output_schema_error(tool_name, detail):
        # type: (Optional[str], str) -> Dict[str, Any]
        """The CallToolResult for a tool that broke its own output contract."""
        message = "Tool '{}' {}".format(tool_name, detail)
        print("[ERROR] {}".format(message))
        return {"content": [{"type": "text", "text": message}], "isError": True}

    @staticmethod
    def _is_tool_result_payload(value):
        # type: (Any) -> bool
        """Return True for dicts that already look like CallToolResult."""
        return (
            isinstance(value, dict)
            and isinstance(value.get("content"), list)
            and "isError" in value
        )


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

    # Methods that also name one thing in `params.uri`, but for which no
    # revision requires the header: the skills extension postdates the
    # transport revision that defined these headers, and neither it nor the
    # base spec asks for one here. Demanding it would reject a conforming
    # client, so absence is fine — but a header that *is* sent is still
    # checked against the body. A gateway that routes or authorizes on
    # Mcp-Name is the reason the header exists, and letting a request through
    # whose body names a different skill than its header would make that
    # gateway a confused deputy.
    _MCP_NAME_OPTIONAL_SOURCES = {
        "skills/get": "uri",
        "resources/directory/read": "uri",
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
        # Required only for the methods a revision actually demands it of;
        # the rest are validated when sent and never missed when absent.
        name_header_required = name_field is not None
        if name_field is None:
            name_field = self._MCP_NAME_OPTIONAL_SOURCES.get(method)
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
            if required and name_header_required and body_name is not None:
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

        # Fallback: match by registered URI suffix (prefer longest match),
        # but only at a path boundary. A bare `endswith` also resolved
        # `https://elsewhere/config://settings` — and any other string
        # ending in a registered URI — to that resource, which is a wider
        # door than a proxy prefix needs.
        resource_uris = sorted(self._resources.keys(), key=len, reverse=True)
        for candidate in normalized_candidates:
            for resource_uri in resource_uris:
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
                    title=getattr(func, "_mcp_tool_title", None),
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

    # A `{name}` placeholder in a registered URI. RFC 6570 has richer
    # operators ({+var}, {?a,b}, …); this implementation handles the simple
    # string expansion the MCP examples use, and `_compile_uri_template`
    # refuses anything else rather than mis-expanding it.
    _TEMPLATE_VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

    @classmethod
    def _compile_uri_template(cls, template):
        # type: (str) -> Any
        """Build the regex that matches URIs against `template`.

        Placeholders match one path segment each — a `{path}` that swallowed
        slashes would make `file:///{path}` shadow every other file:// URI.
        """
        pattern = []
        index = 0
        seen = set()
        for match in cls._TEMPLATE_VAR.finditer(template):
            name = match.group(1)
            if name in seen:
                raise ValueError(
                    "Resource template {!r} repeats the variable {!r}".format(
                        template, name))
            seen.add(name)
            pattern.append(re.escape(template[index:match.start()]))
            pattern.append("(?P<{}>[^/]+)".format(name))
            index = match.end()
        pattern.append(re.escape(template[index:]))

        # Anything brace-shaped that was not a plain {name} is an operator
        # this does not implement. Failing loudly beats silently treating
        # `{+path}` as a literal and never matching anything.
        if "{" in cls._TEMPLATE_VAR.sub("", template):
            raise ValueError(
                "Resource template {!r} uses an unsupported RFC 6570 "
                "operator; only simple {{name}} expansion is handled".format(
                    template))
        return re.compile("^" + "".join(pattern) + "$")

    def _match_resource_template(self, uri):
        # type: (str) -> Optional[tuple]
        """Find the template `uri` instantiates, or None.

        Longest template first, so a more specific pattern wins over a
        catch-all that would also match it.
        """
        with self._registry_lock:
            candidates = sorted(self._resource_templates.items(),
                                key=lambda item: len(item[0]), reverse=True)
        for template, entry in candidates:
            found = entry["pattern"].match(uri)
            if found:
                return entry, found.groupdict()
        return None

    def _register_resource_function(self, func):
        # type: (Callable) -> None
        """Register a decorated resource function, at import or while serving."""
        uri = func._mcp_resource_uri
        common = dict(
            name=func._mcp_resource_name,
            description=func._mcp_resource_description,
            mimeType=func._mcp_resource_mime_type,
            title=getattr(func, "_mcp_resource_title", None),
            meta=getattr(func, "_mcp_resource_meta", None) or {},
            annotations=getattr(func, "_mcp_resource_annotations", None),
        )
        # Only a plain resource has a size; a template names no single one.
        resource_size = getattr(func, "_mcp_resource_size", None)

        if getattr(func, "_mcp_resource_is_template", False):
            # The decorator has documented template URIs since it was
            # written, but the server only ever stored them as literal
            # resources — listed under a URI no read could match, and
            # never offered from resources/templates/list.
            pattern = self._compile_uri_template(uri)
            with self._registry_lock:
                self._resource_templates[uri] = {
                    "definition": ResourceTemplate(uriTemplate=uri, **common),
                    "pattern": pattern,
                    "handler": func,
                }
            self._mark_dynamic("resources")
            return

        with self._registry_lock:
            replaced = uri in self._resources
            self._resources[uri] = {
                "definition": Resource(uri=uri, size=resource_size, **common),
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
                    arguments=func._mcp_prompt_arguments,
                    title=getattr(func, "_mcp_prompt_title", None),
                ),
                "handler": func
            }
        self._mark_dynamic("prompts")

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

    def _register_skill_directory(self, skill_path, directory):
        # type: (str, Any) -> None
        """Register a skill directory. See `nanohubmcp.skills`."""
        _skills.register_skill_directory(self, skill_path, directory)

    def tool(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None,  # type: Optional[Dict[str, Any]]
        input_schema=None,  # type: Optional[Dict[str, Any]]
        output_schema=None,  # type: Optional[Dict[str, Any]]
        annotations=None,  # type: Optional[Dict[str, Any]]
        title=None  # type: Optional[str]
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
                             annotations=annotations, title=title)(func)
            self._register_tool_function(decorated)
            return decorated

        if callable(name):
            func = name
            name = None
            return decorator(func)

        return decorator

    # ── Async tools / MCP Tasks ──────────────────────────────────────
    # Thin delegations to `nanohubmcp.tasks`. They stay methods because
    # `nanohubmcp.context`, the transport and the tests all reach this
    # machinery through the server object.

    def _register_get_job_result(self):
        """Auto-register the built-in get_job_result polling tool."""
        return _tasks.register_get_job_result(self)

    @staticmethod
    def _normalize_task_meta(metadata):
        """Namespace and validate task `_meta` keys per the MCP naming rules."""
        return _tasks.normalize_task_meta(metadata)

    def _notify_task_status(self, job_id):
        """Push `notifications/tasks` to sessions subscribed to this task."""
        return _tasks.notify_task_status(self, job_id)

    def _start_async_tool_job(
        self, handler, msg_id, arguments, session_id=None,
        progress_token=None, meta=None, prepare=None, protocol_version=None,
        tool_name=None
    ):
        """Spawn a background thread for an async tool; return a job_id immediately."""
        return _tasks.start_async_tool_job(
            self, handler, msg_id, arguments, session_id, progress_token, meta,
            prepare, protocol_version, tool_name)

    def _job_to_task(self, task_id, job, include_terminal_payload=True):
        """Convert an internal async-job record into an MCP Task object."""
        return _tasks.job_to_task(self, task_id, job, include_terminal_payload)

    def _task_access_error(self, task_id, job, session_id):
        """Return a JSON-RPC error when the caller cannot access a task."""
        return _tasks.task_access_error(self, task_id, job, session_id)

    def _task_await_input(self, job_id, requests, timeout):
        """Park an async worker in `input_required` until the client answers."""
        return _tasks.task_await_input(self, job_id, requests, timeout)

    def _task_deliver_input(self, job_id, responses):
        """Hand tasks/update answers to a parked worker and wake it."""
        return _tasks.task_deliver_input(self, job_id, responses)

    def _mrtr_load(self, request_state, session_id=None):
        """Answers already collected for this logical request."""
        return _tasks.mrtr_load(self, request_state, session_id)

    def _mrtr_save(self, request_state, responses, session_id=None):
        """Persist accumulated answers; return the state id to hand the client."""
        return _tasks.mrtr_save(self, request_state, responses, session_id)

    def _mrtr_discard(self, request_state):
        """Drop state once the request has finally completed or failed."""
        return _tasks.mrtr_discard(self, request_state)

    def _prune_expired_mrtr_states(self):
        """Expire abandoned round-trips so memory can't grow unbounded."""
        return _tasks.prune_expired_mrtr_states(self)

    def _prune_expired_jobs(self):
        """Remove task/job records whose retention window has elapsed."""
        return _tasks.prune_expired_jobs(self)

    def _client_supports_tasks(self, session_id=None, params=None):
        """Return True when a client opted into the MCP Tasks extension."""
        return _tasks.client_supports_tasks(self, session_id, params)

    # The MCP Tasks methods. Bodies live in `nanohubmcp.tasks`, beside the job
    # records they read.

    def _rpc_tasks_get(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `tasks/get` method."""
        return _tasks.rpc_tasks_get(self, ctx)

    def _rpc_tasks_update(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `tasks/update` method."""
        return _tasks.rpc_tasks_update(self, ctx)

    def _rpc_tasks_cancel(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `tasks/cancel` method."""
        return _tasks.rpc_tasks_cancel(self, ctx)

    def async_tool(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None,  # type: Optional[Dict[str, Any]]
        input_schema=None,  # type: Optional[Dict[str, Any]]
        output_schema=None,  # type: Optional[Dict[str, Any]]
        annotations=None,  # type: Optional[Dict[str, Any]]
        prepare=None,  # type: Optional[Callable]
        title=None  # type: Optional[str]
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
                                   prepare=prepare, title=title)(func)
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
        meta=None,  # type: Optional[Dict[str, Any]]
        title=None,  # type: Optional[str]
        annotations=None,  # type: Optional[Dict[str, Any]]
        size=None  # type: Optional[int]
    ):
        # type: (...) -> Callable
        """
        Decorator to register a resource on this server.
        Aligned with FastMCP @mcp.resource decorator.

        Args:
            uri: Resource URI (e.g., "file:///path" or "config://settings").
                A URI containing ``{placeholders}`` registers a *template*:
                it is served from ``resources/templates/list`` rather than
                ``resources/list``, and a ``resources/read`` whose uri
                matches the pattern calls the handler with the extracted
                values as keyword arguments. Each placeholder matches one
                path segment.
            name: Resource name (defaults to function name)
            description: Resource description (defaults to docstring)
            mime_type: MIME type of the resource content
            tags: Optional set of tags for categorization
            meta: Optional metadata dictionary
            title: Optional human-readable display name
            annotations: Optional audience / priority / lastModified hints
            size: Optional size in bytes, for a resource whose length is
                known ahead of the read. Ignored for a template.
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = resource(uri, name, description, mime_type, tags, meta,
                                 title, annotations, size)(func)
            self._register_resource_function(decorated)
            return decorated
        return decorator

    def prompt(
        self,
        name=None,  # type: Optional[str]
        description=None,  # type: Optional[str]
        tags=None,  # type: Optional[Set[str]]
        meta=None,  # type: Optional[Dict[str, Any]]
        title=None  # type: Optional[str]
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
            title: Optional human-readable display name
        """
        def decorator(func):
            # type: (Callable) -> Callable
            decorated = prompt(name, description, tags, meta, title)(func)
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

        The counterpart to `subscriptions/listen` with `resourceSubscriptions`
        (2026-07-28) and to `resources/subscribe` (every revision before it).
        Both kinds of subscriber are served here, so a server calls this once
        and does not care which revision anyone is speaking.

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
            legacy = [session_id
                      for session_id, uris in self._resource_subs.items()
                      if uri in uris]
        for session_id, sub_id in targets:
            self._broadcast({
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": uri,
                           "_meta": {MCP_SUBSCRIPTION_ID_KEY: sub_id}},
            }, session_id=session_id)
            sent += 1
        for session_id in legacy:
            # No subscriptionId: these revisions have no such concept, and a
            # `_meta` key they never defined would be noise on the wire.
            self._broadcast({
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": uri},
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
        """Unregister a resource, or a template by its URI template.

        Returns True if one was removed.
        """
        with self._registry_lock:
            removed = (self._resources.pop(uri, None) is not None
                       or self._resource_templates.pop(uri, None) is not None)
        if removed:
            # Any subscription to it is now a promise about something that
            # no longer exists, so drop it rather than keep watching a name.
            with self._subs_lock:
                for uris in list(self._resource_subs.values()):
                    uris.discard(uri)
                for subs in self._subscriptions.values():
                    for sub in (subs or {}).values():
                        watched = sub.get("resource_uris")
                        if watched:
                            watched.discard(uri)
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
            resources=(len(self._resources) > 0
                       or len(self._resource_templates) > 0
                       or len(self._skills) > 0),
            prompts=len(self._prompts) > 0,
            logging=True,
            extensions=extensions,
            list_changed=self._dynamic_registry,
            # Both mechanisms are implemented: `resources/subscribe` for the
            # handshake revisions and `subscriptions/listen` with
            # `resourceSubscriptions` for 2026-07-28. Templates count: their
            # instances are readable, and so subscribable.
            subscribe=(len(self._resources) > 0
                       or len(self._resource_templates) > 0
                       or len(self._skill_resources) > 0),
        )

    def _has_mcp_app_resources(self):
        # type: () -> bool
        """True if any registered resource is an MCP App (ui:// HTML template)."""
        with self._registry_lock:
            entries = (list(self._resources.values())
                       + list(self._resource_templates.values()))
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
            session["last_seen"] = time.time()

    def _set_session_protocol_version(self, session_id, version):
        # type: (Optional[str], str) -> None
        """Remember the revision a handshake-era session negotiated."""
        if not session_id:
            return
        with self._sessions_lock:
            session = self._sessions.setdefault(session_id, {})
            session["protocol_version"] = version
            session["last_seen"] = time.time()

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

    def _batch_allowed(self, session_id):
        # type: (Optional[str]) -> bool
        """Whether this caller may send a JSON-RPC batch.

        2025-06-18 removed batching: from that revision on the POST body
        "MUST be a single JSON-RPC request, notification, or response". Only
        a session that negotiated 2024-11-05 still gets an array, and a
        caller with no session at all is assumed to be speaking something
        current — accepting a batch there and answering with an array would
        hand a modern client a shape its schema rejects.
        """
        if not session_id:
            return False
        with self._sessions_lock:
            session = self._sessions.get(session_id) or {}
        return session.get("protocol_version") == "2024-11-05"

    def _handle_jsonrpc_payload(self, payload, session_id=None, headers=None):
        # type: (Any, Optional[str], Optional[Any]) -> Optional[Any]
        """Handle a JSON-RPC message or batch payload."""
        if isinstance(payload, list):
            if not payload:
                return self._invalid_request(None, "JSON-RPC batch must not be empty")
            if not self._batch_allowed(session_id):
                return self._invalid_request(
                    None,
                    "JSON-RPC batching was removed in 2025-06-18; send one "
                    "message per request")
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
        # Stamp *before* sweeping: a session making a request right now is
        # by definition not idle, and pruning first collected the caller's
        # own session whenever it had been quiet up to this moment.
        self._touch_session(session_id)
        self._prune_expired_sessions()

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


        ctx = _RequestContext(
            request=request, method=method, msg_id=msg_id, params=params,
            session_id=session_id, headers=headers, version=version,
            modern=modern, is_notification=is_notification, meta=meta,
            progress_token=progress_token)

        result = None
        error = None

        try:
            handler_name = self._RPC_METHODS.get(method)
            if handler_name is None:
                error = {"code": -32601, "message": "Method not found: {}".format(method)}
            else:
                # Normally (result, error). The two sentinels claim the first
                # slot, and then the second carries whatever they need: nothing
                # for a notification, and a complete envelope for a handler
                # that built its own.
                outcome, payload = getattr(self, handler_name)(ctx)
                if outcome is _NO_RESPONSE:
                    return None
                if outcome is _RAW_RESPONSE:
                    # Already a whole JSON-RPC envelope; the postamble below
                    # would finalize a finalized response a second time.
                    return payload
                result, error = outcome, payload
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

    # JSON-RPC method -> the name of the method on this class that
    # handles it. Attribute names, not functions, so the table can sit
    # above the handlers it points at.
    _RPC_METHODS = {
        "initialize": "_rpc_initialize",
        # `notifications/initialized` is the name every revision through
        # 2025-11-25 actually puts on the wire. The bare "initialized" is
        # kept because clients in the wild send it.
        "notifications/initialized": "_rpc_initialized",
        "initialized": "_rpc_initialized",
        "notifications/cancelled": "_rpc_notifications_cancelled",
        "server/discover": "_rpc_server_discover",
        "ping": "_rpc_ping",
        "subscriptions/listen": "_rpc_subscriptions_listen",
        "tasks/get": "_rpc_tasks_get",
        "tasks/update": "_rpc_tasks_update",
        "tasks/cancel": "_rpc_tasks_cancel",
        "tools/list": "_rpc_tools_list",
        "tools/call": "_rpc_tools_call",
        "resources/list": "_rpc_resources_list",
        "resources/templates/list": "_rpc_resources_templates_list",
        "resources/read": "_rpc_resources_read",
        "resources/subscribe": "_rpc_resources_subscribe",
        "resources/unsubscribe": "_rpc_resources_unsubscribe",
        "prompts/list": "_rpc_prompts_list",
        "prompts/get": "_rpc_prompts_get",
        "skills/list": "_rpc_skills_list",
        "skills/get": "_rpc_skills_get",
        "resources/directory/read": "_rpc_resources_directory_read",
        "logging/setLevel": "_rpc_logging_setlevel",
    }

    def _rpc_initialize(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `initialize` method."""
        params, session_id = ctx.params, ctx.session_id
        result = None

        self._set_session_capabilities(session_id, params.get("capabilities", {}))
        negotiated = self._negotiate_protocol_version(
            params.get("protocolVersion")
        )
        self._set_session_protocol_version(session_id, negotiated)
        result = {
            "protocolVersion": negotiated,
            "serverInfo": ServerInfo(self.name, self.version).to_dict(),
            "capabilities": self._get_capabilities().to_dict(negotiated)
        }
        return result, None

    def _rpc_initialized(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `initialized` method."""
        return _NO_RESPONSE, None

    def _rpc_notifications_cancelled(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `notifications/cancelled` method."""
        params, session_id = ctx.params, ctx.session_id

        self._handle_cancelled(params, session_id)
        return _NO_RESPONSE, None

    def _rpc_server_discover(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `server/discover` method."""
        result = None

        result = self._cacheable({
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": self._get_capabilities().to_dict(
                PROTOCOL_2026_07_28),
        }, PROTOCOL_2026_07_28)
        if self.instructions:
            result["instructions"] = self.instructions
        # Answer in the new shape regardless of the caller's declared
        # version — the method only exists in 2026-07-28.
        result = self._finalize_result(result, PROTOCOL_2026_07_28)
        return result, None

    def _rpc_ping(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `ping` method."""
        version, modern = ctx.version, ctx.modern
        result = None
        error = None

        if modern:
            error = {"code": -32601,
                     "message": "Method not found: ping (removed in {})".format(version)}
        else:
            result = {}
        return result, error

    def _rpc_subscriptions_listen(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `subscriptions/listen` method."""
        msg_id, params = ctx.msg_id, ctx.params
        session_id = ctx.session_id
        error = None

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
                    return _RAW_RESPONSE, {"jsonrpc": "2.0", "id": msg_id, "error": error}

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
                return _NO_RESPONSE, None
        return None, error




    def _rpc_tools_list(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `tools/list` method."""
        params, version = ctx.params, ctx.version
        result = None

        with self._registry_lock:
            tool_dicts = [
                self._tools[name]["definition"].to_dict()
                for name in sorted(self._tools)
            ]
        result = self._cacheable(
            self._paginate(tool_dicts, params, "tools", {}, "name"), version)
        return result, None

    def _rpc_tools_call(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `tools/call` method."""
        msg_id, params, session_id = ctx.msg_id, ctx.params, ctx.session_id
        version, modern, meta = ctx.version, ctx.modern, ctx.meta
        progress_token = ctx.progress_token
        result = None
        error = None

        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        # `isinstance(tool_name, str)` first: a caller can send any JSON
        # value as `name`, and an unhashable one raises TypeError on the
        # `in` test before the validation below ever runs.
        tool_entry = (self._tools.get(tool_name)
                      if isinstance(tool_name, str) else None)
        bad_arguments = (
            self._input_schema_violation(tool_name, arguments)
            if tool_entry is not None and isinstance(arguments, dict) else None)

        if not isinstance(arguments, dict):
            error = {"code": -32602,
                     "message": "tools/call arguments must be a JSON object"}
        elif tool_entry is None:
            error = {"code": ERR_NOT_FOUND,
                     "message": "Unknown tool: {}".format(tool_name)}
        elif bad_arguments:
            error = {"code": -32602,
                     "message": "Invalid arguments for tool {}: {}".format(
                         tool_name, bad_arguments)}
        else:
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
                        tool_name=tool_name,
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
                        result = self._fit_structured_content(
                            call_result.to_dict(), version)
                    else:
                        result = self._shape_tool_result(
                            tool_name, call_result, version)
                    result = self._apply_output_schema(
                        tool_name, result, call_result)
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
        return result, error

    def _rpc_resources_list(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `resources/list` method."""
        params, version = ctx.params, ctx.version
        result = None

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
        return result, None

    def _rpc_resources_templates_list(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `resources/templates/list` method."""
        params, version = ctx.params, ctx.version

        with self._registry_lock:
            templates = [self._resource_templates[uri]["definition"].to_dict()
                         for uri in sorted(self._resource_templates)]
        result = self._cacheable(
            self._paginate(templates, params, "resourceTemplates", {},
                           "uriTemplate"),
            version)
        return result, None

    def _rpc_resources_read(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `resources/read` method."""
        msg_id, params, session_id = ctx.msg_id, ctx.params, ctx.session_id
        version, modern, meta = ctx.version, ctx.modern, ctx.meta
        progress_token = ctx.progress_token
        result = None
        error = None

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
            else:
                entry = self._resources.get(lookup_uri)
                template_args = {}
                if entry is None:
                    # Not a literal resource — it may still instantiate a
                    # registered template, which is the only way a
                    # `{placeholder}` URI is ever readable.
                    matched = self._match_resource_template(lookup_uri)
                    if matched is not None:
                        entry, template_args = matched

                if entry is None:
                    # 2026-07-28 aligns resource-not-found with JSON-RPC's
                    # Invalid Params; older clients keep the code they know.
                    return None, {
                        "code": (ERR_NOT_FOUND if modern
                                 else ERR_RESOURCE_NOT_FOUND_LEGACY),
                        "message": "Resource not found",
                        "data": {"uri": uri},
                    }

                handler = entry["handler"]
                definition = entry["definition"]
                res_mime = getattr(definition, "mimeType", None)
                res_meta = getattr(definition, "meta", None) or {}
                try:
                    content = self._call_handler(
                        handler, msg_id,
                        arguments=template_args or None,
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
        return result, error

    def _subscribable_uri(self, ctx):
        # type: (_RequestContext) -> tuple
        """Resolve params.uri for resources/(un)subscribe, or an error.

        Returns ``(uri, None)`` or ``(None, error)``.
        """
        if ctx.modern:
            # 2026-07-28 replaced both methods with `subscriptions/listen`.
            return None, {
                "code": -32601,
                "message": "Method not found: {} (removed in {}; use "
                           "subscriptions/listen)".format(ctx.method, ctx.version),
            }
        uri = ctx.params.get("uri")
        if not isinstance(uri, str) or not uri:
            return None, {"code": -32602,
                          "message": "{} requires a string uri".format(ctx.method)}
        if not ctx.session_id:
            # Without a session there is no stream to deliver updates on, so
            # accepting the subscription would promise something undeliverable.
            return None, {"code": -32602,
                          "message": "{} requires an MCP session".format(ctx.method)}
        return self._strip_proxy_prefix(uri), None

    def _resource_exists(self, uri):
        # type: (str) -> bool
        """Whether `resources/read` would serve this URI.

        Templates are consulted, not just the literal registries: a URI that
        instantiates a registered template is readable, and subscribe and
        read disagreeing about whether the same URI exists is a worse answer
        than either one alone.
        """
        with self._registry_lock:
            if uri in self._resources or uri in self._skill_resources:
                return True
        return self._match_resource_template(uri) is not None

    def _rpc_resources_subscribe(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `resources/subscribe` method."""
        uri, error = self._subscribable_uri(ctx)
        if error is not None:
            return None, error

        if not self._resource_exists(uri):
            return None, {"code": ERR_RESOURCE_NOT_FOUND_LEGACY,
                          "message": "Resource not found",
                          "data": {"uri": ctx.params.get("uri")}}

        with self._subs_lock:
            uris = self._resource_subs.setdefault(ctx.session_id, set())
            if uri not in uris and len(uris) >= MAX_SUBSCRIPTIONS_PER_SESSION:
                return None, {
                    "code": -32602,
                    "message": "Too many resource subscriptions for this "
                               "session (max {})".format(
                                   MAX_SUBSCRIPTIONS_PER_SESSION),
                }
            uris.add(uri)
        return {}, None

    def _rpc_resources_unsubscribe(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `resources/unsubscribe` method."""
        uri, error = self._subscribable_uri(ctx)
        if error is not None:
            return None, error

        # Unsubscribing from something not subscribed is not an error: the
        # client's intent — "stop sending me this" — already holds.
        with self._subs_lock:
            uris = self._resource_subs.get(ctx.session_id)
            if uris:
                uris.discard(uri)
                if not uris:
                    del self._resource_subs[ctx.session_id]
        return {}, None

    def _rpc_prompts_list(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `prompts/list` method."""
        params, version = ctx.params, ctx.version
        result = None

        with self._registry_lock:
            prompt_dicts = [self._prompts[name]["definition"].to_dict()
                            for name in sorted(self._prompts)]
        result = self._cacheable(
            self._paginate(prompt_dicts, params, "prompts", {}, "name"),
            version)
        return result, None

    def _rpc_prompts_get(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `prompts/get` method."""
        msg_id, params, session_id = ctx.msg_id, ctx.params, ctx.session_id
        meta, progress_token = ctx.meta, ctx.progress_token
        result = None
        error = None

        prompt_name = params.get("name")
        arguments = params.get("arguments", {})
        missing_arguments = (
            self._missing_prompt_arguments(prompt_name, arguments)
            if (isinstance(prompt_name, str) and prompt_name in self._prompts
                and isinstance(arguments, dict)) else [])

        if not isinstance(prompt_name, str) or not prompt_name:
            error = {"code": -32602,
                     "message": "prompts/get requires a string name"}
        elif not isinstance(arguments, dict):
            error = {"code": -32602,
                     "message": "prompts/get arguments must be a JSON object"}
        elif prompt_name not in self._prompts:
            error = {"code": ERR_NOT_FOUND,
                     "message": "Unknown prompt: {}".format(prompt_name)}
        elif missing_arguments:
            # Omitting a required argument reached the handler and raised a
            # Python TypeError, reported as -32603 — blaming the server for
            # the caller's incomplete request, and leaking the signature.
            missing = missing_arguments
            error = {
                "code": -32602,
                "message": "prompts/get is missing required argument(s) for "
                           "{}: {}".format(prompt_name, ", ".join(missing)),
            }
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
        return result, error

    # The three SEP-2640 methods. Their bodies live in `nanohubmcp.skills`
    # alongside the registry they read; these keep the dispatch table's
    # "one method on this class per JSON-RPC method" shape intact.

    def _rpc_skills_list(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `skills/list` method."""
        return _skills.rpc_skills_list(self, ctx)

    def _rpc_skills_get(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `skills/get` method."""
        return _skills.rpc_skills_get(self, ctx)

    def _rpc_resources_directory_read(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `resources/directory/read` method."""
        return _skills.rpc_resources_directory_read(self, ctx)

    def _rpc_logging_setlevel(self, ctx):
        # type: (_RequestContext) -> tuple
        """Handle the JSON-RPC `logging/setLevel` method."""
        version, modern = ctx.version, ctx.modern
        result = None
        error = None

        if modern:
            # Removed: log level is per-request via _meta instead.
            error = {"code": -32601,
                     "message": "Method not found: logging/setLevel "
                                "(removed in {}; use _meta {})".format(
                                    version, META_LOG_LEVEL)}
        else:
            level = ctx.params.get("level")
            if level not in LOG_LEVELS:
                # The spec names -32602 for this, and accepting an unknown
                # level would silently set a threshold nothing compares to.
                error = {"code": -32602,
                         "message": "Invalid log level {!r}; expected one of: "
                                    "{}".format(level, ", ".join(LOG_LEVELS))}
            elif not ctx.session_id:
                # The level is session state; with no session there is
                # nowhere to keep it and nowhere to send the logs.
                error = {"code": -32602,
                         "message": "logging/setLevel requires an MCP session"}
            else:
                with self._sessions_lock:
                    session = self._sessions.setdefault(ctx.session_id, {})
                    session["log_level"] = level
                    session["last_seen"] = time.time()
                result = {}
        return result, error

    def _session_log_level(self, session_id):
        # type: (Optional[str]) -> Optional[str]
        """The level this session last asked for via `logging/setLevel`."""
        if not session_id:
            return None
        with self._sessions_lock:
            session = self._sessions.get(session_id) or {}
        level = session.get("log_level")
        return level if level in LOG_LEVELS else None



    def _register_client(self, session_id, client_queue):
        # type: (str, _SSEQueue) -> None
        """Register an SSE queue for a client session."""
        with self._clients_lock:
            self._clients.setdefault(session_id, []).append(client_queue)

    def _unregister_client(self, session_id, client_queue):
        # type: (str, _SSEQueue) -> None
        """Remove an SSE queue for a client session.

        When the last queue goes, everything that *needed* that stream goes
        with it: subscriptions, and any server-to-client request still
        waiting for an answer that can no longer arrive.

        What survives is the session itself. Closing the notification stream
        is not the same as ending the session — a client may keep POSTing
        without one — and dropping its negotiated capabilities here would
        make the next request 404 and force a pointless re-initialize.
        Idle sessions are collected by `_prune_expired_sessions` instead,
        and a client that really is finished says so with `DELETE`.
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

        self._drop_subscriptions(session_id)

        prefix = "{}:".format(session_id)
        with self._pending_lock:
            stale_keys = [k for k in self._pending_client_requests if k.startswith(prefix)]
            for key in stale_keys:
                pending = self._pending_client_requests.pop(key, None)
                if pending and "event" in pending:
                    # Wake any waiter so it raises instead of timing out
                    pending["event"].set()

    def _touch_session(self, session_id):
        # type: (Optional[str]) -> None
        """Mark a session as used now, deferring its idle expiry."""
        if not session_id:
            return
        with self._sessions_lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session["last_seen"] = time.time()

    def session_exists(self, session_id):
        # type: (Optional[str]) -> bool
        """Whether this id names a session the server is still holding.

        A session exists while it has negotiated state or an open stream.
        The transport asks so it can answer 404 for one that has since been
        terminated or expired, which is how a client learns to re-initialize.
        """
        if not session_id:
            return False
        with self._sessions_lock:
            if session_id in self._sessions:
                return True
        with self._clients_lock:
            return bool(self._clients.get(session_id))

    def terminate_session(self, session_id):
        # type: (Optional[str]) -> bool
        """Forget a session and everything scoped to it. True if one existed.

        The explicit counterpart to an SSE disconnect, reached by `DELETE`
        on the MCP endpoint. Open streams are left to notice on their own —
        closing a socket from another thread is not something this transport
        can do safely — but they are unregistered, so nothing further is
        queued for them.
        """
        if not session_id:
            return False
        existed = self.session_exists(session_id)
        if not existed:
            return False

        with self._clients_lock:
            self._clients.pop(session_id, None)
        with self._sessions_lock:
            self._sessions.pop(session_id, None)
        self._release_session_scoped_state(session_id)
        return True

    def _release_session_scoped_state(self, session_id):
        # type: (str) -> None
        """Drop the subscriptions and waiters that a session owned."""
        self._drop_subscriptions(session_id)
        prefix = "{}:".format(session_id)
        with self._pending_lock:
            for key in [k for k in self._pending_client_requests
                        if k.startswith(prefix)]:
                pending = self._pending_client_requests.pop(key, None)
                if pending and "event" in pending:
                    # Wake any waiter so it raises instead of timing out.
                    pending["event"].set()

    def _prune_expired_sessions(self):
        # type: () -> None
        """Collect sessions that have gone quiet and hold no stream.

        `_unregister_client` only fires when an SSE stream drops, so a
        client that exclusively POSTs never triggered it: its negotiated
        capabilities sat in `_sessions` for the life of the process. This
        is the sweep that bounds that, and it runs off the request path
        like the job and MRTR sweeps beside it.
        """
        cutoff = time.time() - SESSION_IDLE_TIMEOUT_SECONDS
        with self._clients_lock:
            # A session with a live stream is not idle, whatever its last
            # request looked like.
            streaming = {sid for sid, queues in self._clients.items() if queues}

        with self._sessions_lock:
            # Selected *and removed* under the one lock that owns
            # `last_seen`. Scanning here and deleting after releasing it
            # let a concurrent request refresh a session in between, and
            # the sweep collected it anyway — the client's next request
            # then 404'd although it had just been served.
            stale = [sid for sid, session in self._sessions.items()
                     if session.get("last_seen", 0) < cutoff
                     and sid not in streaming]
            for session_id in stale:
                del self._sessions[session_id]

        for session_id in stale:
            self._release_session_scoped_state(session_id)

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
        self._serving = True
        self._path_prefix = path_prefix.rstrip("/") if path_prefix else ""
        # Read by MCPRequestHandler through the server it is attached to,
        # rather than captured from this frame as they were when the handler
        # was declared inside this method.
        self._require_session_header = bool(require_session_header)
        self._max_request_bytes = int(max_request_bytes)
        self._require_route_headers = (
            "auto" if require_route_headers == "auto" else bool(require_route_headers))
        self._allowed_origins = (
            None if allowed_origins is None
            else {str(o).rstrip("/").lower() for o in allowed_origins})

        if allowed_origins is None and host not in ("127.0.0.1", "localhost", "::1"):
            # The transport spec makes Origin validation a MUST, precisely
            # to block DNS rebinding — and a wildcard bind is the exposure
            # that attack needs. A library cannot guess the legitimate
            # origins, so it says so loudly instead of failing silently.
            print("WARNING: no allowed_origins set while bound to {}. Any web "
                  "page can reach this server from a victim's browser. Pass "
                  "allowed_origins=[...] to run(), or bind to 127.0.0.1."
                  .format(host), file=sys.stderr)

        server = ThreadingHTTPServer((host, port), MCPRequestHandler)
        # Set before serve_forever(), so no handler can be constructed without it.
        server.mcp_server = self
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
