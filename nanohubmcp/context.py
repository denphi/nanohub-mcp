"""
Context class for MCP tools and resources.
Provides access to server context within decorated handlers.
Aligned with FastMCP Context API.
"""

from __future__ import print_function

import threading
import traceback
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .types import InputRequired

if TYPE_CHECKING:
    from .server import MCPServer


class Context(object):
    """
    Context object passed to tools and resources.
    Aligned with FastMCP Context API.

    Provides access to:
    - Server information
    - Logging capabilities
    - Request metadata

    Usage:
        @server.tool()
        def my_tool(ctx: Context, arg1: str) -> str:
            ctx.info("Processing arg1: {}".format(arg1))
            return "result"
    """

    def __init__(
        self,
        server=None,  # type: Optional[MCPServer]
        request_id=None,  # type: Optional[str]
        session_id=None,  # type: Optional[str]
        meta=None,  # type: Optional[Dict[str, Any]]
        progress_token=None,  # type: Optional[Any]
        job_id=None,  # type: Optional[str]
        protocol_version=None,  # type: Optional[str]
        input_responses=None,  # type: Optional[Dict[str, Any]]
        request_state=None  # type: Optional[str]
    ):
        # type: (...) -> None
        self._server = server
        self._request_id = request_id
        self._session_id = session_id
        self._meta = meta or {}
        self._progress_token = progress_token
        self._job_id = job_id
        self._log_messages = []  # type: List[Dict[str, Any]]
        # Standalone fallback so is_cancelled()/cancel_event stay usable in
        # sync tools and in unit tests that build a Context by hand.
        self._detached_cancel_event = threading.Event()
        # Multi Round-Trip Requests (2026-07-28): answers the client already
        # supplied on a retry, and the running count of asks in this attempt.
        self._protocol_version = protocol_version
        self._input_responses = input_responses or {}
        self._request_state = request_state
        self._input_index = 0
        self._pending_inputs = {}  # type: Dict[str, Any]

    @property
    def progress_token(self):
        # type: () -> Optional[Any]
        """Token associated with this request for progress notifications."""
        return self._progress_token

    @property
    def server(self):
        # type: () -> Optional[MCPServer]
        """Get the server instance."""
        return self._server

    @property
    def request_id(self):
        # type: () -> Optional[str]
        """Get the current request ID."""
        return self._request_id

    @property
    def session_id(self):
        # type: () -> Optional[str]
        """Get the current client session ID."""
        return self._session_id

    @property
    def meta(self):
        # type: () -> Dict[str, Any]
        """Get request metadata."""
        return self._meta

    # ── Async-task control ────────────────────────────────────────────────
    # Only meaningful inside an @async_tool handler (or its ``prepare``
    # hook), where the server binds this context to a job record.

    @property
    def job_id(self):
        # type: () -> Optional[str]
        """Job/task id of the running async tool, or None for sync tools."""
        return self._job_id

    @property
    def cancel_event(self):
        # type: () -> threading.Event
        """``threading.Event`` set when the client requests cancellation.

        Pass it to blocking waits so a cancel interrupts them promptly::

            while proc.poll() is None:
                if ctx.cancel_event.wait(0.5):
                    proc.terminate()
                    break

        Sync tools get a private event that is never set, so the same code
        is safe to run outside a task.
        """
        job = self._job_record()
        if job is None:
            return self._detached_cancel_event
        event = job.get("cancel_event")
        return event if event is not None else self._detached_cancel_event

    def is_cancelled(self):
        # type: () -> bool
        """True once the client has requested cancellation of this task.

        Long-running handlers should poll this between iterations and return
        (or raise) promptly when it becomes true.
        """
        return self.cancel_event.is_set()

    def on_cancel(self, callback):
        # type: (Callable[[], Any]) -> None
        """Register a callback invoked when the client cancels this task.

        Use it to terminate a process group, close a connection, or delete a
        remote job — work a cooperative poll cannot do while blocked::

            proc = subprocess.Popen(argv, start_new_session=True)
            ctx.on_cancel(lambda: os.killpg(proc.pid, signal.SIGTERM))

        Callbacks run on the thread handling ``tasks/cancel``, not the worker
        thread, so keep them short and non-blocking. Exceptions are caught and
        logged. If cancellation already happened, the callback fires
        immediately — registering late is never silently dropped.
        """
        if not callable(callback):
            raise TypeError("on_cancel() requires a callable")

        job = self._job_record()
        if job is None:
            return

        fire_now = False
        server = self._server
        lock = getattr(server, "_jobs_lock", None) if server else None
        if lock is None:
            return
        with lock:
            if job.get("cancel_event") is not None and job["cancel_event"].is_set():
                fire_now = True
            else:
                job.setdefault("cancel_callbacks", []).append(callback)

        if fire_now:
            try:
                callback()
            except Exception:
                traceback.print_exc()

    @property
    def task_metadata(self):
        # type: () -> Dict[str, Any]
        """Metadata published for this task (a copy; mutate via setter)."""
        job = self._job_record()
        if job is None:
            return {}
        server = self._server
        lock = getattr(server, "_jobs_lock", None) if server else None
        if lock is None:
            return dict(job.get("task_meta") or {})
        with lock:
            return dict(job.get("task_meta") or {})

    def set_task_metadata(self, metadata=None, **kwargs):
        # type: (Optional[Dict[str, Any]], **Any) -> Dict[str, Any]
        """Publish durable metadata for this task; returns the merged dict.

        Values surface as ``_meta`` on ``tasks/get`` responses (and on the
        initial task handle when set from a ``prepare`` hook, which runs
        before the call returns). Use it for identifiers the caller needs in
        order to find the work again — a scheduler job id, a run handle::

            ctx.set_task_metadata(jobHandle=submit_to_cluster(deck))

        Bare keys are namespaced automatically; fully-qualified keys are kept
        as given. Keys reserved by MCP are rejected.
        """
        merged = dict(metadata or {})
        merged.update(kwargs)
        if not merged:
            return self.task_metadata

        job = self._job_record()
        if job is None:
            return {}

        server = self._server
        normalize = getattr(server, "_normalize_task_meta", None)
        if normalize is not None:
            merged = normalize(merged)

        lock = getattr(server, "_jobs_lock", None) if server else None
        if lock is None:
            current = job.setdefault("task_meta", {})
            current.update(merged)
            return dict(current)
        with lock:
            current = job.setdefault("task_meta", {})
            current.update(merged)
            job["lastUpdatedAt"] = server._utc_now()
            snapshot = dict(current)

        # Outside the lock. A no-op until the client has the task id and has
        # subscribed, so metadata set from a prepare hook notifies no one.
        notify = getattr(server, "_notify_task_status", None)
        if notify is not None:
            notify(self._job_id)
        return snapshot

    def _job_record(self):
        # type: () -> Optional[Dict[str, Any]]
        """Look up this context's job record, if it is bound to one."""
        if not self._job_id or self._server is None:
            return None
        jobs = getattr(self._server, "_jobs", None)
        if jobs is None:
            return None
        return jobs.get(self._job_id)

    def debug(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Log a debug message."""
        self._log("debug", message, kwargs)

    def info(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Log an info message."""
        self._log("info", message, kwargs)

    def warning(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Log a warning message."""
        self._log("warning", message, kwargs)

    def error(self, message, **kwargs):
        # type: (str, **Any) -> None
        """Log an error message."""
        self._log("error", message, kwargs)

    _LEVEL_ORDER = ("debug", "info", "notice", "warning", "error",
                    "critical", "alert", "emergency")

    def _log(self, level, message, data):
        # type: (str, str, Dict[str, Any]) -> None
        """Record a log line, and forward it when the client asked for logs.

        2026-07-28 removed `logging/setLevel`: the level rides each request in
        `_meta`, and a server **MUST NOT** emit `notifications/message` for a
        request that did not carry one. So the console line is unconditional
        (it is the operator's log) while the notification is not.
        """
        log_entry = {
            "level": level,
            "message": message,
            "data": data
        }
        self._log_messages.append(log_entry)
        # Also print to console
        print("[{}] {}".format(level.upper(), message))

        requested = self._requested_log_level()
        if requested is None or self._server is None:
            return
        try:
            threshold = self._LEVEL_ORDER.index(requested)
            severity = self._LEVEL_ORDER.index(level)
        except ValueError:
            return
        if severity < threshold:
            return

        params = {"level": level, "logger": self._server.name,
                  "data": {"message": message}}
        if data:
            params["data"]["context"] = data
        self._server._broadcast({
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": params,
        }, session_id=self._session_id)

    def _requested_log_level(self):
        # type: () -> Optional[str]
        """The level this request asked for, or None if it asked for nothing."""
        meta = self._meta if isinstance(self._meta, dict) else {}
        level = meta.get("io.modelcontextprotocol/logLevel")
        return level if isinstance(level, str) and level else None

    def get_log_messages(self):
        # type: () -> List[Dict[str, Any]]
        """Get all logged messages for this context."""
        return self._log_messages

    # ── Multi Round-Trip Requests (2026-07-28) ────────────────────────────

    def _uses_mrtr(self):
        # type: () -> bool
        """True when this request must ask for input by returning, not pushing.

        2026-07-28 removed server-initiated requests. Older revisions keep the
        blocking `elicitation/create` path, so both styles coexist and the tool
        body is written the same way for either.
        """
        server = self._server
        if server is None or not self._protocol_version:
            return False
        checker = getattr(server, "_is_stateless", None)
        return bool(checker and checker(self._protocol_version))

    _MRTR_CAPABILITY = {
        "elicitation/create": "elicitation",
        "sampling/createMessage": "sampling",
        "roots/list": "roots",
    }

    def _require_capability(self, method, mode=None):
        # type: (str, Optional[str]) -> None
        """Raise exactly as the blocking path does when the client can't answer.

        Without this an MRTR client that never declared `elicitation` would be
        sent an `InputRequiredResult` it can never satisfy, and a handler's
        fail-closed `except RuntimeError` would never fire — it would neither
        act nor refuse. Both stacks must fail the same way.
        """
        capability = self._MRTR_CAPABILITY.get(method)
        if not capability or self._server is None:
            return
        supports = getattr(self._server, "_client_supports", None)
        if supports is None:
            return
        if not supports(self._session_id, capability, mode,
                        params={"_meta": self._meta}):
            raise RuntimeError("Client does not support {} {}".format(
                mode or "", capability).strip())

    def _mrtr_ask(self, method, params, timeout=60, mode=None):
        # type: (str, Dict[str, Any], float, Optional[str]) -> Any
        """Ask the client for input under MRTR, by the route this call allows.

        Two shapes, because the two call styles differ in what survives the
        wait:

        * **Sync tool call** — the server has nothing to block on, so it
          returns an ``InputRequiredResult`` and the client re-drives the call.
          The handler therefore runs again from the top, and answers are keyed
          by ask order, so it must be deterministic up to each ask.
        * **Async tool (task)** — the worker thread is alive and holding its
          state, so the task moves to ``input_required`` and the thread parks
          until ``tasks/update`` arrives. It then resumes in place; nothing has
          to be idempotent and no work is repeated.
        """
        self._require_capability(method, mode)

        self._input_index += 1
        key = "input-{}".format(self._input_index)

        if key in self._input_responses:
            return self._input_responses[key]

        request = {"method": method}
        if params is not None:
            request["params"] = params

        if self._job_id and self._server is not None:
            answers = self._server._task_await_input(
                self._job_id, {key: request}, timeout
            )
            self._input_responses.update(answers)
            if key in answers:
                return answers[key]
            raise RuntimeError("No response supplied for {}".format(key))

        raise InputRequired({key: request})

    def elicit(self, message, requested_schema=None, timeout=60):
        # type: (str, Optional[Dict[str, Any]], int) -> Dict[str, Any]
        """Request structured user input from the connected MCP client."""
        if not self._server:
            raise RuntimeError("Context is not attached to a server")
        if self._uses_mrtr():
            return self._mrtr_ask("elicitation/create", {
                "mode": "form",
                "message": message,
                "requestedSchema": requested_schema or {
                    "type": "object", "properties": {}, "required": []
                },
            }, timeout=timeout, mode="form")
        return self._server.request_elicitation(
            session_id=self._session_id,
            message=message,
            requested_schema=requested_schema,
            mode="form",
            timeout=timeout
        )

    def elicit_url(self, message, url, elicitation_id=None, timeout=60):
        # type: (str, str, Optional[str], int) -> Dict[str, Any]
        """Request URL-mode elicitation from the connected MCP client."""
        if not self._server:
            raise RuntimeError("Context is not attached to a server")
        if self._uses_mrtr():
            # 2026-07-28 dropped elicitationId with the completion notification:
            # the client reports the outcome by retrying the original request.
            return self._mrtr_ask("elicitation/create", {
                "mode": "url", "message": message, "url": url,
            }, timeout=timeout, mode="url")
        return self._server.request_elicitation(
            session_id=self._session_id,
            message=message,
            mode="url",
            url=url,
            elicitation_id=elicitation_id,
            timeout=timeout
        )

    def sample(self, params, timeout=60):
        # type: (Dict[str, Any], int) -> Dict[str, Any]
        """Request sampling from the connected MCP client."""
        if not self._server:
            raise RuntimeError("Context is not attached to a server")
        if self._uses_mrtr():
            return self._mrtr_ask("sampling/createMessage", params, timeout=timeout)
        return self._server.request_sampling(self._session_id, params, timeout=timeout)

    def list_roots(self, timeout=60):
        # type: (int) -> Dict[str, Any]
        """Request roots from the connected MCP client."""
        if not self._server:
            raise RuntimeError("Context is not attached to a server")
        if self._uses_mrtr():
            return self._mrtr_ask("roots/list", {}, timeout=timeout)
        return self._server.request_roots(self._session_id, timeout=timeout)

    def report_progress(self, progress, total=None, message=None):
        # type: (float, Optional[float], Optional[str]) -> None
        """
        Report progress for long-running operations.
        Aligned with FastMCP progress reporting.

        Args:
            progress: Current progress value
            total: Total expected value (optional)
            message: Progress message (optional)
        """
        progress_info = {"progress": progress}
        if total is not None:
            progress_info["total"] = total
        if message:
            progress_info["message"] = message

        self.info("Progress: {}".format(progress_info))

        # MCP spec: notifications/progress must carry the progressToken that
        # the client originally sent in the request's _meta.progressToken so
        # the client can correlate the notification with its in-flight call.
        # If no token was provided, suppress the broadcast — emitting an
        # uncorrelated notification would just be noise to the client.
        if (
            self._progress_token is not None
            and self._server
            and hasattr(self._server, "_broadcast")
        ):
            params = {"progressToken": self._progress_token}
            params.update(progress_info)
            self._server._broadcast({
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": params
            }, session_id=self._session_id)
