#!/usr/bin/env python3
"""Scaffold a new nanoHUB MCP tool from your scientific Python code.

Usage:
    python new_tool.py mysolver [--dest ~/Documents/Github]

Creates the standard nanoHUB tool layout with a working server template
(create -> run(async) -> get pattern), the middleware/invoke file, an offline
test suite, and a doc stub. Replace the TODO blocks with calls into your code.
"""

from __future__ import print_function

import argparse
import os
import re
import shutil
import stat
import sys

SERVER_TEMPLATE = '''"""{name} — MCP server exposing {name} on nanoHUB.

Run locally:   start_mcp --app bin/{name}.py --port 8000
Validate:      python scripts/validate_server.py bin/{name}.py
"""

import json
import os
import re
import secrets
import shutil
import tempfile
import threading

from nanohubmcp import MCPServer, ToolResult

APP_NAME = "{name}"
APP_VERSION = "0.1.0"

# `server` (this exact name) is what start_mcp serves.
server = MCPServer(APP_NAME, version=APP_VERSION)

RUN_ROOT = os.path.realpath(os.path.join(tempfile.gettempdir(), "{name}-runs"))
HANDLE_RE = re.compile(r"^{name}_[a-f0-9]{{24}}$")
MAX_CELL_UPDATES = 5_000_000
_RUN_LOCKS = {{}}
_RUN_LOCKS_GUARD = threading.Lock()
os.makedirs(RUN_ROOT, mode=0o700, exist_ok=True)


def _error(code, message):
    # Keep errors server-authored; never reflect arbitrary model input.
    return ToolResult(content=json.dumps({{"error": code, "message": message}},
                                          sort_keys=True), is_error=True)


def _resolve_run_handle(run_handle):
    if not isinstance(run_handle, str) or not HANDLE_RE.fullmatch(run_handle):
        raise ValueError("invalid run handle")
    joined = os.path.join(RUN_ROOT, run_handle)
    if os.path.islink(joined):
        raise ValueError("invalid run handle")
    resolved = os.path.realpath(joined)
    if os.path.commonpath([RUN_ROOT, resolved]) != RUN_ROOT:
        raise ValueError("invalid run handle")
    if not os.path.isdir(resolved):
        raise ValueError("unknown run handle")
    return resolved


def _run_file(run_handle, filename):
    if filename not in ("input.json", "output.json"):
        raise ValueError("invalid run file")
    run_dir = _resolve_run_handle(run_handle)
    path = os.path.join(run_dir, filename)
    if os.path.islink(path):
        raise ValueError("invalid run file")
    return path


def _new_run():
    for _ in range(10):
        handle = "{name}_" + secrets.token_hex(12)
        path = os.path.join(RUN_ROOT, handle)
        try:
            os.mkdir(path, 0o700)
            with _RUN_LOCKS_GUARD:
                _RUN_LOCKS[handle] = threading.Lock()
            return handle, path
        except FileExistsError:
            continue
    raise RuntimeError("could not allocate a run handle")


def _lock_for(run_handle):
    _resolve_run_handle(run_handle)
    with _RUN_LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(run_handle, threading.Lock())


def _bounded_integer(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(field + " must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(field + " is outside the supported range")
    return value


_CREATE_INPUT_SCHEMA = {{
    "type": "object",
    "properties": {{
        "mesh_points": {{"type": "integer", "minimum": 5,
                          "maximum": 10000, "default": 101,
                          "description": "TODO: map to the solver mesh size."}},
        "time_steps": {{"type": "integer", "minimum": 1,
                         "maximum": 100000, "default": 1000,
                         "description": "TODO: map to the solver time/sweep steps."}},
    }},
    "additionalProperties": False,
}}
_HANDLE_INPUT_SCHEMA = {{
    "type": "object",
    "properties": {{"run_handle": {{"type": "string",
                                      "pattern": HANDLE_RE.pattern,
                                      "description": "Opaque handle returned by create_simulation."}}}},
    "required": ["run_handle"],
    "additionalProperties": False,
}}


# --- output schemas: small required envelope, extra fields stay open --------

_CREATE_OUTPUT_SCHEMA = {{
    "type": "object",
    "properties": {{
        "run_handle": {{"type": "string"}},
        "inputs": {{"type": "object"}},
        "estimate": {{"type": "object"}},
    }},
    "required": ["run_handle", "inputs", "estimate"],
}}

_RUN_OUTPUT_SCHEMA = {{
    "type": "object",
    "properties": {{
        "status": {{"type": "string"}},
        "warnings": {{"type": "array", "items": {{"type": "string"}}}},
    }},
    "required": ["status"],
}}

_RESULTS_OUTPUT_SCHEMA = {{
    "type": "object",
    "properties": {{
        "columns": {{"type": "array", "items": {{"type": "string"}}}},
        "data": {{"type": "array"}},
        "units": {{"type": "object"}},
    }},
    "required": ["columns", "data"],
}}


@server.tool(
    input_schema=_CREATE_INPUT_SCHEMA,
    output_schema=_CREATE_OUTPUT_SCHEMA,
    annotations={{"title": "Create {name} simulation",
                 "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False}},
)
def create_simulation(mesh_points=101, time_steps=1000):
    """Create a bounded {name} simulation and return an opaque run handle.

    IMPORTANT for maintainers: the ZERO-ARGUMENT call must produce a
    physically meaningful simulation — it is the model's first call.

    Args:
        mesh_points: TODO map to a bounded mesh dimension (default 101)
        time_steps: TODO map to bounded time/sweep steps (default 1000)

    Returns:
        run_handle — pass it to run_simulation and get_results.
    """
    try:
        mesh = _bounded_integer(mesh_points, "mesh_points", 5, 10000)
        steps = _bounded_integer(time_steps, "time_steps", 1, 100000)
        work = mesh * steps
        if work > MAX_CELL_UPDATES:
            raise ValueError("requested inputs exceed the computation budget")
        run_handle, run_dir = _new_run()
        with open(os.path.join(run_dir, "input.json"), "w") as stream:
            json.dump({{"mesh_points": mesh, "time_steps": steps}}, stream)
    except ValueError as exc:
        return _error("invalid_input", str(exc))
    except (OSError, RuntimeError):
        return _error("run_create_failed", "the run could not be created")
    # TODO: call your code to generate fixed-name input files in run_dir.
    return {{"run_handle": run_handle,
            "inputs": {{"mesh_points": mesh, "time_steps": steps}},
            "estimate": {{"work_units": work,
                          "max_work_units": MAX_CELL_UPDATES}}}}


@server.async_tool(
    input_schema=_HANDLE_INPUT_SCHEMA,
    output_schema=_RUN_OUTPUT_SCHEMA,
    annotations={{"title": "Run {name} simulation", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False}},
)
def run_simulation(run_handle):
    """Run the solver for an opaque run handle. Long-running: returns a task/job —
    poll for the result, then call get_results.

    Args:
        run_handle: value returned by create_simulation.
    """
    try:
        lock = _lock_for(run_handle)
        if not lock.acquire(False):
            return _error("run_busy", "the run is already active")
    except ValueError:
        return _error("invalid_run_handle", "the run handle or input is invalid")
    try:
        run_dir = _resolve_run_handle(run_handle)
        with open(_run_file(run_handle, "input.json")) as stream:
            json.load(stream)
        # TODO: replace with a bounded subprocess.run([...], shell=False,
        # cwd=run_dir, timeout=...) or a bounded in-process solver. Runs in a
        # background thread; the return value is stored as the task result.
        import time
        time.sleep(1)
        return {{"status": "finished", "warnings": []}}
    except (ValueError, OSError, json.JSONDecodeError):
        return _error("invalid_run_data", "the stored run input is invalid")
    finally:
        lock.release()


@server.tool(
    input_schema=_HANDLE_INPUT_SCHEMA,
    output_schema=_RESULTS_OUTPUT_SCHEMA,
    annotations={{"title": "Get {name} results", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False}},
)
def get_results(run_handle):
    """Read the primary result table from a finished run.

    Args:
        run_handle: value returned by create_simulation.

    Returns:
        columns, data (row-major), units — plot-ready.
    """
    try:
        _resolve_run_handle(run_handle)
    except ValueError:
        return _error("invalid_run_handle", "the run handle is invalid or unknown")
    # TODO: parse your solver output
    return {{"columns": ["x", "y"], "data": [[0, 0], [1, 1]],
            "units": {{"x": "um", "y": "a.u."}}}}


@server.tool(
    input_schema=_HANDLE_INPUT_SCHEMA,
    output_schema={{"type": "object", "properties": {{"status": {{"type": "string"}}}},
                    "required": ["status"]}},
    annotations={{"title": "Delete {name} run", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": False}},
)
def delete_run(run_handle):
    """Delete one opaque, confined run handle after explicit user approval."""
    try:
        run_dir = _resolve_run_handle(run_handle)
        lock = _lock_for(run_handle)
        if not lock.acquire(False):
            return _error("run_busy", "the active run cannot be deleted")
    except ValueError:
        return _error("delete_failed", "the run is invalid or could not be deleted")
    try:
        shutil.rmtree(run_dir)
    except OSError:
        return _error("delete_failed", "the run is invalid or could not be deleted")
    finally:
        lock.release()
        with _RUN_LOCKS_GUARD:
            _RUN_LOCKS.pop(run_handle, None)
    return {{"status": "deleted"}}


@server.resource("config://{name}/about", mime_type="application/json")
def about():
    """What this tool simulates, its parameters, units, and defaults."""
    return {{"name": APP_NAME, "version": APP_VERSION,
            "description": "TODO: one paragraph on the science."}}
'''

INVOKE_TEMPLATE = '''#!/bin/sh

#
# {name} — nanoHUB MCP tool entrypoint (headless).
# Pin start_mcp to the conda env that has your dependencies; the bare name
# resolves against the anaconda base env and your imports will fail.
#
/usr/bin/invoke_app "$@" -t {name} \\
                         -e "PYTHONNOUSERSITE=1" \\
                         -C "start_mcp --app @tool/bin/{name}.py" \\
                         -u anaconda-6 \\
                         -r none \\
                         -w headless
'''

TEST_TEMPLATE = '''"""Offline tests for {name} — no solver, no hub session needed.

Run:  python3 -m pytest tests/test_offline.py -q

Unlike skip-happy suites, a broken import FAILS here: silently-skipped
suites report green while testing nothing.
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(_HERE, os.pardir, "bin", "{name}.py")

spec = importlib.util.spec_from_file_location("{name}", _APP)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)          # let import errors fail the suite loudly

from jsonschema import Draft202012Validator, validate


class TestToolContracts(unittest.TestCase):
    def test_server_exists(self):
        self.assertTrue(hasattr(mod, "server"))

    def test_every_tool_has_description_and_schemas(self):
        for name, entry in mod.server._tools.items():
            d = entry["definition"].to_dict()
            self.assertTrue((d.get("description") or "").strip(),
                            name + ": empty description")
            self.assertIn("inputSchema", d, name)
            Draft202012Validator.check_schema(d["inputSchema"])
            if d.get("outputSchema"):
                Draft202012Validator.check_schema(d["outputSchema"])

    def test_create_defaults_are_valid(self):
        """The zero-argument create call is the model's first call —
        it must succeed and honor its own output schema."""
        entry = mod.server._tools["create_simulation"]
        result = entry["handler"]()
        self.assertIn("run_handle", result)
        validate(result, entry["definition"].to_dict()["outputSchema"])
        deleted = mod.server._tools["delete_run"]["handler"](result["run_handle"])
        self.assertEqual(deleted["status"], "deleted")

    def test_get_results_rejects_bad_handle(self):
        result = mod.server._tools["get_results"]["handler"](
            run_handle="../foreign-run")
        self.assertTrue(getattr(result, "is_error", False)
                        or (isinstance(result, dict) and result.get("isError")))

    def test_create_rejects_runaway_work(self):
        result = mod.server._tools["create_simulation"]["handler"](
            mesh_points=10000, time_steps=100000)
        self.assertTrue(getattr(result, "is_error", False))

    def test_delete_rejects_foreign_path(self):
        result = mod.server._tools["delete_run"]["handler"]("/tmp")
        self.assertTrue(getattr(result, "is_error", False))

    def test_delete_rejects_active_run(self):
        created = mod.server._tools["create_simulation"]["handler"]()
        handle = created["run_handle"]
        lock = mod._lock_for(handle)
        lock.acquire()
        try:
            result = mod.server._tools["delete_run"]["handler"](handle)
            self.assertTrue(getattr(result, "is_error", False))
        finally:
            lock.release()
        deleted = mod.server._tools["delete_run"]["handler"](handle)
        self.assertEqual(deleted["status"], "deleted")


if __name__ == "__main__":
    unittest.main()
'''

DOC_TEMPLATE = '''<p><b>{name}</b> exposes TODO-your-science as an MCP server, so AI
assistants (Claude, ChatGPT, the hub chat) can create, run, and analyze
simulations conversationally.</p>
<p>MCP endpoint: <code>https://nanohub.org/api/mcp/{name}/mcp</code></p>
'''

README_TEMPLATE = '''# {name}

A nanoHUB MCP tool. Scaffolded by the build-nanohub-mcp skill.

## Develop
    start_mcp --app bin/{name}.py --port 8000
    python3 -m pytest tests/ -q

## Layout
    bin/{name}.py        the MCP server (create -> run -> get pattern)
    middleware/invoke    how nanoHUB launches it (headless)
    scripts/validate_server.py copied pre-deploy validator
    .github/workflows/ci.yml offline test and validator gate
    tests/               offline tests (no solver needed)
    doc/description.html tool page

See the build-nanohub-mcp skill references for schemas, async tasks, MCP Apps,
publishing, and live verification.
'''

CI_TEMPLATE = '''name: {name} MCP checks

on:
  push:
  pull_request:

jobs:
  offline:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
      - run: python -m pip install --upgrade pip
      - run: python -m pip install nanohub-mcp jsonschema pytest
      - run: python -m pytest tests/ -q -r a --junitxml=test-results.xml
      - name: Reject skipped or empty coverage
        run: python scripts/check_ci.py test-results.xml
      - run: python scripts/validate_server.py bin/{name}.py
'''

CHECK_CI_TEMPLATE = '''"""Fail CI when tests are skipped, empty, or failing."""
import sys
import xml.etree.ElementTree as ElementTree

root = ElementTree.parse(sys.argv[1]).getroot()
skipped = sum(int(item.attrib.get("skipped", "0"))
              for item in root.iter("testsuite"))
tests = sum(int(item.attrib.get("tests", "0"))
            for item in root.iter("testsuite"))
failures = sum(int(item.attrib.get("failures", "0")) +
               int(item.attrib.get("errors", "0"))
               for item in root.iter("testsuite"))
if skipped:
    raise SystemExit("skipped tests: {}".format(skipped))
if not tests:
    raise SystemExit("no tests collected")
if failures:
    raise SystemExit("test failures/errors: {}".format(failures))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="tool name: lowercase, letters/digits/underscore")
    parser.add_argument("--dest", default=".", help="parent directory (default: cwd)")
    args = parser.parse_args()

    name = args.name
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        print("ERROR: tool name must be lowercase snake_case (it becomes the "
              "URL and the invoke -t flag)")
        return 1

    root = os.path.join(os.path.abspath(os.path.expanduser(args.dest)), name)
    if os.path.exists(root):
        print("ERROR: {} already exists".format(root))
        return 1

    files = {
        os.path.join("bin", name + ".py"): SERVER_TEMPLATE,
        os.path.join("middleware", "invoke"): INVOKE_TEMPLATE,
        os.path.join("tests", "test_offline.py"): TEST_TEMPLATE,
        os.path.join("doc", "description.html"): DOC_TEMPLATE,
        os.path.join(".github", "workflows", "ci.yml"): CI_TEMPLATE,
        "README.md": README_TEMPLATE,
    }
    for rel, template in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        with open(path, "w") as f:
            f.write(template.format(name=name))
    for extra in ("examples", "data", "src"):
        os.makedirs(os.path.join(root, extra), exist_ok=True)
        open(os.path.join(root, extra, ".keep"), "w").close()

    validator = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "validate_server.py")
    if os.path.isfile(validator):
        target = os.path.join(root, "scripts", "validate_server.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(validator, target)
    check_ci = os.path.join(root, "scripts", "check_ci.py")
    with open(check_ci, "w") as stream:
        stream.write(CHECK_CI_TEMPLATE)

    invoke = os.path.join(root, "middleware", "invoke")
    os.chmod(invoke, os.stat(invoke).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print("Created {}".format(root))
    print("\nNext steps:")
    print("  1. pip install nanohub-mcp jsonschema")
    print("  2. Fill the TODOs in bin/{}.py with calls into your code".format(name))
    print("  3. python3 -m pytest {}/tests -q".format(name))
    print("  4. start_mcp --app {}/bin/{}.py --port 8000".format(name, name))
    print("  5. python {}/scripts/validate_server.py {}/bin/{}.py".format(
        name, name, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
