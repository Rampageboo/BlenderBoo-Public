"""Pure-Python tests for the shared Blender instance registry."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidStateError
from blenderboo.instance_registry import (
    read_registry,
    registry_path,
    register_instance,
    unregister_instance,
)


def record(instance_id: str, port: int, filepath: str = "") -> dict:
    return {
        "id": instance_id,
        "pid": os.getpid(),
        "port": port,
        "filepath": filepath,
        "started": "2026-07-14T00:00:00+00:00",
        "token_location": f"env:CBT_TOKEN_{instance_id.upper()}",
    }


class InstanceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "instances.json"
        self.environment = patch.dict(
            os.environ,
            {"CBT_INSTANCE_REGISTRY": str(self.path)},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_register_replace_and_remove_preserve_other_instances(self) -> None:
        register_instance(record("b", 9882, "b.blend"))
        register_instance(record("a", 9881, "a.blend"))
        register_instance(record("a", 9881, "a-renamed.blend"))

        payload = read_registry()
        self.assertEqual([item["id"] for item in payload["instances"]], ["a", "b"])
        self.assertEqual(payload["instances"][0]["filepath"], "a-renamed.blend")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("token-that", raw)

        register_instance(record("replacement", 9882, "replacement.blend"))
        self.assertEqual(
            [item["id"] for item in read_registry()["instances"]],
            ["a", "replacement"],
        )

        unregister_instance("a")
        unregister_instance("a")
        self.assertEqual(
            [item["id"] for item in read_registry()["instances"]],
            ["replacement"],
        )

    def test_concurrent_process_style_updates_do_not_lose_entries(self) -> None:
        errors = []

        def worker(index: int) -> None:
            try:
                register_instance(record(f"instance-{index}", 9900 + index))
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2.0)
        self.assertEqual(errors, [])
        self.assertEqual(len(read_registry()["instances"]), 8)

    def test_corrupt_registry_is_preserved_and_lock_is_released(self) -> None:
        self.path.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(InvalidStateError, "Could not read instance registry"):
            register_instance(record("a", 9881))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "not json")
        self.assertFalse(self.path.with_name(self.path.name + ".lock").exists())

    def test_stale_lock_is_recovered(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        lock.write_text("stale", encoding="utf-8")
        stale = time.time() - 60
        os.utime(lock, (stale, stale))
        register_instance(record("a", 9881))
        self.assertEqual(read_registry()["instances"][0]["id"], "a")
        self.assertFalse(lock.exists())

    def test_windows_replace_sharing_violation_is_retried(self) -> None:
        register_instance(record("a", 9881))
        original_replace = os.replace
        attempts = 0

        def flaky_replace(source: str | os.PathLike, target: str | os.PathLike) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError(5, "Access is denied")
            original_replace(source, target)

        with (
            patch(
                "blenderboo.instance_registry.os.replace",
                side_effect=flaky_replace,
            ),
            patch("blenderboo.instance_registry.time.sleep"),
        ):
            register_instance(record("a", 9881, "updated.blend"))

        self.assertEqual(attempts, 3)
        self.assertEqual(read_registry()["instances"][0]["filepath"], "updated.blend")

    def test_invalid_record_never_creates_registry(self) -> None:
        invalid = record("a", 9881)
        invalid["token"] = "must-not-be-written"
        with self.assertRaisesRegex(InvalidStateError, "fields are invalid"):
            register_instance(invalid)
        self.assertFalse(self.path.exists())


class InstanceRegistryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.new_path = self.home / ".blenderboo" / "instances.json"
        self.legacy_path = self.home / ".codex" / "codex_blender_instances.json"
        self.legacy_path.parent.mkdir()
        self.legacy_payload = {"version": 1, "instances": [record("legacy", 9881)]}
        self.legacy_path.write_text(json.dumps(self.legacy_payload), encoding="utf-8")
        home_patch = patch("blenderboo.instance_registry.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        env_patch = patch.dict(os.environ, {"CBT_INSTANCE_REGISTRY": ""})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_legacy_read_is_read_only_and_first_write_migrates_records(self) -> None:
        original = self.legacy_path.read_bytes()
        self.assertEqual(registry_path(), self.new_path)
        self.assertEqual(read_registry(), self.legacy_payload)
        self.assertFalse(self.new_path.exists())

        register_instance(record("new", 9882))
        self.assertTrue(self.new_path.is_file())
        self.assertEqual([item["id"] for item in read_registry()["instances"]], ["legacy", "new"])
        self.assertEqual(self.legacy_path.read_bytes(), original)

    def test_new_registry_is_authoritative_even_when_empty(self) -> None:
        self.new_path.parent.mkdir()
        self.new_path.write_text('{"version": 1, "instances": []}', encoding="utf-8")
        self.assertEqual(read_registry()["instances"], [])

    def test_invalid_new_registry_does_not_fall_back(self) -> None:
        self.new_path.parent.mkdir()
        self.new_path.write_text("invalid registry", encoding="utf-8")
        with self.assertRaises(InvalidStateError):
            read_registry()

    def test_override_never_reads_or_writes_default_locations(self) -> None:
        override = self.home / "override.json"
        original = self.legacy_path.read_bytes()
        with patch.dict(os.environ, {"CBT_INSTANCE_REGISTRY": str(override)}):
            self.assertEqual(registry_path(), override)
            self.assertEqual(read_registry()["instances"], [])
            register_instance(record("override", 9882))
            self.assertEqual([item["id"] for item in read_registry()["instances"]], ["override"])
        self.assertFalse(self.new_path.exists())
        self.assertEqual(self.legacy_path.read_bytes(), original)

    def test_unregister_migrates_without_resurrecting_removed_legacy_record(self) -> None:
        unregister_instance("legacy")
        self.assertTrue(self.new_path.is_file())
        self.assertEqual(read_registry()["instances"], [])
        self.assertEqual(json.loads(self.legacy_path.read_text(encoding="utf-8")), self.legacy_payload)


if __name__ == "__main__":
    unittest.main()
