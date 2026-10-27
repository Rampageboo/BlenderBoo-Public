"""Tests for registry-backed MCP instance selection and token resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blenderboo_mcp.instances import (
    InstanceRouter,
    InstanceRoutingError,
    _pid_is_alive,
    load_instances,
    registry_path,
)
from blenderboo_mcp.proxy import ToolkitProxy


TOKEN_A = "instance-a-token-that-is-at-least-32-characters"
TOKEN_B = "instance-b-token-that-is-at-least-32-characters"


class RegistryPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.current = self.home / ".blenderboo" / "instances.json"
        self.legacy = self.home / ".codex" / "codex_blender_instances.json"
        for mock in (patch.dict(os.environ, {}, clear=True),
                     patch("blenderboo_mcp.instances.Path.home", return_value=self.home)):
            mock.start()
            self.addCleanup(mock.stop)

    def write_registry(self, path: Path, instance_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "instances": [{
            "id": instance_id, "pid": os.getpid(), "port": 9881,
            "filepath": "example.blend", "started": "2026-10-20T00:00:00+00:00",
            "token_location": "clipboard",
        }]}), encoding="utf-8")

    def test_default_path_is_new_even_when_legacy_exists(self) -> None:
        self.write_registry(self.legacy, "legacy")
        self.assertEqual(registry_path(), self.current)

    def test_reads_legacy_only_when_current_is_absent(self) -> None:
        self.write_registry(self.legacy, "legacy")
        self.assertEqual([item.id for item in load_instances()], ["legacy"])
        self.assertFalse(self.current.exists())
        self.write_registry(self.current, "current")
        self.assertEqual([item.id for item in load_instances()], ["current"])

    def test_empty_current_does_not_fall_back(self) -> None:
        self.write_registry(self.legacy, "legacy")
        self.current.parent.mkdir(parents=True)
        self.current.write_text('{"version": 1, "instances": []}', encoding="utf-8")
        self.assertEqual(load_instances(), [])

    def test_invalid_current_does_not_fall_back(self) -> None:
        self.write_registry(self.legacy, "legacy")
        self.current.parent.mkdir(parents=True)
        self.current.write_text("invalid JSON", encoding="utf-8")
        with self.assertRaisesRegex(InstanceRoutingError, "Could not read"):
            load_instances()

    def test_override_is_authoritative_even_when_absent(self) -> None:
        self.write_registry(self.legacy, "legacy")
        self.write_registry(self.current, "current")
        override = self.home / "custom" / "instances.json"
        with patch.dict(os.environ, {"CBT_INSTANCE_REGISTRY": str(override)}):
            self.assertEqual(registry_path(), override)
            self.assertEqual(load_instances(), [])
            self.write_registry(override, "override")
            self.assertEqual([item.id for item in load_instances()], ["override"])

    def test_missing_registries_return_empty(self) -> None:
        self.assertEqual(load_instances(), [])


class InstanceRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = Path(self.temporary.name) / "instances.json"
        self.instances = [
            {
                "id": "instance-a",
                "pid": os.getpid(),
                "port": 9881,
                "filepath": "a.blend",
                "started": "2026-07-14T00:00:00+00:00",
                "token_location": "clipboard",
            },
            {
                "id": "instance-b",
                "pid": os.getpid(),
                "port": 9882,
                "filepath": "b.blend",
                "started": "2026-07-14T00:01:00+00:00",
                "token_location": "env:CBT_TOKEN_B",
            },
        ]
        self.registry.write_text(
            json.dumps({"version": 1, "instances": self.instances}),
            encoding="utf-8",
        )
        self.environment = patch.dict(
            os.environ,
            {
                "CBT_INSTANCE_REGISTRY": str(self.registry),
                "CBT_BRIDGE_TOKENS": json.dumps({"instance-a": TOKEN_A}),
                "CBT_TOKEN_B": TOKEN_B,
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_enumeration_contains_no_token_values(self) -> None:
        router = InstanceRouter(client_id="test-client")
        result = router.list_instances()
        self.assertEqual(result["count"], 2)
        self.assertIsNone(result["default_instance"])
        self.assertEqual([item["id"] for item in result["instances"]], ["instance-a", "instance-b"])
        serialized = json.dumps(result)
        self.assertNotIn(TOKEN_A, serialized)
        self.assertNotIn(TOKEN_B, serialized)
        envelope = ToolkitProxy(router).session_instances()
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["tool"], "session.instances")
        self.assertEqual(envelope["data"], result)

    def test_explicit_id_and_port_route_with_their_own_tokens(self) -> None:
        response = {
            "ok": True,
            "tool": "session.whoami",
            "data": {},
            "error": None,
            "warnings": [],
        }
        with patch("blenderboo_mcp.instances.BridgeClient") as client_type:
            client_type.return_value.call.return_value = response
            router = InstanceRouter(client_id="test-client", connect_timeout=1.5)
            self.assertIs(router.call("session.whoami", {}, "instance-a"), response)
            client_type.assert_called_with(
                TOKEN_A,
                9881,
                connect_timeout=1.5,
                client_id="test-client",
            )
            router.call("session.whoami", {}, 9882)
            client_type.assert_called_with(
                TOKEN_B,
                9882,
                connect_timeout=1.5,
                client_id="test-client",
            )

    def test_multiple_instances_require_explicit_selection(self) -> None:
        with self.assertRaisesRegex(InstanceRoutingError, "Multiple Blender instances"):
            InstanceRouter().call("session.whoami", {})

    def test_missing_token_fails_without_exposing_configured_token(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CBT_INSTANCE_REGISTRY": str(self.registry),
                "CBT_BRIDGE_TOKENS": json.dumps({"instance-a": TOKEN_A}),
            },
            clear=True,
        ):
            with self.assertRaises(InstanceRoutingError) as raised:
                InstanceRouter().call("session.whoami", {}, "instance-b")
        self.assertNotIn(TOKEN_A, str(raised.exception))

    def test_sole_instance_defaults_and_legacy_mode_remains_available(self) -> None:
        self.registry.write_text(
            json.dumps({"version": 1, "instances": [self.instances[0]]}),
            encoding="utf-8",
        )
        with patch("blenderboo_mcp.instances.BridgeClient") as client_type:
            client_type.return_value.call.return_value = {"ok": True}
            InstanceRouter().call("scene.inspect", {})
            client_type.assert_called_once_with(
                TOKEN_A,
                9881,
                connect_timeout=5.0,
                client_id=None,
            )

        self.registry.unlink()
        with patch.dict(
            os.environ,
            {
                "CBT_INSTANCE_REGISTRY": str(self.registry),
                "CBT_BRIDGE_TOKEN": TOKEN_B,
                "CBT_BRIDGE_PORT": "9890",
            },
            clear=True,
        ), patch("blenderboo_mcp.instances.BridgeClient") as client_type:
            client_type.return_value.call.return_value = {"ok": True}
            result = InstanceRouter().list_instances()
            self.assertEqual(result["default_instance"], "port-9890")
            InstanceRouter().call("scene.inspect", {})
            client_type.assert_called_once_with(
                TOKEN_B,
                9890,
                connect_timeout=5.0,
                client_id=None,
            )

    def test_corrupt_or_duplicate_registry_is_rejected(self) -> None:
        self.registry.write_text("bad json", encoding="utf-8")
        with self.assertRaisesRegex(InstanceRoutingError, "Could not read"):
            load_instances()
        duplicate = [self.instances[0], {**self.instances[1], "port": 9881}]
        self.registry.write_text(
            json.dumps({"version": 1, "instances": duplicate}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(InstanceRoutingError, "duplicate ports"):
            load_instances()

    def test_dead_process_entries_are_not_enumerated(self) -> None:
        dead = {**self.instances[0], "pid": 2_000_000_000}
        self.registry.write_text(
            json.dumps({"version": 1, "instances": [dead, self.instances[1]]}),
            encoding="utf-8",
        )
        self.assertEqual([item.id for item in load_instances()], ["instance-b"])

    def test_pid_liveness_handles_a_different_process(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(_pid_is_alive(process.pid))
            process.terminate()
            process.wait(timeout=5)
            self.assertFalse(_pid_is_alive(process.pid))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
