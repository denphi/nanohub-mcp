#!/usr/bin/env python3
"""Pre-deploy validator for a nanohubmcp server file.

Loads the server exactly the way start_mcp does, then statically checks every
tool, resource, and schema against the conventions in this skill.

Usage:
    python validate_server.py bin/yourtool.py
    python validate_server.py bin/yourtool.py --render-apps   # also render ui:// apps and size-check
    python validate_server.py bin/yourtool.py --limit-mb 8

Exit code 0 = no errors (warnings allowed), 1 = errors found, 2 = could not load.
Run it inside the same environment the server will run in (imports must work).
"""

from __future__ import print_function

import argparse
import importlib.util
import inspect
import json
import os
import re
import sys

MCP_APP_MIME = "text/html;profile=mcp-app"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Registered automatically by the nanohubmcp framework, not by the author.
FRAMEWORK_BUILTIN_TOOLS = {"get_job_result"}

try:
    from jsonschema import Draft202012Validator
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


def load_server(app_path):
    app_dir = os.path.dirname(os.path.abspath(app_path))
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    module_name = os.path.splitext(os.path.basename(app_path))[0]
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    server = getattr(module, "server", None)
    if server is None:
        raise RuntimeError("No module-level `server` variable found "
                           "(the start_mcp contract requires one).")
    if not hasattr(server, "_tools"):
        raise RuntimeError("`server` does not look like a nanohubmcp MCPServer "
                           "(missing _tools registry).")
    return server


class Report(object):
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.infos = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        self.infos.append(msg)

    def dump(self):
        for msg in self.infos:
            print("       " + msg)
        for msg in self.warnings:
            print("WARN   " + msg)
        for msg in self.errors:
            print("ERROR  " + msg)
        print("\n{} error(s), {} warning(s)".format(len(self.errors), len(self.warnings)))
        return 1 if self.errors else 0


def check_schema(report, label, schema):
    if schema is None:
        return
    if not isinstance(schema, dict):
        report.error("{}: schema is not an object".format(label))
        return
    if HAVE_JSONSCHEMA:
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            report.error("{}: invalid JSON Schema: {}".format(label, exc))


def check_security_hints(report, label, handler, schema):
    """Flag common hazards for human review; this is not a security proof."""
    try:
        source = inspect.getsource(handler)
    except (OSError, TypeError):
        source = ""
    lowered = source.lower()
    if "shell=true" in lowered:
        report.error("{}: subprocess uses shell=True; pass argv and shell=False".format(label))
    if re.search(r"\b(eval|exec)\s*\(", source) or "os.system(" in source:
        report.error("{}: dynamic command execution found; remove eval/exec/os.system".format(label))
    if "subprocess." in source:
        if "shell=false" not in lowered:
            report.warn("{}: subprocess call lacks an explicit shell=False guard".format(label))
        if "timeout=" not in lowered:
            report.warn("{}: subprocess call lacks a timeout/resource ceiling".format(label))

    properties = (schema or {}).get("properties") or {}
    path_like = [name for name in properties
                 if re.search(r"(^|_)(path|dir|directory|file|filename|handle)$",
                              name, re.IGNORECASE)]
    if path_like:
        doc = (inspect.getdoc(handler) or "").lower()
        if not any(word in doc for word in ("opaque", "confined", "confin", "handle")):
            report.warn("{}: path-like inputs {} need an opaque-handle/confinement contract".format(
                label, ", ".join(path_like)))
        if not any(word in lowered for word in (
                "realpath", "commonpath", "resolve_run_handle", "_load_json",
                "_run_file", "_lock_for")):
            report.warn("{}: path-like inputs have no visible confinement helper".format(label))

    for name, definition in properties.items():
        if isinstance(definition, dict) and name not in ("ctx", "context") \
                and not definition.get("description"):
            report.warn("{}: input '{}' has no description/units".format(label, name))


def validate(server, render_apps=False, limit_mb=8.0):
    report = Report()
    tools = getattr(server, "_tools", {}) or {}
    resources = getattr(server, "_resources", {}) or {}

    report.info("server: {} tools, {} resources".format(len(tools), len(resources)))
    if not tools:
        report.error("no tools registered")

    n_async = 0
    for name, entry in sorted(tools.items()):
        if name in FRAMEWORK_BUILTIN_TOOLS:
            continue
        d = entry["definition"].to_dict()
        label = "tool {}".format(name)

        if not NAME_RE.match(name):
            report.warn("{}: name is not lowercase snake_case".format(label))
        if entry.get("is_async"):
            n_async += 1

        desc = (d.get("description") or "").strip()
        if not desc:
            report.error("{}: empty description (docstring is the model's prompt)".format(label))
        elif len(desc) < 40:
            report.warn("{}: description under 40 chars — say when to call it, "
                        "argument units, and what comes next".format(label))

        check_schema(report, label + " inputSchema", d.get("inputSchema"))
        input_schema = d.get("inputSchema") or {}
        if input_schema.get("type") != "object":
            report.error("{} inputSchema: top-level type must be object".format(label))
        if d.get("outputSchema") is None:
            report.warn("{}: no output_schema — clients get no structuredContent "
                        "contract".format(label))
        else:
            check_schema(report, label + " outputSchema", d.get("outputSchema"))

        ann = d.get("annotations")
        if not ann:
            report.warn("{}: no annotations (at least title + readOnlyHint)".format(label))
        elif not ann.get("title"):
            report.warn("{}: annotations missing title".format(label))

        # Input schema should not advertise a ctx/context parameter.
        props = (d.get("inputSchema") or {}).get("properties") or {}
        for leaked in ("ctx", "context"):
            if leaked in props:
                report.error("{}: '{}' leaked into inputSchema — pass an explicit "
                             "input_schema".format(label, leaked))

        check_security_hints(report, label, entry.get("handler"), input_schema)

        # MCP Apps: tool _meta.ui.resourceUri must point at a registered app resource.
        meta_ui = ((d.get("_meta") or {}).get("ui")) or {}
        uri = meta_ui.get("resourceUri")
        if uri:
            if uri not in resources:
                report.error("{}: _meta.ui.resourceUri {} has no registered "
                             "resource".format(label, uri))
            else:
                mime = getattr(resources[uri]["definition"], "mimeType", None)
                if mime != MCP_APP_MIME:
                    report.error("resource {}: mimeType {!r} != {!r}".format(
                        uri, mime, MCP_APP_MIME))

    if n_async:
        report.info("async tools: {}".format(n_async))
    else:
        slow_looking = [n for n in tools if n.startswith(("run_", "simulate_"))]
        if slow_looking:
            report.warn("no async tools, but {} look long-running — consider "
                        "@server.async_tool".format(", ".join(slow_looking)))

    # ui:// resources
    limit_bytes = int(limit_mb * 1024 * 1024)
    for uri, entry in sorted(resources.items()):
        definition = entry["definition"]
        if not uri.startswith("ui://"):
            continue
        label = "resource {}".format(uri)
        if getattr(definition, "mimeType", None) != MCP_APP_MIME:
            report.error("{}: mimeType must be {!r} for MCP Apps".format(label, MCP_APP_MIME))
        meta = getattr(definition, "meta", None) or {}
        if "ui" not in meta:
            report.warn("{}: no _meta.ui (csp/permissions) block".format(label))
        if render_apps:
            try:
                page = entry["handler"]()
                if hasattr(page, "to_dict"):
                    page = json.dumps(page.to_dict())
                size = len(page.encode("utf-8")) if isinstance(page, str) else len(str(page))
                status = "over limit" if size > limit_bytes else "ok"
                (report.error if size > limit_bytes else report.info)(
                    "{}: {:.2f} MB ({})".format(label, size / 1048576.0, status))
            except Exception as exc:
                report.error("{}: render failed: {}".format(label, exc))

    if not HAVE_JSONSCHEMA:
        report.warn("jsonschema not installed — schema validity NOT checked "
                    "(pip install jsonschema)")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("app", help="path to the server file (e.g. bin/yourtool.py)")
    parser.add_argument("--render-apps", action="store_true",
                        help="render every ui:// resource and check its size")
    parser.add_argument("--limit-mb", type=float, default=8.0,
                        help="app size limit in MB (default 8)")
    args = parser.parse_args()

    try:
        server = load_server(args.app)
    except Exception as exc:
        print("ERROR  could not load {}: {}".format(args.app, exc))
        return 2

    return validate(server, render_apps=args.render_apps, limit_mb=args.limit_mb).dump()


if __name__ == "__main__":
    sys.exit(main())
