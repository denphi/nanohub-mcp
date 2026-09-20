"""Every branch of input-schema generation, and the invariant they must hold.

`tools/call` validates arguments against the `inputSchema` a tool published.
That is only safe while the schema states what the generator *knows*. Three
regressions came from the same mistake in the other direction — a guess
published as a constraint:

  * `**kwargs` emitted as a required property, so no call could satisfy it;
  * a type inferred from a default value, so `def scale(factor=1)` refused
    `factor=2.5`;
  * "I don't recognise this type" answered as `{"type": "string"}`, so an
    unmodelled class annotation refused every object.

Each was found one review round after the last, because each round checked a
few examples rather than the branches. This module checks the branches.

Two halves:

  `test_branch_*`      one case per branch of the generator and of both type
                       converters, with the published schema spelled out.
  `test_invariant_*`   the property those cases exist to protect, asserted
                       over the whole corpus rather than case by case.

`test_every_branch_is_covered` ties them together: it traces execution across
the corpus and fails if any decision point in the three functions went unhit,
so a new branch added without a case is a test failure rather than a silent
gap.
"""

from __future__ import print_function

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Any, Dict, List, Optional, Union  # noqa: E402

from nanohubmcp import MCPServer  # noqa: E402
from nanohubmcp.decorators import (  # noqa: E402
    _generate_input_schema,
    _python_type_to_json_schema,
    _type_expr_to_json_schema,
)


class Widget(object):
    """A class the generator has no model for."""


# ---------------------------------------------------------------------------
# The corpus: one handler per branch of `_generate_input_schema`.
#
# `expect` is the whole `properties` dict, so a branch that starts emitting
# something extra fails here rather than somewhere downstream.
# ---------------------------------------------------------------------------

def _h_resolved_hint(a: int, b: str):
    """Branch: name in get_type_hints()."""


def _h_raw_annotation(a="x"):
    """Branch: annotation present but unresolvable by get_type_hints."""


_h_raw_annotation.__annotations__ = {"a": "NotARealName"}


def _h_type_comment(a, b):
    # type: (int, List[str]) -> dict
    """Branch: no annotation, parseable type comment."""


def _h_informative_default(limit=1000, label="run", opts={"a": 1}):
    """Branch: no type information, non-None default."""


def _h_nothing_known(a, b=None):
    """Branch: no annotation, no comment, no informative default."""


def _h_variadic(a, *items, **extra):
    """Skip branch: VAR_POSITIONAL and VAR_KEYWORD are not named arguments."""


def _h_context(a: int, ctx=None):
    """Skip branch: the framework injects ctx; it is never an argument."""


def _h_self_like(self, cls, context, a: int):
    """Skip branch: the remaining excluded names."""


BRANCH_CASES = [
    ("resolved-hint", _h_resolved_hint,
     {"a": {"type": "integer"}, "b": {"type": "string"}}, ["a", "b"]),
    ("raw-annotation", _h_raw_annotation, {"a": {}}, []),
    ("type-comment", _h_type_comment,
     {"a": {"type": "integer"}, "b": {"type": "array"}}, ["a", "b"]),
    ("informative-default", _h_informative_default,
     {"limit": {"default": 1000}, "label": {"default": "run"},
      "opts": {"default": {"a": 1}}}, []),
    ("nothing-known", _h_nothing_known, {"a": {}, "b": {}}, ["a"]),
    ("variadics-skipped", _h_variadic, {"a": {}}, ["a"]),
    ("context-skipped", _h_context, {"a": {"type": "integer"}}, ["a"]),
    ("excluded-names-skipped", _h_self_like,
     {"a": {"type": "integer"}}, ["a"]),
]


@pytest.mark.parametrize("label,handler,expect_properties,expect_required",
                         BRANCH_CASES, ids=[c[0] for c in BRANCH_CASES])
def test_branch_publishes_exactly_what_it_knows(
        label, handler, expect_properties, expect_required):
    schema = _generate_input_schema(handler)
    assert schema["properties"] == expect_properties
    assert schema["required"] == expect_required


# ---------------------------------------------------------------------------
# Both type converters, branch by branch.
#
# The two paths answer the same questions and once disagreed: `Any` and a
# mixed `Union` were `{}` when resolved from an annotation and `"string"` when
# read from a type comment, so only the comment path rejected valid calls.
# ---------------------------------------------------------------------------

RESOLVED_CASES = [
    ("none", type(None), {"type": "null"}),
    ("str", str, {"type": "string"}),
    ("int", int, {"type": "integer"}),
    ("float", float, {"type": "number"}),
    ("bool", bool, {"type": "boolean"}),
    ("bare-list", list, {"type": "array"}),
    ("typed-list", List[str], {"type": "array", "items": {"type": "string"}}),
    ("bare-dict", dict, {"type": "object"}),
    ("typed-dict", Dict[str, int],
     {"type": "object", "additionalProperties": {"type": "integer"}}),
    ("optional", Optional[int], {"type": ["integer", "null"]}),
    ("mixed-union", Union[int, str], {}),
    ("any", Any, {}),
    ("unmodelled-class", Widget, {}),
]

EXPRESSION_CASES = [
    ("empty", "", {}),
    ("primitive", "int", {"type": "integer"}),
    ("none", "None", {"type": "null"}),
    ("optional", "Optional[int]", {"type": ["integer", "null"]}),
    ("union-with-none", "Union[int, None]", {"type": ["integer", "null"]}),
    ("mixed-union", "Union[int, str]", {}),
    ("container-array", "List[str]", {"type": "array"}),
    ("container-object", "Dict[str, int]", {"type": "object"}),
    ("any", "Any", {}),
    ("unmodelled", "numpy.ndarray", {}),
]


@pytest.mark.parametrize("label,py_type,expect", RESOLVED_CASES,
                         ids=[c[0] for c in RESOLVED_CASES])
def test_branch_resolved_type_conversion(label, py_type, expect):
    assert _python_type_to_json_schema(py_type) == expect


@pytest.mark.parametrize("label,expression,expect", EXPRESSION_CASES,
                         ids=[c[0] for c in EXPRESSION_CASES])
def test_branch_type_expression_conversion(label, expression, expect):
    assert _type_expr_to_json_schema(expression) == expect


@pytest.mark.parametrize("label,expression,expect", EXPRESSION_CASES,
                         ids=[c[0] for c in EXPRESSION_CASES])
def test_both_converters_agree(label, expression, expect):
    """Where both paths can express a type, they must give the same answer.

    They diverged on `Any` and on a mixed `Union`, and the divergence was
    invisible until the narrower of the two answers started being enforced.
    """
    no_counterpart = object()
    equivalents = {
        "": no_counterpart,          # an empty annotation has no Python form
        "int": int,
        "None": type(None),
        "Optional[int]": Optional[int],
        "Union[int, None]": Optional[int],
        "Union[int, str]": Union[int, str],
        "List[str]": List[str],
        "Dict[str, int]": Dict[str, int],
        "Any": Any,
        "numpy.ndarray": Widget,
    }
    counterpart = equivalents[expression]
    if counterpart is no_counterpart:
        pytest.skip("no resolved-type counterpart for {!r}".format(expression))
    resolved = _python_type_to_json_schema(counterpart)
    from_comment = _type_expr_to_json_schema(expression)
    # The comment parser models no container element types, so compare the
    # constraint both can express: the declared `type`.
    assert resolved.get("type") == from_comment.get("type")


# ---------------------------------------------------------------------------
# The invariant the branches exist to protect.
# ---------------------------------------------------------------------------

def _known_typed_parameters(handler):
    """Parameters the generator has real type information for.

    Real means an annotation or a type comment — not a value guessed from a
    default, and not a fallback for something unrecognized.
    """
    schema = _generate_input_schema(handler)
    return {name for name, prop in schema["properties"].items()
            if "type" in prop}


@pytest.mark.parametrize("label,handler", [(c[0], c[1]) for c in BRANCH_CASES],
                         ids=[c[0] for c in BRANCH_CASES])
def test_invariant_untyped_parameters_carry_no_type(label, handler):
    """A parameter with no type information must not be given one.

    This is the rule all three regressions broke. `required` is exempt: it
    comes from the signature, which is knowledge, not inference.
    """
    schema = _generate_input_schema(handler)
    hints = getattr(handler, "__annotations__", {}) or {}
    source = inspect.getsource(handler)
    has_comment = "# type:" in source

    for name, prop in schema["properties"].items():
        if "type" not in prop:
            continue
        annotated = name in hints
        assert annotated or has_comment, (
            "{}: property {!r} publishes {!r} with no annotation or type "
            "comment to justify it".format(label, name, prop["type"]))


@pytest.mark.parametrize("label,handler", [(c[0], c[1]) for c in BRANCH_CASES],
                         ids=[c[0] for c in BRANCH_CASES])
def test_invariant_required_matches_the_signature(label, handler):
    """`required` is exactly the bindable parameters with no default."""
    excluded = {"self", "cls", "ctx", "context"}
    variadic = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    expected = [
        name for name, param in inspect.signature(handler).parameters.items()
        if name not in excluded
        and param.kind not in variadic
        and param.default is inspect.Parameter.empty
    ]
    assert _generate_input_schema(handler)["required"] == expected


@pytest.mark.parametrize("label,handler", [(c[0], c[1]) for c in BRANCH_CASES],
                         ids=[c[0] for c in BRANCH_CASES])
def test_invariant_variadics_are_never_properties(label, handler):
    """`*args` / `**kwargs` are not named arguments and cannot be supplied.

    Emitting them as properties also put them in `required` (they have no
    default), which made every call to such a tool unsatisfiable.
    """
    schema = _generate_input_schema(handler)
    for name, param in inspect.signature(handler).parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                          inspect.Parameter.VAR_KEYWORD):
            assert name not in schema["properties"], label
            assert name not in schema["required"], label


# Values spanning every JSON type, used to prove an unconstrained property
# really is unconstrained.
JSON_VALUES = [None, True, 0, -1, 2.5, "", "text", [], [1, "a"], {}, {"k": 1}]


@pytest.mark.parametrize("label,handler", [(c[0], c[1]) for c in BRANCH_CASES],
                         ids=[c[0] for c in BRANCH_CASES])
def test_invariant_untyped_parameters_accept_any_json_value(label, handler):
    """The property that makes the rule above matter, end to end.

    An untyped parameter must be accepted by `tools/call` whatever it is
    sent, because the server has no basis to refuse it.
    """
    server = MCPServer("invariants")
    server._register_tool_function(_as_tool(handler))
    schema = _generate_input_schema(handler)
    typed = _known_typed_parameters(handler)

    untyped = [name for name in schema["properties"] if name not in typed]
    if not untyped:
        pytest.skip("no untyped parameters in {}".format(label))

    for name in untyped:
        for value in JSON_VALUES:
            arguments = {req: 1 for req in schema["required"]}
            arguments[name] = value
            violation = server._input_schema_violation("probe", arguments)
            assert violation is None, (
                "{}: {!r}={!r} was refused by the published schema "
                "({})".format(label, name, value, violation))


def _as_tool(handler):
    """Decorate `handler` as a tool named `probe`, whatever its signature."""
    from nanohubmcp import decorators

    def probe(*args, **kwargs):
        return "ok"

    probe.__doc__ = handler.__doc__ or "probe"
    decorated = decorators.tool(name="probe")(probe)
    # Keep the generated schema under test, not the trivial one `probe` has.
    decorated._mcp_tool_input_schema = _generate_input_schema(handler)
    return decorated


def test_invariant_a_typed_parameter_is_still_enforced():
    """The complement: the fixes must not disarm validation where it is known."""
    server = MCPServer("invariants")

    @server.tool()
    def typed(a: int, b: Optional[str] = None):
        """Annotated"""
        return {"a": a, "b": b}

    assert server._input_schema_violation("typed", {"a": 1}) is None
    assert server._input_schema_violation("typed", {"a": 1, "b": None}) is None
    assert server._input_schema_violation("typed", {"a": 1, "b": "x"}) is None
    assert server._input_schema_violation("typed", {"a": "x"}) is not None
    assert server._input_schema_violation("typed", {"a": 1, "b": 2}) is not None
    assert server._input_schema_violation("typed", {}) is not None


# ---------------------------------------------------------------------------
# Did the corpus actually reach every branch?
# ---------------------------------------------------------------------------

def _decision_lines(function):
    """Line numbers of every `return` and `continue` in `function`."""
    lines, start = inspect.getsourcelines(function)
    return {
        start + offset
        for offset, line in enumerate(lines)
        if line.strip().startswith(("return ", "return\n", "continue"))
    }


def test_every_branch_is_covered():
    """Fail when a decision point in schema generation has no case above.

    Enumerating branches is only worth something if the enumeration is kept
    honest. This traces the three functions while the whole corpus runs and
    reports any `return`/`continue` that never executed — which is what a
    newly added branch with no case looks like.
    """
    targets = {}
    for function in (_generate_input_schema, _python_type_to_json_schema,
                     _type_expr_to_json_schema):
        targets[function.__code__] = (function, _decision_lines(function))

    covered = set()

    def local_tracer(frame, event, arg):
        if event == "line":
            covered.add((frame.f_code, frame.f_lineno))
        return local_tracer

    def global_tracer(frame, event, arg):
        if event == "call" and frame.f_code in targets:
            return local_tracer
        return None

    previous = sys.gettrace()
    sys.settrace(global_tracer)
    try:
        for _label, handler, _props, _required in BRANCH_CASES:
            _generate_input_schema(handler)
        for _label, py_type, _expect in RESOLVED_CASES:
            _python_type_to_json_schema(py_type)
        for _label, expression, _expect in EXPRESSION_CASES:
            _type_expr_to_json_schema(expression)
    finally:
        sys.settrace(previous)

    missed = []
    for code, (function, decisions) in targets.items():
        for line in sorted(decisions - {ln for c, ln in covered if c is code}):
            missed.append("{}:{}".format(function.__name__, line))

    assert not missed, (
        "schema-generation branches with no case in this module: {}. Add one "
        "to BRANCH_CASES / RESOLVED_CASES / EXPRESSION_CASES so the new "
        "behaviour is pinned rather than inferred.".format(", ".join(missed)))


def test_the_coverage_check_can_fail():
    """A coverage assertion nobody has seen fail proves nothing."""
    unreached = _decision_lines(_type_expr_to_json_schema)
    assert unreached, "expected to find decision lines to track"

    covered = set()

    def local_tracer(frame, event, arg):
        if event == "line":
            covered.add(frame.f_lineno)
        return local_tracer

    def global_tracer(frame, event, arg):
        if event == "call" and frame.f_code is _type_expr_to_json_schema.__code__:
            return local_tracer
        return None

    previous = sys.gettrace()
    sys.settrace(global_tracer)
    try:
        # One expression only: most branches must go unvisited.
        _type_expr_to_json_schema("int")
    finally:
        sys.settrace(previous)

    assert unreached - covered, (
        "tracing a single expression should leave branches uncovered; if it "
        "does not, the tracer is not observing this function")


# ---------------------------------------------------------------------------
# The validator's type rules, checked against the reference implementation
# rather than against my reading of the spec.
# ---------------------------------------------------------------------------

TYPE_PROBES = [
    5, 5.0, 5.5, -0.0, 0, -1, 1e100, float("nan"), float("inf"),
    True, False, "5", "", "text", None, [], [1], {}, {"k": 1},
]

JSON_TYPE_NAMES = ["integer", "number", "string", "boolean",
                   "object", "array", "null"]


@pytest.mark.parametrize("type_name", JSON_TYPE_NAMES)
def test_type_rules_match_the_reference_implementation(type_name):
    """`_matches_type` must agree with jsonschema on every JSON shape.

    Hand-reasoning about this got `integer` wrong: JSON Schema defines it as
    "a JSON number with a zero fractional part", so `5.0` is an integer, and
    rejecting it refused the commonest shape an LLM emits for an integer
    argument. Comparing against the reference removes the reasoning step.
    """
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator({"type": type_name})

    from nanohubmcp.server import MCPServer

    mismatches = []
    for value in TYPE_PROBES:
        expected = validator.is_valid(value)
        actual = MCPServer._matches_type(type_name, value)
        if expected != actual:
            mismatches.append(
                "{!r}: jsonschema={} nanohubmcp={}".format(
                    value, expected, actual))
    assert not mismatches, "type {!r}: {}".format(type_name, mismatches)


def test_integer_accepts_a_whole_float():
    """The specific regression, pinned without needing jsonschema installed."""
    from nanohubmcp.server import MCPServer

    assert MCPServer._matches_type("integer", 5.0) is True
    assert MCPServer._matches_type("integer", -0.0) is True
    assert MCPServer._matches_type("integer", 5.5) is False
    assert MCPServer._matches_type("integer", "5") is False
    assert MCPServer._matches_type("integer", True) is False


def test_a_whole_float_reaches_an_integer_parameter():
    """End to end: the argument a client actually sends must be accepted."""
    server = MCPServer("integers")

    @server.tool()
    def repeat(times: int):
        """Integer argument"""
        return {"times": times}

    assert server._input_schema_violation("repeat", {"times": 5.0}) is None
    assert server._input_schema_violation("repeat", {"times": 5}) is None
    assert server._input_schema_violation("repeat", {"times": 5.5}) is not None
    assert server._input_schema_violation("repeat", {"times": "5"}) is not None
