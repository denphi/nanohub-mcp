"""Tests for cross-environment ``start_mcp`` launching."""

from __future__ import print_function

import os
import subprocess
import sys

import nanohubmcp
from nanohubmcp import cli


def test_runner_environment_pins_launchers_framework(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    app_path = app_dir / "server.py"
    app_path.write_text("server = None\n")

    old_site = tmp_path / "old-site"
    old_package = old_site / "nanohubmcp"
    old_package.mkdir(parents=True)
    (old_package / "__init__.py").write_text("__version__ = '0.3.3'\n")
    monkeypatch.setenv("PYTHONPATH", str(old_site))

    env = cli._runner_environment(str(app_path))
    paths = env["PYTHONPATH"].split(os.pathsep)
    framework_root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))

    assert paths == [str(app_dir), framework_root, str(old_site)]

    # Model the real failure: the selected scientific Python environment has
    # an old nanohubmcp installation.  The child must still import the exact
    # framework that generated its runner.
    imported = subprocess.check_output(
        [sys.executable, "-c",
         "import nanohubmcp; print(nanohubmcp.__version__)"],
        env=env,
    ).decode("utf-8").strip()
    assert imported != "0.3.3"
    assert imported == nanohubmcp.__version__


def test_runner_environment_has_no_empty_path_entry(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    app_path = tmp_path / "server.py"
    env = cli._runner_environment(str(app_path))

    assert "" not in env["PYTHONPATH"].split(os.pathsep)
