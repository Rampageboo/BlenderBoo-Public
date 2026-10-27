"""Focused tests for bounded response profiles and full-result retrieval."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch

from blenderboo_mcp.response_profiles import (
    ResponseArtifactStore,
    apply_response_profile,
    response_mode,
)


def _envelope(data: dict, *, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "tool": "object.create",
        "data": data,
        "error": None if ok else {
            "type": "ExampleError",
            "message": "bounded failure",
            "traceback": "private traceback" * 100,
        },
        "warnings": [],
    }


class ResponseModeTests(unittest.TestCase):
    def test_mode_uses_default_and_environment_override(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(response_mode(None), "default")
        with patch.dict("os.environ", {"CBT_MCP_RESPONSE_MODE": "COMPACT"}, clear=True):
            self.assertEqual(response_mode(None), "compact")

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "compact.*default.*full"):
            response_mode("lossy")


class ResponseArtifactStoreTests(unittest.TestCase):
    def test_unicode_artifact_round_trips_across_pages(self) -> None:
        store = ResponseArtifactStore(max_entries=2, max_item_bytes=4096, max_total_bytes=8192)
        expected = _envelope({"name": "墙", "details": "数据" * 500})
        metadata = store.put(expected)
        self.assertIsNotNone(metadata)
        assert metadata is not None
        chunks = []
        cursor = 0
        while True:
            page = store.get(metadata["artifact_id"], cursor=cursor, max_chars=700)
            chunks.append(page["chunk"])
            if page["next_cursor"] is None:
                break
            cursor = int(page["next_cursor"])
        self.assertEqual(json.loads("".join(chunks)), expected)
        self.assertEqual(metadata["sha256"], page["sha256"])

    def test_lru_evicts_oldest_artifact(self) -> None:
        store = ResponseArtifactStore(max_entries=1, max_item_bytes=2048, max_total_bytes=2048)
        first = store.put(_envelope({"name": "first"}))
        second = store.put(_envelope({"name": "second"}))
        assert first is not None and second is not None
        with self.assertRaises(KeyError):
            store.get(first["artifact_id"])
        self.assertEqual(
            json.loads(store.get(second["artifact_id"])["chunk"])["data"]["name"],
            "second",
        )

    def test_oversized_artifact_is_refused_without_eviction(self) -> None:
        store = ResponseArtifactStore(max_entries=2, max_item_bytes=1024, max_total_bytes=2048)
        retained = store.put(_envelope({"name": "retained"}))
        refused = store.put(_envelope({"payload": "x" * 2000}))
        self.assertIsNotNone(retained)
        self.assertIsNone(refused)
        assert retained is not None
        self.assertTrue(store.get(retained["artifact_id"])["complete"])

    def test_cursor_accepts_exact_end_and_rejects_past_end(self) -> None:
        store = ResponseArtifactStore(max_entries=2, max_item_bytes=2048, max_total_bytes=4096)
        metadata = store.put(_envelope({"name": "boundary"}))
        assert metadata is not None
        page = store.get(metadata["artifact_id"], cursor=metadata["chars"])
        self.assertEqual(page["chunk"], "")
        self.assertTrue(page["complete"])
        with self.assertRaisesRegex(ValueError, "beyond"):
            store.get(metadata["artifact_id"], cursor=metadata["chars"] + 1)

    def test_concurrent_writes_produce_unique_retrievable_artifacts(self) -> None:
        store = ResponseArtifactStore(
            max_entries=32,
            max_item_bytes=2048,
            max_total_bytes=64 * 1024,
        )
        with ThreadPoolExecutor(max_workers=8) as executor:
            metadata = list(executor.map(
                lambda index: store.put(_envelope({"index": index})),
                range(32),
            ))
        artifacts = [item for item in metadata if item is not None]
        self.assertEqual(len(artifacts), 32)
        self.assertEqual(len({item["artifact_id"] for item in artifacts}), 32)
        recovered = {
            json.loads(store.get(item["artifact_id"])["chunk"])["data"]["index"]
            for item in artifacts
        }
        self.assertEqual(recovered, set(range(32)))


class ApplyResponseProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ResponseArtifactStore(
            max_entries=4,
            max_item_bytes=4096,
            max_total_bytes=8192,
        )

    def test_default_keeps_small_result_and_compacts_large_result(self) -> None:
        small = _envelope({"state_token": "state-a"})
        self.assertIs(apply_response_profile(small, mode="default", store=self.store), small)

        large = _envelope(
            {
                "state_token": "state-b",
                "changed": True,
                "object": "Wall_A",
                "detail": {
                    "name": "Wall_A",
                    "type": "MESH",
                    "dimensions": [4.0, 0.2, 3.0],
                    "transform": {"location": [0.0, 0.0, 1.5]},
                },
                "journal": [{"detail": "x" * 2500}],
            }
        )
        compact = apply_response_profile(large, mode="default", store=self.store)
        self.assertEqual(compact["state_token"], "state-b")
        self.assertTrue(compact["changed"])
        self.assertEqual(compact["object"], "Wall_A")
        self.assertNotIn("detail", compact)
        self.assertNotIn("journal", compact)
        self.assertIn("artifact", compact)

    def test_save_disk_evidence_survives_all_profiles(self) -> None:
        original = _envelope({
            "changed": True,
            "write_confirmed": True,
            "after": {"disk_exists": True, "disk_size": 4096, "disk_mtime_ns": 123456789},
            "journal": [{"op": "save_current", "status": "applied"}],
        })
        original["tool"] = "scene.save_current"
        for mode in ("full", "default", "compact"):
            with self.subTest(mode=mode):
                public = apply_response_profile(original, mode=mode, store=self.store)
                receipt = public if mode == "default" else public["data"]
                self.assertIs(receipt["write_confirmed"], True)
                self.assertEqual(receipt["after"]["disk_size"], 4096)
                self.assertEqual(receipt["after"]["disk_mtime_ns"], 123456789)

    def test_default_mutation_uses_receipt_even_when_small(self) -> None:
        original = _envelope({"changed": True, "state_token": "state-b", "object": "Cube", "requested_name": "Cube", "resolved_name": "Cube", "client_id": None, "affected_datablocks": [{"type": "Object", "name": "Cube"}], "journal": [{"after": "Cube"}]})
        public = apply_response_profile(original, mode="default", store=self.store)
        self.assertNotIn("object", public)
        self.assertNotIn("requested_name", public)
        self.assertNotIn("resolved_name", public)
        self.assertNotIn("client_id", public)
        self.assertNotIn("affected_datablocks", public)
        self.assertNotIn("journal", public)
        self.assertNotIn("artifact", public)

    def test_default_keeps_unsupported_reason_and_small_analysis(self) -> None:
        rejected = _envelope({"changed": False, "status": "unsupported",
                              "reason": "Endpoint fillets need an adjoining profile edge"})
        rejected["tool"] = "mesh.build_lathe"
        self.assertIs(apply_response_profile(rejected, mode="default", store=self.store), rejected)
        analysis = _envelope({"changed": False, "status": "planned", "coverage": .94,
                              "clearance": {"minimum_scene_units": .0012},
                              "reason": "Gap ceiling not reached"})
        analysis["tool"] = "mesh.clear_of"
        self.assertIs(apply_response_profile(analysis, mode="default", store=self.store), analysis)

    def test_default_dry_run_keeps_plan_evidence(self) -> None:
        original = _envelope({"changed": False, "dry_run": True, "action": "mesh.join",
                              "source_objects": ["Frame", "Slide"], "sources_preserved": True,
                              "counts": {"vertices": 100}, "journal": [{"op": "join"}]})
        original["tool"] = "mesh.join"
        public = apply_response_profile(original, mode="default", store=self.store)
        self.assertIn("data", public)
        self.assertTrue(public["data"]["dry_run"])
        self.assertEqual(public["data"]["source_objects"], ["Frame", "Slide"])

    def test_default_fair_fit_analysis_keeps_summary_and_retrievable_grid(self) -> None:
        original = _envelope({"changed": False, "status": "partial", "reason": "Missing rays",
                              "coverage": .968, "rays": 1800, "hits": 1742,
                              "coefficients": [.1, .2], "residual_p95_scene_units": .003,
                              "fitted_radius_scene_units": [[.2] * 72 for _ in range(25)],
                              "residual_scene_units": [[.001] * 72 for _ in range(25)]})
        original["tool"] = "mesh.fair_fit"
        store = ResponseArtifactStore(max_entries=2, max_item_bytes=128 * 1024,
                                      max_total_bytes=256 * 1024)
        public = apply_response_profile(original, mode="default", store=store)
        self.assertEqual(public["data"]["reason"], "Missing rays")
        self.assertEqual(public["data"]["coverage"], .968)
        self.assertEqual(public["data"]["coefficients"], [.1, .2])
        self.assertNotIn("fitted_radius_scene_units", public["data"])
        self.assertEqual(store.get(public["data"]["artifact"],
                                        path="$.data.residual_scene_units[0][0]")["value"], .001)

    def test_python_overflow_is_recoverable_from_artifact(self) -> None:
        original = _envelope({"changed": True, "result": 3, "stdout": "first", "stdout_overflow": "x" * 3000})
        original["tool"] = "python.exec"
        public = apply_response_profile(original, mode="default", store=self.store)
        self.assertEqual(public["data"]["result"], 3)
        self.assertEqual(public, original)

    def test_default_exec_summarizes_many_depsgraph_changes_with_full_artifact(self) -> None:
        changes = {"objects": {f"Part_{index:03d}": "modified" for index in range(100)}}
        original = _envelope({"changed": True, "changes": changes,
                              "result": 42, "stdout": "done", "state_token": "state-a"})
        original["tool"] = "python.exec"
        public = apply_response_profile(original, mode="default", store=self.store)
        self.assertEqual(public["data"]["result"], 42)
        self.assertEqual(public["data"]["stdout"], "done")
        self.assertNotIn("changes", public["data"])
        self.assertEqual(public["data"]["changes_summary"]["total_entries"], 100)
        self.assertEqual(self.store.get(public["data"]["artifact"],
                                        path="$.data.changes.objects.Part_099")["value"], "modified")

    def test_compact_exec_preserves_changes_and_detection(self) -> None:
        original = _envelope({"changed": False, "changes": {"geometry": {}}, "detection": "no_changes_detected", "journal": ["x"]})
        original["tool"] = "python.exec"
        public = apply_response_profile(original, mode="compact", store=self.store)
        self.assertFalse(public["data"]["changed"])
        self.assertEqual(public["data"]["changes"], {"geometry": {}})
        self.assertEqual(public["data"]["detection"], "no_changes_detected")

    def test_compact_lookup_and_exec_error_keep_actionable_api_hints(self) -> None:
        lookup = _envelope({"blender_version": "5.2.2", "exists": False,
                            "path": "bpy.types.Scene.node_tree", "owner": "bpy.types.Scene",
                            "suggestions": ["use_nodes", "compositing_node_group"]})
        lookup["tool"] = "api.lookup"
        public = apply_response_profile(lookup, mode="compact", store=self.store)
        self.assertFalse(public["data"]["exists"])
        self.assertEqual(public["data"]["blender_version"], "5.2.2")
        self.assertIn("compositing_node_group", public["data"]["suggestions"])

        failure = _envelope({"changed": False, "api_hint": {"path": "bpy.types.Scene.node_tree",
                            "exists": False, "suggestions": ["compositing_node_group"]}}, ok=False)
        failure["tool"] = "python.exec"
        compact = apply_response_profile(failure, mode="compact", store=self.store)
        self.assertEqual(compact["data"]["api_hint"]["path"], "bpy.types.Scene.node_tree")

    def test_default_mutation_keeps_fragment_warning_evidence(self) -> None:
        finding = {"reason": "new_islands", "object": "Part", "increase": 1,
                   "small_islands": [{"vertices": 7, "faces": 5, "focus": [1, 2, 3]}]}
        original = _envelope({"changed": True, "suspicious_change": [finding]})
        public = apply_response_profile(original, mode="default", store=self.store)
        self.assertEqual(public["suspicious_change"], [finding])

    def test_compact_error_preserves_message_and_omits_traceback(self) -> None:
        failed = _envelope({"changed": False, "journal": ["x"]}, ok=False)
        compact = apply_response_profile(failed, mode="compact", store=self.store)
        self.assertEqual(compact["error"]["type"], "ExampleError")
        self.assertEqual(compact["error"]["message"], "bounded failure")
        self.assertTrue(compact["error"]["traceback_omitted"])

    def test_full_returns_original_object(self) -> None:
        expected = _envelope({"payload": "x" * 3000})
        self.assertIs(apply_response_profile(expected, mode="full", store=self.store), expected)

    def test_storage_failure_returns_full_result_with_warning(self) -> None:
        store = ResponseArtifactStore(max_entries=1, max_item_bytes=1024, max_total_bytes=1024)
        expected = _envelope({"payload": "x" * 2000})
        public = apply_response_profile(expected, mode="compact", store=store)
        self.assertEqual(public["data"], expected["data"])
        self.assertEqual(public, expected)

    def test_exact_success_receipt_has_no_request_echo_and_small_budget(self) -> None:
        original = _envelope({"changed": True, "state_token": "s1", "requested_name": "Wall", "resolved_name": "Wall", "detail": {"name": "Wall"}})
        public = apply_response_profile(original, mode="default", store=self.store, requested={"name": "Wall"})
        self.assertEqual(public, {"ok": True, "tool": "object.create", "changed": True, "state_token": "s1"})
        self.assertLessEqual(len(json.dumps(public, separators=(",", ":")).encode()), 150)

    def test_deviation_and_path_selection(self) -> None:
        original = _envelope({"changed": True, "state_token": "s2", "requested_name": "Wall", "resolved_name": "Wall.001", "journal": [{"after": 1}]})
        public = apply_response_profile(original, mode="default", store=self.store, requested={"name": "Wall"})
        self.assertEqual(public["object"], "Wall.001")
        self.assertEqual(public["renamed_from"], "Wall")
        self.assertEqual(self.store.get(public["artifact"], path="$.data.journal[0].after")["value"], 1)

    def test_default_deviation_receipts_stay_under_400_bytes(self) -> None:
        cases = [
            _envelope({"changed": True, "state_token": "s1", "requested_name": "Wall", "resolved_name": "Wall.001", "journal": [{"after": "Wall.001"}]}),
            _envelope({"changed": True, "state_token": "s2", "clamped": {"width": 1.0}, "journal": [{"after": 1.0}]}),
            _envelope({"changed": False, "state_token": "s3", "journal": [{"status": "unchanged"}]}),
        ]
        for original in cases:
            with self.subTest(data=original["data"]):
                public = apply_response_profile(original, mode="default", store=self.store)
                self.assertLessEqual(len(json.dumps(public, separators=(",", ":")).encode("utf-8")), 400)
        self.assertEqual(apply_response_profile(cases[0], mode="default", store=self.store)["renamed_from"], "Wall")

    def test_default_error_omits_traceback(self) -> None:
        failed = _envelope({"changed": False, "journal": ["x"]}, ok=False)
        public = apply_response_profile(failed, mode="default", store=self.store)
        self.assertNotIn("traceback", public["error"])
        self.assertEqual(public["error"]["type"], "ExampleError")


if __name__ == "__main__":
    unittest.main()
