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
import ast
import inspect
import json
import os
import re
import sys
import textwrap

# Shared invariants (same module the live check_conformance.py uses, so a rule
# can't drift between pre-deploy and post-deploy). It sits next to this script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcp_conformance import (  # noqa: E402
    MCP_APP_MIME,
    APPS_EXTENSION_ID,
    TASKS_EXTENSION_ID,
    SKILLS_EXTENSION_ID,
    ELICITATION_METHOD,
    check_app_handshake,
    check_skill_entry,
    expected_extensions,
)

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


def _source_of(handler):
    try:
        return inspect.getsource(handler)
    except (OSError, TypeError):
        return ""


def _calls_subprocess(source):
    """True when the body really calls subprocess, not merely mentions it.

    A substring test counts the word wherever it appears, including inside a
    string a tool returns to explain itself -- one tool was flagged twice for
    the sentence "Current runner uses blocking subprocess.run". Parsing means
    only an actual call counts.
    """
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError):
        return "subprocess." in source      # unparseable: fall back to the old test
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "subprocess":
                return True
    return False


def _body_with_local_helpers(handler, source, depth=2):
    """`source` plus the source of the module-level helpers it calls.

    A tool that confines a path usually does it in a helper -- that is the
    shape worth encouraging -- so searching only the tool body reports every
    careful implementation as careless.

    Two levels, not one, because the helper is often itself a wrapper: padremcp
    reads through `_load`, which calls `_confined`, which does the realpath
    comparison. At one level the check saw `_load` and stopped, and reported
    eight correctly confined tools as unprotected.
    """
    module = sys.modules.get(getattr(handler, "__module__", "") or "")
    if module is None:
        return source
    text = [source]
    seen = set()
    frontier = [source]
    for _ in range(max(1, depth)):
        names = []
        for body in frontier:
            try:
                tree = ast.parse(textwrap.dedent(body))
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    names.append(node.func.id)
        frontier = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            helper = getattr(module, name, None)
            if inspect.isfunction(helper):
                body = _source_of(helper)
                text.append(body)
                frontier.append(body)
        if not frontier:
            break
    return "\n".join(text)


def check_security_hints(report, label, handler, schema):
    """Flag common hazards for human review; this is not a security proof."""
    source = _source_of(handler)
    lowered = source.lower()
    if "shell=true" in lowered:
        report.error("{}: subprocess uses shell=True; pass argv and shell=False".format(label))
    if re.search(r"\b(eval|exec)\s*\(", source) or "os.system(" in source:
        report.error("{}: dynamic command execution found; remove eval/exec/os.system".format(label))
    if _calls_subprocess(source):
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
        reachable = _body_with_local_helpers(handler, source).lower()
        if not any(word in reachable for word in (
                "realpath", "commonpath", "resolve_run_handle", "_load_json",
                "_run_file", "_lock_for", "_safe_source_name")):
            report.warn("{}: path-like inputs have no visible confinement helper".format(label))

    for name, definition in properties.items():
        if isinstance(definition, dict) and name not in ("ctx", "context") \
                and not definition.get("description"):
            report.warn("{}: input '{}' has no description/units".format(label, name))


def check_skills(report, server):
    """Validate every registered SEP-2640 skill's published entry offline.

    The entry is what a host verifies later reads against, so a wrong
    manifest is caught here rather than as an unexplained "skill won't
    load" after deployment.
    """
    skills = getattr(server, "_skills", None)
    if not skills:
        return 0
    for uri in sorted(skills):
        try:
            entry = skills[uri]["definition"].to_dict()
        except Exception as exc:  # pragma: no cover - framework shape drift
            report.warn("could not read skill {} ({}); skipping".format(uri, exc))
            continue
        errors, warnings = check_skill_entry(entry)
        for message in errors:
            report.error(message)
        for message in warnings:
            report.warn(message)
    report.info("skills registered: {}".format(", ".join(sorted(skills))))
    return len(skills)


def check_extensions(report, server, has_apps, has_async, has_skills=False):
    """Confirm the server advertises the extensions its features require.

    Offline surrogate for the live "does initialize advertise ui/tasks/skills?"
    check: call the framework's own `_get_capabilities()` and compare its
    `extensions` to what a server with these features must declare. Catches,
    e.g., an app resource that won't be renderable because the capability was
    suppressed.
    """
    triggers = {
        APPS_EXTENSION_ID: "ui:// app resources",
        TASKS_EXTENSION_ID: "async tools",
        SKILLS_EXTENSION_ID: "registered skills",
    }
    expected = expected_extensions(has_apps, has_async, has_skills)
    try:
        caps = server._get_capabilities().to_dict()
    except Exception as exc:  # pragma: no cover - framework shape drift
        report.warn("could not read server capabilities ({}); "
                    "skipping extension-advertisement check".format(exc))
        return
    advertised = caps.get("extensions") or {}

    for ext_id, shape in expected.items():
        if ext_id not in advertised:
            report.error("extension {} must be advertised at initialize (server "
                         "has {}) but is absent from capabilities".format(
                             ext_id, triggers.get(ext_id, "the feature")))
        elif ext_id == APPS_EXTENSION_ID:
            mimes = (advertised.get(ext_id) or {}).get("mimeTypes") or []
            if MCP_APP_MIME not in mimes:
                report.error("extension {} advertises mimeTypes {} — must include "
                             "{!r}".format(ext_id, mimes, MCP_APP_MIME))
    if has_skills and not caps.get("resources"):
        # SEP-2640: a server declaring the skills extension MUST also declare
        # the base resources capability — skill files are read with
        # resources/read, and a host that respects capabilities won't try.
        report.error("skills are registered but the base 'resources' capability "
                     "is not advertised; skill files are read via resources/read")
    if expected:
        report.info("extensions advertised: {}".format(", ".join(sorted(advertised)) or "none"))


def check_elicitation_usage(report, tools):
    """Elicitation is a client capability the server *requests*; the framework
    raises if the client lacks it. Flag handlers that elicit without guarding."""
    for name, entry in sorted(tools.items()):
        handler = entry.get("handler")
        try:
            source = inspect.getsource(handler)
        except (OSError, TypeError):
            continue
        if ".elicit(" not in source and ".elicit_url(" not in source \
                and ELICITATION_METHOD not in source:
            continue
        report.info("tool {}: uses elicitation".format(name))
        guarded = any(tok in source for tok in (
            "try:", "_client_supports", "supports_elicitation", "except"))
        if not guarded:
            report.warn("tool {}: calls ctx.elicit without a try/except or "
                        "capability guard — clients without elicitation get a "
                        "RuntimeError; degrade to a chat fallback".format(name))


# `instructions` is delivered in the `initialize` result, on every connection,
# to every client. It is the only channel that reaches a client which has not
# implemented SEP-2640 skills -- which today is most of them, including
# claude.ai -- so a server whose procedural knowledge lives only in skills is
# telling it to nobody.
MIN_INSTRUCTIONS_CHARS = 200


def check_invoke(report, app_path):
    """`middleware/invoke` must keep ~/.local off the tool's path.

    Without `PYTHONNOUSERSITE=1` a stale package in the user's `~/.local`
    silently shadows the hub's copy, and the failure names neither the package
    nor the reason. It cost a debugging session on mcp4jupyter70: the right
    library was installed into anaconda-7, the tool kept importing an older one
    from ~/.local, and the symptom was a capability that half worked rather
    than an import error.
    """
    here = os.path.dirname(os.path.abspath(app_path))
    invoke = None
    for _ in range(4):                       # bin/tool.py, bin/pkg/app.py, ...
        candidate = os.path.join(here, "middleware", "invoke")
        if os.path.isfile(candidate):
            invoke = candidate
            break
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    if invoke is None:
        # Not every server is a published nanoHUB tool; an example run locally
        # has no middleware/ at all. Nothing to check.
        return
    text = open(invoke, "r").read()
    if "PYTHONNOUSERSITE" not in text:
        report.error(
            "middleware/invoke does not set PYTHONNOUSERSITE=1. The user's "
            "~/.local packages then shadow the hub's, so the tool can import "
            "a different library than the one installed for it and the failure "
            "names neither. Add: -e \"PYTHONNOUSERSITE=1\"")
    elif not re.search(r"PYTHONNOUSERSITE\s*=\s*1", text):
        report.error(
            "middleware/invoke mentions PYTHONNOUSERSITE but does not set it "
            "to 1; any other value leaves ~/.local on the path")
    else:
        report.info("invoke: PYTHONNOUSERSITE=1")


def check_instructions(report, server):
    """Require `instructions`, and require it to say something."""
    text = (getattr(server, "instructions", None) or "").strip()
    if not text:
        report.error(
            "server has no `instructions`. It is returned by `initialize` to "
            "every client on every connection, and it is the only place a "
            "client that does not implement SEP-2640 skills can read how to "
            "use this server. Pass instructions= to MCPServer(...)")
        return
    if len(text) < MIN_INSTRUCTIONS_CHARS:
        report.error(
            "server `instructions` is {} chars; under {} is not enough to "
            "carry call order, refusals and anything destructive. Say what a "
            "model gets wrong without it, not what the tool descriptions "
            "already say".format(len(text), MIN_INSTRUCTIONS_CHARS))
        return
    lowered = text.lower()
    if getattr(server, "_skills", None) and "skills/list" not in lowered:
        report.warn(
            "server publishes skills but `instructions` never points at them; "
            "a skills-aware client cannot tell there is a longer form")
    report.info("instructions: {} chars".format(len(text)))


def validate(server, render_apps=False, limit_mb=8.0, app_path="."):
    report = Report()
    tools = getattr(server, "_tools", {}) or {}
    resources = getattr(server, "_resources", {}) or {}

    report.info("server: {} tools, {} resources".format(len(tools), len(resources)))
    if not tools:
        report.error("no tools registered")

    check_instructions(report, server)
    check_invoke(report, app_path)

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
            # An async tool answers with the task/job envelope the framework
            # shapes, not with its own payload, so an output_schema here would
            # describe a reply the tool never sends. The result arrives later
            # through tasks/get, which carries its own shape.
            if not entry.get("is_async"):
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

    # ── MCP Apps (io.modelcontextprotocol/ui): ui:// resources ──────────────
    # The app HTML is always rendered so the ext-apps handshake can be checked
    # (a malformed ui/initialize renders blank on strict hosts — the exact
    # failure this guards). --render-apps additionally size-checks the payload.
    limit_bytes = int(limit_mb * 1024 * 1024)
    n_apps = 0
    for uri, entry in sorted(resources.items()):
        definition = entry["definition"]
        if not uri.startswith("ui://"):
            continue
        n_apps += 1
        label = "resource {}".format(uri)
        if getattr(definition, "mimeType", None) != MCP_APP_MIME:
            report.error("{}: mimeType must be {!r} for MCP Apps".format(label, MCP_APP_MIME))
        meta = getattr(definition, "meta", None) or {}
        if "ui" not in meta:
            report.warn("{}: no _meta.ui (csp/permissions) block".format(label))

        try:
            page = entry["handler"]()
            if hasattr(page, "to_dict"):
                page = json.dumps(page.to_dict())
        except Exception as exc:
            report.error("{}: render failed: {}".format(label, exc))
            continue

        errs, warns = check_app_handshake(page if isinstance(page, str) else str(page))
        for msg in errs:
            report.error("{}: {}".format(label, msg))
        for msg in warns:
            report.warn("{}: {}".format(label, msg))

        if render_apps:
            size = len(page.encode("utf-8")) if isinstance(page, str) else len(str(page))
            status = "over limit" if size > limit_bytes else "ok"
            (report.error if size > limit_bytes else report.info)(
                "{}: {:.2f} MB ({})".format(label, size / 1048576.0, status))

    # ── Skills (SEP-2640): validate each published manifest ─────────────────
    n_skills = check_skills(report, server)

    # ── Extension advertisement (offline: call the server's own capability
    # logic and confirm it declares what its features require) ───────────────
    check_extensions(report, server, has_apps=n_apps > 0, has_async=n_async > 0,
                     has_skills=n_skills > 0)

    # ── Elicitation usage hygiene ───────────────────────────────────────────
    check_elicitation_usage(report, tools)

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

    return validate(server, render_apps=args.render_apps, limit_mb=args.limit_mb,
                    app_path=args.app).dump()


if __name__ == "__main__":
    sys.exit(main())
