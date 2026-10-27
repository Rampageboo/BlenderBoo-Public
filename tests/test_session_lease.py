"""Pure-Python tests for expiring, client-attributed document leases."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.registry import ToolRegistry
from blenderboo.tools import session_lease


def fake_bpy(filepath: str = "C:/projects/scene.blend"):
    return SimpleNamespace(data=SimpleNamespace(filepath=filepath))


class SessionLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        session_lease.clear_lease()
        self.addCleanup(session_lease.clear_lease)
        self.now = 100.0
        self.bpy = fake_bpy()
        self.bpy_patch = patch.object(session_lease, "get_bpy", return_value=self.bpy)
        self.clock_patch = patch.object(
            session_lease.time, "monotonic", side_effect=lambda: self.now
        )
        self.bpy_patch.start()
        self.clock_patch.start()
        self.addCleanup(self.bpy_patch.stop)
        self.addCleanup(self.clock_patch.stop)

        self.registry = ToolRegistry()
        self.registry.register("session.acquire_lease", session_lease.acquire_lease)
        self.registry.register("session.renew_lease", session_lease.renew_lease)
        self.registry.register("session.release_lease", session_lease.release_lease)
        self.registry.register(
            "session.lease_status", lambda params: session_lease.lease_status()
        )
        self.registry.register(
            "session.follow_document", lambda params: {"followed": session_lease.follow_active_document()}
        )

        def write(_params):
            session_lease.require_document_write_access()
            return {"written": True}

        self.registry.register("test.write", write)

    def call(self, tool: str, params: dict | None = None, client_id: str | None = None):
        context = None if client_id is None else {"client_id": client_id}
        return self.registry.dispatch(tool, params or {}, context=context)

    def test_acquire_is_idempotent_for_holder_and_contended_for_others(self) -> None:
        first = self.call("session.acquire_lease", {"ttl": 10}, "client-a")
        second = self.call("session.acquire_lease", {"ttl": 20}, "client-a")
        blocked = self.call("session.acquire_lease", {}, "client-b")

        self.assertTrue(first["ok"])
        self.assertTrue(first["data"]["acquired"])
        self.assertFalse(second["data"]["acquired"])
        self.assertEqual(first["data"]["lease_id"], second["data"]["lease_id"])
        self.assertEqual(second["data"]["ttl"], 10)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["type"], "LockedError")
        self.assertEqual(blocked["data"]["held_by"], "client-a")
        self.assertEqual(blocked["data"]["expires_in"], 10)
        self.assertNotIn("lease_id", blocked["data"])

    def test_holder_can_renew_release_and_observe_private_lease_id(self) -> None:
        acquired = self.call("session.acquire_lease", {"ttl": 10}, "client-a")["data"]
        lease_id = acquired["lease_id"]
        self.now += 4

        observer = self.call("session.lease_status", {}, "client-b")["data"]
        holder = self.call("session.lease_status", {}, "client-a")["data"]
        renewed = self.call(
            "session.renew_lease", {"lease_id": lease_id, "ttl": 30}, "client-a"
        )
        released = self.call(
            "session.release_lease", {"lease_id": lease_id}, "client-a"
        )

        self.assertNotIn("lease_id", observer)
        self.assertEqual(holder["lease_id"], lease_id)
        self.assertEqual(holder["expires_in"], 6)
        self.assertTrue(renewed["data"]["renewed"])
        self.assertEqual(renewed["data"]["ttl"], 30)
        self.assertEqual(renewed["data"]["expires_in"], 30)
        self.assertTrue(released["data"]["released"])
        self.assertFalse(self.call("session.lease_status", {}, "client-a")["data"]["active"])

    def test_document_writes_are_holder_only_while_reads_and_post_release_writes_proceed(self) -> None:
        acquired = self.call("session.acquire_lease", {}, "client-a")["data"]

        self.assertTrue(self.call("session.lease_status", {}, "client-b")["ok"])
        self.assertTrue(self.call("test.write", {}, "client-a")["ok"])
        blocked = self.call("test.write", {}, "client-b")
        anonymous = self.call("test.write", {})
        self.assertEqual(blocked["error"]["type"], "LockedError")
        self.assertEqual(anonymous["error"]["type"], "LockedError")

        self.call(
            "session.release_lease", {"lease_id": acquired["lease_id"]}, "client-a"
        )
        self.assertTrue(self.call("test.write", {}, "client-b")["ok"])

    def test_ttl_expiry_and_document_switch_release_the_old_lease(self) -> None:
        self.call("session.acquire_lease", {"ttl": 5}, "client-a")
        self.now += 5
        self.assertTrue(session_lease.expire_lease())
        self.assertTrue(self.call("test.write", {}, "client-b")["ok"])

        self.call("session.acquire_lease", {}, "client-a")
        self.bpy.data.filepath = "C:/projects/other.blend"
        self.assertFalse(self.call("session.lease_status", {}, "client-b")["data"]["active"])
        self.assertTrue(self.call("test.write", {}, "client-b")["ok"])

    def test_holder_can_follow_a_toolkit_controlled_file_switch(self) -> None:
        acquired = self.call("session.acquire_lease", {}, "client-a")["data"]
        self.bpy.data.filepath = "C:/projects/version.blend"
        followed = self.call("session.follow_document", {}, "client-a")
        status = self.call("session.lease_status", {}, "client-a")["data"]
        released = self.call(
            "session.release_lease", {"lease_id": acquired["lease_id"]}, "client-a"
        )

        self.assertTrue(followed["data"]["followed"])
        self.assertEqual(status["lease_id"], acquired["lease_id"])
        self.assertTrue(released["data"]["released"])

    def test_invalid_identity_bounds_and_lease_ids_fail_loudly(self) -> None:
        no_identity = self.call("session.acquire_lease", {})
        invalid_ttl = self.call("session.acquire_lease", {"ttl": 4}, "client-a")
        acquired = self.call("session.acquire_lease", {}, "client-a")["data"]
        wrong_holder = self.call(
            "session.renew_lease", {"lease_id": acquired["lease_id"]}, "client-b"
        )
        wrong_id = self.call(
            "session.release_lease", {"lease_id": "wrong"}, "client-a"
        )

        self.assertEqual(no_identity["error"]["type"], "InvalidStateError")
        self.assertEqual(invalid_ttl["error"]["type"], "InvalidParamsError")
        self.assertEqual(wrong_holder["error"]["type"], "LockedError")
        self.assertEqual(wrong_id["error"]["type"], "NotFoundError")


if __name__ == "__main__":
    unittest.main()
