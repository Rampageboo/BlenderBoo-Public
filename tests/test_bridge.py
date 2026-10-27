"""Pure-Python tests for the authenticated Blender main-thread bridge."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import socket
import socketserver
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))

from blenderboo.bridge import (
    BRIDGE_HOST,
    _BridgeController,
    _BridgeCall,
    _BridgeRequestHandler,
    _LoopbackServer,
    _MainThreadExecutor,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    _response_too_large_envelope,
    bridge_status,
    start_bridge,
    stop_bridge,
    _validate_start_params,
)
from blenderboo_mcp.client import (
    MAX_REQUEST_BYTES as CLIENT_MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES as CLIENT_MAX_RESPONSE_BYTES,
)
import blenderboo.bridge as bridge_module
from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.registry import DeferredEnvelope, DeferredToolResult, ToolResult


TOKEN = "test-token-that-is-at-least-32-characters"


def success_dispatch(tool, params, context=None):
    return {"ok": True, "tool": tool, "data": dict(params or {}), "error": None, "warnings": []}


class BridgeTests(unittest.TestCase):
    def test_authenticated_long_operation_can_be_polled_without_reexecution(self) -> None:
        calls = []

        def dispatch(tool, params, context=None):
            calls.append(tool)
            return success_dispatch(tool, params, context)

        executor = _MainThreadExecutor(dispatch)
        controller = _BridgeController(TOKEN, executor, 0.01)
        start = controller.handle({
            "token": TOKEN, "tool": "bridge.operation_start",
            "params": {"tool": "python.exec", "arguments": {"code": "result = 1"}},
        })
        self.assertTrue(start["ok"])
        operation_id = start["data"]["operation_id"]
        self.assertEqual(start["data"]["status"], "queued")
        denied = controller.handle({"token": "wrong", "tool": "bridge.operation_status",
                                    "params": {"operation_id": operation_id}})
        self.assertEqual(denied["error"]["type"], "BridgeAuthenticationError")
        self.assertEqual(executor.drain(), 1)
        status = controller.handle({"token": TOKEN, "tool": "bridge.operation_status",
                                    "params": {"operation_id": operation_id}})
        self.assertEqual(status["data"]["status"], "succeeded")
        self.assertEqual(status["data"]["result"]["data"]["code"], "result = 1")
        self.assertEqual(calls, ["python.exec"])
        executor.close()

    def test_status_exposes_inner_failure_and_long_poll(self) -> None:
        def failing(tool, params, context=None):
            return {"ok": False, "tool": tool, "data": {"changed": True, "partial_change_possible": True},
                    "error": {"type": "AssertionError", "message": "bad geometry"}, "warnings": []}

        executor = _MainThreadExecutor(failing)
        controller = _BridgeController(TOKEN, executor, 0.01)
        start = controller.handle({"token": TOKEN, "tool": "bridge.operation_start",
                                   "params": {"tool": "python.exec", "arguments": {"code": "raise AssertionError()"}}})
        operation_id = start["data"]["operation_id"]
        result = []
        waiter = threading.Thread(target=lambda: result.append(controller.handle({
            "token": TOKEN, "tool": "bridge.operation_status",
            "params": {"operation_id": operation_id, "wait_ms": 1000},
        })))
        waiter.start()
        executor.drain()
        waiter.join(timeout=1.0)
        self.assertFalse(waiter.is_alive())
        data = result[0]["data"]
        self.assertEqual(data["status"], "failed")
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], {"type": "AssertionError", "message": "bad geometry"})
        self.assertTrue(data["changed"])
        self.assertTrue(data["partial_change_possible"])
        executor.close()

    def test_long_operation_occupancy_returns_busy_operation_id(self) -> None:
        executor = _MainThreadExecutor(success_dispatch)
        start = executor.start("python.exec", {"code": "pass"})
        operation_id = start["data"]["operation_id"]
        call = executor._operations[operation_id]
        self.assertTrue(call.begin())
        busy = executor.submit("scene.fast_capture", {}, 0.01)
        self.assertEqual(busy["error"]["type"], "BridgeBusyError")
        self.assertEqual(busy["data"]["running_operation_id"], operation_id)
        executor.close()

    def test_background_operation_status_uses_thread_safe_registry(self) -> None:
        executor = _MainThreadExecutor(success_dispatch)
        controller = _BridgeController(TOKEN, executor, 0.01)
        expected = {"operation_id": "background-id", "status": "running", "phase": "rendering"}
        with patch("blenderboo.tools.background_render.lookup_background_status",
                   return_value=expected) as lookup:
            status = controller.handle({"token": TOKEN, "tool": "bridge.operation_status",
                                        "params": {"operation_id": "background-id", "wait_ms": 123}})
        self.assertTrue(status["ok"])
        self.assertEqual(status["data"], expected)
        lookup.assert_called_once_with("background-id", 123)
        executor.close()

    def test_running_call_reports_measured_queue_and_blender_time(self) -> None:
        call = _BridgeCall("scene.inspect", {})
        self.assertTrue(call.begin())
        self.assertTrue(call.complete(success_dispatch("scene.inspect", {})))
        result = call.wait(0)
        timings = result["meta"]["timings"]
        self.assertGreaterEqual(timings["queue_ms"], 0)
        self.assertGreaterEqual(timings["blender_ms"], 0)

    def test_oversized_response_reports_size_and_recovery(self) -> None:
        result = _response_too_large_envelope(
            {
                "ok": True,
                "tool": "object.transform",
                "data": {
                    "changed": True,
                    "state_token": "state-after-transform",
                    "large_detail": "not retained",
                },
            },
            MAX_RESPONSE_BYTES + 17,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "BridgeResponseTooLargeError")
        self.assertEqual(result["data"]["response_bytes"], MAX_RESPONSE_BYTES + 17)
        self.assertEqual(result["data"]["response_limit_bytes"], MAX_RESPONSE_BYTES)
        self.assertTrue(result["data"]["original_result_ok"])
        self.assertTrue(result["data"]["execution_completed"])
        self.assertFalse(result["data"]["automatic_retry_safe"])
        self.assertEqual(
            result["data"]["result_summary"],
            {"changed": True, "state_token": "state-after-transform"},
        )
        self.assertEqual(result["data"]["recovery"]["action"], "inspect_before_retry")

    def test_bridge_and_adapter_transport_limits_stay_aligned(self) -> None:
        self.assertEqual(MAX_REQUEST_BYTES, CLIENT_MAX_REQUEST_BYTES)
        self.assertEqual(MAX_RESPONSE_BYTES, CLIENT_MAX_RESPONSE_BYTES)

    def test_bridge_response_uses_compact_json(self) -> None:
        handler = object.__new__(_BridgeRequestHandler)
        handler.wfile = BytesIO()
        handler._send(success_dispatch("scene.inspect", {"count": 1}))

        payload = handler.wfile.getvalue()
        self.assertTrue(payload.endswith(b"\n"))
        self.assertNotIn(b'": ', payload)
        self.assertEqual(json.loads(payload), success_dispatch("scene.inspect", {"count": 1}))

    def test_actual_oversized_response_blocks_automatic_retry(self) -> None:
        handler = object.__new__(_BridgeRequestHandler)
        handler.wfile = BytesIO()
        handler._send(
            {
                "ok": True,
                "tool": "object.transform",
                "data": {
                    "changed": True,
                    "state_token": "state-after-transform",
                    "detail": "x" * MAX_RESPONSE_BYTES,
                },
                "error": None,
                "warnings": [],
            }
        )

        result = json.loads(handler.wfile.getvalue())
        self.assertEqual(result["error"]["type"], "BridgeResponseTooLargeError")
        self.assertFalse(result["data"]["automatic_retry_safe"])
        self.assertEqual(result["data"]["result_summary"]["changed"], True)
        self.assertEqual(
            result["data"]["result_summary"]["state_token"],
            "state-after-transform",
        )

    def test_oversized_request_is_rejected_before_dispatch(self) -> None:
        def unexpected_dispatch(_request):
            self.fail("oversized request reached the bridge controller")

        handler = object.__new__(_BridgeRequestHandler)
        handler.request = SimpleNamespace(settimeout=lambda _timeout: None)
        handler.rfile = BytesIO(b"x" * (MAX_REQUEST_BYTES + 1))
        handler.wfile = BytesIO()
        handler.server = SimpleNamespace(
            controller=SimpleNamespace(handle=unexpected_dispatch)
        )
        handler.handle()

        result = json.loads(handler.wfile.getvalue())
        self.assertEqual(result["error"]["type"], "BridgeRequestTooLargeError")
        self.assertFalse(result["data"]["execution_started"])
        self.assertTrue(result["data"]["automatic_retry_safe"])
        self.assertEqual(result["data"]["recovery"]["action"], "reduce_request")

    def test_timer_expires_session_lease_even_without_queued_calls(self) -> None:
        with (
            patch.object(bridge_module, "_running_bridge", None),
            patch(
                "blenderboo.tools.session_lease.expire_lease"
            ) as expire_lease,
        ):
            interval = bridge_module._timer_callback()
        expire_lease.assert_called_once_with()
        self.assertIsNone(interval)

    def test_stop_clears_session_lease_even_when_bridge_is_not_running(self) -> None:
        with (
            patch.object(bridge_module, "_running_bridge", None),
            patch(
                "blenderboo.tools.session_lease.clear_lease"
            ) as clear_lease,
        ):
            stopped = stop_bridge()
        clear_lease.assert_called_once_with()
        self.assertFalse(stopped["running"])

    def test_timer_refreshes_registry_after_filepath_change(self) -> None:
        executor = _MainThreadExecutor(success_dispatch)
        running = bridge_module._RunningBridge(
            server=SimpleNamespace(),
            thread=SimpleNamespace(),
            executor=executor,
            port=9877,
            pending_timeout=30.0,
            started="2026-07-14T00:00:00+00:00",
            token_location="runtime",
            registered_filepath="old.blend",
        )
        fake_bpy = SimpleNamespace(data=SimpleNamespace(filepath="new.blend"))
        records = []
        with (
            patch.object(bridge_module, "_running_bridge", running),
            patch.object(bridge_module, "get_bpy", return_value=fake_bpy),
            patch(
                "blenderboo.instance_registry.register_instance",
                side_effect=lambda value: records.append(value),
            ),
            patch("blenderboo.tools.session_lease.expire_lease"),
        ):
            interval = bridge_module._timer_callback()
        self.assertEqual(interval, bridge_module.TIMER_INTERVAL_SECONDS)
        self.assertEqual(records[0]["filepath"], "new.blend")
        self.assertEqual(running.registered_filepath, "new.blend")
        self.assertIsNone(running.registry_error)

    def test_authentication_happens_before_queueing(self) -> None:
        executor = _MainThreadExecutor(success_dispatch)
        controller = _BridgeController(TOKEN, executor, 0.1)
        result = controller.handle({"token": "wrong", "tool": "scene.inspect", "params": {}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "BridgeAuthenticationError")
        self.assertEqual(executor.pending_count, 0)

    def test_pending_timeout_cancels_call_before_dispatch(self) -> None:
        dispatched = []
        executor = _MainThreadExecutor(
            lambda tool, params, context: dispatched.append(tool)
            or success_dispatch(tool, params, context)
        )
        controller = _BridgeController(TOKEN, executor, 0.01)
        result = controller.handle({"token": TOKEN, "tool": "scene.inspect", "params": {}})
        self.assertEqual(result["error"]["type"], "BridgeTimeoutError")
        self.assertEqual(executor.drain(), 0)
        self.assertEqual(dispatched, [])

    def test_started_call_waits_for_authoritative_result(self) -> None:
        started = threading.Event()

        def slow_dispatch(tool, params, context=None):
            started.set()
            time.sleep(0.03)
            return success_dispatch(tool, params)

        executor = _MainThreadExecutor(slow_dispatch)
        controller = _BridgeController(TOKEN, executor, 0.01)
        result_holder = []
        worker = threading.Thread(
            target=lambda: result_holder.append(
                controller.handle({"token": TOKEN, "tool": "scene.inspect", "params": {"limit": 1}})
            )
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(executor.drain(), 1)
        self.assertTrue(started.is_set())
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result_holder[0]["ok"])
        self.assertEqual(result_holder[0]["data"], {"limit": 1})

    def test_deferred_call_waits_until_ui_result_resolves(self) -> None:
        deferred = DeferredToolResult()

        def dispatch(tool, params, context=None):
            self.assertEqual(tool, "scene.screenshot")
            self.assertTrue(context[bridge_module.DEFERRED_CONTEXT_KEY])
            return DeferredEnvelope(tool, deferred)

        executor = _MainThreadExecutor(dispatch)
        controller = _BridgeController(TOKEN, executor, 1.0)
        result_holder = []
        worker = threading.Thread(
            target=lambda: result_holder.append(
                controller.handle({"token": TOKEN, "tool": "scene.screenshot", "params": {}})
            )
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)

        self.assertEqual(executor.drain(), 1)
        self.assertTrue(worker.is_alive())
        deferred.resolve(ToolResult({"path": "viewport.png"}))
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(result_holder[0]["ok"])
        self.assertEqual(result_holder[0]["data"], {"path": "viewport.png"})

    def test_deferred_calls_are_fifo_serialized_across_timer_ticks(self) -> None:
        deferred_results = [DeferredToolResult(), DeferredToolResult()]
        dispatched = []

        def dispatch(tool, params, context=None):
            self.assertEqual(tool, "scene.screenshot")
            self.assertTrue(context[bridge_module.DEFERRED_CONTEXT_KEY])
            dispatched.append(params["name"])
            return DeferredEnvelope(tool, deferred_results[len(dispatched) - 1])

        executor = _MainThreadExecutor(dispatch)
        controller = _BridgeController(TOKEN, executor, 1.0)
        results = {}

        def request(name):
            results[name] = controller.handle(
                {
                    "token": TOKEN,
                    "tool": "scene.screenshot",
                    "params": {"name": name},
                }
            )

        workers = [threading.Thread(target=request, args=(name,)) for name in ("first", "second")]
        workers[0].start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        workers[1].start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count < 2 and time.monotonic() < deadline:
            time.sleep(0.001)

        self.assertEqual(executor.drain(), 1)
        self.assertEqual(dispatched, ["first"])
        self.assertEqual(executor.pending_count, 1)
        self.assertEqual(executor.drain(), 0)

        deferred_results[0].resolve(ToolResult({"path": "first.png"}))
        self.assertEqual(executor.drain(), 1)
        self.assertEqual(dispatched, ["first", "second"])
        deferred_results[1].resolve(ToolResult({"path": "second.png"}))

        for worker in workers:
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results["first"]["data"]["path"], "first.png")
        self.assertEqual(results["second"]["data"]["path"], "second.png")

    def test_close_cancels_active_deferred_call_and_releases_waiter(self) -> None:
        deferred = DeferredToolResult()
        cancelled = threading.Event()
        deferred.set_cancel_callback(cancelled.set)
        executor = _MainThreadExecutor(
            lambda tool, _params, _context=None: DeferredEnvelope(tool, deferred)
        )
        controller = _BridgeController(TOKEN, executor, 1.0)
        result_holder = []
        worker = threading.Thread(
            target=lambda: result_holder.append(
                controller.handle({"token": TOKEN, "tool": "scene.screenshot", "params": {}})
            )
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(executor.drain(), 1)

        executor.close()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result_holder[0]["error"]["type"], "BridgeStoppedError")

    def test_close_releases_pending_waiter_without_dispatch(self) -> None:
        executor = _MainThreadExecutor(success_dispatch)
        controller = _BridgeController(TOKEN, executor, 1.0)
        result_holder = []
        worker = threading.Thread(
            target=lambda: result_holder.append(
                controller.handle({"token": TOKEN, "tool": "scene.inspect", "params": {}})
            )
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        executor.close()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result_holder[0]["error"]["type"], "BridgeStoppedError")

    def test_queue_full_returns_structured_backpressure(self) -> None:
        executor = _MainThreadExecutor(success_dispatch, max_pending=1)
        first_result = []
        first = threading.Thread(
            target=lambda: first_result.append(
                executor.submit("scene.inspect", {}, 1.0, {"client_id": "client-a"})
            )
        )
        first.start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)

        busy = executor.submit("scene.inspect", {}, 0.1, {"client_id": "client-b"})
        self.assertEqual(busy["error"]["type"], "BridgeBusyError")
        self.assertEqual(
            busy["data"],
            {
                "reason": "queue_full",
                "queue_depth": 1,
                "queue_capacity": 1,
                "retry_after_ms": 250,
            },
        )
        executor.close()
        first.join(timeout=1.0)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_result[0]["error"]["type"], "BridgeStoppedError")

    def test_per_client_inflight_limit_preserves_capacity_for_another_client(self) -> None:
        contexts = []

        def record_context(tool, params, context=None):
            contexts.append(context)
            return success_dispatch(tool, params, context)

        executor = _MainThreadExecutor(record_context, max_pending=4)
        controller = _BridgeController(TOKEN, executor, 1.0, max_client_inflight=1)
        results = []
        first = threading.Thread(
            target=lambda: results.append(
                controller.handle({
                    "token": TOKEN,
                    "client_id": "client-a",
                    "tool": "scene.inspect",
                    "params": {"request": "first"},
                })
            )
        )
        first.start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count < 1 and time.monotonic() < deadline:
            time.sleep(0.001)

        busy = controller.handle({
            "token": TOKEN,
            "client_id": "client-a",
            "tool": "scene.inspect",
            "params": {"request": "second"},
        })
        self.assertEqual(busy["error"]["type"], "BridgeBusyError")
        self.assertEqual(busy["data"]["reason"], "client_inflight_limit")
        self.assertEqual(busy["data"]["client_inflight"], 1)
        self.assertEqual(busy["data"]["client_limit"], 1)
        self.assertEqual(busy["data"]["retry_after_ms"], 250)

        second = threading.Thread(
            target=lambda: results.append(
                controller.handle({
                    "token": TOKEN,
                    "client_id": "client-b",
                    "tool": "scene.inspect",
                    "params": {"request": "other-client"},
                })
            )
        )
        second.start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(executor.pending_count, 2)
        self.assertEqual(
            controller.pressure_status(),
            {
                "client_inflight": 2,
                "active_clients": 2,
                "per_client_inflight_limit": 1,
            },
        )
        self.assertEqual(executor.drain(), 2)
        first.join(timeout=1.0)
        second.join(timeout=1.0)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(
            contexts,
            [{"client_id": "client-a"}, {"client_id": "client-b"}],
        )
        self.assertEqual(controller.pressure_status()["client_inflight"], 0)
        self.assertEqual(controller.pressure_status()["active_clients"], 0)

    def test_protocol_shape_and_json_envelope(self) -> None:
        executor = _MainThreadExecutor(success_dispatch)
        controller = _BridgeController(TOKEN, executor, 0.1)
        cases = [
            ([], "BridgeProtocolError"),
            ({"token": TOKEN, "tool": "", "params": {}}, "BridgeProtocolError"),
            ({"token": TOKEN, "tool": "scene.inspect", "params": []}, "BridgeProtocolError"),
            ({"token": TOKEN, "tool": "scene.inspect", "params": {}, "extra": 1}, "BridgeProtocolError"),
            ({"token": TOKEN, "tool": "scene.inspect", "params": {}, "client_id": None}, "BridgeProtocolError"),
        ]
        for request, expected_type in cases:
            with self.subTest(request=request):
                result = controller.handle(request)
                self.assertEqual(result["error"]["type"], expected_type)
                json.dumps(result, allow_nan=False)

    def test_client_id_is_threaded_as_dispatch_context(self) -> None:
        contexts = []

        def record_context(tool, params, context=None):
            contexts.append(context)
            return success_dispatch(tool, params, context)

        executor = _MainThreadExecutor(record_context)
        controller = _BridgeController(TOKEN, executor, 1.0)
        result_holder = []
        worker = threading.Thread(
            target=lambda: result_holder.append(
                controller.handle({
                    "token": TOKEN,
                    "client_id": "mcp-process-1",
                    "tool": "scene.inspect",
                    "params": {},
                })
            )
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while executor.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(executor.drain(), 1)
        worker.join(timeout=1.0)
        self.assertTrue(result_holder[0]["ok"])
        self.assertEqual(contexts, [{"client_id": "mcp-process-1"}])

    def test_start_parameters_are_bounded(self) -> None:
        self.assertEqual(_validate_start_params(TOKEN, 9877, 30), (TOKEN, 9877, 30.0))
        invalid = [
            ("short", 9877, 30),
            (TOKEN, True, 30),
            (TOKEN, 80, 30),
            (TOKEN, 9877, 0),
            (TOKEN, 9877, 301),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(InvalidParamsError):
                _validate_start_params(*args)
        self.assertEqual(bridge_module._validate_token_location("runtime"), "runtime")
        self.assertEqual(
            bridge_module._validate_token_location("env:CBT_TOKEN_A"),
            "env:CBT_TOKEN_A",
        )
        self.assertEqual(bridge_module._validate_token_location("config"), "config")
        for token_location in ("", "env:", "env:BAD-NAME", TOKEN):
            with self.subTest(token_location=token_location), self.assertRaises(
                InvalidParamsError
            ):
                bridge_module._validate_token_location(token_location)

    def test_start_stop_lifecycle_does_not_expose_token(self) -> None:
        class FakeTimers:
            def __init__(self):
                self.registered = set()

            def register(self, callback, **kwargs):
                self.registered.add(callback)

            def is_registered(self, callback):
                return callback in self.registered

            def unregister(self, callback):
                self.registered.remove(callback)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        timers = FakeTimers()
        fake_bpy = SimpleNamespace(
            app=SimpleNamespace(timers=timers),
            data=SimpleNamespace(filepath="scene.blend"),
        )
        registered_records = []
        unregistered_ids = []
        with (
            patch("blenderboo.bridge.get_bpy", return_value=fake_bpy),
            patch(
                "blenderboo.instance_registry.register_instance",
                side_effect=lambda value: registered_records.append(value),
            ),
            patch(
                "blenderboo.instance_registry.unregister_instance",
                side_effect=lambda value: unregistered_ids.append(value),
            ),
        ):
            started = start_bridge(TOKEN, port=port)
            try:
                self.assertTrue(started["running"])
                self.assertEqual(started["port"], port)
                self.assertEqual(started["queue_depth"], 0)
                self.assertEqual(started["queue_capacity"], 64)
                self.assertEqual(started["active_connections"], 0)
                self.assertEqual(started["connection_capacity"], 16)
                self.assertEqual(started["client_inflight"], 0)
                self.assertEqual(started["per_client_inflight_limit"], 4)
                self.assertNotIn(TOKEN, json.dumps(started))
                with self.assertRaisesRegex(InvalidStateError, "address in use"):
                    start_bridge(TOKEN, port=port)
            finally:
                stopped = stop_bridge()
            restarted = start_bridge(TOKEN, port=port)
            try:
                self.assertTrue(restarted["running"])
                self.assertEqual(restarted["port"], port)
            finally:
                stopped_again = stop_bridge()
        self.assertFalse(stopped["running"])
        self.assertFalse(stopped_again["running"])
        self.assertFalse(bridge_status()["running"])
        self.assertEqual(bridge_status()["queue_depth"], 0)
        self.assertEqual(bridge_status()["queue_capacity"], 64)
        self.assertEqual(timers.registered, set())
        self.assertEqual(len(registered_records), 2)
        self.assertEqual(len(unregistered_ids), 2)
        self.assertEqual(
            {record["id"] for record in registered_records},
            set(unregistered_ids),
        )
        self.assertTrue(all(record["token_location"] == "runtime" for record in registered_records))
        self.assertNotIn(TOKEN, json.dumps(registered_records))

    def test_registry_failure_rolls_back_started_listener(self) -> None:
        class FakeTimers:
            def __init__(self):
                self.registered = set()

            def register(self, callback, **kwargs):
                self.registered.add(callback)

            def is_registered(self, callback):
                return callback in self.registered

            def unregister(self, callback):
                self.registered.remove(callback)

        with socket.socket() as probe:
            probe.bind((BRIDGE_HOST, 0))
            port = probe.getsockname()[1]
        timers = FakeTimers()
        fake_bpy = SimpleNamespace(
            app=SimpleNamespace(timers=timers),
            data=SimpleNamespace(filepath="scene.blend"),
        )
        with (
            patch("blenderboo.bridge.get_bpy", return_value=fake_bpy),
            patch(
                "blenderboo.instance_registry.register_instance",
                side_effect=RuntimeError("registry unavailable"),
            ),
            patch("blenderboo.instance_registry.unregister_instance"),
            self.assertRaisesRegex(RuntimeError, "registry unavailable"),
        ):
            start_bridge(TOKEN, port=port)
        self.assertFalse(bridge_status()["running"])
        self.assertEqual(timers.registered, set())
        with socket.socket() as reclaimed:
            reclaimed.bind((BRIDGE_HOST, port))

    def test_server_bind_requests_windows_exclusive_address_use(self) -> None:
        calls = []

        class FakeSocket:
            def setsockopt(self, *args):
                calls.append(args)

        server = object.__new__(_LoopbackServer)
        server.socket = FakeSocket()
        with (
            patch.object(socket, "SO_EXCLUSIVEADDRUSE", 0x1234, create=True),
            patch.object(socketserver.ThreadingTCPServer, "server_bind", return_value=None),
        ):
            server.server_bind()
        self.assertEqual(calls, [(socket.SOL_SOCKET, 0x1234, 1)])
        self.assertFalse(_LoopbackServer.allow_reuse_address)

    def test_connection_threads_are_bounded_before_work_starts(self) -> None:
        class FakeRequest:
            def __init__(self):
                self.closed = False
                self.payload = b""

            def settimeout(self, timeout):
                self.timeout = timeout

            def sendall(self, payload):
                self.payload += payload

            def recv(self, size):
                return b"{}\n"

            def shutdown(self, how):
                self.shutdown_how = how

            def close(self):
                self.closed = True

        server = object.__new__(_LoopbackServer)
        server._connection_capacity = 1
        server._connection_slots = threading.BoundedSemaphore(1)
        server._connection_lock = threading.Lock()
        server._active_connections = 0
        server.controller = SimpleNamespace(
            executor=SimpleNamespace(pending_count=3, capacity=64)
        )
        first = FakeRequest()
        second = FakeRequest()
        with patch.object(socketserver.ThreadingTCPServer, "process_request", return_value=None):
            server.process_request(first, ("127.0.0.1", 1))
            server.process_request(second, ("127.0.0.1", 2))
        self.assertFalse(first.closed)
        self.assertTrue(second.closed)
        response = json.loads(second.payload)
        self.assertEqual(response["error"]["type"], "BridgeBusyError")
        self.assertEqual(response["data"]["reason"], "connection_limit")
        self.assertEqual(response["data"]["queue_depth"], 3)
        self.assertEqual(response["data"]["active_connections"], 1)
        self.assertEqual(response["data"]["connection_capacity"], 1)
        self.assertEqual(response["data"]["retry_after_ms"], 250)
        server._release_connection_slot()
        self.assertEqual(server.connection_status()["active_connections"], 0)

    def test_live_connection_burst_receives_explicit_busy_response(self) -> None:
        executor = _MainThreadExecutor(success_dispatch)
        server = _LoopbackServer(
            (BRIDGE_HOST, 0),
            _BridgeRequestHandler,
            max_connection_threads=1,
        )
        server.controller = _BridgeController(TOKEN, executor, 1.0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        first = None
        second = None
        try:
            first = socket.create_connection(server.server_address, timeout=1.0)
            deadline = time.monotonic() + 1.0
            while (
                server.connection_status()["active_connections"] < 1
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)
            self.assertEqual(server.connection_status()["active_connections"], 1)

            second = socket.create_connection(server.server_address, timeout=1.0)
            raw = second.makefile("rb").readline()
            response = json.loads(raw)
            self.assertEqual(response["error"]["type"], "BridgeBusyError")
            self.assertEqual(response["data"]["reason"], "connection_limit")
            self.assertEqual(response["data"]["connection_capacity"], 1)
            self.assertEqual(response["data"]["retry_after_ms"], 250)
        finally:
            if second is not None:
                second.close()
            if first is not None:
                first.close()
            executor.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_stop_continues_cleanup_after_an_earlier_failure(self) -> None:
        calls = []

        class FailingExecutor:
            pending_count = 0

            def close(self):
                calls.append("executor")
                raise RuntimeError("close failed")

        class FakeServer:
            def shutdown(self):
                calls.append("shutdown")

            def server_close(self):
                calls.append("server_close")

        class FakeThread:
            def is_alive(self):
                return False

            def join(self, timeout):
                calls.append("join")

        class FakeTimers:
            def is_registered(self, callback):
                return True

            def unregister(self, callback):
                calls.append("timer")

        running = bridge_module._RunningBridge(FakeServer(), FakeThread(), FailingExecutor(), 9877, 30.0)
        fake_bpy = SimpleNamespace(app=SimpleNamespace(timers=FakeTimers()))
        with (
            patch.object(bridge_module, "_running_bridge", running),
            patch.object(bridge_module, "get_bpy", return_value=fake_bpy),
            patch(
                "blenderboo.instance_registry.unregister_instance",
                side_effect=lambda value: calls.append("registry"),
            ),
            self.assertRaisesRegex(InvalidStateError, "executor close: close failed"),
        ):
            stop_bridge()
        self.assertEqual(calls, ["registry", "executor", "server_close", "join", "timer"])


if __name__ == "__main__":
    unittest.main()
