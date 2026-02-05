"""Backwards-compatible setup.py for older pip versions that don't support pyproject.toml."""

import os
import re
from setuptools import setup, find_packages


def get_version():
    """Read version from nanohubmcp/_version.py without importing the package."""
    version_file = os.path.join(
        os.path.dirname(__file__), "nanohubmcp", "_version.py"
    )
    with open(version_file, "r") as f:
        content = f.read()
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        raise RuntimeError("Unable to find version in nanohubmcp/_version.py")
    return match.group(1)


def get_long_description():
    """Read README.md for long description."""
    here = os.path.dirname(os.path.abspath(__file__))
    readme_path = os.path.join(here, "README.md")
    if os.path.exists(readme_path):
        with open(readme_path, "r") as f:
            return f.read()
    return ""


setup(
    name="nanohub-mcp",
    version=get_version(),
    description="MCP (Model Context Protocol) server library for nanoHUB tools",
    long_description=get_long_description(),
    long_description_content_type="text/markdown",
    license="MIT",
    author="nanoHUB",
    author_email="support@nanohub.org",
    url="https://nanohub.org",
    project_urls={
        "Homepage": "https://nanohub.org",
        "Documentation": "https://nanohub.org/documentation",
        "Repository": "https://github.com/nanohub/nanohub-mcp",
        "Issues": "https://github.com/nanohub/nanohub-mcp/issues",
    },
    keywords=[
        "mcp",
        "model-context-protocol",
        "nanohub",
        "hubzero",
        "ai",
        "llm",
        "tools",
        "sse",
        "server-sent-events",
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    packages=find_packages(where=".", include=["nanohubmcp*"]),
    python_requires=">=3.6",
    install_requires=[],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "pytest-cov>=4.0",
            "black>=23.0",
            "mypy>=1.0",
            "ruff>=0.1.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "start_mcp=nanohubmcp.cli:start_mcp_main",
        ],
    },
)
