"""SEP-2640 Skills Extension: registration, listing, retrieval, and reads.

https://modelcontextprotocol.io/seps/2640 defines three methods (skills/list,
skills/get, resources/directory/read) and a resource-mapping convention
(skill://<skill-path>/<file-path>) layered entirely on the existing Resources
primitive. These tests pin the server-side half: what a registered skill
looks like on the wire, and that its files are readable and byte-identical
to what skills/list promised.
"""

from __future__ import print_function

import base64
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from nanohubmcp import MCPServer

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _write_skill(base_dir, name, description="A test skill",
                  extra_frontmatter="", files=None):
    # type: (...) -> str
    """Write a skill directory under base_dir/name and return its path."""
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = "---\nname: {}\ndescription: {}\n{}---\n".format(
        name, description, extra_frontmatter)
    (skill_dir / "SKILL.md").write_text(frontmatter + "\n# Body\n")
    for rel_path, content in (files or {}).items():
        target = skill_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    return str(skill_dir)


def _rpc(server, method, params=None, msg_id=1):
    return server._handle_request(
        {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def _server_with_skill(tmp_path, skill_path="git-workflow", **kwargs):
    directory = _write_skill(tmp_path, skill_path.rsplit("/", 1)[-1], **kwargs)
    server = MCPServer("skillsrv", version="0.4.4")

    @server.skill(skill_path)
    def _skill():
        return directory

    return server


# ---------------------------------------------------------------------------
# Registration and capability advertisement
# ---------------------------------------------------------------------------

def test_skill_requires_skill_md(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    server = MCPServer("srv")
    with pytest.raises(ValueError, match="SKILL.md"):
        @server.skill("empty")
        def _skill():
            return str(empty_dir)


def test_skill_name_must_match_final_path_segment(tmp_path):
    directory = _write_skill(tmp_path, "actual-name")
    server = MCPServer("srv")
    with pytest.raises(ValueError, match="must equal the final path segment"):
        @server.skill("declared-name")
        def _skill():
            return directory


def test_skill_requires_description(tmp_path):
    skill_dir = tmp_path / "nodesc"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: nodesc\n---\nbody\n")
    server = MCPServer("srv")
    with pytest.raises(ValueError, match="description"):
        @server.skill("nodesc")
        def _skill():
            return str(skill_dir)


def test_capabilities_advertise_skills_extension_with_directory_read(tmp_path):
    server = _server_with_skill(tmp_path)
    caps = _rpc(server, "initialize", {"protocolVersion": "2025-11-25",
                                        "capabilities": {}})["result"]["capabilities"]
    assert caps["extensions"]["io.modelcontextprotocol/skills"] == {"directoryRead": True}
    # SEP-2640: "A server declaring this extension MUST also declare the
    # resources capability" -- even with no @server.resource() registered.
    assert caps["resources"]["listChanged"] is False


def test_capabilities_omit_skills_extension_with_no_skills():
    server = MCPServer("srv")
    caps = _rpc(server, "initialize", {"protocolVersion": "2025-11-25",
                                        "capabilities": {}})["result"]["capabilities"]
    assert "io.modelcontextprotocol/skills" not in caps.get("extensions", {})
    assert "resources" not in caps


# ---------------------------------------------------------------------------
# skills/list and skills/get
# ---------------------------------------------------------------------------

def test_skills_list_entry_shape(tmp_path):
    server = _server_with_skill(
        tmp_path, description="Follow this team's conventions",
        extra_frontmatter="license: Apache-2.0\n",
        files={"references/GUIDE.md": "# Guide\n"})

    result = _rpc(server, "skills/list")["result"]
    assert len(result["skills"]) == 1
    entry = result["skills"][0]

    assert entry["uri"] == "skill://git-workflow/SKILL.md"
    assert entry["frontmatter"] == {
        "name": "git-workflow",
        "description": "Follow this team's conventions",
        "license": "Apache-2.0",
    }
    uris = sorted(r["uri"] for r in entry["resources"])
    assert uris == [
        "skill://git-workflow/SKILL.md",
        "skill://git-workflow/references/GUIDE.md",
    ]
    for r in entry["resources"]:
        assert _DIGEST_RE.match(r["digest"]), r["digest"]
        assert r["size"] > 0


def test_skills_list_resource_digest_matches_file_content(tmp_path):
    server = _server_with_skill(tmp_path, files={"references/GUIDE.md": "# Guide\n"})
    entry = _rpc(server, "skills/list")["result"]["skills"][0]

    read = _rpc(server, "resources/read",
                {"uri": "skill://git-workflow/references/GUIDE.md"})["result"]
    text = read["contents"][0]["text"]
    actual_digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    manifest_entry = next(
        r for r in entry["resources"]
        if r["uri"] == "skill://git-workflow/references/GUIDE.md")
    assert manifest_entry["digest"] == actual_digest
    assert manifest_entry["size"] == len(text.encode("utf-8"))


def test_skills_get_returns_the_same_entry_as_the_listing(tmp_path):
    server = _server_with_skill(tmp_path)
    listed = _rpc(server, "skills/list")["result"]["skills"][0]
    fetched = _rpc(server, "skills/get",
                   {"uri": "skill://git-workflow/SKILL.md"})["result"]["skill"]
    assert fetched == listed


def test_skills_get_unknown_uri_is_invalid_params(tmp_path):
    server = _server_with_skill(tmp_path)
    error = _rpc(server, "skills/get", {"uri": "skill://nope/SKILL.md"})["error"]
    assert error["code"] == -32602


def test_skills_get_requires_a_string_uri(tmp_path):
    server = _server_with_skill(tmp_path)
    for bad in ({}, {"uri": ""}, {"uri": 5}):
        error = _rpc(server, "skills/get", bad)["error"]
        assert error["code"] == -32602


def test_nested_skill_path_prefix(tmp_path):
    directory = _write_skill(tmp_path, "refunds", description="Process refunds")
    server = MCPServer("srv")

    @server.skill("acme/billing/refunds")
    def _skill():
        return directory

    entry = _rpc(server, "skills/list")["result"]["skills"][0]
    assert entry["uri"] == "skill://acme/billing/refunds/SKILL.md"
    assert entry["frontmatter"]["name"] == "refunds"


# ---------------------------------------------------------------------------
# resources/read for skill:// URIs
# ---------------------------------------------------------------------------

def test_resources_read_serves_skill_md_as_markdown(tmp_path):
    server = _server_with_skill(tmp_path)
    result = _rpc(server, "resources/read",
                  {"uri": "skill://git-workflow/SKILL.md"})["result"]
    content = result["contents"][0]
    assert content["mimeType"] == "text/markdown"
    assert "name: git-workflow" in content["text"]


def test_resources_read_gives_markdown_supporting_files_the_right_mime_type(tmp_path):
    # Regression: Python's stdlib mimetypes has no .md entry, so this used to
    # fall through to application/octet-stream for everything but SKILL.md.
    server = _server_with_skill(tmp_path, files={"references/GUIDE.md": "# Guide\n"})
    result = _rpc(server, "resources/read",
                  {"uri": "skill://git-workflow/references/GUIDE.md"})["result"]
    assert result["contents"][0]["mimeType"] == "text/markdown"


def test_resources_read_serves_binary_skill_files_as_base64_blob(tmp_path):
    payload = bytes(bytearray(range(256)))
    server = _server_with_skill(tmp_path, files={"assets/logo.png": payload})
    result = _rpc(server, "resources/read",
                  {"uri": "skill://git-workflow/assets/logo.png"})["result"]
    content = result["contents"][0]
    assert "text" not in content
    assert base64.b64decode(content["blob"]) == payload


def test_resources_read_unknown_skill_uri_is_not_found(tmp_path):
    server = _server_with_skill(tmp_path)
    error = _rpc(server, "resources/read",
                 {"uri": "skill://git-workflow/nope.md"})["error"]
    assert error["code"] in (-32602, -32601)


# ---------------------------------------------------------------------------
# resources/directory/read
# ---------------------------------------------------------------------------

def test_resources_directory_read_lists_direct_children_only(tmp_path):
    server = _server_with_skill(tmp_path, files={
        "references/GUIDE.md": "# Guide\n",
        "references/nested/DEEP.md": "# Deep\n",
        "scripts/run.py": "print('hi')\n",
    })

    root = _rpc(server, "resources/directory/read",
                {"uri": "skill://git-workflow"})["result"]["resources"]
    by_uri = {r["uri"].rsplit("/", 1)[-1]: r for r in root}
    assert set(by_uri) == {"SKILL.md", "references", "scripts"}
    assert by_uri["references"]["mimeType"] == "inode/directory"
    assert by_uri["scripts"]["mimeType"] == "inode/directory"

    # The SKILL.md resource's name and description come from its frontmatter,
    # not from the filename, so a host can build a skill registry from a
    # directory listing without fetching the file.
    skill_md = by_uri["SKILL.md"]
    assert skill_md["mimeType"] == "text/markdown"
    assert skill_md["name"] == "git-workflow"
    assert skill_md["description"] == "A test skill"

    refs = _rpc(server, "resources/directory/read",
               {"uri": "skill://git-workflow/references"})["result"]["resources"]
    ref_names = {r["name"]: r["mimeType"] for r in refs}
    assert ref_names == {"GUIDE.md": "text/markdown", "nested": "inode/directory"}


def test_empty_directory_yields_an_empty_array_not_an_error(tmp_path):
    """SEP-2640: "An empty directory yields an empty `resources` array", and a
    server declaring directoryRead must answer for *every* directory in the
    namespace. An empty dir has no files to infer it from, so it has to come
    from the directory walk itself."""
    directory = _write_skill(tmp_path, "git-workflow")
    (tmp_path / "git-workflow" / "scripts").mkdir()
    server = MCPServer("srv")

    @server.skill("git-workflow")
    def _skill():
        return directory

    result = _rpc(server, "resources/directory/read",
                  {"uri": "skill://git-workflow/scripts"})["result"]
    assert result["resources"] == []


def test_dotfiles_are_not_published(tmp_path):
    """A skill directory that is a git checkout would otherwise publish
    .git/config and .env to every connected client."""
    directory = _write_skill(tmp_path, "git-workflow",
                             files={"references/GUIDE.md": "# Guide\n",
                                    ".env": "TOKEN=secret\n"})
    (tmp_path / "git-workflow" / ".git").mkdir()
    (tmp_path / "git-workflow" / ".git" / "config").write_text("[core]\n")
    server = MCPServer("srv")

    @server.skill("git-workflow")
    def _skill():
        return directory

    entry = _rpc(server, "skills/list")["result"]["skills"][0]
    uris = {r["uri"] for r in entry["resources"]}
    assert uris == {
        "skill://git-workflow/SKILL.md",
        "skill://git-workflow/references/GUIDE.md",
    }
    # And they are not readable by URI either.
    assert "error" in _rpc(server, "resources/read",
                           {"uri": "skill://git-workflow/.env"})


def test_organizational_prefix_directories_are_listable(tmp_path):
    """`skill://acme` and `skill://acme/billing` are directories in the
    namespace even though nothing on disk corresponds to them; a host walking
    a virtual mount reaches the skill through them."""
    server = MCPServer("srv")
    for name in ("refunds", "invoices"):
        directory = _write_skill(tmp_path, name)

        def _skill(_d=directory):
            return _d

        server.skill("acme/billing/" + name)(_skill)

    top = _rpc(server, "resources/directory/read",
               {"uri": "skill://acme"})["result"]["resources"]
    assert [(c["name"], c["mimeType"]) for c in top] == [("billing", "inode/directory")]

    # Both skills appear under the shared prefix — the second registration
    # must merge into it, not replace it.
    billing = _rpc(server, "resources/directory/read",
                   {"uri": "skill://acme/billing"})["result"]["resources"]
    assert {c["name"] for c in billing} == {"refunds", "invoices"}


def test_resources_directory_read_unknown_uri_is_invalid_params(tmp_path):
    server = _server_with_skill(tmp_path)
    error = _rpc(server, "resources/directory/read",
                {"uri": "skill://git-workflow/references"})["error"]
    assert error["code"] == -32602


def test_resources_directory_read_rejects_a_file_uri(tmp_path):
    server = _server_with_skill(tmp_path)
    error = _rpc(server, "resources/directory/read",
                {"uri": "skill://git-workflow/SKILL.md"})["error"]
    assert error["code"] == -32602
