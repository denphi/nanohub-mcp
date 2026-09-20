"""Async tools: jobs, the MCP Tasks extension, and MRTR input state.

A tool decorated with ``@server.async_tool`` returns immediately and runs in a
background thread. Two protocols are served from that one job record: clients
declaring ``io.modelcontextprotocol/tasks`` get a task handle and poll
``tasks/get``, while older clients get a ``job_id`` and poll the built-in
``get_job_result`` tool. This module holds both, along with the multi-round
tool-request (MRTR) state that lets a running job ask its caller a question
and resume once the answer arrives.

The job records themselves stay on the MCPServer: ``_jobs``, ``_jobs_lock``
and the MRTR dictionaries are read directly by ``nanohubmcp.context`` and by
the request handler, so this module takes the server and works against its
state rather than owning any.
"""

from __future__ import print_function

import threading
import time
import traceback
import uuid

from typing import Any, Callable, Dict, List, Optional

from .decorators import tool
from .protocol import MCP_SUBSCRIPTION_ID_KEY
from .types import ToolResult

# How long a partially-answered MRTR request is remembered between retries.
MRTR_STATE_TTL_SECONDS = 10 * 60

# MCP Tasks extension (https://github.com/modelcontextprotocol/experimental-ext-tasks).
# Async tools can return a task handle to clients that opt into this extension,
# while older clients continue to receive the existing get_job_result flow.
MCP_TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"

MCP_TASK_TTL_MS = 60 * 60 * 1000

MCP_TASK_POLL_INTERVAL_MS = 1000

# The Tasks `Task` object is a closed shape with no `_meta`, but
# `CreateTaskResult = Result & Task` and `GetTaskResult = Result & ...`, so the
# *response envelope* carries the base-protocol `_meta`. That is where a tool's
# durable handles ride. Unqualified keys set by tools get this prefix; MCP
# reserves any prefix whose second label is `modelcontextprotocol` or `mcp`.
TASK_META_PREFIX = "org.nanohub/"

_RESERVED_META_LABELS = ("modelcontextprotocol", "mcp")


def register_get_job_result(server):
    # type: () -> None
    """Auto-register the built-in get_job_result polling tool."""
    server_instance = server

    def get_job_result(job_id):
        # type: (str) -> Dict[str, Any]
        """Poll the result of a long-running async tool call.

        Returns status 'running' while the job is in progress, or the final
        result/error once it completes. The first successful poll consumes
        the job — subsequent polls return ``not_found`` — so the server
        doesn't accumulate finished-job state for the lifetime of the
        process.

        Args:
            job_id: The job ID returned by an async tool call.
        """
        with server_instance._jobs_lock:
            job = server_instance._jobs.get(job_id)
            if job is None:
                return {"status": "not_found", "job_id": job_id}
            task_meta = dict(job.get("task_meta") or {})
            if job["status"] == "running":
                running = {"status": "running", "job_id": job_id}
                if task_meta:
                    running["meta"] = task_meta
                return running
            # Terminal state — remove so memory doesn't grow unbounded.
            server_instance._jobs.pop(job_id, None)

        if job["status"] == "cancelled":
            payload = {"status": "cancelled", "job_id": job_id}
        elif job["status"] == "error":
            payload = {"status": "error", "job_id": job_id, "error": job["result"]}
        else:
            payload = {"status": "done", "job_id": job_id, "result": job["result"]}
        if task_meta:
            payload["meta"] = task_meta
        return payload

    decorated = tool(
        name="get_job_result",
        description=(
            "Poll the result of a long-running async tool call. "
            "Pass the job_id returned by an async tool. "
            "Returns {\"status\": \"running\"} until complete, then the final result."
        ),
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"]
        }
    )(get_job_result)
    server._register_tool_function(decorated)


def normalize_task_meta(metadata):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    """Namespace and validate task `_meta` keys per the MCP naming rules.

    Bare keys (``jobHandle``) get :data:`TASK_META_PREFIX`; already-qualified
    keys pass through. Prefixes reserved by MCP raise ``ValueError`` rather
    than emitting non-conformant traffic.
    """
    normalized = {}
    for key, value in (metadata or {}).items():
        if not isinstance(key, str) or not key:
            raise ValueError("task metadata keys must be non-empty strings")
        if "/" in key:
            prefix, _, name = key.partition("/")
            labels = prefix.split(".")
            if len(labels) >= 2 and labels[1] in _RESERVED_META_LABELS:
                raise ValueError(
                    "task metadata key {!r} uses an MCP-reserved prefix".format(key)
                )
            if not name:
                raise ValueError("task metadata key {!r} has no name".format(key))
            normalized[key] = value
        else:
            normalized[TASK_META_PREFIX + key] = value
    return normalized


def notify_task_status(server, job_id):
    # type: (str) -> None
    """Push `notifications/tasks` to sessions subscribed to this task.

    Complements polling rather than replacing it: the spec says servers MAY
    push status updates *in addition to* servicing `tasks/get`, and clients
    MAY keep polling. Sending is strictly opt-in — only sessions that named
    this task in a `subscriptions/listen` request receive anything.
    """
    with server._jobs_lock:
        job = server._jobs.get(job_id)
        if job is None:
            return
        session_id = job.get("session_id")
        task = server._job_to_task(job_id, job)

    if not session_id:
        return

    with server._subs_lock:
        subscriptions = [
            sub_id
            for sub_id, sub in (server._subscriptions.get(session_id) or {}).items()
            if job_id in sub.get("task_ids", ())
        ]

    for sub_id in subscriptions:
        params = dict(task)
        meta = dict(params.get("_meta") or {})
        meta[MCP_SUBSCRIPTION_ID_KEY] = sub_id
        params["_meta"] = meta
        server._broadcast({
            "jsonrpc": "2.0",
            "method": "notifications/tasks",
            "params": params,
        }, session_id=session_id)


def start_async_tool_job(server, handler, msg_id, arguments, session_id=None,
                          progress_token=None, meta=None, prepare=None,
                          protocol_version=None, tool_name=None):
    # type: (Any, Any, Any, Dict[str, Any], Optional[str], Optional[Any], Optional[Dict[str, Any]], Optional[Callable], Optional[str], Optional[str]) -> str
    """Spawn a background thread for an async tool; return a job_id immediately.

    ``prepare`` runs synchronously on the request thread *before* the
    worker starts, so anything it publishes via ``ctx.set_task_metadata()``
    (or returns as a dict) is already on the job record when the initial
    task handle is built. That is the only way for a durable handle to
    reach the caller at dispatch — the handler itself has not run yet. A
    raising ``prepare`` aborts the call and starts no job.
    """
    # Snapshot the arguments dict — the closure runs on a background thread
    # and we don't want later mutations of the caller's dict to leak in.
    arguments = dict(arguments) if arguments else {}

    job_id = str(uuid.uuid4())
    now = server._utc_now()
    with server._jobs_lock:
        server._jobs[job_id] = {
            "status": "running",
            "result": None,
            "createdAt": now,
            "lastUpdatedAt": now,
            "ttlMs": MCP_TASK_TTL_MS,
            "pollIntervalMs": MCP_TASK_POLL_INTERVAL_MS,
            "session_id": session_id,
            "request_id": msg_id,
            # Kept so the terminal payload can be held to the tool's
            # published outputSchema, exactly as a sync call is.
            "tool_name": tool_name,
            "expires_at": time.time() + (MCP_TASK_TTL_MS / 1000.0),
            "cancel_event": threading.Event(),
            "cancel_callbacks": [],
            "task_meta": {},
            # MRTR for tasks: the worker parks here while the client
            # answers, rather than re-running like a sync call does.
            "input_requests": {},
            "input_responses": {},
            "input_event": threading.Event(),
        }

    if prepare is not None:
        try:
            prepared = server._call_handler(
                prepare, msg_id, arguments,
                session_id=session_id,
                progress_token=progress_token,
                meta=meta,
                job_id=job_id,
                protocol_version=protocol_version,
            )
        except Exception:
            # No worker was started, so drop the record rather than leave a
            # phantom "running" task the client would poll forever.
            with server._jobs_lock:
                server._jobs.pop(job_id, None)
            raise
        if isinstance(prepared, dict) and prepared:
            normalized = normalize_task_meta(prepared)
            with server._jobs_lock:
                job = server._jobs.get(job_id)
                if job is not None:
                    job.setdefault("task_meta", {}).update(normalized)

    server_instance = server

    def _run():
        try:
            call_result = server._call_handler(
                handler, msg_id, arguments,
                session_id=session_id,
                progress_token=progress_token,
                meta=meta,
                job_id=job_id,
                protocol_version=protocol_version,
            )

            # If the tool signalled failure via ToolResult(isError=True),
            # surface that as a job error rather than a successful result.
            if isinstance(call_result, ToolResult):
                payload = call_result.to_dict()
                if payload.get("isError"):
                    items = payload.get("content", [])
                    message = (
                        items[0]["text"]
                        if len(items) == 1 and "text" in items[0]
                        else payload
                    )
                    with server_instance._jobs_lock:
                        if server_instance._jobs[job_id].get("status") == "cancelled":
                            return
                        server_instance._jobs[job_id]["status"] = "error"
                        server_instance._jobs[job_id]["result"] = message
                        server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
                    # Outside the lock: notifying takes other locks.
                    server_instance._notify_task_status(job_id)
                    return
                result = payload
            elif isinstance(call_result, dict):
                result = call_result
            else:
                result = str(call_result)
            with server_instance._jobs_lock:
                if server_instance._jobs[job_id].get("status") == "cancelled":
                    return
                server_instance._jobs[job_id]["status"] = "done"
                server_instance._jobs[job_id]["result"] = result
                server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
            server_instance._notify_task_status(job_id)
        except Exception as e:
            with server_instance._jobs_lock:
                if server_instance._jobs[job_id].get("status") == "cancelled":
                    return
                server_instance._jobs[job_id]["status"] = "error"
                server_instance._jobs[job_id]["result"] = str(e)
                server_instance._jobs[job_id]["lastUpdatedAt"] = server_instance._utc_now()
            server_instance._notify_task_status(job_id)
            traceback.print_exc()

    t = threading.Thread(target=_run)
    t.daemon = True
    t.start()
    return job_id


def job_to_task(server, task_id, job, include_terminal_payload=True):
    # type: (str, Dict[str, Any], bool) -> Dict[str, Any]
    """Convert an internal async-job record into an MCP Task object."""
    status_map = {
        "running": "working",
        "input_required": "input_required",
        "done": "completed",
        "error": "failed",
        "cancelled": "cancelled",
    }
    status = status_map.get(job.get("status"), "working")
    now = server._utc_now()
    task = {
        "taskId": task_id,
        "status": status,
        "createdAt": job.get("createdAt", now),
        "lastUpdatedAt": job.get("lastUpdatedAt", now),
        "ttlMs": job.get("ttlMs", MCP_TASK_TTL_MS),
        "pollIntervalMs": job.get("pollIntervalMs", MCP_TASK_POLL_INTERVAL_MS),
    }
    task_meta = job.get("task_meta") or {}
    if task_meta:
        # Task itself has no _meta; the Result half of the intersection does.
        task["_meta"] = dict(task_meta)
    if status == "input_required":
        # InputRequiredTask requires the outstanding asks.
        task["inputRequests"] = dict(job.get("input_requests") or {})
        task["statusMessage"] = "Waiting for input from the client."
    elif status == "working":
        task["statusMessage"] = "The operation is in progress."
    elif status == "cancelled":
        task["statusMessage"] = "Cancellation was requested."
    elif include_terminal_payload and status == "completed":
        task["result"] = server._tool_result_payload(
            job.get("result"), job.get("tool_name"))
    elif include_terminal_payload and status == "failed":
        task["error"] = {
            "code": -32603,
            "message": str(job.get("result", "Task failed")),
        }
    return task


def task_access_error(server, task_id, job, session_id):
    # type: (str, Optional[Dict[str, Any]], Optional[str]) -> Optional[Dict[str, Any]]
    """Return a JSON-RPC error when the caller cannot access a task.

    Handshake-era clients are confined to tasks their own session created.
    A stateless (2026-07-28) task has no owning session: the server-minted
    `taskId` is itself the unguessable handle, which is the cross-call
    state mechanism that revision prescribes.
    """
    if job is None:
        return {"code": -32602, "message": "Unknown taskId: {}".format(task_id)}
    owner = job.get("session_id")
    if owner is None:
        return None
    if owner != session_id:
        return {
            "code": -32003,
            "message": "Task is not available in this session",
        }
    return None


def task_await_input(server, job_id, requests, timeout):
    # type: (str, Dict[str, Any], float) -> Dict[str, Any]
    """Park an async worker in `input_required` until the client answers.

    Unlike a sync call — which the client re-drives from the top — the
    worker thread is still alive and holding its state, so it waits here
    and resumes in place. Nothing needs to be idempotent.
    """
    with server._jobs_lock:
        job = server._jobs.get(job_id)
        if job is None:
            raise RuntimeError("Task {} no longer exists".format(job_id))
        if job.get("status") != "running":
            raise RuntimeError("Task {} is not running".format(job_id))
        job["input_requests"] = dict(requests)
        job["status"] = "input_required"
        job["lastUpdatedAt"] = server._utc_now()
        event = job.get("input_event")
        if event is None:
            event = threading.Event()
            job["input_event"] = event
        event.clear()
        cancel_event = job.get("cancel_event")

    # Tell subscribers the task now needs something, so a client that
    # pushes rather than polls still learns about the ask.
    server._notify_task_status(job_id)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Task {} was cancelled while awaiting input".format(job_id))
        if event.wait(0.1):
            break
    else:
        with server._jobs_lock:
            job = server._jobs.get(job_id)
            if job is not None and job.get("status") == "input_required":
                job["status"] = "running"
                job["input_requests"] = {}
                job["lastUpdatedAt"] = server._utc_now()
        raise RuntimeError("Timed out waiting for client input on task {}".format(job_id))

    # Cancellation wakes this same event, so re-check it before reading:
    # otherwise a cancelled task reports "no response supplied", and a
    # handler failing closed on RuntimeError logs the wrong cause.
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError(
            "Task {} was cancelled while awaiting input".format(job_id))

    with server._jobs_lock:
        job = server._jobs.get(job_id) or {}
        answers = dict(job.get("input_responses") or {})
    return answers


def task_deliver_input(server, job_id, responses):
    # type: (str, Dict[str, Any]) -> Optional[Dict[str, Any]]
    """Hand tasks/update answers to a parked worker and wake it."""
    with server._jobs_lock:
        job = server._jobs.get(job_id)
        if job is None:
            return {"code": -32602, "message": "Unknown taskId: {}".format(job_id)}
        if job.get("status") != "input_required":
            return {
                "code": -32602,
                "message": "Task {} is not awaiting input".format(job_id),
            }
        outstanding = set(job.get("input_requests") or {})
        unknown = [k for k in responses if k not in outstanding]
        if unknown:
            return {
                "code": -32602,
                "message": "inputResponses keys not outstanding: {}".format(
                    ", ".join(sorted(unknown))),
            }
        job.setdefault("input_responses", {}).update(responses)
        job["input_requests"] = {}
        job["status"] = "running"
        job["lastUpdatedAt"] = server._utc_now()
        event = job.get("input_event")
    if event is not None:
        event.set()
    server._notify_task_status(job_id)
    return None


def mrtr_load(server, request_state, session_id=None):
    # type: (Optional[str], Optional[str]) -> Dict[str, Any]
    """Answers already collected for this logical request.

    Bound to the session that created the state, exactly as a task is.
    Without that, one session presenting another's `requestState` inherits
    answers a different user gave — including an approval it never asked
    for. Returning empty rather than erroring means the worst case is being
    asked again, which is the safe direction.
    """
    if not request_state:
        return {}
    with server._mrtr_lock:
        entry = server._mrtr_states.get(request_state)
        if entry is None or entry.get("expires_at", 0) <= time.time():
            server._mrtr_states.pop(request_state, None)
            return {}
        if entry.get("session_id") != session_id:
            return {}
        return dict(entry.get("responses") or {})


def mrtr_save(server, request_state, responses, session_id=None):
    # type: (Optional[str], Dict[str, Any], Optional[str]) -> str
    """Persist accumulated answers; return the state id to hand the client.

    A multi-step handler asks once per round trip, so answers must survive
    between retries even though the client may only resend the newest one.
    """
    state_id = request_state or ("mrtr_" + uuid.uuid4().hex)
    with server._mrtr_lock:
        existing = server._mrtr_states.get(state_id)
        if existing is not None and existing.get("session_id") != session_id:
            # Someone else's state. Never rebind it — overwriting the owner
            # would let any session permanently break another's in-flight
            # request just by naming its id. Start a fresh one instead.
            state_id = "mrtr_" + uuid.uuid4().hex
        server._mrtr_states[state_id] = {
            "responses": dict(responses or {}),
            "session_id": session_id,
            "expires_at": time.time() + MRTR_STATE_TTL_SECONDS,
        }
    return state_id


def mrtr_discard(server, request_state):
    # type: (Optional[str]) -> None
    """Drop state once the request has finally completed or failed."""
    if not request_state:
        return
    with server._mrtr_lock:
        server._mrtr_states.pop(request_state, None)


def prune_expired_mrtr_states(server):
    # type: () -> None
    """Expire abandoned round-trips so memory can't grow unbounded."""
    now = time.time()
    with server._mrtr_lock:
        for state_id in [
            k for k, v in server._mrtr_states.items()
            if v.get("expires_at", 0) <= now
        ]:
            server._mrtr_states.pop(state_id, None)


def prune_expired_jobs(server):
    # type: () -> None
    """Remove task/job records whose retention window has elapsed."""
    now = time.time()
    with server._jobs_lock:
        expired = [
            job_id for job_id, job in server._jobs.items()
            if job.get("expires_at") is not None and job.get("expires_at") <= now
        ]
        for job_id in expired:
            server._jobs.pop(job_id, None)


def client_supports_tasks(server, session_id=None, params=None):
    # type: (Optional[str], Optional[Dict[str, Any]]) -> bool
    """Return True when a client opted into the MCP Tasks extension."""
    extension_sets = []

    if isinstance(params, dict):
        meta = params.get("_meta")
        if isinstance(meta, dict):
            request_caps = meta.get("io.modelcontextprotocol/clientCapabilities")
            if isinstance(request_caps, dict):
                extension_sets.append(request_caps.get("extensions"))

    capabilities = server._client_capabilities(session_id)
    extension_sets.append(capabilities.get("extensions"))
    experimental = capabilities.get("experimental")
    if isinstance(experimental, dict):
        extension_sets.append(experimental)

    for extensions in extension_sets:
        if isinstance(extensions, dict) and MCP_TASKS_EXTENSION_ID in extensions:
            return True
    return False


def rpc_tasks_get(server, ctx):
    # type: (_RequestContext) -> tuple
    """Handle the JSON-RPC `tasks/get` method."""
    params, session_id = ctx.params, ctx.session_id
    result = None
    error = None

    task_id = params.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        error = {"code": -32602, "message": "tasks/get requires taskId"}
    elif not server._client_supports_tasks(session_id=session_id, params=params):
        error = {
            "code": -32003,
            "message": "Missing required client capability",
            "data": {
                "requiredCapabilities": {
                    "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                }
            },
        }
    else:
        with server._jobs_lock:
            job = server._jobs.get(task_id)
            access_error = server._task_access_error(task_id, job, session_id)
            task = (
                server._job_to_task(task_id, job)
                if access_error is None
                else None
            )
        if access_error is not None:
            error = access_error
        else:
            task["resultType"] = "complete"
            result = task
    return result, error


def rpc_tasks_update(server, ctx):
    # type: (_RequestContext) -> tuple
    """Handle the JSON-RPC `tasks/update` method."""
    params, session_id = ctx.params, ctx.session_id
    result = None
    error = None

    task_id = params.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        error = {"code": -32602, "message": "tasks/update requires taskId"}
    elif not server._client_supports_tasks(session_id=session_id, params=params):
        error = {
            "code": -32003,
            "message": "Missing required client capability",
            "data": {
                "requiredCapabilities": {
                    "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                }
            },
        }
    else:
        with server._jobs_lock:
            job = server._jobs.get(task_id)
            access_error = server._task_access_error(task_id, job, session_id)
        if access_error is not None:
            error = access_error
        else:
            responses = params.get("inputResponses")
            if not isinstance(responses, dict) or not responses:
                error = {
                    "code": -32602,
                    "message": "tasks/update requires inputResponses",
                }
            else:
                error = server._task_deliver_input(task_id, responses)
                if error is None:
                    result = {"resultType": "complete"}
    return result, error


def rpc_tasks_cancel(server, ctx):
    # type: (_RequestContext) -> tuple
    """Handle the JSON-RPC `tasks/cancel` method."""
    params, session_id = ctx.params, ctx.session_id
    result = None
    error = None

    task_id = params.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        error = {"code": -32602, "message": "tasks/cancel requires taskId"}
    elif not server._client_supports_tasks(session_id=session_id, params=params):
        error = {
            "code": -32003,
            "message": "Missing required client capability",
            "data": {
                "requiredCapabilities": {
                    "extensions": {MCP_TASKS_EXTENSION_ID: {}}
                }
            },
        }
    else:
        callbacks = []
        with server._jobs_lock:
            job = server._jobs.get(task_id)
            access_error = server._task_access_error(task_id, job, session_id)
            if access_error is None and job.get("status") in (
                    "running", "input_required"):
                job["status"] = "cancelled"
                job["lastUpdatedAt"] = server._utc_now()
                event = job.get("cancel_event")
                if event is not None:
                    event.set()
                waiting = job.get("input_event")
                if waiting is not None:
                    waiting.set()
                callbacks = list(job.get("cancel_callbacks") or [])
        if access_error is not None:
            error = access_error
        else:
            # Outside the lock: callbacks kill process groups and
            # must not block every other job's bookkeeping.
            server._fire_cancel_callbacks(task_id, callbacks)
            if callbacks or job is not None:
                server._notify_task_status(task_id)
            result = {"resultType": "complete"}
    return result, error
