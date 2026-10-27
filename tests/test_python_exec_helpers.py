"""Pure Python checks of python.exec's change and evidence helpers."""

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.dispatcher import create_registry
from blenderboo.bridge import _BridgeCall
from blenderboo.registry import ToolResult, _failure_envelope, _success_envelope
from blenderboo.tools.python_exec import (
    PythonExecError, _BoundedWriter, _api_hint, _operator_context_hint, _depsgraph_changes, _fragment_summaries,
    _geometry_changes, _json_result, _literal_mesh_targets, _literal_names,
    _mark_updates, _object_fields, _suspicious_geometry, _validate,
    compare_snapshots, snapshot,
)


class Item:
    def __init__(self, pointer, name, location=(0, 0, 0)):
        self.pointer = pointer
        self.session_uid = pointer
        self.name = name
        self.location = location
        self.matrix_world = ((1, 0, 0, location[0]), (0, 1, 0, location[1]),
                             (0, 0, 1, location[2]), (0, 0, 0, 1))
        self.rotation_euler = (0, 0, 0)
        self.scale = (1, 1, 1)
        self.parent = None
        self.users_collection = ()
        self.modifiers = ()
        self.data = None

    def as_pointer(self):
        return self.pointer


class PythonExecHelperTests(unittest.TestCase):
    def test_fbx_import_context_failure_names_typed_tool(self):
        hint = _operator_context_hint(RuntimeError("Context missing active object"),
                                      "bpy.ops.import_scene.fbx(filepath='fixture.fbx')")
        self.assertEqual(hint["recommended_tool"], "interchange.import")
        self.assertIsNone(_operator_context_hint(RuntimeError("other failure"),
                                                 "bpy.ops.import_scene.fbx(filepath='fixture.fbx')"))

    def test_api_hint_uses_live_lookup_for_named_missing_member(self):
        with patch("blenderboo.tools.api_lookup.lookup_api", return_value={
            "exists": False, "suggestions": ["use_nodes"], "blender_version": "5.2.2",
        }) as lookup:
            hint = _api_hint(AttributeError("'Scene' object has no attribute 'node_tree'"))
        lookup.assert_called_once_with({"path": "bpy.types.Scene.node_tree", "detail": "summary", "limit": 5})
        self.assertEqual(hint, {"path": "bpy.types.Scene.node_tree", "exists": False,
                                "suggestions": ["use_nodes"]})
        self.assertIsNone(_api_hint(NameError("missing")))

    def test_non_mesh_object_skips_geometry_measurement(self):
        empty = Item(99, "Empty")
        empty.type = "EMPTY"
        with patch("blenderboo.tools.python_exec._geometry_metrics",
                   side_effect=AssertionError("unexpected mesh measurement")):
            fields = _object_fields(empty)
        self.assertNotIn("geometry_metrics", fields)

    def test_snapshot_detects_create_remove_rename_and_object_modification(self):
        a = Item(1, "Before")
        removed = Item(2, "Removed")
        before = snapshot(SimpleNamespace(data=SimpleNamespace(objects=[a, removed], meshes=[])))
        a.name = "After"
        a.matrix_world = ((1, 0, 0, 2), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))
        created = Item(3, "Created")
        after = snapshot(SimpleNamespace(data=SimpleNamespace(objects=[a, created], meshes=[])))
        result = compare_snapshots(before, after, "detailed")["objects"]
        self.assertEqual(result["created"], ["Created"])
        self.assertEqual(result["removed"], ["Removed"])
        self.assertEqual(result["renamed"], [{"from": "Before", "to": "After"}])
        self.assertIn("matrix_world", result["modified"]["After"])

    def test_stream_capture_is_bounded_and_exposes_overflow(self):
        writer = _BoundedWriter()
        writer.write("x" * 9000)
        preview, overflow, truncated = writer.split()
        self.assertEqual(len(preview), 8192)
        self.assertEqual(len(overflow), 808)
        self.assertTrue(truncated)

    def test_pointer_reuse_is_create_and_remove_not_rename(self):
        before_item = Item(12, "Old")
        after_item = Item(12, "New")
        after_item.session_uid = 13
        before = snapshot(SimpleNamespace(data=SimpleNamespace(objects=[before_item])))
        after = snapshot(SimpleNamespace(data=SimpleNamespace(objects=[after_item])))
        difference = compare_snapshots(before, after)["objects"]
        self.assertEqual(difference["created"], ["New"])
        self.assertEqual(difference["removed"], ["Old"])
        self.assertEqual(difference["renamed"], [])

    def test_category_evidence_and_no_change_semantics(self):
        before = {"meshes": {(1, 1): ("Mesh", {"geometry.positions": "a", "uv_attributes": "a"})}}
        after = {"meshes": {(1, 1): ("Mesh", {"geometry.positions": "b", "uv_attributes": "b"})}}
        difference = compare_snapshots(before, after)
        self.assertEqual(difference["geometry"]["Mesh"], "positions")
        self.assertEqual(difference["uv_attributes"]["Mesh"], "data_or_marks")
        self.assertEqual(compare_snapshots(before, before), {})

    def test_shallow_field_categories(self):
        fields = {"matrix_world": (1,), "parent": None, "modifiers": (),
                  "material_slots": (), "custom_properties": "a"}
        changed = {"matrix_world": (2,), "parent": "Parent", "modifiers": ("Subd",),
                   "material_slots": ("Material",), "custom_properties": "b"}
        before = {"objects": {(1, 1): ("Part", fields)}}
        after = {"objects": {(1, 1): ("Part", changed)}}
        result = compare_snapshots(before, after)
        for category in ("transform_hierarchy", "modifiers", "shading", "other"):
            self.assertIn("Part", result[category])

    def test_depsgraph_classifies_updated_ids(self):
        def item(kind, name, tree_type=""):
            return SimpleNamespace(name=name, bl_rna=SimpleNamespace(identifier=kind), bl_idname=tree_type)
        updates = [(item("Mesh", "Mesh"), True, False, False),
                   (item("GeometryNodeTree", "Nodes", "GeometryNodeTree"), False, False, False),
                   (item("Material", "Mat"), False, False, True)]
        result = _depsgraph_changes(updates, "summary")
        self.assertIn("Mesh", result["geometry"])
        self.assertIn("Nodes", result["geometry_nodes"])
        self.assertIn("Mat", result["shading"])

    def test_bounded_inventory_marks_only_updated_existing_ids(self):
        existing = Item(1, "Existing")
        created = Item(2, "Created")
        before = {"objects": {(1, 1): ("Existing", {})}}
        after = {"objects": {(1, 1): ("Existing", {}), (2, 2): ("Created", {})}}
        _mark_updates(before, after, [(existing, False, True, False),
                                      (created, True, False, False)])
        changes = compare_snapshots(before, after)
        self.assertEqual(changes["objects"]["counts"],
                         {"created": 1, "removed": 0, "renamed": 0, "modified": 1})
        self.assertEqual(_literal_names("D.objects['Existing']['tag'] = 1"), {"Existing", "tag"})

    def test_fragment_probe_uses_only_explicit_mesh_object_names(self):
        code = "obj = D.objects['Part']; other = D.objects.get('Second'); result = 'Unrelated'"
        self.assertEqual(_literal_mesh_targets(code), {"Part", "Second"})
        self.assertEqual(_literal_mesh_targets("result = len(D.objects)"), set())
        part = SimpleNamespace(type="MESH", data=SimpleNamespace(is_editmode=False), matrix_world=())
        bpy = SimpleNamespace(data=SimpleNamespace(objects={"Part": part}))
        with patch("blenderboo.tools.mesh_topology.component_summary",
                   return_value={"islands": 1, "small_islands": []}) as summary:
            results, skipped = _fragment_summaries(bpy, {"Part", "Second"}, .01)
        self.assertEqual(results["Part"]["islands"], 1)
        self.assertEqual(skipped["Second"], "mesh_object_not_found")
        summary.assert_called_once_with(part.data, matrix_world=part.matrix_world, small_fraction=.01)

    def test_non_json_result_uses_repr(self):
        self.assertEqual(_json_result({"a": 1}), ({"a": 1}, False))
        value, flagged = _json_result({1, 2})
        self.assertTrue(flagged)
        self.assertIsInstance(value, str)

    def test_input_validation(self):
        self.assertEqual(_validate({"code": "result = 1"}), ("result = 1", "summary", False))
        for params in ({}, {"code": 1}, {"code": "", "diff": "wide"}, {"code": "", "timeout_hint_s": 0}):
            with self.subTest(params=params), self.assertRaises(Exception):
                _validate(params)
        for params in ({"collapse_face_ratio": 1}, {"collapse_volume_ratio": -1},
                       {"bbox_jump_ratio": 1}, {"fragment_small_ratio": 0},
                       {"fragment_small_ratio": 1.1}):
            with self.subTest(params=params), self.assertRaises(Exception):
                _validate({"code": "pass", **params})

    def test_geometry_drop_reports_metrics_and_warning_evidence(self):
        before_metric = {"faces": 100, "bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1],
                         "bbox_center": [.5, .5, .5], "bbox_diagonal": 1.73, "volume_m3": 1.0}
        after_metric = {"faces": 7, "bbox_min": [0, 0, 0], "bbox_max": [.1, .1, .1],
                        "bbox_center": [.05, .05, .05], "bbox_diagonal": .173, "volume_m3": .001}
        before = {"objects": {(1, 1): ("Part", {"geometry_metrics": before_metric})}}
        after = {"objects": {(1, 1): ("Part", {"geometry_metrics": after_metric})}}
        changes = _geometry_changes(before, after, "summary")
        self.assertEqual(changes["Part"]["before"]["faces"], 100)
        finding = _suspicious_geometry(before, after, .25, .25, 2)[0]
        self.assertEqual(finding["object"], "Part")
        self.assertIn("face_count_drop", finding["reasons"])
        self.assertIn("volume_drop", finding["reasons"])
        self.assertIn("bbox_size_jump", finding["reasons"])

    def test_registry_advertises_python_toolset(self):
        with patch("blenderboo.tools.session_state.reconcile"):
            registry = create_registry()
        specs = registry.specs_for_toolset("python")
        self.assertEqual([spec["name"] for spec in specs],
                         ["python.exec", "python.module_list", "python.module_put", "python.module_remove"])
        self.assertEqual(specs[0]["input_schema"]["required"], ["code"])

    def test_running_python_timeout_reports_pending_without_cancelling(self):
        call = _BridgeCall("python.exec", {"code": "pass"})
        self.assertTrue(call.begin())
        response = call.wait(0)
        self.assertEqual(response["error"]["type"], "BridgeExecutionPendingError")
        self.assertTrue(response["data"]["possibly_still_running"])
        self.assertTrue(call.complete({"ok": True, "data": {}}))

    def test_user_exception_preserves_partial_evidence(self):
        evidence = {"changes": {"objects": {"counts": {"created": 1}}},
                    "stdout": "created\n", "partial_change_possible": True, "state_token": "abc.1"}
        error = PythonExecError(ValueError("boom"), evidence, 'File "<python.exec>", line 2')
        envelope = _failure_envelope("python.exec", error, "internal traceback")
        self.assertEqual(envelope["error"]["type"], "ValueError")
        self.assertEqual(envelope["error"]["traceback"], 'File "<python.exec>", line 2')
        self.assertEqual(envelope["data"], evidence)

    def test_envelope_carries_optional_exec_timings(self):
        timings = {"timings_ms": {"exec_user": 1.5, "exec_overhead": 2.5}}
        self.assertEqual(_success_envelope("python.exec", ToolResult({}, [], timings))["meta"], timings)
        self.assertNotIn("meta", _success_envelope("other", {}))
        error = PythonExecError(ValueError("x"), {}, "traceback")
        error.meta = timings
        self.assertEqual(_failure_envelope("python.exec", error, "traceback")["meta"], timings)


if __name__ == "__main__":
    unittest.main()
