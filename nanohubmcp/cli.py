"""
Command-line interface for nanohub-mcp.

Usage:
    start_mcp --app my_server.py [--python-env ENV_NAME]

On nanoHUB:
    - MCP server runs on port 8001
    - wrwroxy runs on port 8000 to handle proxy
"""

from __future__ import print_function

import argparse
import json
import os
import signal
import shutil
import sys
import tempfile
import time

try:
    from subprocess import Popen
except ImportError:
    Popen = None

from .server import MCPServer

# Global process handles for cleanup on shutdown
mcpProcess = None
wrwProcess = None


def resolve_python_env(env_name):
    # type: (str) -> str
    """
    Resolve a conda environment name to the Python executable path.

    Args:
        env_name: Name of the conda environment (e.g., 'AIIDA', 'ALIGNN').

    Returns:
        str: Absolute path to the Python executable in the conda environment.

    Raises:
        RuntimeError: If the environment is not found or conda is not available.
    """
    import subprocess

    try:
        # Get conda environment info in JSON format
        result = subprocess.run(
            ["conda", "info", "--json"],
            capture_output=True,
            text=True,
            check=True
        )
        conda_info = json.loads(result.stdout)
        envs = conda_info.get("envs", [])
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        raise RuntimeError(
            "Could not retrieve conda environment list. "
            "Make sure conda is installed and available on PATH."
        )

    # Look for the environment by name
    env_path = None
    for env_dir in envs:
        if env_dir.endswith("/envs/{}".format(env_name)) or env_dir.endswith("\\envs\\{}".format(env_name)):
            env_path = env_dir
            break

    if not env_path:
        # List available environments for the user
        env_names = [os.path.basename(e) for e in envs if "/envs/" in e or "\\envs\\" in e]
        raise RuntimeError(
            "Conda environment '{}' not found.\n"
            "Available environments: {}\n"
            "Run 'conda env list' to see all available environments.".format(
                env_name,
                ", ".join(sorted(env_names)) if env_names else "none"
            )
        )

    # Find the Python executable in the environment
    for python_name in ("python3", "python"):
        # Try bin/python (Unix/Linux/macOS)
        bin_python = os.path.join(env_path, "bin", python_name)
        if os.path.isfile(bin_python) and os.access(bin_python, os.X_OK):
            return bin_python

        # Try Scripts/python.exe (Windows)
        scripts_python = os.path.join(env_path, "Scripts", python_name + ".exe")
        if os.path.isfile(scripts_python) and os.access(scripts_python, os.X_OK):
            return scripts_python

    raise RuntimeError(
        "Python executable not found in conda environment '{}' at {}".format(env_name, env_path)
    )


def normalize_prefix(p):
    # type: (str) -> str
    """Normalize a URL path prefix to ensure it starts and ends with '/'."""
    if not p.startswith("/"):
        p = "/" + p
    if not p.endswith("/"):
        p += "/"
    return p


def get_session():
    # type: () -> tuple
    """Retrieve the nanoHUB session ID and session directory from environment."""
    session = os.getenv("SESSION") or os.getenv("SESSION_ID")
    sessiondir = os.getenv("SESSIONDIR") or os.getenv("SESSION_DIR")
    if not session or not sessiondir:
        return None, None
    return session, sessiondir


def is_nanohub_environment():
    # type: () -> bool
    """Check if we're running in a nanoHUB environment."""
    session, sessiondir = get_session()
    return session is not None and sessiondir is not None


def get_proxy_addr():
    # type: () -> tuple
    """
    Parse the nanoHUB resources file to construct proxy URL information.

    Returns:
        tuple: (normalized_path, full_proxy_url, filexfer_port) or (None, None, None) if not on nanoHUB
    """
    session, sessiondir = get_session()
    if not session or not sessiondir:
        return None, None, None

    fn = os.path.join(sessiondir, "resources")
    if not os.path.exists(fn):
        return None, None, None

    url = fxp = fxc = None
    with open(fn) as f:
        for line in f:
            if line.startswith("hub_url"):
                url = line.split(" ", 1)[1].strip().replace('"', "")
            elif line.startswith("filexfer_port"):
                full_port = int(line.split()[1])
                fxp = str(full_port % 1000)
            elif line.startswith("filexfer_cookie"):
                fxc = line.split()[1]

    if not (url and fxp and fxc):
        return None, None, None

    path = "/weber/{}/{}/{}/".format(session, fxc, fxp)
    proxy_url = "https://proxy." + url.split("//", 1)[1] + path
    return normalize_prefix(path), proxy_url, full_port


def shutdown(signum, frame):
    # type: (int, object) -> None
    """Signal handler for graceful shutdown of MCP and wrwroxy processes."""
    global mcpProcess, wrwProcess
    if signum:
        print("{} signal {}".format(time.ctime(), signum), flush=True)

    for name, proc in (("wrwroxy", wrwProcess), ("mcp", mcpProcess)):
        if proc and proc.poll() is None:
            print("terminating {} pid={}".format(name, proc.pid), flush=True)
            proc.terminate()


def write_mcp_runner(app_path, host, port, path_prefix=""):
    # type: (str, str, int, str) -> str
    """
    Create a temporary Python runner script for the MCP server.

    Args:
        app_path: Path to the user's MCP app file
        host: Host to bind to
        port: Port to listen on
        path_prefix: URL path prefix for proxy environments

    Returns:
        str: Path to the generated temporary runner script.
    """
    app_dir = os.path.dirname(os.path.abspath(app_path))
    mod = os.path.splitext(os.path.basename(app_path))[0]

    runner_code = """\
import os, sys, importlib.util

app_dir = {app_dir!r}
app_path = {app_path!r}
host = {host!r}
port = {port!r}
path_prefix = {path_prefix!r}

# Add app directory to path
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

# Load the module
module_name = {mod!r}
spec = importlib.util.spec_from_file_location(module_name, app_path)
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)

# Get the server
if hasattr(module, "server"):
    server = module.server
    print("MCP Server ready!", flush=True)
    server.run(host=host, port=port, path_prefix=path_prefix)
else:
    print("Error: No 'server' variable found in app file", flush=True)
    sys.exit(1)
""".format(app_dir=app_dir, app_path=app_path, host=host, port=port, path_prefix=path_prefix, mod=mod)

    fd, path = tempfile.mkstemp(prefix="mcp_runner_", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(runner_code)
    return path


def load_server_from_app(app_path):
    # type: (str) -> MCPServer
    """
    Load an MCP server from a Python file.
    The file should define a 'server' variable that is an MCPServer instance.
    """
    import importlib.util

    if not os.path.exists(app_path):
        print("Error: App file not found: {}".format(app_path))
        sys.exit(1)

    # Add the app directory to path so relative imports work
    app_dir = os.path.dirname(os.path.abspath(app_path))
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    # Load the module
    module_name = os.path.splitext(os.path.basename(app_path))[0]
    spec = importlib.util.spec_from_file_location(module_name, app_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Look for 'server' variable
    if hasattr(module, "server"):
        server = module.server
        if isinstance(server, MCPServer):
            return server
        else:
            print("Error: 'server' in {} is not an MCPServer instance".format(app_path))
            sys.exit(1)
    else:
        print("Error: No 'server' variable found in {}".format(app_path))
        print("Your app file should define: server = MCPServer(...)")
        sys.exit(1)


def start_mcp_main():
    """
    Entry point for 'start_mcp' command.
    Similar to start_dash:
    - On nanoHUB: runs MCP on 8001, wrwroxy on 8000
    - Locally: runs MCP directly on specified port
    """
    global mcpProcess, wrwProcess

    parser = argparse.ArgumentParser(
        prog="start_mcp",
        description="Start an MCP server on nanoHUB or locally",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  start_mcp --app my_server.py
  start_mcp --app my_server.py --python-env AIIDA
  start_mcp --app my_server.py --port 9000
        """
    )
    parser.add_argument(
        "--app", "-a",
        required=True,
        help="Path to Python file defining 'server = MCPServer(...)'"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=int(os.environ.get("MCP_PORT", 8000)),
        help="Port to listen on (default: 8000). On nanoHUB, proxy uses 8000 and MCP uses 8001."
    )
    parser.add_argument(
        "--python-env",
        default=None,
        help="Conda environment name to use (e.g., AIIDA, ALIGNN). "
             "Run 'conda env list' to see available environments."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode with verbose logging"
    )

    args = parser.parse_args()

    app_path = os.path.abspath(args.app)
    if not os.path.exists(app_path):
        print("Error: App file not found: {}".format(app_path))
        sys.exit(1)

    # Check if we're on nanoHUB
    if is_nanohub_environment():
        # nanoHUB mode: run with proxy
        _start_with_proxy(app_path, args)
    else:
        # Local mode: run directly
        _start_directly(app_path, args)


def _start_directly(app_path, args):
    # type: (str, argparse.Namespace) -> None
    """Start MCP server directly without proxy (local development)."""
    print("Starting MCP server (local mode)", flush=True)

    # Resolve Python environment if specified
    if args.python_env:
        python_executable = resolve_python_env(args.python_env)
        print("Using Python environment: {}".format(python_executable), flush=True)
        # For python-env, we need to use subprocess
        runner = write_mcp_runner(app_path, args.host, args.port)
        env = os.environ.copy()
        app_dir = os.path.dirname(app_path)
        env["PYTHONPATH"] = app_dir + os.pathsep + env.get("PYTHONPATH", "")
        proc = Popen([python_executable, runner], env=env)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
    else:
        # Run directly in current process
        server = load_server_from_app(app_path)
        server.run(host=args.host, port=args.port)


def _find_wrwroxy():
    # type: () -> str or None
    """
    Find an available wrwroxy version using 'use'.

    The 'use' command prints available packages to stderr.
    Returns the version string (e.g. 'wrwroxy-0.3') or None if not found.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["bash", "-lc", "use"],
            capture_output=True, text=True, timeout=10
        )
        available = result.stderr.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    # Collect all wrwroxy-* entries
    # The 'use' output format is "package-version:" so strip trailing colons
    versions = []
    for token in available.split():
        token = token.strip().rstrip(":")
        if token.startswith("wrwroxy-"):
            versions.append(token)

    if not versions:
        return None

    # Sort descending so the newest version is tried first
    versions.sort(reverse=True)
    print("Available wrwroxy versions: {}".format(", ".join(versions)), flush=True)
    return versions[0]


def _start_with_proxy(app_path, args):
    # type: (str, argparse.Namespace) -> int
    """Start MCP server with wrwroxy on nanoHUB."""
    global mcpProcess, wrwProcess

    # Resolve Python environment
    python_executable = sys.executable
    if args.python_env:
        python_executable = resolve_python_env(args.python_env)
        print("Using Python environment: {}".format(python_executable), flush=True)
    else:
        print("Using default Python: {}".format(python_executable), flush=True)

    # Get nanoHUB proxy configuration
    p_path, p_url, p_port = get_proxy_addr()
    if not p_url:
        print("Warning: Could not get proxy configuration, falling back to direct mode", flush=True)
        return _start_directly(app_path, args)

    # Check if wrwroxy is available
    wrwroxy_version = _find_wrwroxy()

    if wrwroxy_version:
        # wrwroxy available: MCP on 8001, proxy on 8000
        # wrwroxy strips the prefix, so the MCP server sees clean paths
        mcp_port = 8001
        proxy_port = 8000
        path_prefix = ""
    else:
        # No wrwroxy: MCP listens directly on 8000
        # Requests arrive with the full weber prefix, so the server must handle it
        print("Warning: No wrwroxy version found, running MCP server directly on port 8000", flush=True)
        mcp_port = 8000
        path_prefix = p_path

    print("Proxy URL : {}".format(p_url), flush=True)
    print("MCP port  : {}".format(mcp_port), flush=True)
    if wrwroxy_version:
        print("Proxy port: {}".format(proxy_port), flush=True)
    if path_prefix:
        print("Path prefix: {}".format(path_prefix), flush=True)

    # Set up environment for subprocess
    env = os.environ.copy()
    app_dir = os.path.dirname(app_path)
    env["PYTHONPATH"] = app_dir + os.pathsep + env.get("PYTHONPATH", "")

    # Create runner script
    runner = write_mcp_runner(app_path, args.host, mcp_port, path_prefix=path_prefix)

    # Launch MCP server
    print("Starting MCP server", flush=True)
    mcpProcess = Popen([python_executable, runner], env=env)

    # Launch wrwroxy reverse proxy (only if available)
    if wrwroxy_version:
        wrw_cmd = (
            "use -e {version} && "
            "exec wrwroxy "
            "--listenHost 0.0.0.0 "
            "--listenPort {proxy_port} "
            "--forwardHost 127.0.0.1 "
            "--forwardPort {mcp_port} ".format(
                version=wrwroxy_version,
                proxy_port=proxy_port,
                mcp_port=mcp_port,
            )
        )
        if args.debug:
            wrw_cmd += " --stream-log"

        print("Starting {} proxy".format(wrwroxy_version), flush=True)
        wrw_env = os.environ.copy()
        session, _ = get_session()
        wrw_env['SESSION'] = session
        wrwProcess = Popen(["bash", "-lc", wrw_cmd], env=wrw_env)

    print("MCP Server ready! Access it at: {}".format(p_url), flush=True)
    print("SSE endpoint: {}sse".format(p_url), flush=True)

    # Wait for MCP to exit and clean up
    rc = mcpProcess.wait()
    shutdown(None, None)
    return rc


if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, shutdown)
    # SIGHUP may not exist on all platforms
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, shutdown)

    start_mcp_main()
