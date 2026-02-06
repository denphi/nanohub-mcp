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
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]

# Create _static dir if it doesn't exist (avoids warnings)
os.makedirs(os.path.join(os.path.dirname(__file__), "_static"), exist_ok=True)
