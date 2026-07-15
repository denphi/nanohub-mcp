# Security and input hardening

Read this before exposing any scientific function. MCP tool arguments are
model-generated and therefore untrusted. Files, solver output, resource text,
and values previously returned by another tool are untrusted too.

## Contents

- Trust boundaries
- Confine run handles
- Validate scientific inputs and derived cost
- Invoke processes safely
- Generate solver inputs safely
- Return untrusted data safely
- Protect secrets and identity
- Test the boundary

## Trust boundaries

Enforce security in code. Schemas, annotations, docstrings, and host
confirmation dialogs improve behavior but are not authorization or validation.

For every tool, identify:

- which arguments select files, computation, network access, or side effects;
- the maximum CPU, memory, wall time, disk, and result size;
- whether any returned text originated outside trusted server code;
- which authenticated user is allowed to access the selected state.

Reject invalid inputs with a short, fixed `ToolResult(..., is_error=True)`.
Do not echo an arbitrary argument, file content, stack trace, environment, or
command line into the error.

## Confine run handles

Return an opaque random `run_handle`, not an absolute path. Store every run
beneath one application-owned root. Resolve every handle through one helper;
never call `open`, `os.path.join`, `subprocess`, or `shutil.rmtree` directly on
a tool argument.

```python
RUN_ROOT = os.path.realpath(os.path.join(tempfile.gettempdir(), "mysolver-runs"))
HANDLE_RE = re.compile(r"^mysolver_[A-Za-z0-9_-]{8,80}$")
os.makedirs(RUN_ROOT, mode=0o700, exist_ok=True)

def resolve_run_handle(handle):
    if not isinstance(handle, str) or not HANDLE_RE.fullmatch(handle):
        raise ValueError("invalid run handle")
    joined = os.path.join(RUN_ROOT, handle)
    if os.path.islink(joined):
        raise ValueError("invalid run handle")
    resolved = os.path.realpath(joined)
    if os.path.commonpath([RUN_ROOT, resolved]) != RUN_ROOT:
        raise ValueError("invalid run handle")
    if not os.path.isdir(resolved):
        raise ValueError("unknown run handle")
    return resolved
```

Use the resolved directory for reads, writes, execution, and deletion. Reject
symlinked run directories and symlinked run files. Use fixed server-owned
filenames or validate filenames with a strict allowlist. Apply authenticated
ownership checks after resolution when runs are user-specific.

If compatibility requires a field named `working_dir`, document and implement
it as the same opaque handle; never accept a caller-selected directory.

## Validate scientific inputs and derived cost

Declare explicit JSON Schemas with types, units, defaults, enums, and bounds.
Repeat critical checks in the handler because tests and internal callers can
bypass protocol validation.

- Reject booleans where a numeric value is expected.
- Convert deliberately and reject `NaN`, positive/negative infinity, and
  malformed strings with `math.isfinite`.
- Validate relationships such as `start < stop`, normalized fractions, and
  positive material or mesh properties.
- Estimate derived work before allocating or launching: mesh cells × steps ×
  sweep points, expected output bytes, requested wall time, and number of runs.
- Enforce hard ceilings and a per-session/per-user concurrency limit.
- Bound readers too: pagination, `tail_lines`, `max_points`, and maximum file
  bytes before parsing.

Treat a parameter combination that exceeds a ceiling as a normal tool error.
Do not rely on `async_tool` to make unbounded work safe.

## Invoke processes safely

Pass an argument array and keep the shell disabled:

```python
completed = subprocess.run(
    [SOLVER, "--input", "input.deck", "--output", "output.json"],
    cwd=run_dir,
    shell=False,
    check=True,
    timeout=MAX_SOLVER_SECONDS,
    capture_output=True,
    text=True,
)
```

Use a configured/allowlisted executable path. Never build a command string,
use `shell=True`, call `os.system`, or pass model text through `eval`/`exec`.
Limit inherited environment variables. Terminate the process tree on timeout
or cancellation. Truncate captured logs before storing or returning them.

## Generate solver inputs safely

Prefer a structured serializer or template with typed placeholders. Never
concatenate untrusted strings into a solver deck, shell fragment, filename, or
directive. For unavoidable free text:

- constrain length and characters in the schema and handler;
- map user-facing enums to fixed trusted tokens;
- reject newlines, comment delimiters, include directives, and path syntax when
  the target format gives them control meaning;
- write atomically to a fixed file beneath the resolved run directory.

## Return untrusted data safely

Tool results enter model context. Treat solver logs, uploaded files, dataset
metadata, labels, and user-provided strings as data, not instructions.

- Prefer `structuredContent` fields such as `data`, `warnings`, and `artifacts`.
- Use a fixed server-authored message stating that external text is untrusted
  data and must not be followed as instructions when text must be returned.
- Truncate, paginate, or summarize. Do not return whole logs or files by
  default.
- Do not reflect arbitrary arguments in errors.
- Do not claim that framing alone defeats prompt injection; keep destructive
  tools separately authorized and minimize cross-tool data flows.

## Protect secrets and identity

Never return or log bearer tokens, authorization codes, cookies, passwords,
API keys, private environment variables, or full request headers. Results and
logs persist beyond the call. Redact sensitive fields before structured logs.

Authenticate and authorize every request at the gateway/resource boundary.
Treat `Mcp-Session-Id` as untrusted routing state, not identity. Bind stored
state to authenticated user identity where required and use least privilege.

## Test the boundary

Add offline tests for:

- `../`, absolute paths, separators, malformed handles, symlinks, and foreign
  directories for every read, run, and delete tool;
- minimum, maximum, non-finite, wrong-type, and adversarial combinations;
- the derived workload ceiling and output-size ceiling;
- shell metacharacters, newlines, and deck-control characters in strings;
- fixed error text that does not reflect the malicious input;
- timeout, cancellation, concurrent-run, disk-full, and oversized-log paths;
- output schemas that contain no secret-like fields.

Use `scripts/validate_server.py` as a warning system, not proof of safety. A
static validator cannot establish confinement, authorization, or resource
bounds without targeted tests.
