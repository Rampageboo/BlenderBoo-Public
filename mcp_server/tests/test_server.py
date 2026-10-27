"""Tests that require the standalone server's official MCP SDK dependency."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from blenderboo_mcp.client import BridgeClient, MAX_REQUEST_BYTES
from blenderboo_mcp.proxy import ToolkitProxy
from blenderboo_mcp.schema_audit import audit_schemas
from blenderboo_mcp.server import _MCP_ONLY_TOOLS, _structured_text_mirror, create_server


class RecordingProxy:
    def __init__(self) -> None:
        self.calls = []
        self.responses = {}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self.responses:
                return copy.deepcopy(self.responses[name])
            return {"ok": True, "tool": name, "data": {}, "error": None, "warnings": []}

        return record


class ServerSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        log_directory = tempfile.TemporaryDirectory()
        self.addCleanup(log_directory.cleanup)
        env_patch = patch.dict("os.environ", {"CBT_CALL_LOG_DIR": log_directory.name})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.proxy = RecordingProxy()
        self.server = create_server(self.proxy, tool_mode="eager")
        tools = asyncio.run(self.server.list_tools())
        self.tools = {tool.name: tool.model_dump(by_alias=True, exclude_none=True) for tool in tools}

    def test_bake_fallback_distance_is_advertised_to_mcp_clients(self) -> None:
        field = self.tools["bake.from_ai_source"]["inputSchema"]["properties"]["fallback_max_distance"]
        numeric = next(branch for branch in field["anyOf"] if branch.get("type") == "number")
        self.assertEqual((numeric["minimum"], numeric["maximum"]), (0, 1000))

    def test_ue_export_actions_share_scale_and_root_contract(self) -> None:
        asyncio.run(self.server.call_tool("armature.export_ue_fbx", {
            "armature_name": "CreatureRig", "path": "C:/temp/Walk.fbx",
            "include_mesh": False, "action_names": ["Walk"],
            "action_fps": {"Walk": 120}, "root_mode": "object_stripped",
            "use_armature_deform_only": True,
        }))
        self.assertEqual(self.proxy.calls[-1][0], "call_named")
        self.assertEqual(self.proxy.calls[-1][1][0], "armature.export_ue_fbx")
        args = self.proxy.calls[-1][1][1]
        self.assertEqual(args["apply_scale_options"], "FBX_SCALE_ALL")
        self.assertEqual(args["action_fps"], {"Walk": 120})
        self.assertFalse(args["include_mesh"])
        self.assertNotIn("mesh_name", args)

    def test_skin_audit_routes_explicit_pose_sample_as_read_only_call(self) -> None:
        asyncio.run(self.server.call_tool("skin.audit", {
            "object_name": "CreatureLow", "armature_name": "CreatureRig",
            "samples": [{"name": "Bend", "bones": [
                {"bone": "upperarm_l", "axis": "X", "degrees": 45}]}],
            "thresholds": {"far_spill_fraction": 0.2},
        }))
        self.assertEqual(self.proxy.calls[-1][0], "call_named")
        self.assertEqual(self.proxy.calls[-1][1][0], "skin.audit")
        args = self.proxy.calls[-1][1][1]
        self.assertEqual(args["samples"][0]["name"], "Bend")
        self.assertEqual(args["thresholds"]["far_spill_fraction"], 0.2)

    def test_goal_and_contour_fit_route_shared_region_constraints(self) -> None:
        common = {"object_name": "Head", "region": {"vertex_group": "HeadFit"},
                  "max_displacement_mm": 5,
                  "symmetry": {"normal": [1, 0, 0]},
                  "pinned_vertex_group": "NeckPins"}
        asyncio.run(self.server.call_tool("mesh.goal_deform", {
            **common, "operation": "landmark_warp",
            "landmarks": [{"vertex_index": 4, "target": [0, 0, 1]}],
            "falloff_radius_mm": 20,
        }))
        self.assertEqual(self.proxy.calls[-1][1][0], "mesh.goal_deform")
        self.assertEqual(self.proxy.calls[-1][1][1]["region"], {"vertex_group": "HeadFit"})
        asyncio.run(self.server.call_tool("mesh.fit_contours", {
            **common, "views": [
                {"camera_name": "Front", "contour": [[1,1],[5,1],[5,5],[1,5]], "width": 32, "height": 32},
                {"camera_name": "Side", "contour": [[1,1],[5,1],[5,5],[1,5]], "width": 32, "height": 32}],
        }))
        self.assertEqual(self.proxy.calls[-1][1][0], "mesh.fit_contours")
        self.assertEqual(self.proxy.calls[-1][1][1]["symmetry"]["normal"], (1.0, 0.0, 0.0))

    def test_usp_named_join_atlas_and_slab_route_through_typed_mcp(self) -> None:
        asyncio.run(self.server.call_tool("mesh.join", {
            "object_names": ["Frame", "Slide"], "name": "USP_Low", "dry_run": True}))
        self.assertEqual(self.proxy.calls[-1][0], "call_named")
        self.assertEqual(self.proxy.calls[-1][1][0], "mesh.join")
        self.assertEqual(self.proxy.calls[-1][1][1]["object_names"], ["Frame", "Slide"])
        asyncio.run(self.server.call_tool("mesh.join", {
            "object_names": ["Frame", "Slide"], "name": "USP_Low",
            "keep_sources": False, "max_elements": 500000,
            "fragment_small_ratio": 0.01, "confirm": True, "dry_run": False}))
        self.assertEqual(self.proxy.calls[-1][0], "call_named")
        self.assertEqual(self.proxy.calls[-1][1][1]["dry_run"], False)
        self.assertNotIn("confirm", self.proxy.calls[-1][1][1])
        asyncio.run(self.server.call_tool("uv.audit", {
            "object_names": ["Frame", "Slide"], "texture_size": 4096,
            "max_overlap_pairs": 50}))
        self.assertEqual(self.proxy.calls[-1][1][1]["object_names"], ["Frame", "Slide"])
        asyncio.run(self.server.call_tool("mesh.build_slab", {
            "name": "Magwell", "outline": [[-2,-1],[2,-1],[2,1],[-2,1]],
            "opening": [[-.5,-.4],[.5,-.4],[.5,.4],[-.5,.4]],
            "thickness": .5, "pocket_depth": .2}))
        self.assertEqual(self.proxy.calls[-1][1][0], "mesh.build_slab")
        self.assertEqual(self.proxy.calls[-1][1][1]["pocket_depth"], .2)

    def test_text_receipt_exposes_semantic_refusal_and_analysis(self) -> None:
        refusal = {"ok": True, "tool": "mesh.build_lathe", "data": {
            "changed": False, "status": "unsupported",
            "reason": "Endpoint fillets need an adjoining profile edge"},
            "error": None, "warnings": []}
        visible = json.loads(_structured_text_mirror(refusal))
        self.assertEqual(visible["status"], "unsupported")
        self.assertIn("adjoining profile edge", visible["reason"])
        analysis = {**refusal, "tool": "mesh.fair_fit", "data": {
            "changed": False, "status": "partial", "coverage": .968,
            "rays": 1800, "hits": 1742, "coefficients": [.1, .2],
            "residual_p95_scene_units": .003, "artifact": "response-1"}}
        visible = json.loads(_structured_text_mirror(analysis))
        self.assertEqual((visible["coverage"], visible["hits"]), (.968, 1742))
        self.assertEqual(visible["artifact"], "response-1")

    def test_r3_refusal_and_fair_fit_analysis_survive_mcp_default_profile(self) -> None:
        self.proxy.responses["call_named"] = {
            "ok": True, "tool": "mesh.build_lathe", "data": {"changed": False,
                "status": "unsupported", "reason": "Endpoint fillets need an adjoining profile edge"},
            "error": None, "warnings": []}
        content, structured = asyncio.run(self.server.call_tool("mesh.build_lathe", {
            "name": "Rejected", "profile": [{"radius": 1, "z": 0}, {"radius": 1, "z": 1}]}))
        self.assertEqual(structured["data"]["status"], "unsupported")
        self.assertIn("adjoining profile edge", json.loads(content[0].text)["reason"])

        self.proxy.responses["call_named"] = {
            "ok": True, "tool": "mesh.fair_fit", "data": {"changed": False,
                "status": "partial", "coverage": .968, "rays": 1800, "hits": 1742,
                "coefficients": [.1, .2], "residual_p95_scene_units": .003,
                "fitted_radius_scene_units": [[.2] * 72 for _ in range(25)],
                "residual_scene_units": [[.001] * 72 for _ in range(25)]},
            "error": None, "warnings": []}
        content, structured = asyncio.run(self.server.call_tool("mesh.fair_fit", {
            "source": "High", "center": [0, 0, 0], "apply": False}))
        self.assertEqual(structured["data"]["coverage"], .968)
        self.assertEqual(structured["data"]["coefficients"], [.1, .2])
        self.assertIn("artifact", structured["data"])
        self.assertEqual(json.loads(content[0].text)["coverage"], .968)

    def test_expected_tools_are_published(self) -> None:
        self.assertEqual(
            set(self.tools),
            {
                "asset.quality_gate",
                "asset.compare_to_source",
                "bake.from_ai_source",
                "export.game_asset",
                "mesh.build_lathe",
                "mesh.build_plate",
                "mesh.build_slab",
                "mesh.build_sweep",
                "mesh.clear_of",
                "mesh.fair_fit",
                "uv.audit",
                "uv.seams_from_regions",
                "uv.cut_strips_by_turn",
                "scene.inspect",
                "annotation.inspect",
                "scene.screenshot",
                "scene.fast_capture",
                "assembly.analyze",
                "creature.graph_set",
                "creature.graph_inspect",
                "creature.graph_suggest",
                "creature.landmark_set",
                "creature.base_from_graph",
                "creature.bake_from_source",
                "mesh.topology_report",
                "mesh.wrap_to_target",
                "mesh.compare_to_points",
                "pointcloud.section",
                "source.inspect",
                "source.prepare",
                "reference.crop_grid",
                "armature.from_body_graph",
                "armature.validate_against_reference",
                "armature.export_ue_fbx",
                "skin.weights_limit",
                "skin.weights_transfer_by_index",
                "rig.rom_test",
                "session.instances",
                "session.call_log_summary",
                "operation.start",
                "operation.status",
                "response.artifact_get",
                "python.exec",
                "api.lookup",
                "python.module_put",
                "python.module_list",
                "python.module_remove",
                "checkpoint.save",
                "checkpoint.list",
                "checkpoint.restore",
                "session.whoami",
                "session.acquire_lease",
                "session.renew_lease",
                "session.release_lease",
                "render.preview",
                "bake.run",
                "render.review_set",
                "render.detail",
                "render.ortho_set",
                "render.reference_match",
                "camera.calibrate_from_reference",
                "render.contour_compare",
                "render.contact_sheet",
                "render.background",
                "render.background_status",
                "render.background_cancel",
                "render.compare",
                "render.manifest",
                "render.manifest_validate",
                "material.inspect",
                "material.create",
                "material.patch",
                "image.inspect",
                "image.load",
                "image.create",
                "image.patch",
                "material.slot_inspect",
                "material.slot_assign",
                "material.slot_remove",
                "material.texture_bind",
                "scene.save_version",
                "scene.save_current",
                "scene.save_as",
                "system.capabilities",
                "system.doctor",
                "workflow.run",
                "workflow.template",
                "workflow.batch_plan",
                "workflow.job_start",
                "workflow.job_control",
                "adapter.schema_audit",
                "toolsets.list",
                "toolsets.describe",
                "toolsets.call",
                "collection.inspect",
                "collection.create",
                "collection.rename",
                "collection.set_visibility",
                "collection.link_objects",
                "collection.unlink_objects",
                "collection.parent_objects",
                "collection.transform",
                "collection.link_child",
                "collection.unlink_child",
                "collection.delete",
                "camera.inspect",
                "camera.patch",
                "camera.set_active",
                "camera.import_photogrammetry",
                "camera.import_colmap_package",
                "light.inspect",
                "light.patch",
                "world.inspect",
                "world.create",
                "world.patch",
                "world.set_active",
                "render.settings",
                "compositor.inspect",
                "compositor.create",
                "compositor.patch",
                "interchange.inspect",
                "interchange.import",
                "interchange.export",
                "interchange.blend_import",
                "armature.inspect",
                "armature.create",
                "armature.edit",
                "armature.bone_remove",
                "armature.validate",
                "pose.inspect",
                "pose.patch",
                "pose.constraint_patch",
                "animation.inspect",
                "animation.action_create",
                "animation.action_assign",
                "animation.action_retarget",
                "animation.action_remove",
                "animation.keyframe_edit",
                "animation.pose_bake",
                "animation.validate",
                "animation.nla_inspect",
                "animation.nla_patch",
                "animation.nla_remove",
                "animation.driver_inspect",
                "animation.driver_create",
                "animation.driver_remove",
                "animation.shape_key_inspect",
                "animation.shape_key_create",
                "animation.shape_key_patch",
                "animation.shape_key_remove",
                "skin.inspect",
                "skin.bind",
                "skin.unbind",
                "skin.auto_weights",
                "skin.weights_patch",
                "skin.weights_normalize",
                "skin.group_remove",
                "skin.validate",
                "skin.audit",
                "mesh.inspect",
                "mesh.measure",
                "mesh.edit",
                "mesh.join",
                "mesh.separate",
                "mesh.goal_deform",
                "mesh.fit_contours",
                "architecture.wall_build",
                "curve.inspect",
                "curve.create",
                "curve.patch",
                "curve.spline_add",
                "curve.spline_patch",
                "curve.spline_remove",
                "geometry_nodes.inspect",
                "geometry_nodes.create",
                "geometry_nodes.patch",
                "geometry_nodes.modifier_inputs",
                "uv.inspect",
                "uv.layer_create",
                "uv.layer_rename",
                "uv.layer_set_active",
                "uv.layer_remove",
                "uv.project",
                "uv.transform",
                "uv.unwrap",
                "uv.pack",
                "modifier.inspect",
                "modifier.add",
                "modifier.patch",
                "modifier.reorder",
                "modifier.remove",
                "modifier.apply",
                "object.inspect",
                "object.create",
                "object.convert_evaluated",
                "object.duplicate",
                "object.rename",
                "object.set_visibility",
                "object.set_selection",
                "object.parent",
                "object.mode_set",
                "object.align_to_workplane",
                "object.transform",
                "object.delete",
            },
        )

    def test_safety_and_resource_bounds_are_in_mcp_schemas(self) -> None:
        inspect_properties = self.tools["scene.inspect"]["inputSchema"]["properties"]
        self.assertEqual(inspect_properties["limit"]["minimum"], 1)
        self.assertEqual(inspect_properties["limit"]["maximum"], 2000)
        self.assertEqual(inspect_properties["cursor"]["anyOf"][0]["pattern"], "^[0-9]{1,18}$")

        annotation_properties = self.tools["annotation.inspect"]["inputSchema"]["properties"]
        self.assertEqual(annotation_properties["limit"]["minimum"], 1)
        self.assertEqual(annotation_properties["limit"]["maximum"], 2000)
        self.assertEqual(
            annotation_properties["cursor"]["anyOf"][0]["pattern"],
            "^[0-9]{1,18}$",
        )
        self.assertEqual(
            annotation_properties["frame_number"]["anyOf"][0]["minimum"],
            -1_048_574,
        )
        self.assertEqual(
            annotation_properties["frame_number"]["anyOf"][0]["maximum"],
            1_048_574,
        )
        self.assertEqual(
            annotation_properties["stroke_index"]["anyOf"][0]["minimum"], 0
        )
        self.assertEqual(
            annotation_properties["stroke_index"]["anyOf"][0]["maximum"],
            100_000_000,
        )

        screenshot_properties = self.tools["scene.screenshot"]["inputSchema"]["properties"]
        self.assertEqual(screenshot_properties["width"]["anyOf"][0]["maximum"], 16384)
        self.assertFalse(screenshot_properties["overwrite"]["default"])
        view_schema = self.tools["scene.screenshot"]["inputSchema"]["$defs"]["ScreenshotView"]
        self.assertFalse(view_schema["additionalProperties"])
        self.assertEqual(view_schema["properties"]["mode"]["enum"], ["current", "orbit"])
        self.assertEqual(
            view_schema["properties"]["elevation_degrees"]["maximum"], 89.9
        )

        restore_properties = self.tools["checkpoint.restore"]["inputSchema"]["properties"]
        self.assertIs(restore_properties["confirm"]["const"], True)
        self.assertEqual(
            set(self.tools["checkpoint.restore"]["inputSchema"]["required"]), {"name", "confirm"}
        )
        self.assertIn("expected_state", restore_properties)

        instances_schema = self.tools["session.instances"]["inputSchema"]
        self.assertEqual(instances_schema.get("properties"), {})
        whoami_schema = self.tools["session.whoami"]["inputSchema"]
        self.assertEqual(set(whoami_schema["properties"]), {"instance"})

        workflow_properties = self.tools["workflow.run"]["inputSchema"]["properties"]
        self.assertEqual(workflow_properties["steps"]["maxItems"], 32)
        self.assertEqual(workflow_properties["idempotency_key"]["anyOf"][0]["maxLength"], 128)
        self.assertEqual(
            workflow_properties["idempotency_key"]["anyOf"][0]["pattern"],
            "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        )

        python_schema = self.tools["python.exec"]["inputSchema"]
        self.assertEqual(python_schema["required"], ["code"])
        self.assertEqual(python_schema["properties"]["expected_state"]["anyOf"][0]["minLength"], 1)
        self.assertEqual(python_schema["properties"]["diff"]["enum"], ["none", "summary", "detailed"])
        self.assertEqual(python_schema["properties"]["timeout_hint_s"]["anyOf"][0]["exclusiveMinimum"], 0)
        self.assertEqual(python_schema["properties"]["fragment_small_ratio"]["maximum"], 1)
        api_schema = self.tools["api.lookup"]["inputSchema"]
        self.assertEqual(api_schema["properties"]["detail"]["enum"], ["summary", "full"])
        self.assertEqual(api_schema["properties"]["limit"]["maximum"], 100)

        template_schema = self.tools["workflow.template"]["inputSchema"]
        self.assertEqual(
            template_schema["properties"]["action"]["enum"],
            ["list", "describe", "expand"],
        )
        batch_schema = self.tools["workflow.batch_plan"]["inputSchema"]
        self.assertEqual(batch_schema["properties"]["jobs"]["maxItems"], 32)
        self.assertEqual(
            batch_schema["properties"]["scheduling"]["enum"],
            ["serial", "parallel"],
        )
        job_start_schema = self.tools["workflow.job_start"]["inputSchema"]
        self.assertEqual(set(job_start_schema["required"]), {"steps", "instance"})
        self.assertEqual(job_start_schema["properties"]["step_budget"]["maximum"], 4)
        job_control_schema = self.tools["workflow.job_control"]["inputSchema"]
        self.assertEqual(
            job_control_schema["properties"]["action"]["enum"],
            ["status", "resume", "cancel"],
        )
        artifact_schema = self.tools["response.artifact_get"]["inputSchema"]
        self.assertEqual(
            artifact_schema["properties"]["max_chars"]["maximum"],
            16_000,
        )

        for name, tool in self.tools.items():
            if name not in {
                "session.instances",
                "workflow.batch_plan",
                "workflow.job_control",
                "response.artifact_get",
            }:
                self.assertIn("instance", tool["inputSchema"]["properties"])

        wall_schema = self.tools["architecture.wall_build"]["inputSchema"]
        self.assertEqual(wall_schema["properties"]["wall_lines"]["maxItems"], 256)
        self.assertEqual(
            wall_schema["$defs"]["ArchitectureWallLine"]["properties"]["openings"]["maxItems"],
            32,
        )

        acquire_properties = self.tools["session.acquire_lease"]["inputSchema"]["properties"]
        self.assertEqual(acquire_properties["scope"]["const"], "document")
        self.assertEqual(acquire_properties["ttl"]["minimum"], 5)
        self.assertEqual(acquire_properties["ttl"]["maximum"], 3600)
        self.assertEqual(acquire_properties["ttl"]["default"], 300)
        self.assertEqual(
            set(self.tools["session.renew_lease"]["inputSchema"]["required"]),
            {"lease_id"},
        )
        self.assertEqual(
            set(self.tools["session.release_lease"]["inputSchema"]["required"]),
            {"lease_id"},
        )

        render_properties = self.tools["render.preview"]["inputSchema"]["properties"]
        self.assertEqual(render_properties["width"]["minimum"], 1)
        self.assertEqual(render_properties["width"]["maximum"], 16384)
        self.assertEqual(render_properties["height"]["maximum"], 16384)

        review_properties = self.tools["render.review_set"]["inputSchema"]["properties"]
        self.assertEqual(review_properties["views"]["anyOf"][0]["minItems"], 1)
        self.assertEqual(
            set(review_properties["views"]["anyOf"][0]["items"]["enum"]),
            {"camera", "top", "side", "three_quarter"},
        )
        self.assertEqual(review_properties["width"]["maximum"], 16384)
        self.assertIn("output_dir", review_properties)
        self.assertIn("output_prefix", review_properties)
        self.assertIn("hide", review_properties)
        self.assertIn("show_only", review_properties)

        detail = self.tools["render.detail"]["inputSchema"]["properties"]
        self.assertEqual(detail["projection"]["default"], "perspective")
        self.assertEqual(detail["width"]["maximum"], 16384)
        self.assertIn("bounds", detail)
        self.assertEqual(self.tools["render.reference_match"]["inputSchema"]["required"], ["reference_path"])
        sheet = self.tools["render.contact_sheet"]["inputSchema"]
        self.assertEqual(set(sheet["required"]), {"images", "output_path"})
        self.assertEqual(sheet["properties"]["images"]["maxItems"], 16)

        compare_schema = self.tools["render.compare"]["inputSchema"]
        self.assertEqual(set(compare_schema["required"]), {"image_a", "image_b"})
        compare_properties = compare_schema["properties"]
        self.assertEqual(compare_properties["threshold"]["minimum"], 0)
        self.assertEqual(compare_properties["threshold"]["maximum"], 255)
        self.assertFalse(compare_properties["write_diff"]["default"])
        self.assertFalse(compare_properties["overwrite"]["default"])

        material_inspect = self.tools["material.inspect"]["inputSchema"]["properties"]
        self.assertEqual(material_inspect["limit"]["maximum"], 2000)

        material_create = self.tools["material.create"]["inputSchema"]
        self.assertEqual(set(material_create["required"]), {"name"})
        self.assertTrue(material_create["properties"]["use_nodes"]["default"])
        self.assertFalse(material_create["properties"]["use_fake_user"]["default"])
        self.assertIn("expected_state", material_create["properties"])

        material_patch = self.tools["material.patch"]["inputSchema"]
        self.assertEqual(set(material_patch["required"]), {"material", "operations"})
        self.assertTrue(material_patch["properties"]["dry_run"]["default"])
        self.assertEqual(material_patch["properties"]["operations"]["minItems"], 1)
        self.assertIn("add_node", material_patch["properties"]["operations"]["description"])
        self.assertIn("expected_state", material_patch["properties"])
        self.assertIn(
            "expected_state",
            self.tools["scene.save_version"]["inputSchema"]["properties"],
        )
        object_inspect = self.tools["object.inspect"]["inputSchema"]["properties"]
        self.assertEqual(object_inspect["limit"]["maximum"], 2000)
        self.assertIn("name_regex", object_inspect)
        create_schema = self.tools["object.create"]["inputSchema"]
        self.assertEqual(set(create_schema["required"]), {"type"})
        self.assertIn("CUBE", create_schema["properties"]["type"]["enum"])
        for vector_name in ("location", "rotation", "scale"):
            vector_schema = create_schema["properties"][vector_name]["anyOf"][0]
            self.assertEqual(vector_schema["type"], "array")
            self.assertEqual(vector_schema["minItems"], 3)
            self.assertEqual(vector_schema["maxItems"], 3)
            self.assertEqual(
                [item["type"] for item in vector_schema["prefixItems"]],
                ["number", "number", "number"],
            )
        transform_schema = self.tools["object.transform"]["inputSchema"]
        for vector_name in ("location", "rotation_euler", "scale"):
            vector_schema = transform_schema["properties"][vector_name]["anyOf"][0]
            self.assertEqual(
                [item["type"] for item in vector_schema["prefixItems"]],
                ["number", "number", "number"],
            )
        parent_schema = self.tools["object.parent"]["inputSchema"]
        self.assertEqual(set(parent_schema["required"]), {"object_name", "parent"})
        self.assertTrue(parent_schema["properties"]["dry_run"]["default"])
        workplane_schema = self.tools["object.align_to_workplane"]["inputSchema"]
        self.assertEqual(
            set(workplane_schema["required"]),
            {"object_name", "reference_object", "plane"},
        )
        self.assertTrue(workplane_schema["properties"]["dry_run"]["default"])
        self.assertEqual(workplane_schema["properties"]["offset"]["default"], [0.0, 0.0, 0.0])
        self.assertEqual(workplane_schema["properties"]["axis_map"]["minItems"], 3)
        self.assertEqual(workplane_schema["properties"]["axis_map"]["maxItems"], 3)
        delete_schema = self.tools["object.delete"]["inputSchema"]
        self.assertEqual(set(delete_schema["required"]), {"objects"})
        self.assertTrue(delete_schema["properties"]["dry_run"]["default"])
        self.assertIs(delete_schema["properties"]["confirm"]["anyOf"][0]["const"], True)
        modifier_type = self.tools["modifier.add"]["inputSchema"]["properties"]["type"]
        self.assertEqual(modifier_type["type"], "string")
        self.assertEqual(modifier_type["minLength"], 1)
        auto_weights = self.tools["skin.auto_weights"]["inputSchema"]
        self.assertEqual(
            set(auto_weights["required"]), {"object_name", "armature_name"}
        )
        auto_properties = auto_weights["properties"]
        self.assertTrue(auto_properties["dry_run"]["default"])
        self.assertTrue(auto_properties["require_all_weighted"]["default"])
        self.assertEqual(auto_properties["max_weight_entries"]["maximum"], 5_000_000)
        self.assertEqual(auto_properties["max_candidate_pairs"]["maximum"], 100_000_000)
        self.assertIs(auto_properties["confirm"]["anyOf"][0]["const"], True)
        constraint_patch = self.tools["pose.constraint_patch"]["inputSchema"]
        self.assertIs(
            constraint_patch["properties"]["confirm"]["anyOf"][0]["const"], True
        )

    def test_server_instructions_describe_routing_and_visual_checks_concisely(self) -> None:
        instructions = self.server.instructions
        self.assertIn("one selected Blender instance", instructions)
        self.assertIn("python.exec covers custom and bulk", instructions)
        self.assertIn("inspect its operation and scene state", instructions)
        self.assertIn("frequent visual checks", instructions)
        self.assertNotIn("only when explicitly requested", instructions)
        self.assertNotIn("avoid intermediate captures", instructions)
        self.assertLessEqual(len(instructions.encode("utf-8")), 1200)

    def test_shared_tool_descriptions_match_addon(self) -> None:
        addon_dir = Path(__file__).resolve().parents[2] / "addon"
        with patch.object(sys, "path", [str(addon_dir), *sys.path]):
            from blenderboo import tool_specs

            addon = {item["name"]: item for item in tool_specs()}
        for name in (
            "python.exec", "architecture.wall_build", "collection.transform",
            "collection.parent_objects", "interchange.blend_import", "object.mode_set",
            "scene.save_current", "assembly.analyze", "scene.fast_capture",
            "render.background", "render.background_status", "render.background_cancel",
            "system.doctor", "mesh.join", "mesh.build_slab", "render.detail",
            "animation.action_retarget", "scene.save_current",
            "armature.export_ue_fbx", "skin.audit",
            "mesh.goal_deform", "mesh.fit_contours",
        ):
            with self.subTest(tool=name):
                self.assertEqual(self.tools[name]["description"], addon[name]["description"])

    def test_doctor_reports_selected_response_mode(self) -> None:
        self.proxy.responses["system_doctor"] = {
            "ok": True,
            "tool": "system.doctor",
            "data": {},
            "error": None,
            "warnings": [],
        }

        _, structured = asyncio.run(self.server.call_tool("system.doctor", {}))

        self.assertEqual(structured["data"]["adapter"]["response_mode"], "default")

    def test_doctor_reports_concrete_adapter_addon_version_mismatch(self) -> None:
        self.proxy.responses["system_doctor"] = {
            "ok": True,
            "tool": "system.doctor",
            "data": {"healthy": True, "problems": [], "addon_version": "0.6.1"},
            "error": None,
            "warnings": [],
        }
        _, structured = asyncio.run(self.server.call_tool("system.doctor", {}))
        self.assertFalse(structured["data"]["healthy"])
        self.assertIn("versions differ", structured["data"]["problems"][0])
        self.assertEqual(structured["data"]["adapter"]["version"], "0.10.0")

    def test_large_default_read_result_is_inline(self) -> None:
        expected = {
            "ok": True,
            "tool": "scene.inspect",
            "data": {"objects": "x" * 20_000, "state_token": "state-a"},
            "error": None,
            "warnings": [],
        }
        self.proxy.responses["scene_inspect"] = expected

        content, structured = asyncio.run(self.server.call_tool("scene.inspect", {}))

        self.assertTrue(structured["ok"])
        self.assertEqual(structured["tool"], "scene.inspect")
        self.assertEqual(structured["data"]["state_token"], "state-a")
        self.assertEqual(structured, expected)
        text_items = [item for item in content if isinstance(item, TextContent)]
        self.assertEqual(len(text_items), 1)
        self.assertNotIn("x" * 100, text_items[0].text)


    def test_full_response_mode_preserves_large_structured_content(self) -> None:
        expected = {
            "ok": True,
            "tool": "scene.inspect",
            "data": {"objects": "x" * 20_000, "state_token": "state-a"},
            "error": None,
            "warnings": [],
        }
        self.proxy.responses["scene_inspect"] = expected
        server = create_server(self.proxy, response_profile="full")

        content, structured = asyncio.run(server.call_tool("scene.inspect", {}))

        self.assertEqual(structured, expected)
        text_items = [item for item in content if isinstance(item, TextContent)]
        self.assertEqual(len(text_items), 1)
        summary = json.loads(text_items[0].text)
        self.assertEqual(summary["tool"], "scene.inspect")
        self.assertNotIn("objects", text_items[0].text)

    def test_full_text_channel_can_restore_compatibility_mirror(self) -> None:
        expected = {"ok": True, "tool": "scene.inspect", "data": {"state_token": "state-a"}, "error": None, "warnings": []}
        self.proxy.responses["scene_inspect"] = expected
        with patch.dict("os.environ", {"CBT_MCP_TEXT_CHANNEL": "full"}):
            server = create_server(self.proxy, response_profile="full")
        content, structured = asyncio.run(server.call_tool("scene.inspect", {}))
        self.assertEqual(structured, expected)
        self.assertEqual(json.loads(content[0].text), expected)

    def test_python_exec_builds_wrapper_arguments_without_nulls(self) -> None:
        _, structured = asyncio.run(self.server.call_tool("python.exec", {"code": "result = 2 + 3"}))
        self.assertTrue(structured["ok"])
        self.assertEqual(
            self.proxy.calls[-1],
            ("call_named", ("python.exec", {
                "code": "result = 2 + 3", "checkpoint": False, "diff": "summary",
                "collapse_face_ratio": 0.25, "collapse_volume_ratio": 0.25,
                "bbox_jump_ratio": 2, "fragment_small_ratio": 0.01,
            }, None), {}),
        )
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("python.exec", {"code": "pass", "timeout_hint_s": 0}))

    def test_compact_response_mode_compacts_small_result(self) -> None:
        expected = {
            "ok": True,
            "tool": "scene.inspect",
            "data": {"state_token": "state-a", "objects": []},
            "error": None,
            "warnings": [],
        }
        self.proxy.responses["scene_inspect"] = expected
        server = create_server(self.proxy, response_profile="compact")

        _, structured = asyncio.run(server.call_tool("scene.inspect", {}))

        self.assertEqual(structured["data"]["state_token"], "state-a")
        self.assertNotIn("objects", structured["data"])
        self.assertNotIn("response_profile", structured["data"])

    def test_toolsets_call_preserves_save_disk_evidence_in_each_profile(self) -> None:
        self.proxy.responses["call_named"] = {
            "ok": True,
            "tool": "scene.save_current",
            "data": {
                "changed": True,
                "write_confirmed": True,
                "after": {"disk_exists": True, "disk_size": 4096, "disk_mtime_ns": 123456789},
                "journal": [{"op": "save_current", "status": "applied"}],
            },
            "error": None,
            "warnings": [],
        }
        for mode in ("full", "default", "compact"):
            with self.subTest(mode=mode):
                server = create_server(self.proxy, response_profile=mode)
                result = asyncio.run(server.call_tool("toolsets.call", {
                    "tool": "scene.save_current", "arguments": {},
                }))
                self.assertIsInstance(result, CallToolResult)
                structured = result.structuredContent
                receipt = structured if mode == "default" else structured["data"]
                self.assertIs(receipt["write_confirmed"], True)
                self.assertEqual(receipt["after"]["disk_size"], 4096)
                self.assertEqual(receipt["after"]["disk_mtime_ns"], 123456789)

    def test_missing_response_artifact_returns_bounded_error(self) -> None:
        _, structured = asyncio.run(
            self.server.call_tool(
                "response.artifact_get",
                {"artifact_id": "response-00000001-000000000000"},
            )
        )

        self.assertFalse(structured["ok"])
        self.assertEqual(structured["error"]["type"], "ResponseArtifactNotFoundError")

    def test_all_eager_schemas_align_with_addon_catalog(self) -> None:
        addon_path = Path(__file__).resolve().parents[2] / "addon"
        sys.path.insert(0, str(addon_path))
        try:
            from blenderboo import tool_specs

            addon_schemas = {
                spec["name"]: spec["input_schema"]
                for spec in tool_specs()
            }
        finally:
            sys.path.remove(str(addon_path))
        mcp_schemas = {
            name: record["inputSchema"]
            for name, record in self.tools.items()
        }
        result = audit_schemas(
            addon_schemas,
            mcp_schemas,
            mcp_only=_MCP_ONLY_TOOLS,
        )
        self.assertTrue(result["aligned"], result["issues"][:20])
        self.assertEqual(result["warning_count"], 0, result["issues"][:20])

    def test_unknown_top_level_fields_are_rejected(self) -> None:
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("system.doctor", {"unexpected": True}))

    def test_open_operation_models_preserve_extra_payload_fields(self) -> None:
        asyncio.run(
            self.server.call_tool(
                "mesh.edit",
                {
                    "object_name": "Mesh",
                    "operations": [{"op": "translate", "vector": [1, 2, 3]}],
                },
            )
        )
        name, args, _kwargs = self.proxy.calls[-1]
        self.assertEqual(name, "mesh_edit")
        self.assertEqual(args[1], [{"op": "translate", "vector": [1, 2, 3]}])

    def test_api_lookup_forwards_bounded_query(self) -> None:
        asyncio.run(self.server.call_tool("api.lookup", {
            "path": "bpy.types.Scene.node_tree", "detail": "full", "limit": 5,
        }))
        name, args, _kwargs = self.proxy.calls[-1]
        self.assertEqual(name, "call_named")
        self.assertEqual(args[0], "api.lookup")
        self.assertEqual(args[1]["path"], "bpy.types.Scene.node_tree")
        self.assertEqual(args[1]["limit"], 5)
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("api.lookup", {
                "path": "Scene", "search": "Mesh",
            }))

    def test_mesh_measure_preserves_typed_query_payloads(self) -> None:
        asyncio.run(
            self.server.call_tool(
                "mesh.measure",
                {
                    "object_name": "Mesh",
                    "queries": [
                        {
                            "type": "EDGE_LENGTH",
                            "space": "WORLD",
                            "predicate": {"kind": "BOUNDARY"},
                        }
                    ],
                },
            )
        )
        name, args, _kwargs = self.proxy.calls[-1]
        self.assertEqual(name, "mesh_measure")
        self.assertEqual(
            args[1],
            [
                {
                    "type": "EDGE_LENGTH",
                    "space": "WORLD",
                    "predicate": {"kind": "BOUNDARY"},
                }
            ],
        )

    def test_mesh_separate_preserves_semantic_selection(self) -> None:
        asyncio.run(
            self.server.call_tool(
                "mesh.separate",
                {
                    "object_name": "Mesh",
                    "mode": "SELECTION",
                    "selection": {
                        "domain": "FACE",
                        "predicate": {"kind": "MATERIAL", "material_index": 2},
                    },
                },
            )
        )
        name, args, _kwargs = self.proxy.calls[-1]
        self.assertEqual(name, "mesh_separate")
        self.assertEqual(
            args[2],
            {
                "domain": "FACE",
                "predicate": {"kind": "MATERIAL", "material_index": 2},
            },
        )

    def test_call_routes_through_typed_proxy(self) -> None:
        result = asyncio.run(self.server.call_tool("scene.inspect", {"include_names": True, "limit": 10}))
        structured = next(item for item in result if isinstance(item, dict))
        self.assertTrue(structured["ok"])
        self.assertEqual(self.proxy.calls[0][0], "scene_inspect")
        self.assertEqual(self.proxy.calls[0][1], (None, True, 10, None, None))
        operations = [{"op": "set_material_flag", "flag": "use_backface_culling", "value": True}]
        result = asyncio.run(
            self.server.call_tool(
                "material.patch",
                {"material": "Leaf", "operations": operations, "expected_state": "state-a"},
            )
        )
        self.assertTrue(any(isinstance(item, dict) and item["ok"] for item in result))
        self.assertEqual(self.proxy.calls[1][0], "material_patch")
        self.assertEqual(
            self.proxy.calls[1][1],
            ("Leaf", operations, None, True, None, "state-a", None),
        )
        result = asyncio.run(
            self.server.call_tool(
                "scene.save_version", {"tag": "repair", "expected_state": "state-b"}
            )
        )
        self.assertTrue(any(isinstance(item, dict) and item["ok"] for item in result))
        self.assertEqual(self.proxy.calls[2][0], "scene_save_version")
        self.assertEqual(self.proxy.calls[2][1], (None, "repair", "state-b", None))

        result = asyncio.run(
            self.server.call_tool(
                "render.review_set",
                {"views": ["top", "side"], "hide": ["Guides"], "width": 640},
            )
        )
        self.assertIsInstance(result, CallToolResult)
        self.assertEqual(self.proxy.calls[3][0], "render_review_set")
        self.assertEqual(
            self.proxy.calls[3][1],
            (
                None,
                None,
                ["top", "side"],
                640,
                540,
                False,
                False,
                ["Guides"],
                None,
                None,
            ),
        )

        result = asyncio.run(
            self.server.call_tool(
                "render.compare",
                {
                    "image_a": "before.png",
                    "image_b": "after.png",
                    "threshold": 12,
                    "write_diff": True,
                    "diff_output_path": "delta.png",
                    "overwrite": True,
                },
            )
        )
        self.assertIsInstance(result, CallToolResult)
        self.assertEqual(self.proxy.calls[4][0], "render_compare")
        self.assertEqual(
            self.proxy.calls[4][1],
            ("before.png", "after.png", 12, True, "delta.png", True, None),
        )

    def test_annotation_inspect_routes_through_proxy(self) -> None:
        asyncio.run(
            self.server.call_tool(
                "annotation.inspect",
                {
                    "annotation_name": "ReviewNotes",
                    "layer_name": "Issues",
                    "frame_number": 12,
                    "stroke_index": 3,
                    "limit": 25,
                    "cursor": "50",
                    "instance": "instance-a",
                },
            )
        )
        self.assertEqual(
            self.proxy.calls,
            [
                (
                    "annotation_inspect",
                    ("ReviewNotes", "Issues", 12, 3, 25, "50", "instance-a"),
                    {},
                )
            ],
        )

    def test_screenshot_routes_through_proxy(self) -> None:
        result = asyncio.run(
            self.server.call_tool("scene.screenshot", {"width": 320, "overwrite": True})
        )
        self.assertIsInstance(result, CallToolResult)
        self.assertEqual(
            self.proxy.calls,
            [("scene_screenshot", (None, True, 320, None, None, None), {})],
        )

        result = asyncio.run(
            self.server.call_tool(
                "scene.screenshot",
                {
                    "view": {
                        "mode": "orbit",
                        "object_name": "Shrub",
                        "azimuth_degrees": 135,
                    }
                },
            )
        )
        self.assertIsInstance(result, CallToolResult)
        self.assertEqual(
            self.proxy.calls[1],
            (
                "scene_screenshot",
                (
                    None,
                    False,
                    None,
                    None,
                    None,
                    {"mode": "orbit", "object_name": "Shrub", "azimuth_degrees": 135.0},
                ),
                {},
            ),
        )

        asyncio.run(
            self.server.call_tool(
                "session.acquire_lease", {"scope": "document", "ttl": 60}
            )
        )
        asyncio.run(
            self.server.call_tool(
                "session.renew_lease", {"lease_id": "lease-a", "ttl": 120}
            )
        )
        asyncio.run(
            self.server.call_tool("session.release_lease", {"lease_id": "lease-a"})
        )
        self.assertEqual(self.proxy.calls[2][0], "session_acquire_lease")
        self.assertEqual(self.proxy.calls[2][1], ("document", 60, None))
        self.assertEqual(self.proxy.calls[3][0], "session_renew_lease")
        self.assertEqual(self.proxy.calls[3][1], ("lease-a", 120, None))
        self.assertEqual(self.proxy.calls[4][0], "session_release_lease")
        self.assertEqual(self.proxy.calls[4][1], ("lease-a", None))

    def test_object_tools_route_through_typed_proxy(self) -> None:
        calls = [
            ("object.inspect", {"name": "Hero", "type": "MESH", "limit": 5}),
            ("object.create", {"type": "CUBE", "name": "Hero", "location": [1, 2, 3]}),
            ("object.duplicate", {"object_name": "Hero", "linked": True}),
            ("object.rename", {"object_name": "Hero", "name": "HeroRenamed"}),
            ("object.set_visibility", {"objects": ["Hero", "Copy"], "hide_render": True}),
            ("object.parent", {"object_name": "Copy", "parent": "Hero", "dry_run": False}),
            (
                "object.align_to_workplane",
                {
                    "object_name": "Copy",
                    "reference_object": "Hero",
                    "plane": {"source": "FACE", "face_index": 0, "edge_index": 0},
                },
            ),
            ("object.transform", {"object_name": "Hero", "location": [3, 2, 1], "space": "world"}),
            ("object.delete", {"objects": "Copy", "dry_run": False, "confirm": True}),
        ]
        for tool, params in calls:
            result = asyncio.run(self.server.call_tool(tool, params))
            self.assertTrue(any(isinstance(item, dict) and item["ok"] for item in result))
        self.assertEqual(
            [item[0] for item in self.proxy.calls],
            [
                "object_inspect", "object_create", "object_duplicate", "object_rename",
                "object_set_visibility", "object_parent", "object_align_to_workplane",
                "object_transform", "object_delete",
            ],
        )
        self.assertEqual(
            self.proxy.calls[0][1],
            ("Hero", None, "MESH", None, None, None, None, None, None, None, None, 5, None, None),
        )
        self.assertEqual(self.proxy.calls[1][1][2], (1.0, 2.0, 3.0))
        self.assertEqual(self.proxy.calls[-1][1], ("Copy", False, True, None, None))

    def test_architecture_wall_build_routes_nested_typed_payload(self) -> None:
        result = asyncio.run(self.server.call_tool(
            "architecture.wall_build",
            {
                "name": "Floor_A",
                "wall_lines": [{
                    "start": [0, 0],
                    "end": [8, 0],
                    "openings": [{
                        "id": "Door_A",
                        "kind": "DOOR",
                        "offset": 2,
                        "width": 1,
                        "height": 2.1,
                    }],
                }],
                "height": 3,
                "thickness": 0.2,
                "material": "M_Wall",
                "dry_run": False,
                "expected_state": "state-a",
                "instance": "instance-a",
            },
        ))

        self.assertTrue(any(isinstance(item, dict) and item["ok"] for item in result))
        name, args, kwargs = self.proxy.calls[-1]
        self.assertEqual(name, "architecture_wall_build")
        self.assertEqual(args[0][0]["start"], (0.0, 0.0))
        self.assertEqual(args[0][0]["openings"][0]["kind"], "DOOR")
        self.assertEqual(args[1:4], (3.0, 0.2, "Floor_A"))
        self.assertEqual(args[6], "M_Wall")
        self.assertFalse(args[8])
        self.assertEqual(args[9:], ("state-a", "instance-a"))

    def test_search_mode_publishes_only_discovery_and_workflow_tools(self) -> None:
        server = create_server(self.proxy, tool_mode="search")
        tools = asyncio.run(server.list_tools())
        self.assertEqual(
            {tool.name for tool in tools},
            {"toolsets.list", "toolsets.describe", "toolsets.call", "workflow.run", "workflow.template", "workflow.batch_plan", "workflow.job_start", "workflow.job_control", "adapter.schema_audit", "response.artifact_get", "session.call_log_summary", "operation.start", "operation.status"},
        )

    def test_workflow_template_expands_locally_without_proxy_calls(self) -> None:
        result = asyncio.run(
            self.server.call_tool(
                "workflow.template",
                {
                    "action": "expand",
                    "name": "animation_health_v1",
                    "parameters": {"object_name": "Rig"},
                    "instance": "instance-a",
                },
            )
        )
        structured = next(item for item in result if isinstance(item, dict))
        self.assertTrue(structured["ok"])
        self.assertFalse(structured["data"]["run_request"]["execute"])
        self.assertEqual(structured["data"]["run_request"]["instance"], "instance-a")
        self.assertFalse(structured["data"]["run_request"]["rollback_on_failure"])
        self.assertEqual(structured["data"]["step_count"], 2)
        self.assertEqual(self.proxy.calls, [])

    def test_workflow_batch_plan_is_local_and_requires_explicit_attribution(self) -> None:
        result = asyncio.run(
            self.server.call_tool(
                "workflow.batch_plan",
                {
                    "jobs": [
                        {
                            "id": "inspect_a",
                            "instance": "instance-a",
                            "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                        }
                    ]
                },
            )
        )
        structured = next(item for item in result if isinstance(item, dict))
        self.assertTrue(structured["ok"])
        self.assertFalse(structured["data"]["execution"]["performed"])
        self.assertEqual(structured["data"]["jobs"][0]["instance"], "instance-a")
        self.assertFalse(structured["data"]["jobs"][0]["run_request"]["execute"])
        self.assertEqual(self.proxy.calls, [])

    def test_workflow_job_plan_is_local_and_reserves_no_state(self) -> None:
        result = asyncio.run(
            self.server.call_tool(
                "workflow.job_start",
                {
                    "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                    "instance": "instance-a",
                },
            )
        )
        structured = next(item for item in result if isinstance(item, dict))
        self.assertTrue(structured["ok"])
        self.assertEqual(structured["data"]["state"], "planned")
        self.assertFalse(structured["data"]["created"])
        self.assertFalse(structured["data"]["executed"])
        self.assertEqual(self.proxy.calls, [])

    def test_explicit_instance_selector_reaches_proxy(self) -> None:
        result = asyncio.run(
            self.server.call_tool("checkpoint.list", {"instance": "instance-a"})
        )
        structured = next(item for item in result if isinstance(item, dict))
        self.assertTrue(structured["ok"])
        self.assertEqual(self.proxy.calls[0][0], "checkpoint_list")
        self.assertEqual(self.proxy.calls[0][1], ("instance-a", None, None, None))

    def test_invalid_bounds_are_rejected_before_proxy(self) -> None:
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("scene.inspect", {"limit": 0}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("render.preview", {"width": 16385}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("render.review_set", {"views": []}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("render.review_set", {"views": ["rear"]}))
        with self.assertRaises(ToolError):
            asyncio.run(
                self.server.call_tool(
                    "render.compare", {"image_a": "a.png", "image_b": "b.png", "threshold": 256}
                )
            )
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("material.patch", {"material": "Leaf", "operations": []}))
        with self.assertRaises(ToolError):
            asyncio.run(
                self.server.call_tool(
                    "material.patch",
                    {"material": "Leaf", "operations": [{}], "expected_state": ""},
                )
            )
        with self.assertRaises(ToolError):
            asyncio.run(
                self.server.call_tool(
                    "checkpoint.restore",
                    {"name": "safe.blend", "confirm": True, "expected_state": ""},
                )
            )
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("scene.save_version", {"expected_state": ""}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("session.acquire_lease", {"ttl": 4}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("session.renew_lease", {"lease_id": ""}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("session.release_lease", {"lease_id": ""}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("object.inspect", {"limit": 0}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("object.create", {"type": "ARBITRARY"}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("object.transform", {"object_name": "A", "location": [1, 2]}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("object.delete", {"objects": []}))
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("object.delete", {"objects": "A", "dry_run": False, "confirm": False}))
        self.assertEqual(self.proxy.calls, [])


class RenderImageContentTests(unittest.TestCase):
    # Valid 1x1 transparent PNG; the adapter intentionally does not decode it.
    TINY_PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+X0Y2AAAAAElFTkSuQmCC"
    )

    def setUp(self) -> None:
        self.proxy = RecordingProxy()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        env_patch = patch.dict("os.environ", {"CBT_CALL_LOG_DIR": self.temp_dir.name})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.server = create_server(self.proxy)
        self.png = Path(self.temp_dir.name) / "tiny.png"
        self.png.write_bytes(self.TINY_PNG)

    @staticmethod
    def _envelope(tool: str, data: dict) -> dict:
        return {"ok": True, "tool": tool, "data": data, "error": None, "warnings": []}

    def test_preview_returns_structured_envelope_and_image_content(self) -> None:
        expected = self._envelope("render.preview", {"path": str(self.png), "width": 1, "height": 1})
        self.proxy.responses["render_preview"] = expected

        result = asyncio.run(self.server.call_tool("render.preview", {}))

        self.assertIsInstance(result, CallToolResult)
        self.assertEqual(result.structuredContent, expected)
        self.assertTrue(any(isinstance(item, TextContent) for item in result.content))
        images = [item for item in result.content if isinstance(item, ImageContent)]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mimeType, "image/png")
        self.assertEqual(base64.b64decode(images[0].data), self.TINY_PNG)

    def test_screenshot_returns_structured_envelope_and_image_content(self) -> None:
        expected = self._envelope("scene.screenshot", {"path": str(self.png), "width": 1, "height": 1})
        self.proxy.responses["scene_screenshot"] = expected
        result = asyncio.run(self.server.call_tool("scene.screenshot", {}))
        self.assertEqual(result.structuredContent, expected)
        self.assertEqual(len([item for item in result.content if isinstance(item, ImageContent)]), 1)

    def test_review_set_returns_one_image_per_manifest_view(self) -> None:
        expected = self._envelope(
            "render.review_set",
            {
                "views": ["top", "side"],
                "manifest": {
                    "top": {"path": str(self.png), "camera": "CBT_Top", "width": 1, "height": 1, "warnings": []},
                    "side": {"path": str(self.png), "camera": "CBT_Side", "width": 1, "height": 1, "warnings": []},
                },
            },
        )
        self.proxy.responses["render_review_set"] = expected

        result = asyncio.run(self.server.call_tool("render.review_set", {}))

        self.assertEqual(result.structuredContent, expected)
        self.assertEqual(
            len([item for item in result.content if isinstance(item, ImageContent)]),
            2,
        )

    def test_detail_review_tools_forward_typed_arguments_and_inline_outputs(self) -> None:
        self.proxy.responses["render_detail"] = self._envelope("render.detail", {"path": str(self.png)})
        detail = asyncio.run(self.server.call_tool("render.detail", {"object_name": "Receiver", "direction": "front"}))
        self.assertEqual(self.proxy.calls[-1][0], "render_detail")
        self.assertEqual(self.proxy.calls[-1][1][0]["object_name"], "Receiver")
        self.assertEqual(len([item for item in detail.content if isinstance(item, ImageContent)]), 1)
        asyncio.run(self.server.call_tool("render.detail", {"bounds": {"min": [-1,-1,0], "max": [1,1,1]},
                                                            "clip_to_bounds": True}))
        self.assertTrue(self.proxy.calls[-1][1][0]["clip_to_bounds"])

        self.proxy.responses["render_ortho_set"] = self._envelope("render.ortho_set", {
            "views": ["front"], "manifest": {"front": {"path": str(self.png)}}})
        ortho = asyncio.run(self.server.call_tool("render.ortho_set", {"point": [0, 0, 0], "radius": 1, "views": ["front"]}))
        self.assertEqual(self.proxy.calls[-1][1][0]["views"], ["front"])
        self.assertEqual(len([item for item in ortho.content if isinstance(item, ImageContent)]), 1)

        self.proxy.responses["render_reference_match"] = self._envelope("render.reference_match", {
            "render_path": str(self.png), "side_by_side_path": str(self.png), "overlay_path": str(self.png)})
        reference = asyncio.run(self.server.call_tool("render.reference_match", {"reference_path": "reference.png"}))
        self.assertEqual(len([item for item in reference.content if isinstance(item, ImageContent)]), 3)

        self.proxy.responses["render_contact_sheet"] = self._envelope("render.contact_sheet", {"path": str(self.png)})
        sheet = asyncio.run(self.server.call_tool("render.contact_sheet", {
            "images": [{"path": str(self.png), "label": "front"}], "output_path": "sheet.png"}))
        self.assertEqual(self.proxy.calls[-1][1][0]["images"], [{"path": str(self.png), "label": "front"}])
        self.assertEqual(len([item for item in sheet.content if isinstance(item, ImageContent)]), 1)

    def test_calibration_and_contour_return_inline_images(self) -> None:
        self.proxy.responses["call_named"] = self._envelope(
            "camera.calibrate_from_reference", {"overlay_path": str(self.png), "rms_px": 0.0})
        points = [{"pixel": [i, i + 1], "world": [i, 0, 0]} for i in range(4)]
        calibration = asyncio.run(self.server.call_tool("camera.calibrate_from_reference", {
            "reference_path": "reference.png", "camera_name": "Camera", "correspondences": points,
        }))
        self.assertEqual(calibration.structuredContent["data"]["rms_px"], 0.0)
        self.assertEqual(len([item for item in calibration.content if isinstance(item, ImageContent)]), 1)
        self.assertEqual(self.proxy.calls[-1][1][0], "camera.calibrate_from_reference")

        self.proxy.responses["call_named"] = self._envelope(
            "render.contour_compare", {"deviation_path": str(self.png), "iou": 1.0})
        contour = asyncio.run(self.server.call_tool("render.contour_compare", {
            "reference_path": "reference.png", "camera_name": "Camera", "object_names": ["Mesh"],
        }))
        self.assertEqual(contour.structuredContent["data"]["iou"], 1.0)
        self.assertEqual(len([item for item in contour.content if isinstance(item, ImageContent)]), 1)
        self.assertEqual(self.proxy.calls[-1][1][0], "render.contour_compare")

    def test_compare_returns_diff_image_when_written(self) -> None:
        expected = self._envelope(
            "render.compare",
            {
                "width": 1,
                "height": 1,
                "total_pixels": 1,
                "changed_pixels": 1,
                "changed_fraction": 1.0,
                "mean_abs_rgb_delta": 12.0,
                "bbox": [0, 0, 0, 0],
                "diff_path": str(self.png),
            },
        )
        self.proxy.responses["render_compare"] = expected

        result = asyncio.run(
            self.server.call_tool(
                "render.compare",
                {"image_a": "before.png", "image_b": "after.png", "write_diff": True},
            )
        )

        self.assertEqual(result.structuredContent, expected)
        self.assertEqual(
            len([item for item in result.content if isinstance(item, ImageContent)]),
            1,
        )

    def test_oversized_png_keeps_path_and_adds_warning(self) -> None:
        oversized = Path(self.temp_dir.name) / "oversized.png"
        with oversized.open("wb") as handle:
            handle.truncate(7 * 1024 * 1024)
        expected = self._envelope("render.preview", {"path": str(oversized)})
        self.proxy.responses["render_preview"] = expected

        result = asyncio.run(self.server.call_tool("render.preview", {}))

        self.assertEqual(result.structuredContent["data"]["path"], str(oversized))
        self.assertFalse(any(isinstance(item, ImageContent) for item in result.content))
        self.assertTrue(
            any("inline cap" in warning and str(oversized) in warning for warning in result.structuredContent["warnings"])
        )
        self.assertEqual(expected["warnings"], [])

    def test_missing_png_keeps_path_and_adds_warning(self) -> None:
        missing = Path(self.temp_dir.name) / "missing.png"
        expected = self._envelope("render.preview", {"path": str(missing)})
        self.proxy.responses["render_preview"] = expected

        result = asyncio.run(self.server.call_tool("render.preview", {}))

        self.assertEqual(result.structuredContent["data"]["path"], str(missing))
        self.assertFalse(any(isinstance(item, ImageContent) for item in result.content))
        self.assertTrue(
            any("file is unavailable" in warning for warning in result.structuredContent["warnings"])
        )


class ProxyRoutingTests(unittest.TestCase):
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def call(self, tool, params, **kwargs):
            if kwargs:
                self.calls.append((tool, params, kwargs))
            else:
                self.calls.append((tool, params))
            return {"ok": True, "tool": tool, "data": {}, "error": None, "warnings": []}

    def test_annotation_proxy_sends_only_selected_hierarchy_fields(self) -> None:
        client = self.Client()
        proxy = ToolkitProxy(client)

        proxy.annotation_inspect(
            annotation_name="ReviewNotes",
            layer_name="Issues",
            frame_number=12,
            stroke_index=3,
            limit=25,
            cursor="50",
            instance="instance-a",
        )

        self.assertEqual(
            client.calls,
            [
                (
                    "annotation.inspect",
                    {
                        "annotation_name": "ReviewNotes",
                        "layer_name": "Issues",
                        "frame_number": 12,
                        "stroke_index": 3,
                        "limit": 25,
                        "cursor": "50",
                    },
                    {"instance": "instance-a"},
                )
            ],
        )

    def test_new_proxy_methods_send_bridge_tool_names_and_params(self) -> None:
        client = self.Client()
        proxy = ToolkitProxy(client)

        proxy.render_review_set(
            output_prefix="repair",
            views=["camera", "three_quarter"],
            show_only=["Shrub"],
        )
        proxy.render_compare(
            "before.png",
            "after.png",
            threshold=4,
            write_diff=True,
            diff_output_path="delta.png",
            overwrite=True,
        )
        proxy.session_whoami()
        proxy.checkpoint_restore("scene_ckpt_001_safe.blend", True, "state-a")
        proxy.session_acquire_lease(ttl=60)
        proxy.session_renew_lease("lease-a")
        proxy.session_release_lease("lease-a")
        proxy.checkpoint_list(instance="instance-a")
        proxy.object_create("CUBE", name="Hero", location=(1, 2, 3), expected_state="state-b")
        proxy.object_parent("HeroCopy", "Hero", dry_run=False)
        proxy.object_delete(["HeroCopy"], dry_run=False, confirm=True)
        proxy.compositor_create(include_viewer=True, dry_run=False, expected_state="state-c")
        proxy.compositor_patch(
            [{"op": "add_node", "node_type": "CompositorNodeGlare", "name": "Bloom"}],
            expected_node_count=2,
        )
        proxy.render_manifest_validate(require_file_output=True)
        proxy.interchange_import("asset.glb", dry_run=False, confirm=True)
        proxy.interchange_export("asset.fbx", format="FBX", scope="scene")
        proxy.armature_create(
            "Rig", bones=[{"name": "Root", "head": [0, 0, 0], "tail": [0, 0, 1]}],
        )
        proxy.armature_edit("Rig", [{"op": "rename", "bone": "Root", "name": "Root_CTRL"}])
        proxy.pose_constraint_patch(
            "Rig", [{"op": "add", "bone": "Root_CTRL", "name": "Track", "type": "DAMPED_TRACK", "settings": {"target": "Target"}}],
            confirm=True,
        )
        proxy.animation_action_create("Rig", "RigAction")
        proxy.animation_keyframe_edit(
            "Rig", [{"op": "set", "target": "pose_bone", "bone": "Root_CTRL", "property": "location", "index": 0, "frame": 1, "value": 0.0, "interpolation": "LINEAR"}],
        )
        proxy.animation_validate("Rig", frame_start=1, frame_end=24)
        proxy.skin_bind("Body", "Rig", preserve_volume=True)
        proxy.skin_auto_weights(
            "Body", "Rig", max_weight_entries=1000,
            max_candidate_pairs=800, dry_run=False, confirm=True,
        )
        proxy.skin_weights_patch(
            "Body", "Root_CTRL", [{"op": "SET", "vertices": [0, 1], "weight": 1.0}],
            expected_vertex_count=8,
        )
        proxy.skin_validate("Body", armature_name="Rig")
        proxy.animation_nla_patch(
            "Rig", [{"op": "add_track", "track": "Main"}],
        )
        proxy.animation_driver_create(
            "Rig", "location", 0, "Control", "LOC_X", multiplier=2.0,
        )
        proxy.animation_shape_key_create("Body", "Basis")
        proxy.animation_shape_key_patch(
            "Body", "Smile", value=0.5, clear_vertex_group=True,
        )
        proxy.armature_validate("Rig", max_bound_meshes=20)
        proxy.object_align_to_workplane(
            "Part",
            "Reference",
            {"source": "FACE", "face_index": 2, "edge_index": 5},
            offset=(1.0, 2.0, 3.0),
            axis_map=["V", "U", "-N"],
            dry_run=False,
            expected_state="state-workplane",
        )

        proxy.animation_action_assign(
            "Rig", "RigAction", dry_run=False, confirm=True,
            expected_state="state-action",
        )
        proxy.skin_weights_normalize(
            "Body", "Rig", max_influences=4, min_weight=0.001,
            max_weight_entries=1000, dry_run=False, confirm=True,
            expected_state="state-weights",
        )

        proxy.animation_action_retarget(
            "Rig", "SourceAction", "RetargetRig", "Retargeted",
            bone_map={"Root": "RootTarget"}, dry_run=False,
            expected_state="state-retarget",
        )
        proxy.animation_pose_bake(
            "Rig", "BakedPose", 1, 24, bones=["Root"], dry_run=False,
            expected_state="state-bake",
        )
        proxy.animation_action_remove(
            "OldAction", dry_run=False, confirm=True, expected_state="state-remove-action",
        )
        proxy.animation_nla_remove(
            "Rig", "Main", strip="Clip", dry_run=False, confirm=True,
            expected_state="state-remove-nla",
        )
        proxy.animation_driver_remove(
            "Rig", "location", 0, dry_run=False, confirm=True,
            expected_state="state-remove-driver",
        )
        proxy.animation_shape_key_remove(
            "Body", "Smile", dry_run=False, confirm=True,
            expected_state="state-remove-shape",
        )
        proxy.skin_unbind(
            "Body", armature_name="Rig", dry_run=False, confirm=True,
            expected_state="state-unbind",
        )
        proxy.skin_group_remove(
            "Body", "Unused", dry_run=False, confirm=True,
            expected_state="state-remove-group",
        )
        proxy.armature_bone_remove(
            "Rig", "Tip", dry_run=False, confirm=True,
            expected_state="state-remove-bone",
        )
        self.assertEqual(client.calls[0][0], "render.review_set")
        self.assertNotIn("output_dir", client.calls[0][1])
        self.assertEqual(client.calls[0][1]["output_prefix"], "repair")
        self.assertEqual(client.calls[0][1]["views"], ["camera", "three_quarter"])
        self.assertEqual(client.calls[0][1]["show_only"], ["Shrub"])
        self.assertEqual(
            client.calls[1],
            (
                "render.compare",
                {
                    "image_a": "before.png",
                    "image_b": "after.png",
                    "threshold": 4,
                    "write_diff": True,
                    "overwrite": True,
                    "diff_output_path": "delta.png",
                },
            ),
        )
        self.assertEqual(client.calls[2], ("session.whoami", {}))
        self.assertEqual(
            client.calls[3],
            (
                "checkpoint.restore",
                {
                    "name": "scene_ckpt_001_safe.blend",
                    "confirm": True,
                    "expected_state": "state-a",
                },
            ),
        )
        self.assertEqual(
            client.calls[4],
            ("session.acquire_lease", {"scope": "document", "ttl": 60}),
        )
        self.assertEqual(
            client.calls[5],
            ("session.renew_lease", {"lease_id": "lease-a"}),
        )
        self.assertEqual(
            client.calls[6],
            ("session.release_lease", {"lease_id": "lease-a"}),
        )
        self.assertEqual(
            client.calls[7],
            ("checkpoint.list", {}, {"instance": "instance-a"}),
        )
        self.assertEqual(
            client.calls[8],
            (
                "object.create",
                {"type": "CUBE", "name": "Hero", "location": [1, 2, 3], "expected_state": "state-b"},
            ),
        )
        self.assertEqual(
            client.calls[9],
            (
                "object.parent",
                {"object_name": "HeroCopy", "parent": "Hero", "keep_transform": True, "dry_run": False},
            ),
        )
        self.assertEqual(
            client.calls[10],
            ("object.delete", {"objects": ["HeroCopy"], "dry_run": False, "confirm": True}),
        )
        self.assertEqual(
            client.calls[11],
            (
                "compositor.create",
                {"include_viewer": True, "dry_run": False, "expected_state": "state-c"},
            ),
        )
        self.assertEqual(
            client.calls[12],
            (
                "compositor.patch",
                {
                    "operations": [{"op": "add_node", "node_type": "CompositorNodeGlare", "name": "Bloom"}],
                    "dry_run": True,
                    "expected_node_count": 2,
                },
            ),
        )
        self.assertEqual(
            client.calls[13],
            (
                "render.manifest_validate",
                {"include_paths": False, "require_composite": True, "require_file_output": True},
            ),
        )
        self.assertEqual(
            client.calls[14],
            (
                "interchange.import",
                {"filepath": "asset.glb", "max_bytes": 2147483648, "dry_run": False, "confirm": True},
            ),
        )
        self.assertEqual(
            client.calls[15],
            (
                "interchange.export",
                {"filepath": "asset.fbx", "scope": "scene", "overwrite": False, "dry_run": True, "format": "FBX"},
            ),
        )
        self.assertEqual(client.calls[16][0], "armature.create")
        self.assertEqual(client.calls[16][1]["bones"][0]["name"], "Root")
        self.assertEqual(client.calls[17][0], "armature.edit")
        self.assertEqual(client.calls[18][0], "pose.constraint_patch")
        self.assertTrue(client.calls[18][1]["confirm"])
        self.assertEqual(client.calls[19][0], "animation.action_create")
        self.assertEqual(client.calls[20][0], "animation.keyframe_edit")
        self.assertEqual(
            client.calls[21],
            ("animation.validate", {"object_name": "Rig", "frame_start": 1, "frame_end": 24}),
        )
        self.assertEqual(client.calls[22][0], "skin.bind")
        self.assertTrue(client.calls[22][1]["preserve_volume"])
        self.assertEqual(client.calls[23][0], "skin.auto_weights")
        self.assertEqual(client.calls[23][1]["max_weight_entries"], 1000)
        self.assertEqual(client.calls[23][1]["max_candidate_pairs"], 800)
        self.assertTrue(client.calls[23][1]["confirm"])
        self.assertEqual(client.calls[24][0], "skin.weights_patch")
        self.assertEqual(client.calls[24][1]["expected_vertex_count"], 8)
        self.assertEqual(client.calls[25][0], "skin.validate")
        self.assertEqual(client.calls[26][0], "animation.nla_patch")
        self.assertEqual(client.calls[27][0], "animation.driver_create")
        self.assertEqual(client.calls[27][1]["multiplier"], 2.0)
        self.assertEqual(client.calls[28][0], "animation.shape_key_create")
        self.assertEqual(client.calls[29][0], "animation.shape_key_patch")
        self.assertTrue(client.calls[29][1]["clear_vertex_group"])
        self.assertEqual(client.calls[30][0], "armature.validate")
        self.assertEqual(client.calls[30][1]["max_bound_meshes"], 20)
        self.assertEqual(
            client.calls[31],
            (
                "object.align_to_workplane",
                {
                    "object_name": "Part",
                    "reference_object": "Reference",
                    "plane": {"source": "FACE", "face_index": 2, "edge_index": 5},
                    "offset": [1.0, 2.0, 3.0],
                    "axis_map": ["V", "U", "-N"],
                    "align_location": True,
                    "align_rotation": True,
                    "dry_run": False,
                    "expected_state": "state-workplane",
                },
            ),
        )

        self.assertEqual(client.calls[32][0], "animation.action_assign")
        self.assertEqual(client.calls[32][1]["action_name"], "RigAction")
        self.assertTrue(client.calls[32][1]["confirm"])
        self.assertEqual(client.calls[33][0], "skin.weights_normalize")
        self.assertEqual(client.calls[33][1]["max_influences"], 4)
        self.assertEqual(client.calls[33][1]["min_weight"], 0.001)
        self.assertEqual(client.calls[33][1]["max_weight_entries"], 1000)
        self.assertTrue(client.calls[33][1]["confirm"])


        self.assertEqual(client.calls[34][0], "animation.action_retarget")
        self.assertEqual(client.calls[34][1]["bone_map"], {"Root": "RootTarget"})
        self.assertEqual(client.calls[34][1]["expected_state"], "state-retarget")
        self.assertEqual(client.calls[35][0], "animation.pose_bake")
        self.assertEqual(client.calls[35][1]["bones"], ["Root"])
        self.assertEqual(client.calls[36][0], "animation.action_remove")
        self.assertTrue(client.calls[36][1]["confirm"])
        self.assertEqual(client.calls[37][0], "animation.nla_remove")
        self.assertEqual(client.calls[37][1]["strip"], "Clip")
        self.assertEqual(client.calls[38][0], "animation.driver_remove")
        self.assertEqual(client.calls[39][0], "animation.shape_key_remove")
        self.assertEqual(client.calls[40][0], "skin.unbind")
        self.assertEqual(client.calls[40][1]["armature_name"], "Rig")
        self.assertEqual(client.calls[41][0], "skin.group_remove")
        self.assertEqual(client.calls[42][0], "armature.bone_remove")
        self.assertEqual(client.calls[42][1]["bone"], "Tip")

class BridgeClientEnvelopeTests(unittest.TestCase):
    def test_workflow_exec_reaches_bridge_and_returns_step_result(self) -> None:
        bridge_result = {
            "ok": True,
            "tool": "python.exec",
            "data": {
                "result": 7,
                "stdout": "",
                "stderr": "",
                "changes": {"objects": {"created": ["Cube"]}},
                "changed": True,
                "state_token": "state-after",
                "checkpoint": None,
            },
            "error": None,
            "warnings": [],
        }

        class FakeBridgeSocket:
            def __init__(self) -> None:
                self.sent = b""
                self.response = b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def sendall(self, payload):
                self.sent += payload
                request = json.loads(payload)
                reply = ({"ok": True, "tool": "system.doctor", "data": {"addon_version": "0.10.0"},
                          "error": None, "warnings": []} if request["tool"] == "system.doctor" else bridge_result)
                self.response = json.dumps(reply).encode("utf-8") + b"\n"

            def shutdown(self, how):
                pass

            def settimeout(self, timeout):
                pass

            def recv(self, size):
                payload, self.response = self.response[:size], self.response[size:]
                return payload

        fake_socket = FakeBridgeSocket()
        with tempfile.TemporaryDirectory() as log_dir, patch.dict("os.environ", {"CBT_CALL_LOG_DIR": log_dir}), patch("blenderboo_mcp.client.socket.create_connection", return_value=fake_socket), patch("blenderboo_mcp.server._addon_schema_catalog", return_value=({"python.exec": {"type": "object", "properties": {"code": {"type": "string"}, "diff": {"type": "string"}}, "required": ["code"], "additionalProperties": False}}, None)):
            server = create_server(
                ToolkitProxy(BridgeClient("x" * 32, client_id="workflow-exec-test")),
                response_profile="full",
            )
            _, workflow = asyncio.run(server.call_tool("workflow.run", {
                "steps": [{
                    "id": "script",
                    "tool": "python.exec",
                    "arguments": {"code": "bpy.ops.mesh.primitive_cube_add(); result = 7", "diff": "summary"},
                }],
                "execute": True,
            }))

        request = json.loads(fake_socket.sent.splitlines()[-1])
        self.assertEqual(request["tool"], "python.exec")
        self.assertEqual(request["client_id"], "workflow-exec-test")
        self.assertEqual(request["params"]["code"], "bpy.ops.mesh.primitive_cube_add(); result = 7")
        self.assertTrue(workflow["ok"])
        step = workflow["data"]["steps"][0]
        self.assertEqual(step["status"], "completed")
        self.assertEqual(step["result"], bridge_result)

    def test_oversized_request_is_rejected_before_opening_a_socket(self) -> None:
        client = BridgeClient("x" * 32, client_id="stable-mcp-client")
        with patch("blenderboo_mcp.client.socket.create_connection") as connection:
            result = client.call(
                "scene.inspect", {"inline_payload": "x" * MAX_REQUEST_BYTES}
            )
        connection.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["tool"], "scene.inspect")
        self.assertEqual(result["error"]["type"], "BridgeRequestTooLargeError")
        self.assertFalse(result["data"]["execution_started"])
        self.assertTrue(result["data"]["automatic_retry_safe"])
        self.assertEqual(result["data"]["recovery"]["action"], "reduce_request")

    def test_client_id_is_added_to_every_bridge_request_outside_params(self) -> None:
        response = {
            "ok": True,
            "tool": "session.whoami",
            "data": {},
            "error": None,
            "warnings": [],
        }

        class FakeSocket:
            def __init__(self):
                self.sent = b""
                self.response = json.dumps(response).encode("utf-8") + b"\n"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def sendall(self, payload):
                self.sent += payload

            def shutdown(self, how):
                pass

            def settimeout(self, timeout):
                pass

            def recv(self, size):
                payload, self.response = self.response[:size], self.response[size:]
                return payload

        fake_socket = FakeSocket()
        client = BridgeClient("x" * 32, client_id="stable-mcp-client")
        with patch(
            "blenderboo_mcp.client.socket.create_connection",
            return_value=fake_socket,
        ):
            ToolkitProxy(client).session_whoami()

        request = json.loads(fake_socket.sent.decode("utf-8"))
        self.assertEqual(request["client_id"], "stable-mcp-client")
        self.assertNotIn("client_id", request["params"])
        self.assertNotIn(b'": ', fake_socket.sent)


if __name__ == "__main__":
    unittest.main()
