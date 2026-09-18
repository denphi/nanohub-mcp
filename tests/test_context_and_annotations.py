"""Context surface, tool-annotation validation, and Image helper.

These are small, widely-used pieces whose failure modes are quiet: a misspelled
annotation is dropped by clients without complaint, and a Context method that
misbehaves off-session breaks a tool only in deployment.
"""

from __future__ import print_function

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanohubmcp import Context, MCPServer  # noqa: E402
from nanohubmcp.decorators import _validate_annotations  # noqa: E402
from nanohubmcp.types import Image, ImageContent  # noqa: E402


# ---------------------------------------------------------------------------
# Tool annotations
# ---------------------------------------------------------------------------

def test_annotation_validation_accepts_the_documented_keys():
    valid = {"title": "Run", "readOnlyHint": False, "destructiveHint": True,
             "idempotentHint": False, "openWorldHint": False}
    assert _validate_annotations(valid) == valid
    assert _validate_annotations(None) is None


def test_a_misspelled_annotation_is_rejected_at_decoration_time():
    """Clients ignore unknown annotation keys silently — fail loudly instead."""
    with pytest.raises(ValueError) as excinfo:
        _validate_annotations({"readonlyHint": True})     # lowercase 'o'
    assert "readonlyHint" in str(excinfo.value)


def test_annotation_types_are_enforced():
    with pytest.raises(ValueError):
        _validate_annotations({"readOnlyHint": "yes"})     # str where bool wanted
    with pytest.raises(ValueError):
        _validate_annotations({"title": True})             # bool where str wanted
    with pytest.raises(ValueError):
        _validate_annotations(["readOnlyHint"])            # not a dict


def test_a_bad_annotation_stops_the_tool_registering():
    server = MCPServer("ann")
    with pytest.raises(ValueError):
        @server.tool(annotations={"destructiveHInt": True})
        def oops():
            """A tool whose annotation key is misspelled."""
            return 1
    assert "oops" not in server._tools


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------

def test_image_from_inline_data():
    content = Image(data="Zm9v", mime_type="image/jpeg").to_content()
    assert isinstance(content, ImageContent)
    assert content.to_dict() == {"type": "image", "data": "Zm9v",
                                 "mimeType": "image/jpeg"}


def test_image_from_a_path_is_base64_encoded(tmp_path):
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    content = Image(path=str(png)).to_content()
    assert base64.b64decode(content.data) == b"\x89PNG\r\n\x1a\n"
    assert content.mimeType == "image/png"


def test_image_with_neither_source_yields_empty_data():
    assert Image().to_content().data == ""


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def test_context_exposes_the_request_it_belongs_to():
    server = MCPServer("ctx")
    ctx = Context(server=server, request_id="r-1", session_id="s-1",
                  progress_token="tok", meta={"a": 1}, job_id="j-1")
    assert ctx.server is server
    assert ctx.request_id == "r-1"
    assert ctx.session_id == "s-1"
    assert ctx.progress_token == "tok"
    assert ctx.meta == {"a": 1}
    assert ctx.job_id == "j-1"


def test_context_without_a_server_refuses_client_round_trips():
    """A detached Context must raise, not silently no-op."""
    ctx = Context()
    for call in (lambda: ctx.elicit("m"),
                 lambda: ctx.elicit_url("m", url="https://x/"),
                 lambda: ctx.sample({"messages": []}),
                 lambda: ctx.list_roots()):
        with pytest.raises(RuntimeError):
            call()


def test_logging_records_every_level_and_survives_no_session():
    server = MCPServer("log")
    ctx = Context(server=server)
    ctx.debug("d")
    ctx.info("i", extra=1)
    ctx.warning("w")
    ctx.error("e")
    levels = [entry["level"] for entry in ctx.get_log_messages()]
    assert levels == ["debug", "info", "warning", "error"]
    assert ctx.get_log_messages()[1]["data"] == {"extra": 1}


def test_task_metadata_helpers_are_inert_off_task():
    """A sync tool may call these; they must not raise or invent a job."""
    ctx = Context(server=MCPServer("t"))
    assert ctx.task_metadata == {}
    assert ctx.set_task_metadata(jobHandle="x") == {}
    assert ctx.job_id is None
    assert ctx.is_cancelled() is False


def test_on_cancel_rejects_a_non_callable():
    server = MCPServer("c")

    @server.async_tool()
    def work(ctx=None):
        return "done"

    job_id = server._start_async_tool_job(server._tools["work"]["handler"], 1, {})
    ctx = Context(server=server, job_id=job_id)
    with pytest.raises(TypeError):
        ctx.on_cancel("not callable")


def test_report_progress_without_a_token_emits_nothing():
    """An uncorrelated progress notification is noise the client cannot use."""
    server = MCPServer("p")
    sent = []
    server._broadcast = lambda message, session_id=None: sent.append(message)

    Context(server=server, session_id="S").report_progress(0.5, total=1.0)
    assert sent == []

    Context(server=server, session_id="S",
            progress_token="tok").report_progress(0.5, total=1.0, message="half")
    assert len(sent) == 1
    params = sent[0]["params"]
    assert params["progressToken"] == "tok"
    assert params["progress"] == 0.5
    assert params["total"] == 1.0
