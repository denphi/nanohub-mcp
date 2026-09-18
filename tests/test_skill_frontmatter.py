"""Unit tests for the SKILL.md YAML frontmatter subset parser.

nanohub-mcp ships with zero dependencies (see pyproject.toml), so SKILL.md
frontmatter is parsed by a hand-rolled YAML subset rather than PyYAML.
SEP-2640 requires the parsed `frontmatter` object to be "identical in
content to the frontmatter of the SKILL.md it describes" -- these tests
pin exactly which YAML constructs that subset covers, so a gap shows up
here rather than as a silently wrong `skills/list` entry.
"""

from __future__ import print_function

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nanohubmcp.server import _parse_skill_frontmatter


def _fm(body):
    return _parse_skill_frontmatter("---\n" + body + "\n---\nbody text\n")


def test_flat_scalars():
    fm = _fm(
        "name: git-workflow\n"
        "description: Follow this team's Git conventions\n"
        "license: Apache-2.0\n"
    )
    assert fm == {
        "name": "git-workflow",
        "description": "Follow this team's Git conventions",
        "license": "Apache-2.0",
    }


def test_quoted_strings_and_scalar_types():
    fm = _fm(
        "name: t\n"
        "description: 'single quoted'\n"
        "active: true\n"
        "disabled: false\n"
        "nothing: null\n"
        "tilde_null: ~\n"
        "count: 3\n"
        "ratio: 1.5\n"
    )
    assert fm["description"] == "single quoted"
    assert fm["active"] is True
    assert fm["disabled"] is False
    assert fm["nothing"] is None
    assert fm["tilde_null"] is None
    assert fm["count"] == 3
    assert fm["ratio"] == 1.5


def test_version_like_string_is_not_coerced_to_a_number():
    fm = _fm("name: t\ndescription: d\nmetadata:\n  version: \"2.1.0\"\n")
    assert fm["metadata"]["version"] == "2.1.0"


def test_block_list():
    fm = _fm("name: t\ndescription: d\nallowed-tools:\n  - bash\n  - read_file\n")
    assert fm["allowed-tools"] == ["bash", "read_file"]


def test_flow_list():
    fm = _fm("name: t\ndescription: d\ntags: [finance, forms, \"multi word\"]\n")
    assert fm["tags"] == ["finance", "forms", "multi word"]


def test_nested_block_mapping():
    fm = _fm(
        "name: t\ndescription: d\nmetadata:\n"
        "  version: \"2.1.0\"\n"
        "  tags: [a, b]\n"
    )
    assert fm["metadata"] == {"version": "2.1.0", "tags": ["a", "b"]}


def test_flow_mapping():
    fm = _fm('name: t\ndescription: d\nmetadata: {version: "2.1.0", author: acme}\n')
    assert fm["metadata"] == {"version": "2.1.0", "author": "acme"}


def test_literal_block_scalar_preserves_newlines():
    fm = _fm(
        "name: t\n"
        "description: |\n"
        "  Line one.\n"
        "  Line two.\n"
        "license: MIT\n"
    )
    assert fm["description"] == "Line one.\nLine two.\n"
    assert fm["license"] == "MIT"


def test_literal_block_scalar_strip_chomping():
    fm = _fm("name: t\ndescription: |-\n  Line one.\n  Line two.\n")
    assert fm["description"] == "Line one.\nLine two."


def test_folded_block_scalar_joins_lines_with_spaces():
    fm = _fm("name: t\ndescription: >\n  Line one.\n  Line two.\n")
    assert fm["description"] == "Line one. Line two.\n"


def test_wrapped_plain_scalar_folds_into_one_value():
    """A long description wrapped over several lines is one folded string.

    Reading only the first line truncated the value *and* turned each
    continuation into a junk top-level key, which fails the host's
    field-by-field frontmatter comparison and makes the skill unloadable.
    """
    fm = _fm(
        "name: t\n"
        "description: a long description that keeps\n"
        "  going onto another line\n"
        "  and one more\n"
        "license: MIT\n"
    )
    assert fm == {
        "name": "t",
        "description": "a long description that keeps going onto another line and one more",
        "license": "MIT",
    }


def test_block_list_at_the_keys_own_indent():
    """YAML allows a sequence at its key's indent, not only deeper."""
    fm = _fm("name: t\ndescription: d\nallowed-tools:\n- Bash\n- Read\n")
    assert fm["allowed-tools"] == ["Bash", "Read"]


def test_inline_comments_are_stripped_but_not_inside_values():
    fm = _fm("name: t\ndescription: d # internal note\nurl: http://x/#frag\n")
    assert fm["description"] == "d"
    # '#' only starts a comment at a line start or after whitespace.
    assert fm["url"] == "http://x/#frag"


def test_yaml_only_numbers_are_coerced():
    """Python accepts literals YAML does not. `nan`/`inf` would become floats
    that json.dumps renders as bare NaN/Infinity — invalid JSON that strict
    client parsers reject outright."""
    fm = _fm("name: t\ndescription: d\nthreshold: nan\ncap: inf\n"
             "build: 1_000\nreal: 2.5\nexp: 1e3\ncount: 7\n")
    assert fm["threshold"] == "nan"
    assert fm["cap"] == "inf"
    assert fm["build"] == "1_000"
    assert fm["real"] == 2.5
    assert fm["exp"] == 1000.0
    assert fm["count"] == 7
    json.dumps(fm, allow_nan=False)  # raises if a non-finite float slipped in


def test_missing_frontmatter_raises():
    import pytest
    with pytest.raises(ValueError, match="must begin with YAML frontmatter"):
        _parse_skill_frontmatter("# just a heading\n")


def test_unterminated_frontmatter_raises():
    import pytest
    with pytest.raises(ValueError, match="not terminated"):
        _parse_skill_frontmatter("---\nname: t\n")
