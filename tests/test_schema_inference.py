"""Schema inference and wire serialisation.

Both are contract surfaces: a bug in inference publishes a wrong
``inputSchema``, and a bug in ``to_dict`` puts a malformed object on the wire.
Neither shows up in a happy-path integration test, so they are exercised
directly here.
"""

from __future__ import print_function

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanohubmcp.decorators import (  # noqa: E402
    _generate_input_schema,
    _python_value_to_json_schema,
    _split_top_level_commas,
    _type_expr_to_json_schema,
)
from nanohubmcp.types import (  # noqa: E402
    ImageContent, Message, Prompt, PromptResult, Resource, ResourceContent,
    ResourceResult, Role, ServerCapabilities, ServerInfo, TextContent, Tool,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Type expressions -> JSON Schema
# ---------------------------------------------------------------------------

def test_primitive_type_expressions():
    for expr, expected in (
        ("int", "integer"), ("float", "number"), ("str", "string"),
        ("bool", "boolean"), ("list", "array"), ("dict", "object"),
    ):
        assert _type_expr_to_json_schema(expr) == {"type": expected}, expr


def test_none_maps_to_null():
    for expr in ("None", "NoneType", "type(None)"):
        assert _type_expr_to_json_schema(expr) == {"type": "null"}


def test_optional_unwraps_to_the_inner_type_and_admits_null():
    """`Optional[X]` is X *or* null, and the schema has to say both.

    Publishing a bare `{"type": "integer"}` told clients the parameter
    never accepts null — so a schema-following client would not send one,
    and a validating client would reject it if the model did.
    """
    assert _type_expr_to_json_schema("Optional[int]") == {
        "type": ["integer", "null"]}
    assert _type_expr_to_json_schema("Optional[List[str]]") == {
        "type": ["array", "null"]}


def test_union_collapses_when_one_real_type_remains():
    # Union[int, None] is Optional[int] spelled the long way.
    assert _type_expr_to_json_schema("Union[int, None]") == {
        "type": ["integer", "null"]}


def test_mixed_union_falls_back_to_string():
    """A genuinely ambiguous union degrades rather than guessing wrong.

    Worth pinning: the fallback is permissive, so a mixed union publishes a
    looser schema than the handler accepts. That is deliberate — a wrong
    narrow type would reject valid calls — but it means unions are a poor way
    to describe a tool argument.
    """
    assert _type_expr_to_json_schema("Union[int, str]") == {"type": "string"}


def test_container_expressions():
    for expr in ("List[int]", "list[int]", "Tuple[int, str]", "Set[str]",
                 "Sequence[float]", "Iterable[str]"):
        assert _type_expr_to_json_schema(expr) == {"type": "array"}, expr
    for expr in ("Dict[str, int]", "dict[str, int]", "Mapping[str, int]",
                 "MutableMapping[str, int]"):
        assert _type_expr_to_json_schema(expr) == {"type": "object"}, expr


def test_unknown_type_falls_back_to_string():
    assert _type_expr_to_json_schema("numpy.ndarray") == {"type": "string"}


def test_split_top_level_commas_respects_nesting():
    assert _split_top_level_commas("int, str") == ["int", "str"]
    assert _split_top_level_commas("Dict[str, int], bool") == ["Dict[str, int]", "bool"]
    assert _split_top_level_commas("List[Tuple[int, int]], str") == [
        "List[Tuple[int, int]]", "str"]


def test_default_values_infer_a_type():
    assert _python_value_to_json_schema(3) == {"type": "integer"}
    assert _python_value_to_json_schema(3.5) == {"type": "number"}
    assert _python_value_to_json_schema("x") == {"type": "string"}
    assert _python_value_to_json_schema(True) == {"type": "boolean"}
    assert _python_value_to_json_schema([1]) == {"type": "array"}
    assert _python_value_to_json_schema({"a": 1}) == {"type": "object"}


def test_bool_is_not_reported_as_an_integer():
    """bool is a subclass of int in Python; the schema must not say integer."""
    assert _python_value_to_json_schema(False)["type"] == "boolean"


# ---------------------------------------------------------------------------
# Signature -> inputSchema
# ---------------------------------------------------------------------------

def test_generated_schema_marks_only_argument_less_params_required():
    def handler(mesh_points, time_steps=1000, label="run"):
        """Docstring."""

    schema = _generate_input_schema(handler)
    assert schema["type"] == "object"
    assert schema["required"] == ["mesh_points"]
    assert schema["properties"]["time_steps"]["type"] == "integer"
    assert schema["properties"]["label"]["type"] == "string"


def test_generated_schema_excludes_the_context_parameter():
    """ctx is injected by the framework and must never appear as an argument."""
    def handler(value, ctx=None):
        """Docstring."""

    schema = _generate_input_schema(handler, exclude_params={"ctx"})
    assert "ctx" not in schema["properties"]
    assert "ctx" not in schema.get("required", [])
    assert "value" in schema["properties"]


def test_generated_schema_reads_annotations():
    def handler(count: int, ratio: float = 1.0, tags: list = None):
        """Docstring."""

    schema = _generate_input_schema(handler)
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["properties"]["ratio"]["type"] == "number"
    # `tags: list = None` is implicitly Optional — the default *is* null —
    # so the published type admits it.
    assert schema["properties"]["tags"]["type"] == ["array", "null"]


# ---------------------------------------------------------------------------
# Wire serialisation
# ---------------------------------------------------------------------------

def test_resource_content_emits_only_the_fields_it_has():
    assert ResourceContent(uri="x://a", text="hello").to_dict() == {
        "uri": "x://a", "text": "hello"}

    # The spec splits these into TextResourceContents (requires text) and
    # BlobResourceContents (requires blob). A blob resource that also carried
    # text="" matched neither cleanly and read as empty text to a strict
    # client, so no text key is emitted at all.
    blob = ResourceContent(uri="x://b", blob="Zm9v", mime_type="image/png").to_dict()
    assert blob == {"uri": "x://b", "blob": "Zm9v", "mimeType": "image/png"}
    assert "text" not in blob

    # An explicit text alongside a blob is still honoured, and a resource with
    # neither still emits the empty text it always did.
    assert ResourceContent(uri="x://c", text="t", blob="Zm9v").to_dict()["text"] == "t"
    assert ResourceContent(uri="x://d").to_dict() == {"uri": "x://d", "text": ""}


def test_resource_content_accepts_content_as_an_alias_for_text():
    assert ResourceContent(uri="x://a", content="hi").to_dict()["text"] == "hi"


def test_tool_to_dict_omits_absent_optional_fields():
    minimal = Tool(name="t", description="d",
                   inputSchema={"type": "object", "properties": {}}).to_dict()
    assert minimal["name"] == "t"
    assert "outputSchema" not in minimal
    assert "annotations" not in minimal

    full = Tool(name="t", description="d",
                inputSchema={"type": "object", "properties": {}},
                outputSchema={"type": "object"},
                annotations={"title": "T", "readOnlyHint": True}).to_dict()
    assert full["outputSchema"] == {"type": "object"}
    assert full["annotations"]["readOnlyHint"] is True


def test_prompt_result_accepts_strings_and_messages():
    result = PromptResult(messages=["plain", Message("typed", role=Role.ASSISTANT)])
    emitted = result.to_dict()["messages"]
    assert len(emitted) == 2
    assert emitted[0]["role"] == "user"          # a bare string defaults to user
    assert emitted[1]["role"] == "assistant"


def test_tool_result_marks_errors():
    ok = ToolResult(content="fine").to_dict()
    assert ok["isError"] is False
    assert ok["content"][0]["text"] == "fine"

    bad = ToolResult(content="broke", is_error=True).to_dict()
    assert bad["isError"] is True


def test_text_and_image_content_shapes():
    assert TextContent("hi").to_dict() == {"type": "text", "text": "hi"}
    image = ImageContent(data="Zm9v", mimeType="image/png").to_dict()
    assert image["type"] == "image"
    assert image["mimeType"] == "image/png"


def test_resource_result_wraps_contents():
    result = ResourceResult(contents=[ResourceContent(uri="x://a", text="t")])
    assert result.to_dict()["contents"][0]["uri"] == "x://a"


def test_resource_and_prompt_definitions():
    resource = Resource(uri="config://a", name="a", description="d",
                        mimeType="application/json").to_dict()
    assert resource["uri"] == "config://a"
    assert resource["mimeType"] == "application/json"

    prompt = Prompt(name="p", description="d",
                    arguments=[{"name": "x", "required": True}]).to_dict()
    assert prompt["arguments"][0]["name"] == "x"


def test_server_info_and_capabilities_shapes():
    assert ServerInfo("srv", "1.2.3").to_dict() == {"name": "srv", "version": "1.2.3"}

    empty = ServerCapabilities().to_dict()
    assert empty == {}, "a server with nothing registered advertises nothing"

    full = ServerCapabilities(tools=True, resources=True, prompts=True,
                              logging=True, extensions={"x/y": {}},
                              list_changed=True).to_dict()
    assert full["tools"]["listChanged"] is True
    assert full["resources"]["subscribe"] is False
    assert full["logging"] == {}
    assert full["extensions"] == {"x/y": {}}
