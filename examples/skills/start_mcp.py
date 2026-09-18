#!/usr/bin/env python
"""
Unit Conversion MCP Server — demonstrates the SEP-2640 Skills Extension.

Demonstrates:

    * ``@server.skill``  — serve a SKILL.md + supporting files as MCP
                            resources under skill://unit-conversion/...
    * ``@server.tool``   — the tool the skill's instructions point at

Run with::

    start_mcp --app start_mcp.py

Or directly::

    python start_mcp.py 8000

Once running, a skill-aware client can discover the skill with
``skills/list``, fetch it with ``skills/get``, and read its files with the
standard ``resources/read`` — see docs/examples.rst for full request/response
examples.
"""

from __future__ import print_function

import os
import sys
from pathlib import Path

# Add package path for development
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from nanohubmcp import MCPServer, ToolResult

server = MCPServer("skills-demo", version="1.0.0")

# meters per unit, and joules per unit, grouped by physical quantity. Two
# units convert into each other only if they're in the same group.
_LENGTH_TO_METERS = {"m": 1.0, "um": 1e-6, "nm": 1e-9, "angstrom": 1e-10}
_ENERGY_TO_JOULES = {"J": 1.0, "eV": 1.602176634e-19}
_GROUPS = {
    "length": _LENGTH_TO_METERS,
    "energy": _ENERGY_TO_JOULES,
    "temperature": {"K", "C"},
}


def _group_of(unit):
    # type: (str) -> str
    for group, members in _GROUPS.items():
        if unit in members:
            return group
    return ""


_UNIT_ENUM = ["m", "um", "nm", "angstrom", "J", "eV", "K", "C"]


@server.tool(
    input_schema={
        "type": "object",
        "properties": {
            "value": {"type": "number",
                      "description": "Magnitude to convert, in from_unit"},
            "from_unit": {"type": "string", "enum": _UNIT_ENUM,
                          "description": "Unit of the supplied value"},
            "to_unit": {"type": "string", "enum": _UNIT_ENUM,
                        "description": "Unit to convert into; must be the same "
                                       "physical quantity as from_unit"},
        },
        "required": ["value", "from_unit", "to_unit"],
    },
    annotations={"title": "Convert units", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def convert_units(value, from_unit, to_unit):
    # type: (float, str, str) -> ToolResult
    """Convert a value between length (m/um/nm/angstrom), energy (J/eV), or
    temperature (K/C) units. from_unit and to_unit must be in the same
    group; see the unit-conversion skill's references/UNITS.md."""
    value = float(value)
    from_group = _group_of(from_unit)
    to_group = _group_of(to_unit)

    if not from_group or not to_group:
        unknown = from_unit if not from_group else to_unit
        return ToolResult(
            content="Unknown unit '{}'. See references/UNITS.md for supported "
                    "units.".format(unknown),
            is_error=True)
    if from_group != to_group:
        return ToolResult(
            content="Cannot convert '{}' ({}) to '{}' ({}): different physical "
                    "quantities.".format(from_unit, from_group, to_unit, to_group),
            is_error=True)

    if from_group == "temperature":
        kelvin = value if from_unit == "K" else value + 273.15
        result = kelvin if to_unit == "K" else kelvin - 273.15
    else:
        table = _GROUPS[from_group]
        base = value * table[from_unit]
        result = base / table[to_unit]

    # Ten significant figures, not ten decimal places — a binary-fraction
    # artifact like 0.5429999999999999 reads as noise no matter the unit's
    # magnitude, from angstrom-scale lengths to eV-in-joules.
    return ToolResult(content="{:.10g}".format(result))


@server.skill("unit-conversion")  # -> skill://unit-conversion/SKILL.md
def unit_conversion_skill():
    return Path(__file__).parent / "skills" / "unit-conversion"


def main():
    port = int(os.environ.get("MCP_PORT", 8000))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    server.run(port=port)


if __name__ == "__main__":
    main()
