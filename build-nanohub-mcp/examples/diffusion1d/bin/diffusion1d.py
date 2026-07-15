"""Secure worked example: expose a bounded 1D diffusion solver through MCP."""

import json
import math
import os
import re
import secrets
import shutil
import tempfile
import threading

from nanohubmcp import MCPServer, ToolResult

APP_NAME = "diffusion1d"
APP_VERSION = "0.2.0"
MAX_CELL_UPDATES = 5_000_000
MAX_TIME_STEPS = 200_000
MAX_RUN_FILE_BYTES = 8 * 1024 * 1024
MAX_RESULT_POINTS = 500

_RUN_ROOT = os.path.realpath(os.environ.get(
    "DIFFUSION1D_RUN_ROOT",
    os.path.join(tempfile.gettempdir(), "diffusion1d-runs"),
))
_HANDLE_RE = re.compile(r"^diffusion1d_[a-f0-9]{24}$")
_RUN_LOCKS = {}
_RUN_LOCKS_GUARD = threading.Lock()

os.makedirs(_RUN_ROOT, mode=0o700, exist_ok=True)
server = MCPServer(APP_NAME, version=APP_VERSION)


def _error(code, message):
    """Return fixed server-authored error data; never reflect caller input."""
    return ToolResult(
        content=json.dumps({"error": code, "message": message}, sort_keys=True),
        is_error=True,
    )


def _finite_number(value, field):
    if isinstance(value, bool):
        raise ValueError("{} must be a finite number".format(field))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be a finite number".format(field))
    if not math.isfinite(number):
        raise ValueError("{} must be a finite number".format(field))
    return number


def _resolve_run_handle(run_handle, must_exist=True):
    """Resolve one opaque handle beneath the application-owned run root."""
    if not isinstance(run_handle, str) or not _HANDLE_RE.fullmatch(run_handle):
        raise ValueError("invalid run handle")
    joined = os.path.join(_RUN_ROOT, run_handle)
    if os.path.islink(joined):
        raise ValueError("invalid run handle")
    resolved = os.path.realpath(joined)
    if os.path.commonpath([_RUN_ROOT, resolved]) != _RUN_ROOT:
        raise ValueError("invalid run handle")
    if must_exist and not os.path.isdir(resolved):
        raise ValueError("unknown run handle")
    return resolved


def _run_file(run_handle, filename, must_exist=False):
    if filename not in ("input.json", "output.json"):
        raise ValueError("invalid run file")
    run_dir = _resolve_run_handle(run_handle)
    path = os.path.join(run_dir, filename)
    if os.path.islink(path):
        raise ValueError("invalid run file")
    if os.path.commonpath([run_dir, os.path.realpath(path)]) != run_dir:
        raise ValueError("invalid run file")
    if must_exist:
        if not os.path.isfile(path):
            raise ValueError("run file is not available")
        if os.path.getsize(path) > MAX_RUN_FILE_BYTES:
            raise ValueError("run file exceeds the size limit")
    return path


def _new_run():
    for _ in range(10):
        handle = "diffusion1d_" + secrets.token_hex(12)
        path = _resolve_run_handle(handle, must_exist=False)
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


def _write_json_atomic(path, value):
    temporary = path + ".tmp-" + secrets.token_hex(6)
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=1, allow_nan=False)
        if os.path.getsize(temporary) > MAX_RUN_FILE_BYTES:
            raise ValueError("run file exceeds the size limit")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_json(run_handle, filename):
    path = _run_file(run_handle, filename, must_exist=True)
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _plan_inputs(length_um, diffusivity_um2_s, peak_position, sigma_um,
                 duration_s, points):
    length = _finite_number(length_um, "length_um")
    diffusivity = _finite_number(diffusivity_um2_s, "diffusivity_um2_s")
    peak = _finite_number(peak_position, "peak_position")
    sigma = _finite_number(sigma_um, "sigma_um")
    duration = _finite_number(duration_s, "duration_s")
    if isinstance(points, bool) or not isinstance(points, int):
        raise ValueError("points must be an integer")
    if not 0.01 <= length <= 10_000:
        raise ValueError("length_um must be in [0.01, 10000]")
    if not 1e-12 <= diffusivity <= 1e6:
        raise ValueError("diffusivity_um2_s must be in [1e-12, 1e6]")
    if not 0.0 <= peak <= 1.0:
        raise ValueError("peak_position must be in [0, 1]")
    if not 1e-9 <= sigma <= 10_000:
        raise ValueError("sigma_um must be in [1e-9, 10000]")
    if not 1e-9 <= duration <= 1e7:
        raise ValueError("duration_s must be in [1e-9, 1e7]")
    if not 5 <= points <= 5001:
        raise ValueError("points must be in [5, 5001]")

    dx = length / float(points - 1)
    dt = 0.4 * dx * dx / diffusivity
    n_steps = max(1, int(math.ceil(duration / dt)))
    cell_updates = points * n_steps
    if n_steps > MAX_TIME_STEPS or cell_updates > MAX_CELL_UPDATES:
        raise ValueError(
            "requested mesh and duration exceed the computation budget"
        )
    inputs = {
        "length_um": length,
        "diffusivity_um2_s": diffusivity,
        "peak_position": peak,
        "sigma_um": sigma,
        "duration_s": duration,
        "points": points,
    }
    estimate = {
        "dx_um": dx,
        "dt_s": dt,
        "n_steps": n_steps,
        "cell_updates": cell_updates,
        "max_cell_updates": MAX_CELL_UPDATES,
        "scheme": "FTCS, r=0.4, no-flux boundaries",
    }
    return inputs, estimate


def _initial_profile(nx, dx, peak_position_um, sigma_um):
    return [math.exp(-0.5 * ((i * dx - peak_position_um) / sigma_um) ** 2)
            for i in range(nx)]


def _ftcs_solve(u, d_coeff, dx, dt, n_steps, snapshot_every, progress=None):
    r = d_coeff * dt / (dx * dx)
    snapshots = [(0.0, list(u))]
    last = len(u) - 1
    for step in range(1, n_steps + 1):
        prev = u
        u = list(prev)
        u[0] = prev[0] + r * (prev[1] - prev[0])
        u[last] = prev[last] + r * (prev[last - 1] - prev[last])
        for i in range(1, last):
            u[i] = prev[i] + r * (prev[i - 1] - 2.0 * prev[i] + prev[i + 1])
        if step % snapshot_every == 0 or step == n_steps:
            snapshots.append((step * dt, list(u)))
        if progress and step % max(1, n_steps // 20) == 0:
            progress(step, n_steps)
    return snapshots


def _decimate(values, max_points):
    if len(values) <= max_points:
        return values
    stride = (len(values) - 1) / float(max_points - 1)
    return [values[int(round(index * stride))] for index in range(max_points)]


_CREATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "length_um": {"type": "number", "minimum": 0.01, "maximum": 10000,
                      "default": 10.0, "description": "Domain length in microns."},
        "diffusivity_um2_s": {"type": "number", "minimum": 1e-12,
                               "maximum": 1e6, "default": 1.0,
                               "description": "Diffusion coefficient in um^2/s."},
        "peak_position": {"type": "number", "minimum": 0, "maximum": 1,
                          "default": 0.5,
                          "description": "Initial peak as a fraction of domain length."},
        "sigma_um": {"type": "number", "exclusiveMinimum": 0,
                     "maximum": 10000, "default": 0.5,
                     "description": "Initial Gaussian width in microns."},
        "duration_s": {"type": "number", "exclusiveMinimum": 0,
                       "maximum": 1e7, "default": 5.0,
                       "description": "Simulated duration in seconds."},
        "points": {"type": "integer", "minimum": 5, "maximum": 5001,
                   "default": 101, "description": "Spatial grid points."},
    },
    "additionalProperties": False,
}
_HANDLE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "run_handle": {"type": "string", "pattern": _HANDLE_RE.pattern,
                       "maxLength": 80,
                       "description": "Opaque handle returned by create_diffusion_sim."},
    },
    "required": ["run_handle"],
    "additionalProperties": False,
}
_PROFILE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "run_handle": _HANDLE_INPUT_SCHEMA["properties"]["run_handle"],
        "time_s": {"type": ["number", "null"], "default": None,
                   "description": "Requested time in seconds; null selects the final snapshot."},
        "max_points": {"type": "integer", "minimum": 5,
                       "maximum": MAX_RESULT_POINTS, "default": 200,
                       "description": "Maximum returned plot points."},
    },
    "required": ["run_handle"],
    "additionalProperties": False,
}
_ESTIMATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "length_um": {"type": "number", "exclusiveMinimum": 0,
                      "maximum": 10000, "default": 10.0,
                      "description": "Domain length in microns."},
        "diffusivity_um2_s": {"type": "number", "exclusiveMinimum": 0,
                               "maximum": 1e6, "default": 1.0,
                               "description": "Diffusion coefficient in um^2/s."},
    },
    "additionalProperties": False,
}

_CREATE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"run_handle": {"type": "string"},
                   "inputs": {"type": "object"},
                   "estimate": {"type": "object"}},
    "required": ["run_handle", "inputs", "estimate"],
}
_RUN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"},
                   "n_snapshots": {"type": "integer"},
                   "warnings": {"type": "array", "items": {"type": "string"}}},
    "required": ["status"],
}
_PROFILE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"time_s": {"type": "number"},
                   "x_um": {"type": "array", "items": {"type": "number"}},
                   "concentration": {"type": "array", "items": {"type": "number"}},
                   "units": {"type": "object"}},
    "required": ["time_s", "x_um", "concentration", "units"],
}
_HISTORY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"time_s": {"type": "array", "items": {"type": "number"}},
                   "peak_concentration": {"type": "array", "items": {"type": "number"}},
                   "total_mass": {"type": "array", "items": {"type": "number"}}},
    "required": ["time_s", "peak_concentration", "total_mass"],
}
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
              "idempotentHint": True, "openWorldHint": False}


@server.tool(
    input_schema=_CREATE_INPUT_SCHEMA,
    output_schema=_CREATE_OUTPUT_SCHEMA,
    annotations={"title": "Create 1D diffusion simulation",
                 "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
def create_diffusion_sim(length_um=10.0, diffusivity_um2_s=1.0,
                         peak_position=0.5, sigma_um=0.5,
                         duration_s=5.0, points=101):
    """Validate and create a bounded 1D diffusion run; then call run_simulation."""
    try:
        inputs, estimate = _plan_inputs(
            length_um, diffusivity_um2_s, peak_position, sigma_um, duration_s, points
        )
        run_handle, run_dir = _new_run()
        _write_json_atomic(os.path.join(run_dir, "input.json"), inputs)
    except ValueError as exc:
        return _error("invalid_input", str(exc))
    except OSError:
        return _error("run_create_failed", "could not create the run")
    return {"run_handle": run_handle, "inputs": inputs, "estimate": estimate}


@server.async_tool(
    input_schema=_HANDLE_INPUT_SCHEMA,
    output_schema=_RUN_OUTPUT_SCHEMA,
    annotations={"title": "Run diffusion simulation", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def run_simulation(run_handle, ctx=None):
    """Run an opaque, confined handle asynchronously; then call get_* readers."""
    try:
        lock = _lock_for(run_handle)
        if not lock.acquire(False):
            return _error("run_busy", "the run is already active")
    except ValueError:
        return _error("invalid_run_handle", "the run handle is invalid or unknown")

    try:
        raw_inputs = _load_json(run_handle, "input.json")
        inputs, estimate = _plan_inputs(
            raw_inputs.get("length_um"), raw_inputs.get("diffusivity_um2_s"),
            raw_inputs.get("peak_position"), raw_inputs.get("sigma_um"),
            raw_inputs.get("duration_s"), raw_inputs.get("points"),
        )
        nx = inputs["points"]
        dx = estimate["dx_um"]
        dt = estimate["dt_s"]
        n_steps = estimate["n_steps"]
        snapshot_every = max(1, n_steps // 25)

        def progress(step, total):
            if ctx is not None:
                ctx.report_progress(step, total, "FTCS progress")

        initial = _initial_profile(
            nx, dx, inputs["peak_position"] * inputs["length_um"], inputs["sigma_um"]
        )
        snapshots = _ftcs_solve(
            initial, inputs["diffusivity_um2_s"], dx, dt, n_steps,
            snapshot_every, progress=progress,
        )
        mass0 = sum(snapshots[0][1]) * dx
        mass1 = sum(snapshots[-1][1]) * dx
        warnings = []
        if mass0 > 0 and abs(mass1 - mass0) / mass0 > 1e-6:
            warnings.append("mass conservation tolerance was exceeded")
        output = {"dx_um": dx,
                  "snapshots": [{"time_s": t, "u": u} for t, u in snapshots]}
        _write_json_atomic(_run_file(run_handle, "output.json"), output)
        return {"status": "finished", "n_snapshots": len(snapshots),
                "warnings": warnings}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return _error("invalid_run_data", "the stored run input is invalid")
    except OSError:
        return _error("run_failed", "the run could not write its result")
    finally:
        lock.release()


@server.tool(
    input_schema=_PROFILE_INPUT_SCHEMA,
    output_schema=_PROFILE_OUTPUT_SCHEMA,
    annotations={"title": "Get concentration profile", **_READ_ONLY},
)
def get_concentration_profile(run_handle, time_s=None, max_points=200):
    """Return bounded data from an opaque, confined handle after completion."""
    try:
        if isinstance(max_points, bool) or not isinstance(max_points, int):
            raise ValueError("invalid max_points")
        if not 5 <= max_points <= MAX_RESULT_POINTS:
            raise ValueError("invalid max_points")
        requested_time = None if time_s is None else _finite_number(time_s, "time_s")
        output = _load_json(run_handle, "output.json")
        snapshots = output["snapshots"]
        snapshot = snapshots[-1] if requested_time is None else min(
            snapshots, key=lambda item: abs(item["time_s"] - requested_time)
        )
        xs = [index * output["dx_um"] for index in range(len(snapshot["u"]))]
    except (ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError):
        return _error("result_unavailable", "the requested result is unavailable")
    return {"time_s": snapshot["time_s"],
            "x_um": _decimate(xs, max_points),
            "concentration": _decimate(snapshot["u"], max_points),
            "units": {"x_um": "um", "concentration": "a.u.", "time_s": "s"}}


@server.tool(
    input_schema=_HANDLE_INPUT_SCHEMA,
    output_schema=_HISTORY_OUTPUT_SCHEMA,
    annotations={"title": "Get peak history", **_READ_ONLY},
)
def get_peak_history(run_handle):
    """Return bounded histories from an opaque, confined handle as data."""
    try:
        output = _load_json(run_handle, "output.json")
        snapshots = output["snapshots"]
        dx = output["dx_um"]
        if len(snapshots) > 27:
            raise ValueError("too many snapshots")
        return {"time_s": [item["time_s"] for item in snapshots],
                "peak_concentration": [max(item["u"]) for item in snapshots],
                "total_mass": [sum(item["u"]) * dx for item in snapshots]}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return _error("result_unavailable", "the requested result is unavailable")


@server.tool(
    input_schema=_ESTIMATE_INPUT_SCHEMA,
    output_schema={"type": "object",
                   "properties": {"time_s": {"type": "number"},
                                  "note": {"type": "string"}},
                   "required": ["time_s", "note"]},
    annotations={"title": "Estimate diffusion time", **_READ_ONLY},
)
def estimate_diffusion_time(length_um=10.0, diffusivity_um2_s=1.0):
    """Estimate t ~ L^2/(4D) for selecting a bounded simulation duration."""
    try:
        length = _finite_number(length_um, "length_um")
        diffusivity = _finite_number(diffusivity_um2_s, "diffusivity_um2_s")
        if not 0 < length <= 10000 or not 0 < diffusivity <= 1e6:
            raise ValueError("estimate inputs are outside supported bounds")
    except ValueError as exc:
        return _error("invalid_input", str(exc))
    return {"time_s": (length ** 2) / (4.0 * diffusivity),
            "note": "Order-of-magnitude data; validate with a bounded run."}


@server.tool(
    input_schema=_HANDLE_INPUT_SCHEMA,
    output_schema={"type": "object",
                   "properties": {"status": {"type": "string"}},
                   "required": ["status"]},
    annotations={"title": "Delete run", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True,
                 "openWorldHint": False},
)
def delete_run(run_handle):
    """Delete one confined run after the user explicitly requests cleanup."""
    try:
        run_dir = _resolve_run_handle(run_handle)
        lock = _lock_for(run_handle)
        if not lock.acquire(False):
            return _error("run_busy", "the active run cannot be deleted")
    except ValueError:
        return _error("invalid_run_handle", "the run handle is invalid or unknown")
    try:
        shutil.rmtree(run_dir)
    except OSError:
        return _error("delete_failed", "the run could not be deleted")
    finally:
        lock.release()
        with _RUN_LOCKS_GUARD:
            _RUN_LOCKS.pop(run_handle, None)
    return {"status": "deleted"}


@server.resource("config://diffusion1d/about", mime_type="application/json")
def about():
    """Describe the science, safety limits, workflow, and deployed version."""
    return {"name": APP_NAME, "version": APP_VERSION,
            "physics": "1D transient diffusion (FTCS, no-flux boundaries)",
            "limits": {"max_cell_updates": MAX_CELL_UPDATES,
                       "max_time_steps": MAX_TIME_STEPS,
                       "max_result_points": MAX_RESULT_POINTS},
            "workflow": ["create_diffusion_sim", "run_simulation",
                         "get_concentration_profile", "get_peak_history",
                         "delete_run"]}
