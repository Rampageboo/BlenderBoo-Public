"""Socket-level integration tests for the standalone bridge client."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))

from blenderboo.bridge import (
    BRIDGE_HOST,
    _BridgeController,
    _BridgeRequestHandler,
    _LoopbackServer,
    _MainThreadExecutor,
)
from blenderboo_mcp.client import BridgeClient, BridgeClientError
from blenderboo_mcp.instances import InstanceRouter, InstanceRoutingError
from blenderboo_mcp.proxy import ToolkitProxy


TOKEN = "socket-test-token-that-is-at-least-32-chars"


def success_dispatch(tool, params, context=None):
    return {"ok": True, "tool": tool, "data": dict(params or {}), "error": None, "warnings": []}


class BridgeClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contexts = []

        def recording_dispatch(tool, params, context=None):
            self.contexts.append(context)
            return success_dispatch(tool, params, context)

        self.executor = _MainThreadExecutor(recording_dispatch)
        self.server = _LoopbackServer((BRIDGE_HOST, 0), _BridgeRequestHandler)
        self.server.controller = _BridgeController(TOKEN, self.executor, 1.0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = BridgeClient(TOKEN, self.server.server_address[1])

    def tearDown(self) -> None:
        self.executor.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)

    def test_socket_round_trip(self) -> None:
        result_holder = []
        worker = threading.Thread(
            target=lambda: result_holder.append(self.client.call("scene.inspect", {"limit": 3}))
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while self.executor.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(self.executor.drain(), 1)
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result_holder[0]["data"], {"limit": 3})
        self.assertEqual(self.contexts, [{"client_id": self.client.client_id}])

    def test_wrong_token_is_rejected(self) -> None:
        result = BridgeClient("x" * 32, self.server.server_address[1]).call("scene.inspect", {})
        self.assertEqual(result["error"]["type"], "BridgeAuthenticationError")
        self.assertEqual(self.executor.pending_count, 0)

    def test_client_receives_structured_busy_when_connection_limit_is_full(self) -> None:
        executor = _MainThreadExecutor(success_dispatch)
        server = _LoopbackServer((BRIDGE_HOST, 0), _BridgeRequestHandler)
        server.controller = _BridgeController(TOKEN, executor, 1.0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        occupying_sockets = []
        try:
            for _ in range(16):
                occupying_sockets.append(
                    socket.create_connection(server.server_address, timeout=1.0)
                )
            deadline = time.monotonic() + 1.0
            while (
                server.connection_status()["active_connections"] < 16
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)
            self.assertEqual(server.connection_status()["active_connections"], 16)

            for index in range(5):
                result = BridgeClient(
                    TOKEN,
                    server.server_address[1],
                    connect_timeout=1.0,
                    client_id=f"waiting-client-{index}",
                ).call("scene.inspect", {})
                self.assertEqual(result["error"]["type"], "BridgeBusyError")
                self.assertEqual(result["data"]["reason"], "connection_limit")
                self.assertEqual(result["data"]["connection_capacity"], 16)
                self.assertEqual(result["data"]["retry_after_ms"], 250)
        finally:
            for occupying_socket in occupying_sockets:
                occupying_socket.close()
            executor.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_two_registered_instances_route_independently(self) -> None:
        token_a = "multi-instance-a-token-at-least-32-characters"
        token_b = "multi-instance-b-token-at-least-32-characters"
        contexts = {"a": [], "b": []}

        def make_dispatch(label):
            def dispatch(tool, params, context=None):
                contexts[label].append(context)
                return {
                    "ok": True,
                    "tool": tool,
                    "data": {"instance": label},
                    "error": None,
                    "warnings": [],
                }

            return dispatch

        executor_a = _MainThreadExecutor(make_dispatch("a"))
        executor_b = _MainThreadExecutor(make_dispatch("b"))
        server_a = _LoopbackServer((BRIDGE_HOST, 0), _BridgeRequestHandler)
        server_b = _LoopbackServer((BRIDGE_HOST, 0), _BridgeRequestHandler)
        server_a.controller = _BridgeController(token_a, executor_a, 1.0)
        server_b.controller = _BridgeController(token_b, executor_b, 1.0)
        thread_a = threading.Thread(target=server_a.serve_forever, daemon=True)
        thread_b = threading.Thread(target=server_b.serve_forever, daemon=True)
        thread_a.start()
        thread_b.start()

        try:
            with tempfile.TemporaryDirectory() as temporary:
                registry = Path(temporary) / "instances.json"
                records = [
                    {
                        "id": "instance-a",
                        "pid": os.getpid(),
                        "port": server_a.server_address[1],
                        "filepath": "a.blend",
                        "started": "2026-07-14T00:00:00+00:00",
                        "token_location": "clipboard",
                    },
                    {
                        "id": "instance-b",
                        "pid": os.getpid(),
                        "port": server_b.server_address[1],
                        "filepath": "b.blend",
                        "started": "2026-07-14T00:01:00+00:00",
                        "token_location": "clipboard",
                    },
                ]
                registry.write_text(
                    json.dumps({"version": 1, "instances": records}),
                    encoding="utf-8",
                )
                environment = {
                    "CBT_INSTANCE_REGISTRY": str(registry),
                    "CBT_BRIDGE_TOKENS": json.dumps(
                        {"instance-a": token_a, str(server_b.server_address[1]): token_b}
                    ),
                }
                with patch.dict(os.environ, environment, clear=True):
                    router = InstanceRouter(client_id="shared-router-client")
                    self.assertEqual(router.list_instances()["count"], 2)
                    with self.assertRaisesRegex(
                        InstanceRoutingError, "Multiple Blender instances"
                    ):
                        router.call("session.whoami", {})

                    def routed_call(selector, selected, other):
                        results = []
                        worker = threading.Thread(
                            target=lambda: results.append(
                                router.call("session.whoami", {}, selector)
                            )
                        )
                        worker.start()
                        deadline = time.monotonic() + 1.0
                        while selected.pending_count == 0 and time.monotonic() < deadline:
                            time.sleep(0.001)
                        self.assertEqual(selected.pending_count, 1)
                        self.assertEqual(other.pending_count, 0)
                        self.assertEqual(selected.drain(), 1)
                        worker.join(timeout=1.0)
                        self.assertFalse(worker.is_alive())
                        return results[0]

                    result_a = routed_call("instance-a", executor_a, executor_b)
                    result_b = routed_call(
                        server_b.server_address[1], executor_b, executor_a
                    )
                self.assertNotIn(token_a, registry.read_text(encoding="utf-8"))
                self.assertNotIn(token_b, registry.read_text(encoding="utf-8"))
            self.assertEqual(result_a["data"]["instance"], "a")
            self.assertEqual(result_b["data"]["instance"], "b")
            self.assertEqual(contexts["a"], [{"client_id": "shared-router-client"}])
            self.assertEqual(contexts["b"], [{"client_id": "shared-router-client"}])
        finally:
            executor_a.close()
            executor_b.close()
            for server in (server_a, server_b):
                server.shutdown()
                server.server_close()
            thread_a.join(timeout=1.0)
            thread_b.join(timeout=1.0)

    def test_environment_configuration_does_not_expose_token(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CBT_BRIDGE_TOKEN": TOKEN,
                "CBT_BRIDGE_PORT": str(self.server.server_address[1]),
                "CBT_CLIENT_ID": "configured-client",
            },
            clear=True,
        ):
            client = BridgeClient.from_env()
        self.assertEqual(client.port, self.server.server_address[1])
        self.assertEqual(client.client_id, "configured-client")
        self.assertNotIn(TOKEN, repr(client.__dict__.keys()))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(BridgeClientError):
            BridgeClient.from_env()

    def test_typed_proxy_maps_arguments(self) -> None:
        calls = []

        class RecordingClient:
            def call(self, tool, params):
                calls.append((tool, params))
                return success_dispatch(tool, params)

        proxy = ToolkitProxy(RecordingClient())
        proxy.scene_inspect(include_names=True, limit=10)
        proxy.checkpoint_restore("scene_ckpt_001_safe.blend", True, "state-a")
        proxy.session_whoami()
        proxy.session_acquire_lease(ttl=60)
        proxy.session_renew_lease("lease-a", ttl=120)
        proxy.session_release_lease("lease-a")
        proxy.render_preview(width=320, height=180, overwrite=True)
        proxy.material_create("Chassis", use_fake_user=True, expected_state="state-b")
        proxy.material_patch(
            "Leaf",
            [{"op": "set_material_flag", "flag": "use_backface_culling", "value": True}],
            expected_state="state-b",
        )
        proxy.scene_save_version(tag="repair", expected_state="state-c")
        self.assertEqual(calls[0], ("scene.inspect", {"include_names": True, "limit": 10}))
        self.assertEqual(calls[1][0], "checkpoint.restore")
        self.assertEqual(calls[1][1]["expected_state"], "state-a")
        self.assertEqual(calls[2], ("session.whoami", {}))
        self.assertEqual(
            calls[3],
            ("session.acquire_lease", {"scope": "document", "ttl": 60}),
        )
        self.assertEqual(
            calls[4],
            ("session.renew_lease", {"lease_id": "lease-a", "ttl": 120}),
        )
        self.assertEqual(calls[5], ("session.release_lease", {"lease_id": "lease-a"}))
        self.assertEqual(calls[6][1]["overwrite"], True)
        self.assertEqual(
            calls[7],
            (
                "material.create",
                {"name": "Chassis", "use_nodes": True, "use_fake_user": True, "expected_state": "state-b"},
            ),
        )
        self.assertEqual(calls[8][0], "material.patch")
        self.assertTrue(calls[8][1]["dry_run"])
        self.assertEqual(calls[8][1]["expected_state"], "state-b")
        self.assertEqual(
            calls[9],
            ("scene.save_version", {"tag": "repair", "expected_state": "state-c"}),
        )


if __name__ == "__main__":
    unittest.main()
