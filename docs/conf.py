# Configuration file for the Sphinx documentation builder.

import os
import sys

# Add the project root to the path so autodoc can find the package
sys.path.insert(0, os.path.abspath(".."))

from nanohubmcp._version import __version__

# -- Project information -----------------------------------------------------

project = "nanohub-mcp"
copyright = "2025, nanoHUB"
author = "nanoHUB"
version = __version__
release = __version__

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "myst_parser",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Bring the build-nanohub-mcp skill into the docs tree --------------------
# The skill lives at repo_root/build-nanohub-mcp as Markdown so it works as a
# Claude skill. Copy it under docs/skill/ at build time so MyST can render it
# and its relative reference/example links keep resolving. The copy is
# regenerated on every build and is git-ignored.

import shutil

_docs_dir = os.path.dirname(__file__)
_repo_root = os.path.abspath(os.path.join(_docs_dir, ".."))
_skill_src = os.path.join(_repo_root, "build-nanohub-mcp")
_skill_dst = os.path.join(_docs_dir, "skill")

if os.path.isdir(_skill_src):
    shutil.rmtree(_skill_dst, ignore_errors=True)
    shutil.copytree(_skill_src, _skill_dst)

    # SKILL.md carries YAML frontmatter that MyST would render as a docinfo
    # field list. Strip it and write the body to skill/_skill_body.md, which
    # docs/building-mcp.md includes. Exclude the raw SKILL.md from the build so
    # it does not become an orphan document.
    _skill_md = os.path.join(_skill_dst, "SKILL.md")
    with open(_skill_md, encoding="utf-8") as fh:
        _text = fh.read()
    if _text.startswith("---"):
        # Drop the leading YAML frontmatter block (--- ... ---).
        _parts = _text.split("---", 2)
        _body = _parts[2].lstrip("\n") if len(_parts) == 3 else _text
    else:
        _body = _text
    # The H1 is provided by building-mcp.md; drop the skill's own top heading
    # so the page has a single title.
    _body = "\n".join(
        line for line in _body.splitlines()
        if not line.startswith("# ")
    )
    # SKILL.md's links are relative to the skill root (references/..., examples/
    # ...). The body is included from docs/building-mcp.md, so rewrite them to
    # point at the copied skill/ subtree. Turn .md reference links into bare
    # doc references so MyST resolves them to the rendered pages.
    import re

    _body = re.sub(r"\]\(references/([^)]+?)\.md\)", r"](skill/references/\1)", _body)
    # The example is a source directory, not a rendered page; link it to GitHub.
    _gh_tree = "https://github.com/denphi/nanohub-mcp/tree/main/build-nanohub-mcp"
    _body = _body.replace("](examples/", "](%s/examples/" % _gh_tree)
    with open(
        os.path.join(_skill_dst, "_skill_body.md"), "w", encoding="utf-8"
    ) as fh:
        fh.write(_body)
    # Only skill/references/*.md are rendered as pages (via the toctree in
    # building-mcp.md). SKILL.md is surfaced through _skill_body.md, which is an
    # include fragment; example READMEs and any other Markdown in the copied
    # tree must not become standalone/orphan documents. Exclude every .md under
    # skill/ except the reference pages.
    exclude_patterns.append("skill/*.md")
    exclude_patterns.append("skill/examples/**/*.md")

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

# Create _static dir if it doesn't exist (avoids warnings)
os.makedirs(os.path.join(os.path.dirname(__file__), "_static"), exist_ok=True)
