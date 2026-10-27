"""Authenticated loopback bridge that dispatches Blender work on its main thread."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hmac
import json
import os
from queue import Empty, Full, Queue
import re
import socket
import socketserver
import threading
import time
from typing import Any
from uuid import uuid4

from .errors import InvalidParamsError, InvalidStateError
from .registry import DEFERRED_CONTEXT_KEY, DeferredEnvelope
from .tools._blender import get_bpy
from .version import version_string

BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 9877
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 4_194_304
MAX_PENDING_CALLS = 64
MAX_CONNECTION_THREADS = 16
MAX_CLIENT_INFLIGHT = 4
MAX_OPERATION_RECORDS = 128
BUSY_RETRY_AFTER_MS = 250
TIMER_INTERVAL_SECONDS = 0.05
REGISTRY_RETRY_SECONDS = 2.0
_ANONYMOUS_CLIENT = object()
_PROCESS_INSTANCE_ID = str(uuid4())
_LONG_OPERATION_TOOLS = frozenset({
    "python.exec", "render.preview", "render.review_set", "scene.save_current",
    "scene.save_as", "bake.run", "render.detail", "render.ortho_set",
    "render.reference_match", "render.contact_sheet", "creature.bake_from_source", "rig.rom_test",
})
_TOKEN_LOCATION_PATTERN = re.compile(r"^(?:runtime|clipboard|config|env:[A-Za-z_][A-Za-z0-9_]*)$")
_RESULT_SUMMARY_KEYS = (
    "changed",
    "partial_change_possible",
    "state_token",
    "dirty",
    "saved",
)

Dispatch = Callable[
    [str, Mapping[str, Any] | None, Mapping[str, Any] | None], dict[str, Any]
]


def _error_envelope(
    tool: Any,
    error_type: str,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool if isinstance(tool, str) else "",
        "data": dict(data or {}),
        "error": {"type": error_type, "message": message, "traceback": ""},
        "warnings": [],
        "meta": {"addon_version": version_string()},
    }


def _encode_json_line(value: Any) -> bytes:
    """Encode one compact newline-delimited bridge frame."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _bounded_result_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key in _RESULT_SUMMARY_KEYS:
        if key not in data:
            continue
        value = data.get(key)
        if isinstance(value, (bool, int, float)) or value is None:
            summary[key] = value
        elif isinstance(value, str) and len(value) <= 512:
            summary[key] = value
    return summary


def _response_too_large_envelope(
    result: Mapping[str, Any], response_bytes: int
) -> dict[str, Any]:
    """Preserve bounded outcome evidence while refusing unsafe automatic retry."""
    return _error_envelope(
        result.get("tool"),
        "BridgeResponseTooLargeError",
        "Tool result exceeds the response size limit",
        {
            "response_bytes": response_bytes,
            "response_limit_bytes": MAX_RESPONSE_BYTES,
            "original_result_ok": result.get("ok") is True,
            "execution_completed": True,
            "result_discarded": True,
            "automatic_retry_safe": False,
            "result_summary": _bounded_result_summary(result),
            "recovery": {
                "action": "inspect_before_retry",
                "message": (
                    "Do not automatically retry: a mutating call may already have changed Blender. "
                    "Inspect the current document state first. For a confirmed read-only call, retry "
                    "with narrower filters or a smaller limit and continue with next_cursor."
                ),
                "capability_discovery": (
                    "Use search mode and request one toolset at a time."
                ),
            },
        },
    )


def _request_too_large_envelope(received_bytes: int) -> dict[str, Any]:
    """Report a pre-dispatch rejection that is safe to retry with less input."""
    return _error_envelope(
        "",
        "BridgeRequestTooLargeError",
        "Request exceeds the size limit",
        {
            "received_bytes_at_least": received_bytes,
            "request_limit_bytes": MAX_REQUEST_BYTES,
            "execution_started": False,
            "automatic_retry_safe": True,
            "recovery": {
                "action": "reduce_request",
                "message": (
                    "Pass an allowed file path instead of inline content, or use a smaller "
                    "filtered page. The rejected call did not reach Blender."
                ),
            },
        },
    )


def _busy_envelope(
    tool: Any,
    *,
    reason: str,
    message: str,
    queue_depth: int,
    queue_capacity: int,
    **details: Any,
) -> dict[str, Any]:
    return _error_envelope(
        tool,
        "BridgeBusyError",
        message,
        {
            "reason": reason,
            "queue_depth": queue_depth,
            "queue_capacity": queue_capacity,
            "retry_after_ms": BUSY_RETRY_AFTER_MS,
            **details,
        },
    )


@dataclass
class _BridgeCall:
    tool: str
    params: Mapping[str, Any] | None
    context: Mapping[str, Any] | None = None
    _event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state: str = "pending"
    _result: dict[str, Any] | None = None
    _submitted_at: float = field(default_factory=time.perf_counter)
    _started_at: float | None = None
    _finished_at: float | None = None
    _last_updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    operation_id: str = field(default_factory=lambda: str(uuid4()))

    def begin(self) -> bool:
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "running"
            self._started_at = time.perf_counter()
            self._last_updated_at = datetime.now(timezone.utc).isoformat()
            return True

    def complete(self, result: dict[str, Any]) -> bool:
        with self._lock:
            if self._state != "running":
                return False
            if self._started_at is not None:
                timings = {
                    "queue_ms": max(0, round((self._started_at - self._submitted_at) * 1000)),
                    "blender_ms": max(0, round((time.perf_counter() - self._started_at) * 1000)),
                }
                meta = result.setdefault("meta", {})
                if isinstance(meta, dict):
                    meta["timings"] = timings
                    meta["addon_version"] = version_string()
            self._result = result
            self._state = "done"
            self._finished_at = time.perf_counter()
            self._last_updated_at = datetime.now(timezone.utc).isoformat()
            self._event.set()
            return True

    def fail_if_pending(self, error_type: str, message: str) -> bool:
        with self._lock:
            if self._state != "pending":
                return False
            self._result = _error_envelope(self.tool, error_type, message)
            self._state = "cancelled"
            self._finished_at = time.perf_counter()
            self._last_updated_at = datetime.now(timezone.utc).isoformat()
            self._event.set()
            return True

    def fail_if_incomplete(self, error_type: str, message: str) -> bool:
        with self._lock:
            if self._state not in {"pending", "running"}:
                return False
            self._result = _error_envelope(self.tool, error_type, message)
            self._state = "cancelled"
            self._finished_at = time.perf_counter()
            self._last_updated_at = datetime.now(timezone.utc).isoformat()
            self._event.set()
            return True

    def wait(self, pending_timeout: float) -> dict[str, Any]:
        if not self._event.wait(pending_timeout):
            cancelled = self.fail_if_pending(
                "BridgeTimeoutError",
                "Blender did not start the queued call before the bridge timeout; the call was cancelled",
            )
            if not cancelled:
                if self.tool == "python.exec":
                    return _error_envelope(
                        self.tool,
                        "BridgeExecutionPendingError",
                        "Python execution passed the bridge wait limit and may still be running in Blender; inspect state before any retry",
                        {"execution_started": True, "possibly_still_running": True,
                         "automatic_retry_safe": False, "operation_id": self.operation_id},
                    )
                # Other mutations retain their existing authoritative-result behavior.
                self._event.wait()
        assert self._result is not None
        return self._result

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = self._state
            result = self._result
            elapsed_ms = max(0, round(((self._finished_at or time.perf_counter()) - self._submitted_at) * 1000))
            last_updated_at = self._last_updated_at
        status = {
            "pending": "queued", "running": "running", "done": "succeeded" if result and result.get("ok") else "failed",
            "cancelled": "cancelled",
        }[state]
        data: dict[str, Any] = {"operation_id": self.operation_id, "status": status,
                                "last_updated_at": last_updated_at, "elapsed_ms": elapsed_ms}
        if result is not None:
            data["result"] = result
            inner_data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
            error = result.get("error") if isinstance(result.get("error"), Mapping) else None
            data.update({
                "ok": result.get("ok") is True,
                "error": {"type": error.get("type"), "message": error.get("message")} if error else None,
                "changed": inner_data.get("changed"),
                "partial_change_possible": inner_data.get("partial_change_possible", False),
            })
        return data

    def wait_status(self, wait_ms: int) -> dict[str, Any]:
        if wait_ms:
            self._event.wait(wait_ms / 1000)
        return self.status()


class _MainThreadExecutor:
    def __init__(self, dispatch: Dispatch, max_pending: int = MAX_PENDING_CALLS) -> None:
        self._dispatch = dispatch
        self._capacity = max_pending
        self._queue: Queue[_BridgeCall] = Queue(maxsize=max_pending)
        self._closed = False
        self._lock = threading.Lock()
        self._active_deferred: dict[int, tuple[_BridgeCall, DeferredEnvelope]] = {}
        self._operations: OrderedDict[str, _BridgeCall] = OrderedDict()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @property
    def capacity(self) -> int:
        return self._capacity

    def submit(
        self,
        tool: str,
        params: Mapping[str, Any] | None,
        pending_timeout: float,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        call, error = self._enqueue(tool, params, context)
        if error is not None:
            return error
        assert call is not None
        return call.wait(pending_timeout)

    def start(self, tool: str, params: Mapping[str, Any] | None, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        call, error = self._enqueue(tool, params, context)
        if error is not None:
            return error
        assert call is not None
        return {"ok": True, "tool": "bridge.operation_start", "data": call.status(), "error": None, "warnings": [],
                "meta": {"addon_version": version_string()}}

    def operation_status(self, operation_id: str, wait_ms: int = 0) -> dict[str, Any]:
        with self._lock:
            call = self._operations.get(operation_id)
        if call is None:
            from .tools.background_render import lookup_background_status

            background = lookup_background_status(operation_id, wait_ms)
            if background is not None:
                return {"ok": True, "tool": "bridge.operation_status", "data": background,
                        "error": None, "warnings": [], "meta": {"addon_version": version_string()}}
            return _error_envelope("bridge.operation_status", "NotFoundError", "Operation ID is unknown or expired")
        return {"ok": True, "tool": "bridge.operation_status", "data": call.wait_status(wait_ms), "error": None,
                "warnings": [], "meta": {"addon_version": version_string()}}

    def _enqueue(
        self, tool: str, params: Mapping[str, Any] | None, context: Mapping[str, Any] | None
    ) -> tuple[_BridgeCall | None, dict[str, Any] | None]:
        call = _BridgeCall(tool=tool, params=params, context=context)
        with self._lock:
            if self._closed:
                return None, _error_envelope(tool, "BridgeStoppedError", "The Blender bridge is stopped")
            running = next((prior for prior in self._operations.values()
                            if prior.tool in _LONG_OPERATION_TOOLS and prior.status()["status"] == "running"), None)
            if running is not None:
                return None, _busy_envelope(
                    tool, reason="long_operation_running",
                    message=f"Blender main thread is occupied by operation {running.operation_id}",
                    queue_depth=self.pending_count, queue_capacity=self.capacity,
                    running_operation_id=running.operation_id,
                )
            while len(self._operations) >= MAX_OPERATION_RECORDS:
                completed_id = next((operation_id for operation_id, prior in self._operations.items()
                                     if prior.status()["status"] in {"succeeded", "failed", "cancelled"}), None)
                if completed_id is None:
                    return None, _busy_envelope(tool, reason="operation_registry_full",
                        message="Operation registry is full of running calls",
                        queue_depth=self.pending_count, queue_capacity=self.capacity)
                self._operations.pop(completed_id)
            try:
                self._queue.put_nowait(call)
            except Full:
                return None, _busy_envelope(
                    tool,
                    reason="queue_full",
                    message=f"The Blender bridge already has {self._capacity} queued calls",
                    queue_depth=self.pending_count,
                    queue_capacity=self._capacity,
                )
            self._operations[call.operation_id] = call
        return call, None

    def drain(self, max_calls: int = 8) -> int:
        # A deferred UI job owns Blender's real draw context until it resolves.
        # Keep later calls queued so two MCP clients cannot batch multiple GPU
        # captures into the same timer callback.  FIFO submission order is
        # preserved and ordinary draining resumes on the next timer tick after
        # the active job completes.
        with self._lock:
            if self._active_deferred:
                return 0
        completed = 0
        for _ in range(max_calls):
            try:
                call = self._queue.get_nowait()
            except Empty:
                break
            try:
                if not call.begin():
                    continue
                try:
                    dispatch_context = call.context
                    if call.tool in {"scene.screenshot", "scene.fast_capture", "render.detail", "render.ortho_set",
                                     "render.reference_match", "render.contact_sheet", "rig.rom_test"}:
                        dispatch_context = dict(call.context or {})
                        dispatch_context[DEFERRED_CONTEXT_KEY] = True
                    result = self._dispatch(call.tool, call.params, dispatch_context)
                except Exception as exc:  # Protect the timer from adapter-level failures.
                    result = _error_envelope(call.tool, type(exc).__name__, str(exc))
                if isinstance(result, DeferredEnvelope):
                    self._track_deferred(call, result)
                else:
                    call.complete(result)
                completed += 1
                if isinstance(result, DeferredEnvelope):
                    break
            finally:
                self._queue.task_done()
        return completed

    def _track_deferred(self, call: _BridgeCall, result: DeferredEnvelope) -> None:
        key = id(call)
        with self._lock:
            if self._closed:
                closed = True
            else:
                self._active_deferred[key] = (call, result)
                closed = False
        if closed:
            call.fail_if_incomplete(
                "BridgeStoppedError", "The Blender bridge stopped before the deferred call completed"
            )
            result.cancel()
            return

        def complete(envelope: dict[str, Any]) -> None:
            with self._lock:
                self._active_deferred.pop(key, None)
            call.complete(envelope)

        result.add_done_callback(complete)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            active_deferred = list(self._active_deferred.values())
            self._active_deferred.clear()
        while True:
            try:
                call = self._queue.get_nowait()
            except Empty:
                break
            try:
                call.fail_if_pending("BridgeStoppedError", "The Blender bridge stopped before the call ran")
            finally:
                self._queue.task_done()
        for call, result in active_deferred:
            call.fail_if_incomplete(
                "BridgeStoppedError", "The Blender bridge stopped before the deferred call completed"
            )
            result.cancel()


class _BridgeController:
    def __init__(
        self,
        token: str,
        executor: _MainThreadExecutor,
        pending_timeout: float,
        max_client_inflight: int = MAX_CLIENT_INFLIGHT,
    ) -> None:
        self._token = token
        self.executor = executor
        self.pending_timeout = pending_timeout
        self._max_client_inflight = max_client_inflight
        self._client_inflight: dict[object, int] = {}
        self._client_lock = threading.Lock()

    def pressure_status(self) -> dict[str, int]:
        with self._client_lock:
            return {
                "client_inflight": sum(self._client_inflight.values()),
                "active_clients": len(self._client_inflight),
                "per_client_inflight_limit": self._max_client_inflight,
            }

    def _acquire_client_slot(self, client_key: object) -> tuple[bool, int]:
        with self._client_lock:
            current = self._client_inflight.get(client_key, 0)
            if current >= self._max_client_inflight:
                return False, current
            current += 1
            self._client_inflight[client_key] = current
            return True, current

    def _release_client_slot(self, client_key: object) -> None:
        with self._client_lock:
            current = self._client_inflight.get(client_key, 0)
            if current <= 1:
                self._client_inflight.pop(client_key, None)
            else:
                self._client_inflight[client_key] = current - 1

    def handle(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            return _error_envelope("", "BridgeProtocolError", "Request must be a JSON object")
        supplied_token = request.get("token")
        if not isinstance(supplied_token, str) or not hmac.compare_digest(supplied_token, self._token):
            return _error_envelope("", "BridgeAuthenticationError", "Bridge authentication failed")
        unknown = sorted(set(request) - {"token", "tool", "params", "client_id"})
        tool = request.get("tool")
        if unknown:
            return _error_envelope(tool, "BridgeProtocolError", f"Unknown request fields: {', '.join(unknown)}")
        if not isinstance(tool, str) or not tool:
            return _error_envelope("", "BridgeProtocolError", "tool must be a non-empty string")
        params = request.get("params")
        if params is not None and not isinstance(params, Mapping):
            return _error_envelope(tool, "BridgeProtocolError", "params must be a JSON object or null")
        client_id = request.get("client_id")
        if "client_id" in request and (
            not isinstance(client_id, str) or not client_id
        ):
            return _error_envelope(
                tool, "BridgeProtocolError", "client_id must be a non-empty string when supplied"
            )
        client_key = client_id if isinstance(client_id, str) else _ANONYMOUS_CLIENT
        acquired, client_inflight = self._acquire_client_slot(client_key)
        if not acquired:
            return _busy_envelope(
                tool,
                reason="client_inflight_limit",
                message=(
                    f"Client already has {self._max_client_inflight} in-flight bridge calls"
                ),
                queue_depth=self.executor.pending_count,
                queue_capacity=self.executor.capacity,
                client_inflight=client_inflight,
                client_limit=self._max_client_inflight,
            )
        context = {"client_id": client_id} if client_id is not None else None
        try:
            if tool == "bridge.operation_status":
                operation_id = (params or {}).get("operation_id")
                if not isinstance(operation_id, str) or not operation_id:
                    return _error_envelope(tool, "InvalidParamsError", "operation_id must be a non-empty string")
                wait_ms = (params or {}).get("wait_ms", 0)
                if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= 30_000:
                    return _error_envelope(tool, "InvalidParamsError", "wait_ms must be an integer from 0 to 30000")
                return self.executor.operation_status(operation_id, wait_ms)
            if tool == "bridge.operation_start":
                target = (params or {}).get("tool")
                arguments = (params or {}).get("arguments", {})
                if not isinstance(target, str) or target not in _LONG_OPERATION_TOOLS or not isinstance(arguments, Mapping):
                    return _error_envelope(tool, "InvalidParamsError", "tool must be an allowed long operation and arguments must be an object")
                return self.executor.start(target, arguments, context)
            return self.executor.submit(tool, params, self.pending_timeout, context)
        finally:
            self._release_client_slot(client_key)


class _LoopbackServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = MAX_CONNECTION_THREADS

    def __init__(
        self,
        *args,
        max_connection_threads: int = MAX_CONNECTION_THREADS,
        **kwargs,
    ) -> None:
        self._connection_capacity = max_connection_threads
        self._connection_slots = threading.BoundedSemaphore(max_connection_threads)
        self._connection_lock = threading.Lock()
        self._active_connections = 0
        super().__init__(*args, **kwargs)

    def connection_status(self) -> dict[str, int]:
        with self._connection_lock:
            return {
                "active_connections": self._active_connections,
                "connection_capacity": self._connection_capacity,
            }

    def _increment_active_connections(self) -> None:
        with self._connection_lock:
            self._active_connections += 1

    def _release_connection_slot(self) -> None:
        with self._connection_lock:
            self._active_connections -= 1
        self._connection_slots.release()

    def _reject_busy_connection(self, request: socket.socket) -> None:
        connection_status = self.connection_status()
        controller = getattr(self, "controller", None)
        executor = getattr(controller, "executor", None)
        queue_depth = getattr(executor, "pending_count", 0)
        queue_capacity = getattr(executor, "capacity", MAX_PENDING_CALLS)
        result = _busy_envelope(
            "",
            reason="connection_limit",
            message=(
                f"The Blender bridge already has {self._connection_capacity} active connections"
            ),
            queue_depth=queue_depth,
            queue_capacity=queue_capacity,
            **connection_status,
        )
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        # On Windows, closing a socket with unread client data can emit an RST and
        # discard the busy response. Drain at most one bounded request under a short
        # deadline before replying; this path never parses/authenticates or calls bpy.
        try:
            request.settimeout(0.25)
            remaining = MAX_REQUEST_BYTES + 1
            while remaining > 0:
                chunk = request.recv(min(65_536, remaining))
                if not chunk or b"\n" in chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        try:
            request.settimeout(1.0)
            request.sendall(payload)
            try:
                request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            request.close()

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()

    def process_request(self, request, client_address) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self._reject_busy_connection(request)
            return
        self._increment_active_connections()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release_connection_slot()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_connection_slot()


class _BridgeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(5.0)
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_REQUEST_BYTES:
                result = _request_too_large_envelope(len(raw))
            elif not raw.endswith(b"\n"):
                result = _error_envelope(
                    "", "BridgeProtocolError", "Request must end with a newline"
                )
            else:
                try:
                    request = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    result = _error_envelope("", "BridgeProtocolError", "Request must be valid UTF-8 JSON")
                else:
                    result = self.server.controller.handle(request)  # type: ignore[attr-defined]
            self._send(result)
        except (ConnectionError, TimeoutError, OSError):
            # A disconnected local client needs no response; queued/running work retains its
            # normal executor semantics and is never retried implicitly.
            return

    def _send(self, result: dict[str, Any]) -> None:
        try:
            payload = _encode_json_line(result)
        except (TypeError, ValueError):
            payload = _encode_json_line(
                _error_envelope(result.get("tool"), "BridgeSerializationError", "Tool result is not valid JSON")
            )
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = _encode_json_line(_response_too_large_envelope(result, len(payload)))
        self.wfile.write(payload)
        self.wfile.flush()


@dataclass
class _RunningBridge:
    server: _LoopbackServer
    thread: threading.Thread
    executor: _MainThreadExecutor
    port: int
    pending_timeout: float
    instance_id: str = _PROCESS_INSTANCE_ID
    started: str = ""
    token_location: str = "runtime"
    registered_filepath: str = ""
    registry_error: str | None = None
    last_registry_attempt: float = 0.0


_bridge_lock = threading.Lock()
_running_bridge: _RunningBridge | None = None


def _validate_start_params(token: Any, port: Any, pending_timeout: Any) -> tuple[str, int, float]:
    if not isinstance(token, str) or not 32 <= len(token) <= 4096:
        raise InvalidParamsError("token must be a string from 32 to 4096 characters")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise InvalidParamsError("port must be an integer from 1024 to 65535")
    if isinstance(pending_timeout, bool) or not isinstance(pending_timeout, (int, float)):
        raise InvalidParamsError("pending_timeout must be a number")
    timeout = float(pending_timeout)
    if not 0.1 <= timeout <= 300.0:
        raise InvalidParamsError("pending_timeout must be from 0.1 to 300 seconds")
    return token, port, timeout


def _validate_token_location(token_location: Any) -> str:
    if not isinstance(token_location, str) or not _TOKEN_LOCATION_PATTERN.fullmatch(
        token_location
    ):
        raise InvalidParamsError(
            "token_location must be runtime, clipboard, config, or env:<VARIABLE_NAME>"
        )
    return token_location


def _registry_record(running: _RunningBridge, filepath: str) -> dict[str, Any]:
    return {
        "id": running.instance_id,
        "pid": os.getpid(),
        "port": running.port,
        "filepath": filepath,
        "started": running.started,
        "token_location": running.token_location,
    }


def _sync_instance_registry(running: _RunningBridge, bpy: Any) -> None:
    from .instance_registry import register_instance

    running.last_registry_attempt = time.monotonic()
    filepath = str(bpy.data.filepath or "")
    register_instance(_registry_record(running, filepath))
    running.registered_filepath = filepath
    running.registry_error = None


def _timer_callback() -> float | None:
    from .tools.session_lease import expire_lease

    expire_lease()
    with _bridge_lock:
        running = _running_bridge
    if running is None:
        return None
    try:
        bpy = get_bpy()
        filepath = str(bpy.data.filepath or "")
        should_sync = filepath != running.registered_filepath or (
            running.registry_error is not None
            and time.monotonic() - running.last_registry_attempt >= REGISTRY_RETRY_SECONDS
        )
        if should_sync:
            _sync_instance_registry(running, bpy)
    except Exception as exc:
        running.registry_error = f"{type(exc).__name__}: {exc}"
    running.executor.drain()
    return TIMER_INTERVAL_SECONDS


def start_bridge(
    token: str,
    port: int = DEFAULT_BRIDGE_PORT,
    pending_timeout: float = 30.0,
    token_location: str = "runtime",
) -> dict[str, Any]:
    """Start an authenticated localhost bridge. Must be called on Blender's main thread."""
    token, port, pending_timeout = _validate_start_params(token, port, pending_timeout)
    token_location = _validate_token_location(token_location)
    if threading.current_thread() is not threading.main_thread():
        raise InvalidStateError("start_bridge must run on Blender's main thread")
    bpy = get_bpy()
    from .dispatcher import dispatch

    global _running_bridge
    with _bridge_lock:
        if _running_bridge is not None:
            if _running_bridge.port == port:
                raise InvalidStateError(
                    f"Could not bind Blender bridge to {BRIDGE_HOST}:{port}: address in use"
                )
            raise InvalidStateError("The Blender bridge is already running")
        executor = _MainThreadExecutor(dispatch)
        try:
            server = _LoopbackServer((BRIDGE_HOST, port), _BridgeRequestHandler)
        except OSError as exc:
            error_code = getattr(exc, "winerror", None) or exc.errno
            if error_code in {errno.EACCES, errno.EADDRINUSE, 10013, 10048}:
                raise InvalidStateError(
                    f"Could not bind Blender bridge to {BRIDGE_HOST}:{port}: address in use"
                ) from exc
            raise InvalidStateError(f"Could not bind Blender bridge to {BRIDGE_HOST}:{port}: {exc}") from exc
        server.controller = _BridgeController(token, executor, pending_timeout)  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="BlenderBooBridge",
            daemon=True,
        )
        running = _RunningBridge(
            server,
            thread,
            executor,
            port,
            pending_timeout,
            started=datetime.now(timezone.utc).isoformat(),
            token_location=token_location,
        )
        _running_bridge = running
        try:
            bpy.app.timers.register(_timer_callback, first_interval=0.0, persistent=True)
            thread.start()
            _sync_instance_registry(running, bpy)
        except Exception as exc:
            _running_bridge = None
            rollback_errors: list[str] = []
            rollback_steps = [
                ("executor close", executor.close),
                (
                    "server shutdown",
                    lambda: server.shutdown() if thread.is_alive() else None,
                ),
                ("server close", server.server_close),
                ("server thread join", lambda: thread.join(timeout=2.0)),
                (
                    "timer unregister",
                    lambda: bpy.app.timers.unregister(_timer_callback)
                    if bpy.app.timers.is_registered(_timer_callback)
                    else None,
                ),
            ]
            for label, cleanup in rollback_steps:
                try:
                    cleanup()
                except Exception as cleanup_exc:
                    rollback_errors.append(f"{label}: {cleanup_exc}")
            try:
                from .instance_registry import unregister_instance

                unregister_instance(running.instance_id)
            except Exception as cleanup_exc:
                rollback_errors.append(f"registry unregister: {cleanup_exc}")
            if rollback_errors:
                raise InvalidStateError(
                    f"Bridge start failed: {exc}; rollback errors: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise
    return bridge_status()


def stop_bridge() -> dict[str, Any]:
    """Stop the bridge and cancel calls that have not begun executing."""
    if threading.current_thread() is not threading.main_thread():
        raise InvalidStateError("stop_bridge must run on Blender's main thread")
    from .tools.session_lease import clear_lease
    from .tools.background_render import shutdown_background_jobs

    clear_lease()
    global _running_bridge
    with _bridge_lock:
        running = _running_bridge
        _running_bridge = None
    if running is None:
        return bridge_status()
    from .instance_registry import unregister_instance

    cleanup_errors: list[str] = []
    cleanup_steps = [
        (
            "background jobs shutdown",
            shutdown_background_jobs,
        ),
        (
            "registry unregister",
            lambda: unregister_instance(running.instance_id),
        ),
        ("executor close", running.executor.close),
        (
            "server shutdown",
            lambda: running.server.shutdown() if running.thread.is_alive() else None,
        ),
        ("server close", running.server.server_close),
        ("server thread join", lambda: running.thread.join(timeout=2.0)),
    ]
    for label, cleanup in cleanup_steps:
        try:
            cleanup()
        except Exception as exc:
            cleanup_errors.append(f"{label}: {exc}")
    try:
        bpy = get_bpy()
        if bpy.app.timers.is_registered(_timer_callback):
            bpy.app.timers.unregister(_timer_callback)
    except Exception as exc:
        cleanup_errors.append(f"timer unregister: {exc}")
    if cleanup_errors:
        raise InvalidStateError("Bridge stopped with cleanup errors: " + "; ".join(cleanup_errors))
    return bridge_status()


def bridge_status() -> dict[str, Any]:
    """Return non-secret bridge state."""
    with _bridge_lock:
        running = _running_bridge
        if running is None:
            from .instance_registry import registry_path

            return {
                "running": False,
                "host": BRIDGE_HOST,
                "port": None,
                "instance_id": _PROCESS_INSTANCE_ID,
                "started": None,
                "token_location": None,
                "registry_path": str(registry_path()),
                "registry_error": None,
                "pending_calls": 0,
                "queue_depth": 0,
                "queue_capacity": MAX_PENDING_CALLS,
                "active_connections": 0,
                "connection_capacity": MAX_CONNECTION_THREADS,
                "client_inflight": 0,
                "active_clients": 0,
                "per_client_inflight_limit": MAX_CLIENT_INFLIGHT,
            }
        controller = running.server.controller  # type: ignore[attr-defined]
        from .instance_registry import registry_path

        queue_depth = running.executor.pending_count
        return {
            "running": True,
            "host": BRIDGE_HOST,
            "port": running.port,
            "instance_id": running.instance_id,
            "started": running.started,
            "token_location": running.token_location,
            "registry_path": str(registry_path()),
            "registry_error": running.registry_error,
            "pending_calls": queue_depth,
            "queue_depth": queue_depth,
            "queue_capacity": running.executor.capacity,
            **running.server.connection_status(),
            **controller.pressure_status(),
            "pending_timeout": running.pending_timeout,
        }
