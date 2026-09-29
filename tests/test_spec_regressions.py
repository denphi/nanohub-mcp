"""Spec conformance defects found by review, pinned so they stay fixed.

Each test names the requirement it enforces and, in its docstring, the
behaviour that violated it. They are gathered here rather than scattered
because what they have in common is that the server *answered* in every case —
nothing crashed, nothing failed a test, the answer was simply not the one the
spec asks for.
"""

from __future__ import print_function

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanohubmcp import MCPServer  # noqa: E402
from nanohubmcp.server import _SSEQueue  # noqa: E402


def _methods(queue):
    """The JSON-RPC methods queued on an SSE stream, in order."""
    import json
    out = []
    for raw in queue:
        message = json.loads(raw)
        if message.get("method"):
            out.append(message["method"])
    return out


# ---------------------------------------------------------------------------
# "The server MUST send each of its JSON-RPC messages on only one of the
#  connected streams; that is, it MUST NOT broadcast the same message across
#  multiple streams." -- Streamable HTTP transport
# ---------------------------------------------------------------------------

def test_a_message_goes_to_one_stream_even_when_a_session_has_several():
    """Every notification used to be queued on every stream of the session.

    A session routinely has more than one: a GET stream plus the POST stream
    held open for `subscriptions/listen` register under the same id. A
    server-to-client *request* went to both, so one `elicitation/create`
    prompted the user twice and was answered twice with the same id.
    """
    server = MCPServer("fanout")
    first, second = _SSEQueue(), _SSEQueue()
    server._register_client("S", first)
    server._register_client("S", second)

    server._broadcast({"jsonrpc": "2.0", "method": "notifications/message",
                       "params": {}}, session_id="S")

    assert len(first) + len(second) == 1


def test_a_subscriptions_notification_goes_to_the_stream_that_opened_it():
    """Not merely to *a* stream: to the one holding that subscription open.

    `subscriptions/listen` is answered by keeping its POST open as the stream,
    so its notifications belong there and nowhere else.
    """
    server = MCPServer("routed")
    server._serving = True
    getstream, listenstream = _SSEQueue(), _SSEQueue()
    server._register_client("S", getstream)
    server._register_client("S", listenstream)

    server._handle_request(
        {"jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen",
         "params": {"notifications": {"toolsListChanged": True},
                    "_meta": {"io.modelcontextprotocol/protocolVersion":
                              "2026-07-28"}}},
        session_id="S", stream=listenstream)

    @server.tool()
    def added():
        """Registered after start-up, so a list_changed goes out."""
        return 1

    assert _methods(getstream) == []
    assert _methods(listenstream) == [
        "notifications/subscriptions/acknowledged",
        "notifications/tools/list_changed",
    ]


# ---------------------------------------------------------------------------
# "Unlike base JSON-RPC, the ID MUST NOT be `null`." -- base protocol
# ---------------------------------------------------------------------------

def test_an_explicitly_null_id_is_rejected_not_run_as_a_notification():
    """`msg_id is None` conflated "no id" with "id: null".

    A `tools/call` carrying `"id": null` therefore ran — side effects and all —
    and was answered with 202 and no body, so the caller never learned what
    happened.
    """
    server = MCPServer("nullid")
    ran = []

    @server.tool()
    def touch():
        """Has a side effect."""
        ran.append(1)
        return "done"

    response = server._handle_request(
        {"jsonrpc": "2.0", "id": None, "method": "tools/call",
         "params": {"name": "touch", "arguments": {}}})

    assert response["error"]["code"] == -32600
    assert response["id"] is None
    assert ran == [], "the tool must not run"

    # A genuinely absent id is still a notification, and still gets no reply.
    assert server._handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


# ---------------------------------------------------------------------------
# JSON-RPC 2.0: the `jsonrpc` member "MUST be exactly '2.0'"
# ---------------------------------------------------------------------------

def test_the_jsonrpc_version_member_is_enforced():
    """It was never read, so `"jsonrpc": "1.0"` — and no member at all —
    were both served as if they had said 2.0."""
    server = MCPServer("version")

    for request in ({"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
                    {"id": 1, "method": "tools/list"},
                    {"jsonrpc": 2.0, "id": 1, "method": "tools/list"}):
        response = server._handle_request(request)
        assert response["error"]["code"] == -32600, request

    assert "result" in server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


# ---------------------------------------------------------------------------
# "Servers MUST validate all tool inputs." -- tools
# ---------------------------------------------------------------------------

def test_an_unknown_argument_is_a_protocol_error_not_a_tool_failure():
    """It reached `handler(**arguments)` and died there with a TypeError.

    That was reported as `isError`, which tells the model the tool ran and
    failed when the call never happened, and the message leaked the handler's
    signature: "add() got an unexpected keyword argument 'zzz'".
    """
    server = MCPServer("strict")
    ran = []

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        ran.append(1)
        return a + b

    response = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "add", "arguments": {"a": 1, "b": 2, "zzz": 9}}})

    assert response["error"]["code"] == -32602
    assert "zzz" in response["error"]["message"]
    assert "unexpected keyword argument" not in response["error"]["message"]
    assert ran == []


def test_a_handler_taking_kwargs_accepts_anything():
    """Nothing is unexpected when the handler declared it would take it."""
    server = MCPServer("open")

    @server.tool()
    def anything(**kwargs):
        """Takes whatever it is given."""
        return sorted(kwargs)

    response = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "anything", "arguments": {"x": 1, "y": 2}}})

    assert response["result"]["isError"] is False


# ---------------------------------------------------------------------------
# "If the server receives a cursor it does not recognize, it SHOULD return
#  error -32602." -- pagination
# ---------------------------------------------------------------------------

def test_an_unrecognized_cursor_is_rejected_even_with_paging_off():
    """Paging is off by default, and `_paginate` returned before the cursor
    was ever looked at — so the careful rejection below it was unreachable in
    the configuration nearly every server runs, and a junk cursor silently
    returned page one."""
    server = MCPServer("nopaging")

    @server.tool()
    def only():
        """The one tool."""
        return 1

    response = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
         "params": {"cursor": "!!!"}})
    assert response["error"]["code"] == -32602

    # No cursor is still the whole list in one response, with none minted.
    listed = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
    assert len(listed["tools"]) == 1
    assert "nextCursor" not in listed


def test_a_cursor_this_server_minted_is_still_honoured():
    """The rejection above must not swallow real paging."""
    server = MCPServer("paging", list_page_size=1)

    @server.tool()
    def alpha():
        """First."""
        return 1

    @server.tool()
    def beta():
        """Second."""
        return 2

    first = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
    assert [t["name"] for t in first["tools"]] == ["alpha"]

    second = server._handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
         "params": {"cursor": first["nextCursor"]}})["result"]
    assert [t["name"] for t in second["tools"]] == ["beta"]


# ---------------------------------------------------------------------------
# Progress and the context log buffer
# ---------------------------------------------------------------------------

def test_progress_does_not_also_emit_a_logging_notification():
    """`report_progress` called `self.info(...)`, so every tick cost two
    notifications on the stream — a `notifications/message` about the
    `notifications/progress` sitting next to it."""
    server = MCPServer("progress")
    server._serving = True
    stream = _SSEQueue()
    server._register_client("S", stream)
    server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "logging/setLevel",
         "params": {"level": "debug"}}, session_id="S")

    @server.tool()
    def work(ctx=None):
        """Reports progress."""
        ctx.report_progress(1, 2, "half")
        return "ok"

    del stream[:]
    server._handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "work", "arguments": {},
                    "_meta": {"progressToken": "t"}}}, session_id="S")

    assert _methods(stream) == ["notifications/progress"]


def test_the_context_log_buffer_is_bounded():
    """It grew for the life of an async job while nothing but
    `get_log_messages()` ever read it."""
    from nanohubmcp.context import Context, MAX_BUFFERED_LOG_MESSAGES

    ctx = Context(server=None, request_id="r")
    for index in range(MAX_BUFFERED_LOG_MESSAGES + 50):
        ctx.info("line {}".format(index))

    kept = ctx.get_log_messages()
    assert len(kept) == MAX_BUFFERED_LOG_MESSAGES
    # The newest are the ones worth keeping.
    assert kept[-1]["message"].endswith(str(MAX_BUFFERED_LOG_MESSAGES + 49))


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------

def _write_skill(tmp_path, name="demo"):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: {}\ndescription: A demo skill\n---\n\n# Demo\n".format(name))
    return directory


def test_skill_can_be_registered_without_the_decorator_dance(tmp_path):
    """`server.skill("demo")` as a statement registered nothing and said
    nothing: `skills/list` came back empty and the extension was never
    advertised."""
    server = MCPServer("skills")
    server.skill("demo", _write_skill(tmp_path))

    listed = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "skills/list"})["result"]
    assert [s["uri"] for s in listed["skills"]] == ["skill://demo/SKILL.md"]


def test_skill_used_as_a_decorator_still_works(tmp_path):
    server = MCPServer("skills")
    directory = _write_skill(tmp_path)

    @server.skill("demo")
    def _demo():
        return directory

    listed = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "skills/list"})["result"]
    assert [s["uri"] for s in listed["skills"]] == ["skill://demo/SKILL.md"]


def test_an_unapplied_skill_decorator_says_so(capsys):
    """Dropping it on the floor is the likeliest misuse; it must not be silent."""
    server = MCPServer("skills")
    server.skill("demo")          # never applied to anything
    import gc
    gc.collect()
    assert "never used as a decorator" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Second review pass
# ---------------------------------------------------------------------------

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}


def test_a_stateless_client_is_recognised_without_an_initialize():
    """2026-07-28 "MUST NOT send a notification type the client did not
    request" — but the only place the revision was ever recorded was
    `initialize`, which that revision removed. So the one client the guard
    exists for read as handshake-era and got the notifications anyway.
    """
    server = MCPServer("stateless")
    server._serving = True
    stream = _SSEQueue()
    server._register_client("stream-1", stream)     # a minted stream, no session

    server._handle_request(
        {"jsonrpc": "2.0", "id": "sub", "method": "subscriptions/listen",
         "params": {"notifications": {"taskIds": []}, "_meta": MODERN}},
        session_id="stream-1", stream=stream)
    del stream[:]

    @server.tool()
    def added():
        """Registered at runtime, and never subscribed to."""
        return 1

    assert _methods(stream) == []
    assert server._session_is_stateless("stream-1") is True


def test_a_subscription_dies_with_the_stream_that_opened_it():
    """It was dropped only when the session's *last* stream closed, so a
    closed `subscriptions/listen` left an entry behind holding a dead queue and
    every notification it matched was built and then silently discarded."""
    server = MCPServer("streams")
    server._serving = True
    getstream, listenstream = _SSEQueue(), _SSEQueue()
    server._register_client("S", getstream)
    server._register_client("S", listenstream)
    server._handle_request(
        {"jsonrpc": "2.0", "id": "sub", "method": "subscriptions/listen",
         "params": {"notifications": {"toolsListChanged": True}}},
        session_id="S", stream=listenstream)
    assert server._subscriptions["S"]

    server._unregister_client("S", listenstream)
    assert "S" not in server._subscriptions
    # The surviving stream is untouched and still a live client.
    assert server._clients["S"] == [getstream]


def test_the_context_parameter_is_not_an_argument_a_client_may_name():
    """It is injected by the server and never published in `inputSchema`, so a
    client naming it was naming something that is not an input — and the value
    was silently overwritten rather than refused."""
    server = MCPServer("ctx")

    @server.tool()
    def uses(a: int, ctx=None) -> int:
        """Takes a context."""
        return a

    response = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "uses", "arguments": {"a": 1, "ctx": "pwned"}}})
    assert response["error"]["code"] == -32602
    assert "ctx" in response["error"]["message"]


# ---------------------------------------------------------------------------
# The gateway prepends its own origin to resource URIs
# ---------------------------------------------------------------------------

def _gateway_server(tmp_path):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill\n---\n\n# Demo\n")

    server = MCPServer("gw")

    @server.resource("config://settings")
    def settings():
        """A literal resource."""
        return {"a": 1}

    @server.resource("weather://{city}/current")
    def weather(city):
        """A template."""
        return {"city": city}

    @server.resource("ui://tool/app", mime_type="text/html;profile=mcp-app")
    def app():
        """An MCP App."""
        return "<html></html>"

    server.skill("demo", skill_dir)
    return server


def test_gateway_prefixed_uris_are_readable_for_every_resource_kind(tmp_path):
    """com_mcp prepends its origin (`https://nanohub.org/ui://tool/app`).
    Literal resources were recovered; template instances and skill files were
    not, because the stripping only ever consulted `_resources`.
    """
    server = _gateway_server(tmp_path)
    prefix = "https://nanohub.org/"

    for uri in ("config://settings", "weather://paris/current",
                "ui://tool/app", "skill://demo/SKILL.md"):
        for candidate in (uri, prefix + uri):
            response = server._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                 "params": {"uri": candidate}})
            assert "result" in response, candidate


def test_proxy_stripping_still_refuses_what_it_never_resolved(tmp_path):
    """The recovery must stay a proxy-prefix rule, not a suffix free-for-all."""
    server = _gateway_server(tmp_path)

    for uri in ("xconfig://settings", "notaconfig://settings",
                "https://nanohub.org/nope://x"):
        assert server._strip_proxy_prefix(uri) == uri


# ---------------------------------------------------------------------------
# prompts/get argument validation
# ---------------------------------------------------------------------------

def test_an_unknown_prompt_argument_is_the_callers_error():
    """The same defect `tools/call` had, on the prompts path — and worse: it
    surfaced as -32603, blaming the server, with the handler's signature in the
    message and a traceback in the log for every bad call."""
    server = MCPServer("prompts")
    ran = []

    @server.prompt()
    def greet(who: str):
        """Greet someone."""
        ran.append(1)
        return "hi " + who

    response = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
         "params": {"name": "greet", "arguments": {"who": "bob", "zzz": 1}}})

    assert response["error"]["code"] == -32602
    assert "zzz" in response["error"]["message"]
    assert "unexpected keyword argument" not in response["error"]["message"]
    assert ran == []

    # The valid call is untouched.
    assert "result" in server._handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
         "params": {"name": "greet", "arguments": {"who": "bob"}}})



# ---------------------------------------------------------------------------
# Result `_meta`, prompt content, and schema generation
# ---------------------------------------------------------------------------

def test_every_result_type_emits_the_meta_it_accepts():
    """`ToolResult` had this fixed; `ResourceResult` and `PromptResult` still
    accepted `meta=` in `__init__` and dropped it in `to_dict`, so it was
    silently lost on the wire."""
    from nanohubmcp.types import (
        ToolResult, ResourceResult, ResourceContent, PromptResult, Message)

    assert ToolResult("x", meta={"k": 1}).to_dict()["_meta"] == {"k": 1}
    assert ResourceResult(
        [ResourceContent(uri="u", text="t")], meta={"k": 1}
    ).to_dict()["_meta"] == {"k": 1}
    assert PromptResult(
        messages=[Message("hi")], meta={"k": 1}
    ).to_dict()["_meta"] == {"k": 1}


def test_a_non_text_prompt_message_survives_as_a_content_block():
    """A dict content block was flattened with `str()`, which put a Python
    repr on the wire and destroyed everything that was not text."""
    from nanohubmcp.types import PromptResult

    image = {"type": "image", "data": "AAA", "mimeType": "image/png"}
    result = PromptResult(messages=[{"role": "user", "content": image}]).to_dict()
    assert result["messages"][0]["content"] == image

    # `{"text": ...}` is the one dict that meant a plain string.
    plain = PromptResult(
        messages=[{"role": "user", "content": {"text": "hi"}}]).to_dict()
    assert plain["messages"][0]["content"] == {"type": "text", "text": "hi"}


def test_a_default_with_no_json_spelling_does_not_break_tools_list():
    """It was published verbatim, so `json.dumps` raised inside `tools/list`
    and the response was an HTTP 500 — one such tool took the whole listing
    down and no client could see *any* tool on the server."""
    import json as _json

    server = MCPServer("sentinel")
    sentinel = object()

    @server.tool()
    def uses(sink=sentinel):
        """Has a sentinel default."""
        return "ok"

    listed = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    _json.dumps(listed)             # must not raise
    assert listed["result"]["tools"][0]["inputSchema"]["properties"]["sink"] == {}


def test_a_closed_set_of_values_is_published_as_an_enum():
    """A `Literal` or `enum.Enum` names the permitted values, and publishing
    nothing left the model untold and the wrong value unrefused."""
    import enum as _enum
    from typing import Literal as _Literal

    class Colour(_enum.Enum):
        RED = "red"
        BLUE = "blue"

    server = MCPServer("enums")

    @server.tool()
    def pick(mode: _Literal["fast", "slow"], colour: Colour = Colour.RED):
        """Pick a mode."""
        return mode

    props = server._tools["pick"]["definition"].inputSchema["properties"]
    assert props["mode"] == {"enum": ["fast", "slow"], "type": "string"}
    assert props["colour"]["enum"] == ["red", "blue"]

    # And the published constraint is now enforced.
    bad = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "pick", "arguments": {"mode": "sideways"}}})
    assert bad["error"]["code"] == -32602


def test_a_symlink_out_of_a_skill_directory_is_not_served(tmp_path):
    """The walk followed it, so anything a symlink in a skill tree pointed at
    became an ordinary `skill://` resource. Traversal through the URI was
    already refused; this was the same escape by another route."""
    import os

    outside = tmp_path / "secret.txt"
    outside.write_text("TOPSECRET")
    skill_dir = tmp_path / "sk"
    (skill_dir / "real").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sk\ndescription: A skill\n---\n\n# S\n")
    (skill_dir / "real" / "a.txt").write_text("inside")
    os.symlink(str(outside), str(skill_dir / "escape.txt"))
    os.symlink(str(skill_dir / "real" / "a.txt"), str(skill_dir / "alias.txt"))

    server = MCPServer("skills")
    server.skill("sk", skill_dir)

    served = sorted(r["uri"] for r
                    in server._skills["skill://sk/SKILL.md"]["definition"].resources)
    assert "skill://sk/escape.txt" not in served
    # A symlink that stays inside the skill is still served.
    assert "skill://sk/alias.txt" in served

    denied = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
         "params": {"uri": "skill://sk/escape.txt"}})
    assert "error" in denied


def test_one_session_cannot_destroy_anothers_mrtr_state():
    """`mrtr_load` and `mrtr_save` are both session-scoped — `mrtr_save` even
    refuses to rebind another session's id because that "would let any session
    permanently break another's in-flight request just by naming it".
    `mrtr_discard` was not, so naming the id and completing a call did exactly
    that: the owner lost every answer it had accumulated.
    """
    modern = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
              "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}}
    headers = {"Mcp-Method": "tools/call", "Mcp-Name": "ask"}
    server = MCPServer("mrtr")

    @server.tool()
    def ask(ctx=None):
        """Asks the caller a question."""
        return "hi {}".format(ctx.elicit("n?", {"type": "object"}))

    def call(session_id, request_id, **extra):
        params = {"name": "ask", "arguments": {}, "_meta": modern}
        params.update(extra)
        return server._handle_request(
            {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
             "params": params}, session_id=session_id, headers=headers)

    state = call("A", 1)["result"]["requestState"]
    assert state in server._mrtr_states

    call("B", 2, requestState=state, inputResponses={"input-1": {"n": "b"}})
    assert state in server._mrtr_states, "B must not discard A's state"

    # The owner still completes and still cleans up after itself.
    call("A", 3, requestState=state, inputResponses={"input-1": {"n": "a"}})
    assert state not in server._mrtr_states


def test_prompt_arguments_must_be_strings():
    """`GetPromptRequest.params.arguments` is typed `{[key: string]: string}`.
    A non-string reached the handler and raised there, so the caller's
    malformed request came back as -32603 carrying the handler's own
    TypeError."""
    server = MCPServer("prompts")

    @server.prompt()
    def greet(who: str):
        """Greet someone."""
        return "hi " + who

    for value in (None, 5, {"a": 1}, ["x"], True):
        response = server._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
             "params": {"name": "greet", "arguments": {"who": value}}})
        assert response["error"]["code"] == -32602, value
        assert "who" in response["error"]["message"]

    assert "result" in server._handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "prompts/get",
         "params": {"name": "greet", "arguments": {"who": "bob"}}})


def test_skills_get_and_directory_read_accept_a_gateway_prefixed_uri(tmp_path):
    """`resources/read` recovered the real URI; these two looked theirs up raw.

    Found by testing against a live nanoHUB session rather than locally: a
    gateway-prefixed skill URI came back "not a skill" / "Not a directory
    resource" for a skill the server was serving.
    """
    skill_dir = tmp_path / "unit-conversion"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: unit-conversion\ndescription: A skill\n---\n\n# S\n")
    (skill_dir / "references" / "UNITS.md").write_text("units\n")

    server = MCPServer("skills")
    server.skill("unit-conversion", skill_dir)
    prefix = "https://nanohub.org/"

    def ok(method, uri):
        return "result" in server._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"uri": uri}})

    cases = [
        ("skills/get", "skill://unit-conversion/SKILL.md"),
        ("resources/read", "skill://unit-conversion/references/UNITS.md"),
        ("resources/directory/read", "skill://unit-conversion/references"),
        ("resources/directory/read", "skill://unit-conversion"),
    ]
    for method, uri in cases:
        assert ok(method, uri), (method, uri)
        assert ok(method, prefix + uri), (method, prefix + uri)

    # A prefixed URI that names nothing is still refused.
    assert not ok("resources/directory/read", prefix + "skill://unit-conversion/nope")
    assert not ok("skills/get", prefix + "skill://other/SKILL.md")


def test_initialize_carries_the_server_instructions():
    """`InitializeResult.instructions` is standard in every revision that has
    an `initialize`. It was only ever returned from `server/discover`, which
    exists solely in 2026-07-28 — so a server could set `instructions=` and no
    handshake-era client, which today is all of them, would ever see it.
    """
    server = MCPServer("guided", instructions="Create a workspace first.")

    for revision in ("2024-11-05", "2025-06-18", "2025-11-25"):
        result = server._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": revision, "capabilities": {}}})["result"]
        assert result["instructions"] == "Create a workspace first.", revision

    # Still offered on the 2026-07-28 discovery method it was already on.
    discover = server._handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "server/discover",
         "params": {"_meta": {"io.modelcontextprotocol/protocolVersion":
                              "2026-07-28"}}},
        headers={"Mcp-Method": "server/discover"})["result"]
    assert discover["instructions"] == "Create a workspace first."

    # A server that set none must not emit the key at all.
    plain = MCPServer("plain")._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})["result"]
    assert "instructions" not in plain


def test_an_explicit_null_reads_as_an_omitted_optional_argument():
    """Models and UI layers send `null` for "I have no value for this".

    Strictly it is invalid -- `{"type": "string"}` does not admit null -- so
    the call was refused with -32602 and the user was told
    "arguments.label should be string, got NoneType" about a field they had
    simply left blank. The key is dropped rather than coerced, so the
    handler's own default applies, which is what absence means.
    """
    server = MCPServer("nulls")

    @server.tool()
    def start(tool_name, label="untitled"):
        """Start something.

        Args:
            tool_name: which tool.
            label: a name for the run.
        """
        return {"tool_name": tool_name, "label": label}

    @server.tool(input_schema={
        "type": "object",
        "properties": {"clear": {"type": ["string", "null"]},
                       "name": {"type": "string"}},
        "required": ["name"]})
    def nullable(name, clear="unset"):
        """Takes an argument that is genuinely nullable.

        Args:
            name: required.
            clear: null means clear it.
        """
        return {"name": name, "clear": clear}

    def call(tool, arguments):
        return server._handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments}})

    # The reported failure: an optional string sent as null.
    reply = call("start", {"tool_name": "pntoy", "label": None})
    assert "error" not in reply, reply
    assert reply["result"]["structuredContent"]["label"] == "untitled"

    # Absent and null now agree, which is the whole point.
    assert (call("start", {"tool_name": "pntoy"})["result"]["structuredContent"]
            == reply["result"]["structuredContent"])

    # A property that asked for null keeps it: the tool wanted that value.
    kept = call("nullable", {"name": "x", "clear": None})
    assert "error" not in kept, kept
    assert kept["result"]["structuredContent"]["clear"] is None

    # An argument the tool does not declare is still named, not swallowed.
    unknown = call("start", {"tool_name": "pntoy", "nope": None})
    assert unknown["error"]["code"] == -32602
    assert "nope" in unknown["error"]["message"]
