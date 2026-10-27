"""Pure-Python tests for registry, dispatch, and envelope behavior."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

import blenderboo
from blenderboo.registry import (
    DeferredEnvelope,
    DeferredToolResult,
    ToolRegistry,
    ToolResult,
)


class RegistryTests(unittest.TestCase):
    def test_success_envelope(self) -> None:
        registry = ToolRegistry()
        registry.register("test.echo", lambda params: {"value": params["value"]})
        result = registry.dispatch("test.echo", {"value": 7})
        self.assertEqual(
            result,
            {"ok": True, "tool": "test.echo", "data": {"value": 7}, "error": None, "warnings": []},
        )
        json.dumps(result)

    def test_tool_result_warnings(self) -> None:
        registry = ToolRegistry()
        registry.register("test.warn", lambda params: ToolResult({"done": True}, ["heads up"]))
        result = registry.dispatch("test.warn")
        self.assertTrue(result["ok"])
        self.assertEqual(result["warnings"], ["heads up"])

    def test_deferred_result_resolves_to_stable_envelope(self) -> None:
        deferred = DeferredToolResult()
        registry = ToolRegistry()
        registry.register("test.deferred", lambda _params: deferred)

        result = registry.dispatch("test.deferred")
        self.assertIsInstance(result, DeferredEnvelope)
        completed = []
        result.add_done_callback(completed.append)
        deferred.resolve(ToolResult({"done": True}, ["later"]))

        self.assertEqual(
            completed,
            [{
                "ok": True,
                "tool": "test.deferred",
                "data": {"done": True},
                "error": None,
                "warnings": ["later"],
            }],
        )

    def test_deferred_rejection_preserves_error_and_traceback(self) -> None:
        deferred = DeferredToolResult()
        registry = ToolRegistry()
        registry.register("test.deferred", lambda _params: deferred)
        result = registry.dispatch("test.deferred")
        completed = []
        result.add_done_callback(completed.append)

        deferred.reject(ValueError("later failure"), "captured draw traceback")

        self.assertFalse(completed[0]["ok"])
        self.assertEqual(completed[0]["error"]["type"], "ValueError")
        self.assertEqual(completed[0]["error"]["message"], "later failure")
        self.assertEqual(completed[0]["error"]["traceback"], "captured draw traceback")

    def test_deferred_cancel_reports_cleanup_failure_without_skipping_callbacks(self) -> None:
        deferred = DeferredToolResult()

        def cleanup():
            raise RuntimeError("cleanup failed")

        deferred.set_cancel_callback(cleanup)
        result = DeferredEnvelope("test.deferred", deferred)
        completed = []
        result.add_done_callback(completed.append)

        self.assertTrue(result.cancel())

        self.assertEqual(completed[0]["error"]["type"], "RuntimeError")
        self.assertEqual(completed[0]["error"]["message"], "cleanup failed")
        self.assertIn("RuntimeError: cleanup failed", completed[0]["error"]["traceback"])

    def test_exception_is_structured_with_traceback(self) -> None:
        registry = ToolRegistry()

        def fail(params):
            raise ValueError("bad value")

        registry.register("test.fail", fail)
        result = registry.dispatch("test.fail", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "ValueError")
        self.assertEqual(result["error"]["message"], "bad value")
        self.assertIn("ValueError: bad value", result["error"]["traceback"])

    def test_unknown_tool_is_structured(self) -> None:
        result = ToolRegistry().dispatch("missing", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "NotFoundError")

    def test_invalid_params_are_structured(self) -> None:
        registry = ToolRegistry()
        registry.register("test.ok", lambda params: {})
        result = registry.dispatch("test.ok", ["not", "a", "mapping"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "InvalidParamsError")

    def test_invalid_handler_return_is_structured(self) -> None:
        registry = ToolRegistry()
        registry.register("test.invalid", lambda params: None)
        result = registry.dispatch("test.invalid", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "TypeError")

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register("test.same", lambda params: {})
        with self.assertRaises(ValueError):
            registry.register("test.same", lambda params: {})

    def test_specs_are_transport_ready_and_isolated(self) -> None:
        registry = ToolRegistry()
        schema = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        registry.register("test.echo", lambda params: params, description="Echo a value.", input_schema=schema)
        schema["properties"]["value"]["type"] = "string"

        first = registry.specs()
        self.assertEqual(first[0]["description"], "Echo a value.")
        self.assertEqual(first[0]["toolset"], "test")
        self.assertEqual(first[0]["input_schema"]["properties"]["value"]["type"], "integer")
        first[0]["input_schema"]["properties"].clear()
        self.assertIn("value", registry.specs()[0]["input_schema"]["properties"])
        json.dumps(first)

    def test_invalid_spec_metadata_is_rejected(self) -> None:
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            registry.register("test.bad", lambda params: {}, input_schema={"type": "array"})
        with self.assertRaises(TypeError):
            registry.register(
                "test.bad",
                lambda params: {},
                input_schema={"type": "object", "example": object()},
            )
        with self.assertRaises(TypeError):
            registry.register(
                "test.nan",
                lambda params: {},
                input_schema={"type": "object", "maximum": float("nan")},
            )

    def test_keyframe_operation_accepts_typed_curve_style(self) -> None:
        from blenderboo.errors import InvalidParamsError
        from blenderboo.tools.animation import _key_operation

        operation = _key_operation(
            {
                "op": "set", "target": "pose_bone", "bone": "Root",
                "property": "rotation_euler", "index": 1, "frame": 12,
                "value": 0.5, "interpolation": "linear", "easing": "ease_in",
                "handle_left_type": "vector", "handle_right_type": "auto_clamped",
            },
            0,
        )
        self.assertEqual(operation["interpolation"], "LINEAR")
        self.assertEqual(operation["easing"], "EASE_IN")
        self.assertEqual(operation["handle_left_type"], "VECTOR")
        self.assertEqual(operation["handle_right_type"], "AUTO_CLAMPED")
        with self.assertRaisesRegex(InvalidParamsError, "only valid for set"):
            _key_operation(
                {
                    "op": "remove", "property": "location", "index": 0,
                    "frame": 1, "interpolation": "LINEAR",
                },
                0,
            )

    def test_public_package_import_does_not_load_bpy(self) -> None:
        self.assertNotIn("bpy", sys.modules)
        self.assertEqual(
            set(blenderboo.tool_names()),
            {
                "scene.inspect", "annotation.inspect", "scene.screenshot", "scene.fast_capture", "python.exec", "api.lookup",
                "python.module_put", "python.module_list", "python.module_remove",
                "assembly.analyze",
                "asset.compare_to_source", "asset.quality_gate",
                "bake.from_ai_source", "export.game_asset",
                "mesh.build_lathe", "mesh.build_plate", "mesh.build_slab", "mesh.build_sweep",
                "mesh.clear_of", "mesh.fair_fit", "uv.audit", "uv.seams_from_regions", "uv.cut_strips_by_turn",
                "creature.graph_set", "creature.graph_inspect", "creature.graph_suggest", "creature.landmark_set",
                "creature.base_from_graph", "creature.bake_from_source",
                "mesh.topology_report", "mesh.wrap_to_target", "mesh.compare_to_points", "pointcloud.section",
                "source.inspect", "source.prepare", "reference.crop_grid",
                "armature.from_body_graph", "armature.validate_against_reference", "armature.export_ue_fbx",
                "skin.weights_limit", "skin.weights_transfer_by_index", "rig.rom_test",
                "checkpoint.save", "checkpoint.list", "checkpoint.restore",
                "session.whoami", "session.acquire_lease", "session.renew_lease",
                "session.release_lease",
                "render.preview", "render.review_set", "render.compare",
                "render.detail", "render.ortho_set", "render.reference_match", "render.contact_sheet",
                "camera.calibrate_from_reference", "render.contour_compare",
                "render.background", "render.background_status", "render.background_cancel",
                "render.manifest", "render.manifest_validate", "bake.run",
                "material.inspect", "material.create", "material.patch", "scene.save_version", "scene.save_current", "scene.save_as",
                "image.inspect", "image.load", "image.create", "image.patch",
                "material.slot_inspect", "material.slot_assign", "material.slot_remove",
                "material.texture_bind",
                "system.capabilities", "system.doctor",
                "collection.inspect", "collection.create", "collection.rename",
                "collection.set_visibility", "collection.link_objects",
                "collection.unlink_objects", "collection.link_child",
                "collection.unlink_child", "collection.delete", "collection.parent_objects", "collection.transform",
                "camera.inspect", "camera.patch", "camera.set_active", "camera.import_photogrammetry",
                "camera.import_colmap_package",
                "light.inspect", "light.patch",
                "world.inspect", "world.create", "world.patch", "world.set_active",
                "render.settings", "compositor.inspect", "compositor.create", "compositor.patch",
                "interchange.inspect", "interchange.import", "interchange.export", "interchange.blend_import",
                "armature.inspect", "armature.create", "armature.edit", "armature.bone_remove", "armature.validate",
                "pose.inspect", "pose.patch", "pose.constraint_patch",
                "animation.inspect", "animation.action_create", "animation.action_assign",
                "animation.action_retarget", "animation.action_remove",
                "animation.keyframe_edit", "animation.pose_bake", "animation.validate",
                "animation.nla_inspect", "animation.nla_patch", "animation.nla_remove",
                "animation.driver_inspect", "animation.driver_create", "animation.driver_remove",
                "animation.shape_key_inspect", "animation.shape_key_create",
                "animation.shape_key_patch", "animation.shape_key_remove",
                "skin.inspect", "skin.bind", "skin.unbind", "skin.group_remove", "skin.auto_weights", "skin.weights_patch", "skin.weights_normalize", "skin.validate", "skin.audit",
                "mesh.inspect", "mesh.measure", "mesh.edit", "mesh.join", "mesh.separate",
                "mesh.goal_deform", "mesh.fit_contours",
                "architecture.wall_build",
                "curve.inspect", "curve.create", "curve.patch", "curve.spline_add",
                "curve.spline_patch", "curve.spline_remove",
                "geometry_nodes.inspect", "geometry_nodes.create",
                "geometry_nodes.patch", "geometry_nodes.modifier_inputs",
                "uv.inspect", "uv.layer_create", "uv.layer_rename", "uv.layer_set_active",
                "uv.layer_remove", "uv.project", "uv.transform", "uv.unwrap", "uv.pack",
                "modifier.inspect", "modifier.add", "modifier.patch",
                "modifier.reorder", "modifier.remove", "modifier.apply",
                "object.inspect", "object.create", "object.convert_evaluated", "object.duplicate", "object.rename",
                "object.set_visibility", "object.set_selection", "object.parent", "object.mode_set", "object.align_to_workplane", "object.transform", "object.delete",
            },
        )
        specs = blenderboo.tool_specs()
        self.assertEqual({spec["name"] for spec in specs}, set(blenderboo.tool_names()))
        self.assertTrue(all(spec["description"] for spec in specs))
        self.assertTrue(all(spec["toolset"] for spec in specs))
        self.assertTrue(all(spec["input_schema"]["type"] == "object" for spec in specs))
        by_name = {spec["name"]: spec for spec in specs}
        self.assertEqual(
            by_name["session.whoami"]["input_schema"],
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
        self.assertEqual(
            by_name["object.align_to_workplane"]["input_schema"]["properties"]["offset"]["default"],
            [0.0, 0.0, 0.0],
        )
        for name in (
            "material.create", "material.patch", "scene.save_version", "scene.save_current", "checkpoint.restore", "object.create", "python.exec",
            "architecture.wall_build",
            "image.load", "image.create", "image.patch", "material.slot_assign",
            "material.slot_remove", "material.texture_bind",
            "object.duplicate", "object.rename", "object.set_visibility", "object.set_selection", "object.parent", "object.mode_set", "object.align_to_workplane",
            "object.transform", "object.delete",
            "collection.create", "collection.rename", "collection.set_visibility",
            "collection.link_objects", "collection.unlink_objects", "collection.parent_objects", "collection.transform", "collection.link_child",
            "collection.unlink_child", "collection.delete",
            "camera.patch", "camera.set_active", "light.patch",
            "world.create", "world.patch", "world.set_active", "render.settings",
            "compositor.create", "compositor.patch",
            "interchange.import", "interchange.export", "interchange.blend_import",
            "armature.create", "armature.edit", "armature.bone_remove", "pose.patch", "pose.constraint_patch",
            "animation.action_create", "animation.action_assign", "animation.action_retarget",
            "animation.action_remove", "animation.keyframe_edit", "animation.pose_bake",
            "animation.nla_patch", "animation.nla_remove", "animation.driver_create", "animation.driver_remove",
            "animation.shape_key_create", "animation.shape_key_patch", "animation.shape_key_remove",
            "skin.bind", "skin.unbind", "skin.group_remove", "skin.auto_weights", "skin.weights_patch", "skin.weights_normalize",
            "mesh.edit", "mesh.join", "mesh.separate", "curve.create", "curve.patch", "curve.spline_add",
            "curve.spline_patch", "curve.spline_remove",
            "geometry_nodes.create", "geometry_nodes.patch", "geometry_nodes.modifier_inputs",
            "uv.layer_create", "uv.layer_rename", "uv.layer_set_active", "uv.layer_remove",
            "uv.project", "uv.transform", "uv.unwrap", "uv.pack",
            "modifier.add", "modifier.patch", "modifier.reorder",
            "modifier.remove", "modifier.apply",
        ):
            expected = by_name[name]["input_schema"]["properties"]["expected_state"]
            self.assertEqual(expected["type"], "string")
            self.assertEqual(expected["minLength"], 1)
        acquire = by_name["session.acquire_lease"]["input_schema"]["properties"]
        self.assertEqual(acquire["scope"]["const"], "document")
        self.assertEqual(acquire["ttl"]["minimum"], 5)
        self.assertEqual(acquire["ttl"]["maximum"], 3600)

    def test_public_dispatch_uses_envelope(self) -> None:
        result = blenderboo.dispatch("does.not.exist", {})
        self.assertEqual(set(result), {"ok", "tool", "data", "error", "warnings"})
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
