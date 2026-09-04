"""Tests for cross-environment ``start_mcp`` launching."""

from __future__ import print_function

import json
import os
import shutil
import subprocess
import sys

import nanohubmcp
from nanohubmcp import cli


def test_runner_environment_excludes_launcher_package_root(monkeypatch, tmp_path):
    """The child's PYTHONPATH carries the app and the session, nothing more.

    0.4.1 added the launcher's own package root here to pin the framework
    version.  For a pip-installed launcher that root is a shared
    site-packages, so it handed the child every library sitting beside the
    framework — a NumPy built for the launcher's Python among them.  The
    runner pins the framework by file instead, so this must stay narrow.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    app_path = app_dir / "server.py"
    app_path.write_text("server = None\n")

    session_site = tmp_path / "session-site"
    session_site.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(session_site))

    env = cli._runner_environment(str(app_path))
    paths = env["PYTHONPATH"].split(os.pathsep)
    framework_root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))

    assert paths == [str(app_dir), str(session_site)]
    assert framework_root not in paths


def test_runner_environment_has_no_empty_path_entry(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    app_path = tmp_path / "server.py"
    env = cli._runner_environment(str(app_path))

    assert "" not in env["PYTHONPATH"].split(os.pathsep)


RECORDING_APP = """\
import json
import os
import sys

import nanohubmcp
import numpy


class RecordingServer(object):
    def run(self, **kwargs):
        with open(os.environ["NANOHUB_MCP_TEST_RESULT"], "w") as result:
            json.dump({
                "python": sys.executable,
                "framework_version": nanohubmcp.__version__,
                "framework_file": nanohubmcp.__file__,
                "numpy_version": numpy.__version__,
                "numpy_file": numpy.__file__,
                "sys_path": sys.path,
            }, result)


server = RecordingServer()
"""


def _real_paths(paths):
    """sys.path as the child saw it, comparable across symlinks."""
    return [os.path.realpath(entry) for entry in paths if entry]


def _venv_python(venv_dir):
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _write_stub_package(directory, name, version):
    """Write a package whose only content is a recognisable __version__."""
    package = os.path.join(str(directory), name)
    os.makedirs(package)
    with open(os.path.join(package, "__init__.py"), "w") as f:
        f.write("__version__ = {!r}\n".format(version))
    return package


def _make_env(tmp_path, name, numpy_version):
    """Build a real interpreter, isolated from this one, holding a stub numpy.

    Returns its directory and site-packages path.  Call before patching
    ``subprocess.run``: ``check_output`` is implemented in terms of it.
    """
    env_dir = tmp_path / "envs" / name
    subprocess.check_call([
        sys.executable, "-m", "venv", "--without-pip", str(env_dir)
    ])
    site = subprocess.check_output([
        str(_venv_python(env_dir)), "-c",
        "import sysconfig; print(sysconfig.get_paths()['purelib'])",
    ]).decode("utf-8").strip()
    _write_stub_package(site, "numpy", numpy_version)
    return env_dir, site


def _run_start_mcp(monkeypatch, tmp_path, env_dir, env_name):
    """Drive start_mcp --python-env against a stubbed conda, and report back.

    Returns what the app actually imported, plus the ``conda`` commands run.
    """
    result_path = tmp_path / "loaded.json"
    monkeypatch.setenv("NANOHUB_MCP_TEST_RESULT", str(result_path))
    app_path = tmp_path / "server.py"
    app_path.write_text(RECORDING_APP)

    conda_calls = []

    class CondaInfo(object):
        stdout = json.dumps({"envs": [str(env_dir)]})

    def conda_run(command, **kwargs):
        conda_calls.append(command)
        return CondaInfo()

    monkeypatch.setattr(subprocess, "run", conda_run)
    for variable in ("SESSION", "SESSION_ID", "SESSIONDIR", "SESSION_DIR"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(sys, "argv", [
        "start_mcp", "--app", str(app_path), "--python-env", env_name
    ])

    cli.start_mcp_main()

    return json.loads(result_path.read_text()), conda_calls


def test_python_env_uses_selected_interpreter_and_its_numpy(monkeypatch, tmp_path):
    """Exercise the CLI branch with a genuinely separate Python environment.

    An inherited PYTHONPATH models a launcher environment containing a newer,
    incompatible numpy.  The app must instead import numpy from the environment
    named by --python-env while importing nanohubmcp from this launcher.
    """
    env_dir, selected_site = _make_env(tmp_path, "scientific", "1.26.4-selected-env")
    _write_stub_package(selected_site, "nanohubmcp", "0.3.3-selected-env")

    launcher_site = tmp_path / "launcher-site"
    launcher_site.mkdir()
    _write_stub_package(launcher_site, "numpy", "2.2.6-launcher-env")
    monkeypatch.setenv("PYTHONPATH", str(launcher_site))

    loaded, conda_calls = _run_start_mcp(
        monkeypatch, tmp_path, env_dir, "scientific")

    framework_root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    assert conda_calls == [["conda", "info", "--json"]]
    assert os.path.realpath(loaded["python"]) == os.path.realpath(
        str(_venv_python(env_dir)))
    assert loaded["framework_version"] == nanohubmcp.__version__
    assert os.path.commonpath([loaded["framework_file"], framework_root]) == framework_root
    assert loaded["numpy_version"] == "1.26.4-selected-env"
    assert os.path.commonpath([loaded["numpy_file"], selected_site]) == selected_site

    # The framework arrives by file, so its directory never joins the search
    # path; the session's own entries still do, behind the chosen environment.
    child = _real_paths(loaded["sys_path"])
    assert os.path.realpath(framework_root) not in child
    assert os.path.realpath(str(launcher_site)) in child
    assert child.index(os.path.realpath(selected_site)) < child.index(
        os.path.realpath(str(launcher_site)))


def test_python_env_ignores_packages_beside_an_installed_launcher(monkeypatch, tmp_path):
    """A pip-installed launcher shares site-packages with unrelated libraries.

    Pinning that whole directory would pull the launcher's numpy in ahead of
    the selected environment, which is the very clash --python-env exists to
    avoid.  Only the nanohubmcp package itself may be pinned.
    """
    env_dir, selected_site = _make_env(tmp_path, "scientific", "1.26.4-selected-env")
    _write_stub_package(selected_site, "nanohubmcp", "0.3.3-selected-env")

    launcher_site = tmp_path / "launcher-site"
    launcher_site.mkdir()
    installed_framework = launcher_site / "nanohubmcp"
    shutil.copytree(
        os.path.dirname(os.path.abspath(nanohubmcp.__file__)),
        str(installed_framework),
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    _write_stub_package(launcher_site, "numpy", "2.2.6-launcher-env")
    monkeypatch.setattr(cli, "__file__", str(installed_framework / "cli.py"))
    monkeypatch.delenv("PYTHONPATH", raising=False)

    loaded, _ = _run_start_mcp(monkeypatch, tmp_path, env_dir, "scientific")

    assert loaded["framework_version"] == nanohubmcp.__version__
    assert os.path.commonpath(
        [loaded["framework_file"], str(installed_framework)]
    ) == str(installed_framework)
    assert loaded["numpy_version"] == "1.26.4-selected-env"
    assert os.path.commonpath([loaded["numpy_file"], selected_site]) == selected_site

    # The whole point: nothing beside the launcher is reachable at all.
    assert os.path.realpath(str(launcher_site)) not in _real_paths(loaded["sys_path"])
