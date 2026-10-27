"""The toolkit's single transport-independent public dispatch entry point."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import InvalidParamsError
from .registry import ToolRegistry


def create_registry() -> ToolRegistry:
    from .tools.session_state import reconcile

    registry = ToolRegistry(before_dispatch=reconcile)

    from .tools.api_lookup import lookup_api
    registry.register(
        "api.lookup", lookup_api, toolset="api",
        description="Inspect the installed Blender Python API by dotted path or paged search without reading or changing the scene.",
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 200},
            "search": {"type": "string", "minLength": 1, "maxLength": 100},
            "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            "cursor": {"type": "string", "pattern": "^[0-9]{1,12}$"},
        }, "oneOf": [{"required": ["path"]}, {"required": ["search"]}],
            "additionalProperties": False},
    )

    from .tools.python_exec import execute_python
    registry.register(
        "python.exec",
        execute_python,
        toolset="python",
        description=(
            "Run unrestricted Python once on Blender's main thread with bpy, bmesh, mathutils, "
            "Vector, Matrix, C, D, and view3d_override. Assign result to return a value; stdout/stderr "
            "are bounded to 8 KiB inline. changes is observational: changed:false means "
            "no_changes_detected. checkpoint saves before execution when the file has a path. "
            "A running script cannot be cancelled. GPU/render calls in the bridge timer can crash "
            "Blender; use deferred capture/render tools. File switches or add-on reloads can "
            "invalidate bridge state. Document-local cbt_modules imports persist across calls; the receipt "
            "lists their source hashes. Store reusable helpers with python.module_put."
            " Check unfamiliar or version-sensitive bpy paths with api.lookup before writing exec code."
            " diff:summary (default) uses depsgraph updates and an ID inventory; diff:none skips detection;"
            " diff:detailed is slow, for targeted forensics. A recognized missing-member error adds api_hint."
            " new_islands flags fragments on changed meshes for review; it is not a failure."
            " A windowless FBX operator-context failure names interchange.import as the typed route."
            " Default MCP responses summarize large change lists with full detail in an artifact."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "expected_state": {"type": "string", "minLength": 1},
                "checkpoint": {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
                "diff": {"type": "string", "enum": ["none", "summary", "detailed"], "default": "summary"},
                "timeout_hint_s": {"type": "number", "exclusiveMinimum": 0},
                "collapse_face_ratio": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1, "default": 0.25},
                "collapse_volume_ratio": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1, "default": 0.25},
                "bbox_jump_ratio": {"type": "number", "exclusiveMinimum": 1, "default": 2},
                "fragment_small_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1, "default": 0.01},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    )

    from .tools.python_modules import module_list, module_put, module_remove
    from .tools.source_intake import inspect as inspect_source, prepare as prepare_source
    from .tools.creature import landmark_set, graph_suggest, graph_set, graph_inspect, base_from_graph, wrap_to_target
    from .tools.mesh_topology import topology_report
    from .tools.pointcloud_review import section as pointcloud_section, compare_to_points
    from .tools.reference_crop import crop_grid as reference_crop_grid

    registry.register(
        "python.module_put", module_put, toolset="python",
        description="Store a Python helper in this .blend. Saved source reappears when the same file is reopened; a different .blend has its own helpers. A path is copied once, not linked. Immutable versions remain for replay.",
        input_schema={"type": "object", "properties": {
            "name": {"type": "string", "minLength": 1},
            "code": {"type": "string"},
            "path": {"type": "string", "minLength": 1},
            "expected_state": {"type": "string", "minLength": 1},
        }, "required": ["name"], "additionalProperties": False},
    )
    registry.register(
        "python.module_list", module_list, toolset="python",
        description="List active document-local Python helpers and their source hashes.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    registry.register(
        "python.module_remove", module_remove, toolset="python",
        description="Remove an active document-local Python helper; retained source versions remain in the .blend.",
        input_schema={"type": "object", "properties": {
            "name": {"type": "string", "minLength": 1},
            "expected_state": {"type": "string", "minLength": 1},
        }, "required": ["name"], "additionalProperties": False},
    )

    from .tools.annotation import inspect_annotation
    from .tools.assembly import analyze_assembly
    from .tools.architecture import build_walls
    from .tools.background_render import (
        background_render_status, cancel_background_render, start_background_render,
    )
    from .tools.bake import run_bake
    from .tools.checkpoint import list_checkpoints, restore_checkpoint, save_checkpoint
    from .tools.collection import (
        create_collection,
        delete_collection,
        inspect_collection,
        link_collection_child,
        link_collection_objects,
        parent_collection_objects,
        rename_collection,
        set_collection_visibility,
        transform_collection_objects,
        unlink_collection_child,
        unlink_collection_objects,
    )
    from .tools.compositor import create_compositor, inspect_compositor, patch_compositor
    from .tools.curve import (
        add_curve_spline,
        create_curve,
        inspect_curve,
        patch_curve,
        patch_curve_spline,
        remove_curve_spline,
    )
    from .tools.geometry import (
        add_modifier,
        apply_modifier,
        convert_evaluated,
        inspect_mesh,
        inspect_modifier,
        patch_modifier,
        remove_modifier,
        reorder_modifier,
    )
    from .tools.geometry_nodes import (
        create_geometry_nodes,
        inspect_geometry_nodes,
        patch_geometry_nodes,
        patch_geometry_nodes_inputs,
    )
    from .tools.interchange import export_interchange, import_blend_data, import_interchange, inspect_interchange
    from .tools.animation import assign_action, bake_pose, create_action, edit_keyframes, inspect_animation, remove_action, retarget_action, validate_animation
    from .tools.animation_advanced import (
        create_driver,
        create_shape_key,
        inspect_drivers,
        inspect_nla,
        inspect_shape_keys,
        patch_nla,
        patch_shape_key,
        remove_driver,
        remove_nla,
        remove_shape_key,
    )
    from .tools.material import create_material, inspect_material, patch_material
    from .tools.lookdev import (
        assign_material_slot,
        bind_material_texture,
        create_image,
        inspect_image,
        inspect_material_slots,
        load_image,
        patch_image,
        remove_material_slot,
    )
    from .tools.mesh_edit import edit_mesh, measure_mesh
    from .tools.mesh_objects import join_meshes as legacy_join_meshes, separate_mesh
    from .tools.mesh_join import join_meshes as named_join_meshes

    def join_meshes(params):
        if "object_names" not in params and "name" not in params:
            return legacy_join_meshes(params)
        named = dict(params)
        for key, default in (("keep_sources", False), ("max_elements", 500000),
                             ("fragment_small_ratio", 0.01), ("confirm", True)):
            if key not in named:
                continue
            if named[key] != default:
                raise InvalidParamsError(f"Legacy mesh.join option {key} conflicts with object_names+name")
            named.pop(key)
        return named_join_meshes(named)
    from .tools.object import (
        align_object_to_workplane,
        create_object,
        delete_object,
        duplicate_object,
        inspect_object,
        parent_object,
        rename_object,
        set_object_selection,
        set_object_visibility,
        set_object_mode,
        transform_object,
    )
    from .tools.render import (
        render_compare, render_contact_sheet, render_detail, render_ortho_set,
        render_preview, render_reference_match, render_review_set,
    )
    from .tools.render_manifest import inspect_render_manifest, validate_render_manifest
    from .tools.reference_review import calibrate_from_reference, contour_compare
    from .tools.photogrammetry_import import import_photogrammetry
    from .tools.colmap_import import import_colmap_package
    from .tools.creature_qa import bake_from_source
    from .tools.rig_rom import rom_test
    from .tools.creature_rig import from_body_graph, validate_against_reference, export_ue_fbx
    from .tools.goal_deform import goal_deform, GOAL_DEFORM_DESCRIPTION, GOAL_DEFORM_SCHEMA
    from .tools.contour_fit import fit_contours, DESCRIPTION as CONTOUR_FIT_DESCRIPTION
    from .tools.rigging import (
        create_armature,
        edit_armature,
        inspect_armature,
        inspect_pose,
        patch_pose,
        patch_pose_constraints,
        remove_bone,
        validate_armature,
    )
    from .tools.scene import fast_capture_scene, inspect_scene, screenshot_scene
    from .tools.session import whoami
    from .tools.session_lease import acquire_lease, release_lease, renew_lease
    from .tools.skinning import automatic_weights, bind_skin, inspect_skin, normalize_weights, patch_weights, remove_vertex_group, unbind_skin, validate_skin, weights_limit, weights_transfer_by_index
    from .tools.skin_audit import audit_skin
    from .tools.stage import (
        create_world,
        inspect_camera,
        inspect_light,
        inspect_world,
        patch_camera,
        patch_light,
        patch_world,
        render_settings,
        set_active_camera,
        set_active_world,
    )
    from .tools.system import capabilities_handler, doctor_handler
    from .tools.uv import (
        audit_uv,
        cut_strips_by_turn,
        create_uv_layer,
        inspect_uv,
        pack_uv,
        project_uv,
        remove_uv_layer,
        rename_uv_layer,
        seams_from_regions,
        set_active_uv_layer,
        transform_uv,
        unwrap_uv,
    )
    from .tools.versioning import save_as, save_current, save_version

    registry.register(
        "scene.inspect",
        inspect_scene,
        description="Inspect a bounded summary of the current Blender scene or one named object.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "description": "Optional object to inspect."},
                "include_names": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include one paginated page of object names and types.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {
                    "type": "string",
                    "pattern": "^[0-9]{1,18}$",
                    "description": "Opaque cursor returned by the previous page.",
                },
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "annotation.inspect",
        inspect_annotation,
        description=(
            "Inspect bounded Annotation datablocks, layers, frames, strokes, and "
            "point coordinates without changing Blender state."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "annotation_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "frame_number": {
                    "type": "integer",
                    "minimum": -1048574,
                    "maximum": 1048574,
                },
                "stroke_index": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000000,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "default": 200,
                },
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "scene.screenshot",
        screenshot_scene,
        description="Capture the current viewport or an independent orbit view as a PNG without changing Blender's visible UI state.",
        input_schema={
            "type": "object",
            "properties": {
                "output_path": {"type": "string", "minLength": 1},
                "overwrite": {"type": "boolean", "default": False},
                "width": {"type": "integer", "minimum": 1, "maximum": 16384},
                "height": {"type": "integer", "minimum": 1, "maximum": 16384},
                "view": {
                    "type": "object",
                    "description": "Current viewport (default) or a non-mutating independent orbit view.",
                    "properties": {
                        "mode": {"type": "string", "enum": ["current", "orbit"], "default": "current"},
                        "object_name": {"type": "string", "minLength": 1},
                        "target": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 3,
                            "maxItems": 3,
                        },
                        "azimuth_degrees": {"type": "number", "default": 45.0},
                        "elevation_degrees": {
                            "type": "number",
                            "minimum": -89.9,
                            "maximum": 89.9,
                            "default": 30.0,
                        },
                        "distance": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "session.whoami",
        whoami,
        description="Report the active Blender process, document identity, state token, and bridge health.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    registry.register(
        "session.acquire_lease",
        acquire_lease,
        description="Acquire a document lease when several clients edit one document; single-client work needs no lease.",
        input_schema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "const": "document", "default": "document"},
                "ttl": {"type": "integer", "minimum": 5, "maximum": 3600, "default": 300},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "session.renew_lease",
        renew_lease,
        description="Renew the document lease held by this MCP client.",
        input_schema={
            "type": "object",
            "properties": {
                "lease_id": {"type": "string", "minLength": 1},
                "ttl": {"type": "integer", "minimum": 5, "maximum": 3600},
            },
            "required": ["lease_id"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "session.release_lease",
        release_lease,
        description="Release the document lease held by this MCP client.",
        input_schema={
            "type": "object",
            "properties": {"lease_id": {"type": "string", "minLength": 1}},
            "required": ["lease_id"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "checkpoint.save",
        save_checkpoint,
        description="Save a checkpoint copy. Fixed filenames require confirm:true to overwrite; retention without an index requires an explicit asset identity.",
        input_schema={
            "type": "object",
            "properties": {
                "tag": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Optional filename-safe checkpoint label.",
                },
                "directory": {"type": "string", "minLength": 1},
                "name_template": {"type": "string", "minLength": 1},
                "asset": {"type": "string", "minLength": 1, "maxLength": 128},
                "keep_latest": {"type": "integer", "minimum": 1, "maximum": 10000},
                "preview_only": {"type": "boolean", "default": False},
                "prune": {"type": "boolean", "default": False},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "checkpoint.list",
        list_checkpoints,
        description="List checkpoints for the current document or an explicit asset identity.",
        input_schema={"type": "object", "properties": {
            "directory": {"type": "string", "minLength": 1},
            "name_template": {"type": "string", "minLength": 1},
            "asset": {"type": "string", "minLength": 1, "maxLength": 128},
        }, "additionalProperties": False},
    )
    registry.register(
        "checkpoint.restore",
        restore_checkpoint,
        description="Open a named checkpoint after explicit confirmation, discarding unsaved state; asset selects fixed-name checkpoint history.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "description": "Checkpoint filename."},
                "directory": {"type": "string", "minLength": 1},
                "name_template": {"type": "string", "minLength": 1},
                "asset": {"type": "string", "minLength": 1, "maxLength": 128},
                "confirm": {
                    "type": "boolean",
                    "const": True,
                    "description": "Must be true because restore discards unsaved state.",
                },
                "expected_state": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional state_token that must still identify the active document state.",
                },
            },
            "required": ["name", "confirm"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "render.preview",
        render_preview,
        description="Render a bounded PNG preview while restoring temporary render and camera state.",
        input_schema={
            "type": "object",
            "properties": {
                "width": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 960},
                "height": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 540},
                "output_path": {"type": "string", "minLength": 1},
                "overwrite": {"type": "boolean", "default": False},
                "allow_large": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow renders above the default 4K pixel budget, up to the hard cap.",
                },
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "render.review_set",
        render_review_set,
        description="Render a fixed multi-view PNG review set with a manifest and restored scene state.",
        input_schema={
            "type": "object",
            "properties": {
                "output_dir": {"type": "string", "minLength": 1},
                "output_prefix": {"type": "string", "minLength": 1},
                "views": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": ["camera", "top", "side", "three_quarter"]},
                    "description": "Optional subset of canonical views; output order is fixed.",
                },
                "width": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 960},
                "height": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 540},
                "overwrite": {"type": "boolean", "default": False},
                "allow_large": {"type": "boolean", "default": False},
                "hide": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Collection names hidden for every view.",
                },
                "show_only": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Render only these collections and their descendants; hide takes precedence.",
                },
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "render.compare",
        render_compare,
        description="Compare equal-size PNGs with RGB difference metrics and an optional diff artifact.",
        input_schema={
            "type": "object",
            "properties": {
                "image_a": {"type": "string", "minLength": 1},
                "image_b": {"type": "string", "minLength": 1},
                "threshold": {"type": "integer", "minimum": 0, "maximum": 255, "default": 8},
                "write_diff": {"type": "boolean", "default": False},
                "diff_output_path": {"type": "string", "minLength": 1},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["image_a", "image_b"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "material.inspect",
        inspect_material,
        description="Inspect bounded material summaries or paginated node graphs without mutating Blender data.",
        input_schema={
            "type": "object",
            "properties": {
                "material": {"type": "string", "minLength": 1},
                "object_name": {"type": "string", "minLength": 1},
                "collection": {"type": "string", "minLength": 1},
                "include_nodes": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "material.create",
        create_material,
        description="Create one independent material datablock with an optional default Principled node tree.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "use_nodes": {"type": "boolean", "default": True},
                "use_fake_user": {"type": "boolean", "default": False},
                "expected_state": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional state_token that must still identify the active document state.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "material.patch",
        patch_material,
        description="Dry-run-first material graph patch using Blender RNA-validated node properties. Apply failures return a partial journal.",
        input_schema={
            "type": "object",
            "properties": {
                "material": {"type": "string", "minLength": 1},
                "operations": {"type": "array", "minItems": 1, "items": {"type": "object"}, "description": "Allowlisted ops: add_node, remove_node, set_input, connect, disconnect, set_material_flag."},
                "preconditions": {"type": "array", "items": {"type": "object"}},
                "dry_run": {"type": "boolean", "default": True},
                "expected_user_count": {"type": "integer", "minimum": 0},
                "expected_state": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional state_token that must still identify the active document state.",
                },
            },
            "required": ["material", "operations"],
            "additionalProperties": False,
        },
    )
    expected_state = {
        "type": "string",
        "minLength": 1,
        "description": "Optional state_token that must still identify the active document state.",
    }
    point2 = {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 2,
        "maxItems": 2,
    }
    architecture_opening = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 63},
            "kind": {
                "type": "string",
                "enum": ["DOOR", "WINDOW", "OPENING"],
                "default": "OPENING",
            },
            "offset": {"type": "number", "minimum": 0},
            "width": {"type": "number", "exclusiveMinimum": 0},
            "bottom": {"type": "number", "minimum": 0, "default": 0},
            "height": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": ["offset", "width", "height"],
        "additionalProperties": False,
    }
    architecture_wall_line = {
        "type": "object",
        "properties": {
            "start": point2,
            "end": point2,
            "openings": {
                "type": "array",
                "maxItems": 32,
                "items": architecture_opening,
            },
        },
        "required": ["start", "end"],
        "additionalProperties": False,
    }
    registry.register(
        "architecture.wall_build",
        build_walls,
        description=(
            "Plan or create up to 256 editable world-XY wall meshes with 512 rectangular "
            "openings. Junctions remain separate objects without mitering or Boolean union."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 63, "default": "Walls"},
                "wall_lines": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 256,
                    "items": architecture_wall_line,
                },
                "height": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000},
                "thickness": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000},
                "base_z": {"type": "number", "minimum": -1000000, "maximum": 1000000, "default": 0},
                "collection": {"type": "string", "minLength": 1, "maxLength": 63},
                "material": {"type": "string", "minLength": 1, "maxLength": 63},
                "max_elements": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["wall_lines", "height", "thickness"],
            "additionalProperties": False,
        },
    )
    vector3 = {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 3,
        "maxItems": 3,
    }
    object_names = {
        "anyOf": [
            {"type": "string", "minLength": 1},
            {
                "type": "array",
                "minItems": 1,
                "maxItems": 2000,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        ]
    }
    registry.register(
        "image.inspect",
        inspect_image,
        description="Inspect bounded image datablock metadata without reading pixel buffers; file paths are hidden unless explicitly requested.",
        input_schema={
            "type": "object",
            "properties": {
                "image": {"type": "string", "minLength": 1},
                "include_filepath": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "image.load",
        load_image,
        description="Load one explicit existing allowlisted image file into Blender without writing or deleting the source file.",
        input_schema={
            "type": "object",
            "properties": {
                "filepath": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "check_existing": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["filepath"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "image.create",
        create_image,
        description="Create a bounded generated image datablock with an explicit pixel budget.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "width": {"type": "integer", "minimum": 1, "maximum": 32768},
                "height": {"type": "integer", "minimum": 1, "maximum": 32768},
                "color": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number", "minimum": 0, "maximum": 1}},
                "alpha": {"type": "boolean", "default": True},
                "float_buffer": {"type": "boolean", "default": False},
                "is_data": {"type": "boolean", "default": False},
                "max_pixels": {"type": "integer", "minimum": 1, "maximum": 268435456, "default": 16777216},
                "expected_state": expected_state,
            },
            "required": ["name", "width", "height"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "image.patch",
        patch_image,
        description="Atomically patch allowlisted image metadata with field-level rollback on failure.",
        input_schema={
            "type": "object",
            "properties": {
                "image": {"type": "string", "minLength": 1},
                "colorspace": {"type": "string", "minLength": 1},
                "alpha_mode": {"type": "string", "enum": ["STRAIGHT", "PREMUL", "CHANNEL_PACKED", "NONE"]},
                "use_fake_user": {"type": "boolean"},
                "expected_state": expected_state,
            },
            "required": ["image"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "material.slot_inspect",
        inspect_material_slots,
        description="Inspect a bounded page of an object's material slots and polygon usage counts.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "material.slot_assign",
        assign_material_slot,
        description="Assign an existing material to one object-data slot or append a slot; shared object data is rejected.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "material": {"type": "string", "minLength": 1},
                "slot_index": {"type": "integer", "minimum": 0, "maximum": 999},
                "expected_state": expected_state,
            },
            "required": ["object_name", "material"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "material.slot_remove",
        remove_material_slot,
        description="Dry-run-first confirmed material-slot removal with bounded polygon snapshot and rollback attempt.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "slot_index": {"type": "integer", "minimum": 0, "maximum": 999},
                "max_polygons": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "slot_index"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "material.texture_bind",
        bind_material_texture,
        description="Dry-run-first atomic image-texture binding with optional UV/mapping chain and confirmation before replacing links.",
        input_schema={
            "type": "object",
            "properties": {
                "material": {"type": "string", "minLength": 1},
                "image": {"type": "string", "minLength": 1},
                "image_node": {"type": "string", "minLength": 1, "default": "CBT Image Texture"},
                "uv_map": {"type": "string", "minLength": 1},
                "mapping": {"type": "object"},
                "color_target": {"type": "object"},
                "alpha_target": {"type": "object"},
                "interpolation": {"type": "string", "enum": ["Closest", "Linear", "Cubic", "Smart"], "default": "Linear"},
                "extension": {"type": "string", "enum": ["REPEAT", "EXTEND", "CLIP"], "default": "REPEAT"},
                "projection": {"type": "string", "enum": ["FLAT", "BOX", "SPHERE", "TUBE"], "default": "FLAT"},
                "replace_existing": {"type": "boolean", "default": False},
                "allow_shared": {"type": "boolean", "default": False},
                "expected_user_count": {"type": "integer", "minimum": 0},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["material", "image"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.inspect",
        inspect_collection,
        description="List a bounded filtered page of scene collections and optionally return one collection's hierarchy and object detail.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "name_regex": {"type": "string", "minLength": 1},
                "parent": {"type": "string", "minLength": 1},
                "hidden_viewport": {"type": "boolean"},
                "hidden_render": {"type": "boolean"},
                "custom_property": {"type": "string", "minLength": 1},
                "custom_value": {
                    "anyOf": [
                        {"type": "string"}, {"type": "number"},
                        {"type": "boolean"},
                    ]
                },
                "collection_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.create",
        create_collection,
        description="Create one collection under the scene root or a named scene collection, resolving name collisions deterministically.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "parent": {"type": "string", "minLength": 1},
                "expected_state": expected_state,
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.rename",
        rename_collection,
        description="Rename one non-root collection exactly, rejecting collisions unless allow_suffix is true.",
        input_schema={
            "type": "object",
            "properties": {
                "collection_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "allow_suffix": {"type": "boolean", "default": False},
                "expected_state": expected_state,
            },
            "required": ["collection_name", "name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.set_visibility",
        set_collection_visibility,
        description="Patch viewport, render, or selection visibility flags for a bounded collection set.",
        input_schema={
            "type": "object",
            "properties": {
                "collections": object_names,
                "hide_viewport": {"type": "boolean"},
                "hide_render": {"type": "boolean"},
                "hide_select": {"type": "boolean"},
                "expected_state": expected_state,
            },
            "required": ["collections"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.link_objects",
        link_collection_objects,
        description="Link one or more existing scene objects directly into a named scene collection.",
        input_schema={
            "type": "object",
            "properties": {
                "collection": {"type": "string", "minLength": 1},
                "objects": object_names,
                "expected_state": expected_state,
            },
            "required": ["collection", "objects"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.unlink_objects",
        unlink_collection_objects,
        description="Dry-run-first removal of direct object links from a collection; apply requires explicit confirmation.",
        input_schema={
            "type": "object",
            "properties": {
                "collection": {"type": "string", "minLength": 1},
                "objects": object_names,
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["collection", "objects"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.parent_objects",
        parent_collection_objects,
        description="Dry-run-first parenting of at most 2,000 direct or recursive collection members in one operation.",
        input_schema={"type": "object", "properties": {"collection": {"type": "string", "minLength": 1}, "parent": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]}, "recursive": {"type": "boolean", "default": False}, "keep_transform": {"type": "boolean", "default": True}, "max_objects": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 2000}, "dry_run": {"type": "boolean", "default": True}, "expected_state": expected_state}, "required": ["collection", "parent"], "additionalProperties": False},
    )
    registry.register(
        "collection.transform",
        transform_collection_objects,
        description=(
            "Dry-run-first world-space transform of at most 2,000 direct or recursive collection "
            "members; refuses scopes that would move external descendants indirectly."
        ),
        input_schema={"type": "object", "properties": {"collection": {"type": "string", "minLength": 1}, "translation": {**vector3, "default": [0, 0, 0]}, "rotation_euler": {**vector3, "default": [0, 0, 0]}, "scale": {**vector3, "default": [1, 1, 1]}, "recursive": {"type": "boolean", "default": False}, "max_objects": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 2000}, "dry_run": {"type": "boolean", "default": True}, "expected_state": expected_state}, "required": ["collection"], "additionalProperties": False},
    )
    registry.register(
        "collection.link_child",
        link_collection_child,
        description="Add a direct parent-child collection link after rejecting hierarchy cycles.",
        input_schema={
            "type": "object",
            "properties": {
                "parent": {"type": "string", "minLength": 1},
                "child": {"type": "string", "minLength": 1},
                "expected_state": expected_state,
            },
            "required": ["parent", "child"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.unlink_child",
        unlink_collection_child,
        description="Dry-run-first removal of a direct collection hierarchy link; apply requires explicit confirmation.",
        input_schema={
            "type": "object",
            "properties": {
                "parent": {"type": "string", "minLength": 1},
                "child": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["parent", "child"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "collection.delete",
        delete_collection,
        description="Dry-run-first collection deletion with shared-use checks; recursive apply requires explicit confirmation.",
        input_schema={
            "type": "object",
            "properties": {
                "collections": object_names,
                "recursive": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["collections"],
            "additionalProperties": False,
        },
    )
    color3 = {
        "type": "array",
        "items": {"type": "number", "minimum": 0, "maximum": 1},
        "minItems": 3,
        "maxItems": 3,
    }
    registry.register(
        "camera.inspect",
        inspect_camera,
        description="Inspect a bounded page of scene camera objects and the active camera assignment.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "active": {"type": "boolean"},
                "object_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "camera.patch",
        patch_camera,
        description="Patch allowlisted camera data fields with document lease, expected-state, journal, and rollback handling.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "type": {"type": "string", "enum": ["PERSP", "ORTHO", "PANO"]},
                "lens": {"type": "number", "minimum": 1, "maximum": 5000},
                "ortho_scale": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000},
                "clip_start": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000},
                "clip_end": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000000},
                "shift_x": {"type": "number", "minimum": -10, "maximum": 10},
                "shift_y": {"type": "number", "minimum": -10, "maximum": 10},
                "sensor_width": {"type": "number", "minimum": 1, "maximum": 1000},
                "sensor_height": {"type": "number", "minimum": 1, "maximum": 1000},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "camera.set_active",
        set_active_camera,
        description="Assign a named scene camera or clear the active camera with a journaled mutation.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "light.inspect",
        inspect_light,
        description="Inspect a bounded page of scene light objects and their type-specific settings.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "type": {"type": "string", "enum": ["POINT", "SUN", "SPOT", "AREA"]},
                "object_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "light.patch",
        patch_light,
        description="Patch allowlisted common or type-specific light fields with journal and rollback handling.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "type": {"type": "string", "enum": ["POINT", "SUN", "SPOT", "AREA"]},
                "energy": {"type": "number", "minimum": 0, "maximum": 1000000000},
                "color": color3,
                "use_shadow": {"type": "boolean"},
                "shadow_soft_size": {"type": "number", "minimum": 0, "maximum": 1000000},
                "shape": {"type": "string", "enum": ["SQUARE", "RECTANGLE", "DISK", "ELLIPSE"]},
                "size": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000},
                "size_y": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000},
                "spot_size": {"type": "number", "exclusiveMinimum": 0, "maximum": 3.141592653589793},
                "spot_blend": {"type": "number", "minimum": 0, "maximum": 1},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "world.inspect",
        inspect_world,
        description="Inspect a bounded page of World datablocks, active assignment, and basic Background settings.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "active": {"type": "boolean"},
                "world_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "world.create",
        create_world,
        description="Create a World datablock with optional default Background values and active-scene assignment.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "color": color3,
                "use_nodes": {"type": "boolean", "default": False},
                "background_color": color3,
                "background_strength": {"type": "number", "minimum": 0, "maximum": 1000000},
                "set_active": {"type": "boolean", "default": False},
                "expected_state": expected_state,
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "world.patch",
        patch_world,
        description="Patch World color, node usage, or one unambiguous Background node with rollback handling.",
        input_schema={
            "type": "object",
            "properties": {
                "world_name": {"type": "string", "minLength": 1},
                "color": color3,
                "use_nodes": {"type": "boolean"},
                "background_color": color3,
                "background_strength": {"type": "number", "minimum": 0, "maximum": 1000000},
                "expected_state": expected_state,
            },
            "required": ["world_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "world.set_active",
        set_active_world,
        description="Assign a named World datablock to the scene or clear the active World.",
        input_schema={
            "type": "object",
            "properties": {
                "world_name": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
                "expected_state": expected_state,
            },
            "required": ["world_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "render.settings",
        render_settings,
        description="Inspect or patch allowlisted persistent scene render, output, frame-range, and image-format settings.",
        input_schema={
            "type": "object",
            "properties": {
                "engine": {"type": "string", "minLength": 1},
                "resolution_x": {"type": "integer", "minimum": 1, "maximum": 16384},
                "resolution_y": {"type": "integer", "minimum": 1, "maximum": 16384},
                "resolution_percentage": {"type": "integer", "minimum": 1, "maximum": 100},
                "film_transparent": {"type": "boolean"},
                "fps": {"type": "integer", "minimum": 1, "maximum": 240},
                "fps_base": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000},
                "frame_start": {"type": "integer", "minimum": -1048574, "maximum": 1048574},
                "frame_end": {"type": "integer", "minimum": -1048574, "maximum": 1048574},
                "frame_step": {"type": "integer", "minimum": 1, "maximum": 1048574},
                "image_format": {"type": "string", "enum": ["PNG", "JPEG", "OPEN_EXR", "TIFF"]},
                "filepath": {"type": "string", "minLength": 1},
                "cycles_samples": {"type": "integer", "minimum": 1, "maximum": 65536},
                "cycles_use_denoising": {"type": "boolean"},
                "eevee_use_raytracing": {"type": "boolean"},
                "view_transform": {"type": "string", "minLength": 1},
                "look": {"type": "string", "minLength": 1},
                "exposure": {"type": "number", "minimum": -32, "maximum": 32},
                "gamma": {"type": "number", "minimum": 0.001, "maximum": 5},
                "expected_state": expected_state,
            },
            "additionalProperties": False,
        },
    )
    assembly_pair = {"type": "array", "items": {"type": "string", "minLength": 1},
                     "minItems": 2, "maxItems": 2}
    registry.register(
        "assembly.analyze", analyze_assembly, toolset="assembly",
        description=(
            "Measure candidate part interfaces on evaluated world-space mesh surfaces; distances and "
            "penetration are sampled estimates. Face budgets refuse oversized meshes, and max_pairs "
            "caps candidate work. Returns contact, gap, intersection, expectation status, and focus. "
            "Penetration is undetermined when a target mesh is open. isolated_parts lists parts touching "
            "nothing for review; name intentionally free parts in expect_isolated."
        ),
        input_schema={"type": "object", "properties": {
            "pairs": {"type": "array", "minItems": 1, "items": assembly_pair},
            "object_names": {"type": "array", "minItems": 2, "uniqueItems": True,
                             "items": {"type": "string", "minLength": 1}},
            "collection_name": {"type": "string", "minLength": 1},
            "search_margin": {"type": "number", "minimum": 0},
            "contact_tolerance": {"type": "number", "minimum": 0, "default": 0.0001},
            "borderline_relative_band": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.02},
            "max_faces_per_object": {"type": "integer", "minimum": 1, "maximum": 2000000, "default": 250000},
            "max_pairs": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 200},
            "evaluated": {"type": "boolean", "default": True},
            "expectations": {"type": "array", "items": {"type": "object", "properties": {
                "pair": assembly_pair,
                "contact": {"type": "boolean"},
                "gap_between": {"type": "array", "items": {"type": "number", "minimum": 0},
                                "minItems": 2, "maxItems": 2},
                "no_intersection": {"type": "boolean"},
                "intersection_allowed": {"type": "boolean"},
            }, "required": ["pair"], "additionalProperties": False}},
            "expect_isolated": {"type": "array", "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1}},
        }, "additionalProperties": False},
    )
    registry.register("source.inspect", inspect_source, toolset="source",
        description="Inspect source mesh topology, islands, symmetry, bounds, textures, and pose hints without changing it.",
        input_schema={"type": "object", "properties": {"object_name": {"type": "string", "minLength": 1}},
                      "required": ["object_name"], "additionalProperties": False})
    registry.register("source.prepare", prepare_source, toolset="source",
        description="Create a separate watertight projection proxy from a source mesh while preserving the original.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1}, "voxel_size": {"type": "number", "exclusiveMinimum": 0},
            "min_island_vertices": {"type": "integer", "minimum": 1}, "output_name": {"type": "string", "minLength": 1},
            "symmetrize": {"type": "boolean"}, "symmetry_plane_x": {"type": "number"},
            "expected_state": expected_state}, "required": ["object_name", "voxel_size"], "additionalProperties": False})
    registry.register("creature.graph_suggest", graph_suggest, toolset="creature",
        description="Suggest a body graph from body-plan landmarks and a projection proxy; inspect confidence before applying.",
        input_schema={"type": "object", "properties": {"body_plan": {"type": "string", "enum": ["humanoid", "quadruped", "reptile"]},
                      "proxy_name": {"type": "string", "minLength": 1}},
                      "required": ["body_plan", "proxy_name"], "additionalProperties": False})
    registry.register("creature.landmark_set", landmark_set, toolset="creature",
        description="Create or move a named landmark Empty for body-graph suggestions.",
        input_schema={"type": "object", "properties": {"name": {"type": "string", "minLength": 1},
                      "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                      "object_name": {"type": "string", "minLength": 1}, "expected_state": expected_state},
                      "required": ["name", "position"], "additionalProperties": False})
    registry.register("creature.graph_set", graph_set, toolset="creature",
        description="Validate and store an explicit body graph on an object for mesh, region, and skeleton generation.",
        input_schema={"type": "object", "properties": {"object_name": {"type": "string", "minLength": 1},
                      "graph": {"type": "object"}, "expected_state": expected_state},
                      "required": ["object_name", "graph"], "additionalProperties": False})
    registry.register("creature.graph_inspect", graph_inspect, toolset="creature",
        description="Read the validated body graph attached to an object.",
        input_schema={"type": "object", "properties": {"object_name": {"type": "string", "minLength": 1}},
                      "required": ["object_name"], "additionalProperties": False})
    registry.register("creature.base_from_graph", base_from_graph, toolset="creature",
        description="Build a quad-dominant region-labeled creature base from a body graph as a new mesh.",
        input_schema={"type": "object", "properties": {"graph_object": {"type": "string", "minLength": 1},
                      "output_name": {"type": "string", "minLength": 1}, "expected_state": expected_state},
                      "required": ["graph_object"], "additionalProperties": False})
    registry.register("mesh.wrap_to_target", wrap_to_target, toolset="mesh",
        description="Fit a copy of a fixed-topology mesh to a target surface and report distance and topology proof.",
        input_schema={"type": "object", "properties": {"object_name": {"type": "string", "minLength": 1},
                      "target_name": {"type": "string", "minLength": 1}, "output_name": {"type": "string", "minLength": 1},
                      "iterations": {"type": "integer", "minimum": 1},
                      "projection_strength": {"type": "number", "minimum": 0, "maximum": 1},
                      "smooth_strength": {"type": "number", "minimum": 0, "maximum": 1},
                      "landmark_pairs": {"type": "array", "minItems": 3, "items": {"type": "object", "properties": {
                          "source": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                          "target": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}},
                          "required": ["source", "target"], "additionalProperties": False}},
                      "region_stiffness": {"type": "object"}, "symmetry_plane_x": {"type": "number"},
                      "coverage_tolerance": {"type": "number", "exclusiveMinimum": 0},
                      "expected_state": expected_state},
                      "required": ["object_name", "target_name"], "additionalProperties": False})
    registry.register("mesh.topology_report", topology_report, toolset="mesh",
        description="Report face quality, poles, symmetry, bend zones, budget, and connectivity of a mesh.",
        input_schema={"type": "object", "properties": {"object_name": {"type": "string", "minLength": 1},
                      "triangle_budget": {"type": "integer", "minimum": 1}, "tiny_face_area": {"type": "number", "minimum": 0},
                      "symmetry_plane_x": {"type": "number"},
                      "bend_zones": {"type": "array", "items": {"type": "object", "properties": {
                          "name": {"type": "string", "minLength": 1}, "point": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                          "radius": {"type": "number", "exclusiveMinimum": 0}},
                          "required": ["name", "point", "radius"], "additionalProperties": False}}},
                      "required": ["object_name"], "additionalProperties": False})
    pointcloud_roi = {"type": "object", "properties": {
        "min": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
        "max": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}},
        "required": ["min", "max"], "additionalProperties": False}
    registry.register("pointcloud.section", pointcloud_section, toolset="pointcloud",
        description="Slice a point cloud and optional mesh in a bounded ROI and return 2D evidence with a section image.",
        input_schema={"type": "object", "properties": {
            "pointcloud_name": {"type": "string", "minLength": 1}, "mesh_name": {"type": "string", "minLength": 1},
            "plane_point": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "plane_normal": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "band_width": {"type": "number", "exclusiveMinimum": 0}, "roi": pointcloud_roi,
            "max_points": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 100000},
            "output_path": {"type": "string", "minLength": 1}, "overwrite": {"type": "boolean", "default": False}},
            "required": ["pointcloud_name", "plane_point", "plane_normal", "band_width"], "additionalProperties": False})
    registry.register("reference.crop_grid", reference_crop_grid, toolset="render",
        description="Return a zoomed pixel-addressable crop with grid coordinates for manual reference correspondences.",
        input_schema={"type": "object", "properties": {
            "reference_path": {"type": "string", "minLength": 1},
            "center": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
            "radius": {"type": "integer", "minimum": 8, "maximum": 512, "default": 64},
            "zoom": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
            "grid_spacing": {"type": "integer", "minimum": 1, "maximum": 128, "default": 10},
            "output_path": {"type": "string", "minLength": 1}, "overwrite": {"type": "boolean", "default": False}},
            "required": ["reference_path", "center"], "additionalProperties": False})
    registry.register("mesh.compare_to_points", compare_to_points, toolset="mesh",
        description="Measure point-to-mesh distances in an ROI with outlier filtering, sparse-state evidence, a heat map, and focus.",
        input_schema={"type": "object", "properties": {
            "pointcloud_name": {"type": "string", "minLength": 1}, "mesh_name": {"type": "string", "minLength": 1},
            "roi": pointcloud_roi, "max_points": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 100000},
            "min_points": {"type": "integer", "minimum": 1, "default": 50},
            "outlier_sigma": {"type": "number", "exclusiveMinimum": 0, "default": 3.0},
            "max_p95": {"type": "number", "minimum": 0},
            "borderline_band": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.02},
            "output_path": {"type": "string", "minLength": 1}, "overwrite": {"type": "boolean", "default": False}},
            "required": ["pointcloud_name", "mesh_name"], "additionalProperties": False})
    detail_vector3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
    calibration_world = {"anyOf": [detail_vector3,
        {"type": "object", "properties": {"object": {"type": "string", "minLength": 1}, "vertex_index": {"type": "integer", "minimum": 0}}, "required": ["object", "vertex_index"], "additionalProperties": False},
        {"type": "object", "properties": {"empty": {"type": "string", "minLength": 1}}, "required": ["empty"], "additionalProperties": False}]}
    registry.register(
        "camera.calibrate_from_reference", calibrate_from_reference,
        description="Calibrate a scene camera from image/world correspondences and return pixel residuals with an overlay PNG.",
        input_schema={"type": "object", "properties": {
            "reference_path": {"type": "string", "minLength": 1},
            "correspondences": {"type": "array", "minItems": 4, "items": {"type": "object", "properties": {
                "pixel": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                "world": calibration_world}, "required": ["pixel", "world"], "additionalProperties": False}},
            "model": {"type": "string", "enum": ["perspective", "orthographic"], "default": "perspective"},
            "camera_name": {"type": "string", "minLength": 1},
            "focal_length_mm": {"type": "number", "minimum": 1},
            "output_path": {"type": "string", "minLength": 1},
            "overwrite": {"type": "boolean", "default": False},
            "high_error_fraction": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.01},
            "expected_state": expected_state,
        }, "required": ["reference_path", "correspondences", "camera_name"], "additionalProperties": False},
    )
    registry.register(
        "camera.import_photogrammetry", import_photogrammetry,
        description="Preview or import RealityScan GUI CSV cameras aligned through a point-cloud object's transform; orientation remains provisional until image correspondences verify it. csv_principal keeps solved intrinsics with their poses; centered_zero is a diagnostic that breaks that fit.",
        input_schema={"type": "object", "properties": {
            "csv_path": {"type": "string", "minLength": 1},
            "images_dir": {"type": "string", "minLength": 1},
            "pointcloud_object": {"type": "string", "minLength": 1},
            "collection_name": {"type": "string", "minLength": 1, "default": "RealityScan Cameras"},
            "mode": {"type": "string", "enum": ["centered_zero", "csv_principal"], "default": "csv_principal"},
            "image_names": {"type": "array", "minItems": 1, "maxItems": 100000, "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1}},
            "dry_run": {"type": "boolean", "default": True},
            "confirm_unverified_orientation": {"type": "boolean", "default": False},
            "expected_state": expected_state,
        }, "required": ["csv_path", "images_dir", "pointcloud_object"], "additionalProperties": False},
    )
    registry.register(
        "camera.import_colmap_package", import_colmap_package,
        description="Preview or import a Camera Refinement Workbench handoff (cameras.json or undistorted COLMAP text) with solved intrinsics, poses, kind, and scores. Intrinsics and poses are one solution; never mix them across packages or kinds.",
        input_schema={"type": "object", "properties": {
            "package_dir": {"type": "string", "minLength": 1},
            "images_dir": {"type": "string", "minLength": 1},
            "transform_object": {"type": "string", "minLength": 1},
            "collection_name": {"type": "string", "minLength": 1},
            "dry_run": {"type": "boolean", "default": True},
            "import_points": {"type": "boolean", "default": False},
            "kind": {"type": "string", "enum": ["reproduction", "constrained"]},
            "expected_state": expected_state,
        }, "required": ["package_dir"], "additionalProperties": False},
    )
    registry.register(
        "render.contour_compare", contour_compare,
        description="Compare a reference silhouette with evaluated target meshes and return deviation statistics, focus points, and a PNG.",
        input_schema={"type": "object", "properties": {
            "reference_path": {"type": "string", "minLength": 1},
            "camera_name": {"type": "string", "minLength": 1},
            "object_names": {"type": "array", "minItems": 1, "maxItems": 64, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "mask_path": {"type": "string", "minLength": 1},
            "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            "foreground": {"type": "string", "enum": ["dark", "light"]},
            "background_color": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "color_tolerance": {"type": "number", "minimum": 0, "maximum": 1},
            "output_path": {"type": "string", "minLength": 1},
            "top_n": {"type": "integer", "minimum": 1, "maximum": 10},
            "min_iou": {"type": "number", "minimum": 0, "maximum": 1},
            "borderline_band": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.01},
            "overwrite": {"type": "boolean", "default": False},
        }, "required": ["reference_path", "camera_name", "object_names"], "additionalProperties": False},
    )
    detail_bounds = {"type": "object", "properties": {"min": detail_vector3, "max": detail_vector3},
                     "required": ["min", "max"], "additionalProperties": False}
    detail_target = {
        "object_name": {"type": "string", "minLength": 1},
        "object_names": {"type": "array", "minItems": 1, "uniqueItems": True,
                         "items": {"type": "string", "minLength": 1}},
        "point": detail_vector3,
        "radius": {"type": "number", "exclusiveMinimum": 0},
        "bounds": detail_bounds,
    }
    detail_output = {
        "width": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 960},
        "height": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 540},
        "overwrite": {"type": "boolean", "default": False},
        "allow_large": {"type": "boolean", "default": False},
    }
    view_mode = {"type": "string", "enum": ["lit", "solid", "normal", "matcap", "curvature", "depth", "wireframe_on_shaded"], "default": "lit"}
    registry.register(
        "scene.fast_capture", fast_capture_scene,
        description=(
            "Capture one to six targeted viewport-quality views through the deferred draw handler "
            "and return one PNG sheet. Solid/material shading and overlays are temporary; "
            "current view requires no target, named views require one."
        ),
        input_schema={"type": "object", "properties": {
            **detail_target,
            "views": {"type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True,
                      "items": {"type": "string", "enum": ["current", "front", "back", "left", "right",
                                                          "top", "bottom", "three_quarter"]}},
            "projection": {"type": "string", "enum": ["perspective", "orthographic"], "default": "perspective"},
            "shading": {"type": "string", "enum": ["current", "solid", "material"], "default": "current"},
            "view_mode": view_mode,
            "overlays": {"type": "boolean"}, "wireframe": {"type": "boolean"},
            "width": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 512},
            "height": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 512},
            "output_path": {"type": "string", "minLength": 1},
            "columns": {"type": "integer", "minimum": 1, "maximum": 6},
            "overwrite": {"type": "boolean", "default": False},
        }, "additionalProperties": False},
    )
    registry.register(
        "render.detail", render_detail,
        description="Render a reproducible close-up. bounds may fix framing while object_name/object_names select isolation subjects; isolate changes render visibility, and hide_set alone does not affect rendered output. clip_to_bounds requires explicit bounds and clips camera depth to their near/far extent, not the exact 3D box; scene geometry is unchanged. solid uses Workbench lighting; lit warns when no render-visible Light objects exist.",
        input_schema={"type": "object", "properties": {
            **detail_target,
            "direction": {"anyOf": [{"type": "string", "enum": ["front", "back", "left", "right", "top", "bottom", "three_quarter"]}, detail_vector3]},
            "projection": {"type": "string", "enum": ["perspective", "orthographic"], "default": "perspective"},
            "view_mode": view_mode,
            "isolate": {"type": "boolean", "default": False},
            "clip_to_bounds": {"type": "boolean", "default": False},
            **detail_output,
            "output_path": {"type": "string", "minLength": 1},
        }, "additionalProperties": False},
    )
    registry.register(
        "render.ortho_set", render_ortho_set,
        description="Render selected orthographic views with optional shared scale. output_dir plus output_prefix uses a basename inside that directory; explicit names are overwrite-guarded. Isolation uses render visibility, not hide_set.",
        input_schema={"type": "object", "properties": {
            **detail_target,
            "views": {"type": "array", "minItems": 1, "uniqueItems": True,
                      "items": {"type": "string", "enum": ["front", "back", "left", "right", "top", "bottom"]}},
            "shared_scale": {"type": "boolean", "default": True},
            "view_mode": view_mode,
            "isolate": {"type": "boolean", "default": False},
            **detail_output,
            "output_dir": {"type": "string", "minLength": 1},
            "output_prefix": {"type": "string", "minLength": 1},
        }, "additionalProperties": False},
    )
    registry.register(
        "render.reference_match", render_reference_match,
        description="Render with a supplied camera at reference aspect and write side-by-side and overlay PNGs.",
        input_schema={"type": "object", "properties": {
            "reference_path": {"type": "string", "minLength": 1},
            "camera_name": {"type": "string", "minLength": 1},
            "camera": {"type": "object", "properties": {
                "location": detail_vector3, "rotation_euler": detail_vector3,
                "projection": {"type": "string", "enum": ["perspective", "orthographic"]},
                "lens": {"type": "number", "exclusiveMinimum": 0},
                "ortho_scale": {"type": "number", "exclusiveMinimum": 0},
            }, "required": ["location", "rotation_euler"], "additionalProperties": False},
            "output_prefix": {"type": "string", "minLength": 1},
            "opacity": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
            "overlay_mode": {"type": "string", "enum": ["blend", "edges"], "default": "blend"},
            "overwrite": {"type": "boolean", "default": False},
            "allow_large": {"type": "boolean", "default": False},
        }, "required": ["reference_path"], "additionalProperties": False},
    )
    registry.register(
        "render.contact_sheet", render_contact_sheet,
        description="Compose up to 16 labeled PNG review images into one sheet without rendering.",
        input_schema={"type": "object", "properties": {
            "images": {"type": "array", "minItems": 1, "maxItems": 16,
                       "items": {"type": "object", "properties": {
                           "path": {"type": "string", "minLength": 1},
                           "label": {"type": "string", "minLength": 1},
                       }, "required": ["path"], "additionalProperties": False}},
            "output_path": {"type": "string", "minLength": 1},
            "columns": {"type": "integer", "minimum": 1, "maximum": 16},
            "view_mode": view_mode,
            "overwrite": {"type": "boolean", "default": False},
            "allow_large": {"type": "boolean", "default": False},
        }, "required": ["images", "output_path"], "additionalProperties": False},
    )
    registry.register(
        "render.background", start_background_render,
        description=(
            "Queue a final-quality render in a separate Blender process and return an operation ID. "
            "The copy is saved when the job starts, which may be after queueing; inspect status before "
            "editing if exact snapshot contents matter. Later live changes mark the snapshot stale."
        ),
        input_schema={"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["preview", "detail", "ortho_set", "review_set", "reference_match"],
                     "default": "preview"},
            "options": {"type": "object"},
            "engine": {"type": "string", "enum": ["CYCLES", "BLENDER_EEVEE_NEXT"]},
            "samples": {"type": "integer", "minimum": 1, "maximum": 100000},
        }, "additionalProperties": False},
    )
    registry.register(
        "render.background_status", background_render_status,
        description="Read queued, running, completed, failed, or cancelled status for a background render ID.",
        input_schema={"type": "object", "properties": {
            "operation_id": {"type": "string", "minLength": 1},
        }, "required": ["operation_id"], "additionalProperties": False},
    )
    registry.register(
        "render.background_cancel", cancel_background_render,
        description="Cancel a queued background render or terminate its separate Blender child process.",
        input_schema={"type": "object", "properties": {
            "operation_id": {"type": "string", "minLength": 1},
        }, "required": ["operation_id"], "additionalProperties": False},
    )
    registry.register(
        "bake.run",
        run_bake,
        description="Bake Cycles surface detail to an explicit PNG and restore render, selection, and UV context.",
        input_schema={"type": "object", "properties": {
            "type": {"type": "string", "enum": ["NORMAL", "AO", "DIFFUSE", "ROUGHNESS", "EMIT"]},
            "sources": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
            "target": {"type": "string", "minLength": 1},
            "uv_layer": {"type": "string", "minLength": 1},
            "image": {"type": "string", "minLength": 1},
            "resolution": {"type": "integer", "minimum": 16, "maximum": 4096, "default": 1024},
            "margin": {"type": "integer", "minimum": 0, "maximum": 128, "default": 16},
            "output_path": {"type": "string", "minLength": 1},
            "overwrite": {"type": "boolean", "default": False},
            "cage_object": {"type": "string", "minLength": 1},
            "cage_extrusion": {"type": "number", "minimum": 0, "maximum": 1000},
            "max_ray_distance": {"type": "number", "minimum": 0, "maximum": 1000},
            "normal_g": {"type": "string", "enum": ["POS_Y", "NEG_Y"], "default": "POS_Y"},
            "expected_state": expected_state,
        }, "required": ["type", "target", "uv_layer", "image", "output_path"], "additionalProperties": False},
    )
    registry.register(
        "creature.bake_from_source", bake_from_source, toolset="creature",
        description="Bake normal, AO, and base-color maps from unchanged original source meshes to one fitted UV mesh; report coverage and possible baked lighting.",
        input_schema={"type": "object", "properties": {
            "sources": {"type": "array", "minItems": 1, "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1}},
            "target": {"type": "string", "minLength": 1},
            "uv_layer": {"type": "string", "minLength": 1},
            "output_dir": {"type": "string", "minLength": 1},
            "maps": {"type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
                     "items": {"type": "string", "enum": ["normal", "ao", "base_color"]},
                     "default": ["normal", "ao", "base_color"]},
            "resolution": {"type": "integer", "minimum": 16, "maximum": 4096, "default": 1024},
            "image_prefix": {"type": "string", "minLength": 1},
            "margin": {"type": "integer", "minimum": 0, "maximum": 128, "default": 16},
            "overwrite": {"type": "boolean", "default": False},
            "cage_object": {"type": "string", "minLength": 1},
            "cage_extrusion": {"type": "number", "minimum": 0, "maximum": 1000, "default": 0},
            "max_ray_distance": {"type": "number", "minimum": 0, "maximum": 1000, "default": 0},
            "normal_g": {"type": "string", "enum": ["POS_Y", "NEG_Y"], "default": "POS_Y"},
            "expected_state": expected_state,
        }, "required": ["sources", "target", "uv_layer", "output_dir"], "additionalProperties": False},
    )
    rom_rotation = {"type": "object", "properties": {
        "bone": {"type": "string", "minLength": 1},
        "axis": {"type": "string", "enum": ["X", "Y", "Z"]},
        "degrees": {"type": "number", "minimum": -180, "maximum": 180},
    }, "required": ["bone", "axis", "degrees"], "additionalProperties": False}
    registry.register(
        "rig.rom_test", rom_test, toolset="rig",
        description="Measure editable creature range-of-motion poses for local volume, edge stretch, intersections, and twist; render deferred pose PNGs and a contact sheet, then restore the pose.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1},
            "armature_name": {"type": "string", "minLength": 1},
            "body_plan": {"type": "string", "minLength": 1},
            "poses": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                "type": "object", "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "bones": {"type": "array", "minItems": 1, "maxItems": 8, "items": rom_rotation},
                }, "required": ["name", "bones"], "additionalProperties": False}},
            "output_prefix": {"type": "string", "minLength": 1},
            "width": {"type": "integer", "minimum": 128, "maximum": 2048, "default": 256},
            "height": {"type": "integer", "minimum": 128, "maximum": 2048, "default": 256},
            "view_mode": {**view_mode, "default": "matcap"},
            "direction": {"anyOf": [{"type": "string", "enum": ["front", "back", "left", "right", "top", "bottom", "three_quarter"]}, detail_vector3]},
            "overwrite": {"type": "boolean", "default": False},
            "allow_large": {"type": "boolean", "default": False},
            "max_triangles": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 20000},
            "min_volume_ratio": {"type": "number", "minimum": 0.01, "maximum": 1, "default": 0.6},
            "max_volume_ratio": {"type": "number", "minimum": 1, "maximum": 10, "default": 1.4},
            "max_edge_stretch": {"type": "number", "minimum": 1, "maximum": 10, "default": 1.5},
            "min_edge_compression": {"type": "number", "minimum": 0.01, "maximum": 1, "default": 0.67},
            "min_twist_area_ratio": {"type": "number", "minimum": 0.01, "maximum": 1, "default": 0.5},
            "expected_state": expected_state,
        }, "required": ["object_name", "armature_name", "output_prefix"], "additionalProperties": False},
    )
    registry.register(
        "compositor.inspect",
        inspect_compositor,
        description="Inspect a bounded page of the active scene compositor graph, including File Output nodes and slots.",
        input_schema={
            "type": "object",
            "properties": {
                "scene": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "compositor.create",
        create_compositor,
        description="Dry-run-first creation of a non-destructive Render Layers to Composite graph, with an optional Viewer.",
        input_schema={
            "type": "object",
            "properties": {
                "scene": {"type": "string", "minLength": 1},
                "include_viewer": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "compositor.patch",
        patch_compositor,
        description="Dry-run-first allowlisted compositor edits with structural preconditions, confirmation, and graph rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "scene": {"type": "string", "minLength": 1},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "object", "required": ["op"]},
                    "description": "Allowlisted node, link, input, File Output, and file-slot operations.",
                },
                "expected_node_count": {"type": "integer", "minimum": 0, "maximum": 10000},
                "expected_link_count": {"type": "integer", "minimum": 0, "maximum": 50000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "render.manifest",
        inspect_render_manifest,
        description="Produce a structured manifest of render settings, view-layer passes, and compositor outputs without rendering.",
        input_schema={
            "type": "object",
            "properties": {
                "scene": {"type": "string", "minLength": 1},
                "include_paths": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "render.manifest_validate",
        validate_render_manifest,
        description="Validate camera, view layers, Composite output, optional File Output nodes, and path collisions without mutation.",
        input_schema={
            "type": "object",
            "properties": {
                "scene": {"type": "string", "minLength": 1},
                "include_paths": {"type": "boolean", "default": False},
                "require_composite": {"type": "boolean", "default": True},
                "require_file_output": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    )
    interchange_format = {"type": "string", "enum": ["OBJ", "FBX", "GLTF", "USD", "ALEMBIC", "PLY"]}
    registry.register(
        "interchange.inspect",
        inspect_interchange,
        description="Inspect guarded core interchange formats, operator availability, extensions, and allowlisted options.",
        input_schema={
            "type": "object",
            "properties": {
                "format": interchange_format,
                "direction": {"type": "string", "enum": ["import", "export"]},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "interchange.import",
        import_interchange,
        description="Dry-run-first explicit-file import with byte budget, operator option allowlist, confirmation, rollback, and created-data postflight.",
        input_schema={
            "type": "object",
            "properties": {
                "format": interchange_format,
                "filepath": {"type": "string", "minLength": 1},
                "options": {"type": "object"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 17179869184, "default": 2147483648},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["filepath"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "interchange.export",
        export_interchange,
        description="Dry-run-first single-file export with explicit scope, staged replacement, overwrite confirmation, and file postflight.",
        input_schema={
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["OBJ", "FBX", "GLTF", "USD", "ALEMBIC"]},
                "filepath": {"type": "string", "minLength": 1},
                "scope": {"type": "string", "enum": ["selected", "scene", "collection"], "default": "selected"},
                "collection": {"type": "string", "minLength": 1},
                "options": {"type": "object"},
                "overwrite": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["filepath"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "interchange.blend_import",
        import_blend_data,
        description="Dry-run-first append or link of named Collection/Object datablocks from one .blend file, subject to confirmation and a byte budget.",
        input_schema={"type": "object", "properties": {"filepath": {"type": "string", "minLength": 1}, "data_type": {"type": "string", "enum": ["COLLECTION", "OBJECT"], "default": "COLLECTION"}, "names": object_names, "link": {"type": "boolean", "default": False}, "target_collection": {"type": "string", "minLength": 1}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 17179869184, "default": 2147483648}, "dry_run": {"type": "boolean", "default": True}, "confirm": {"type": "boolean", "const": True}, "expected_state": expected_state}, "required": ["filepath", "names"], "additionalProperties": False},
    )
    bone_spec = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "head": vector3,
            "tail": vector3,
            "roll": {"type": "number"},
            "parent": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
            "use_connect": {"type": "boolean", "default": False},
            "use_deform": {"type": "boolean", "default": True},
            "inherit_scale": {"type": "string", "enum": ["FULL", "FIX_SHEAR", "ALIGNED", "AVERAGE", "NONE", "NONE_LEGACY"]},
        },
        "required": ["name", "head", "tail"],
        "additionalProperties": False,
    }
    registry.register(
        "armature.inspect",
        inspect_armature,
        description="Inspect a bounded page of armature bones, hierarchy, deformation settings, pose transforms, and optional constraints.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "include_pose": {"type": "boolean", "default": True},
                "include_constraints": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "armature.create",
        create_armature,
        description="Dry-run-first armature creation with bounded initial bones, exact-name checks, rollback, and context restoration.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "data_name": {"type": "string", "minLength": 1},
                "collection": {"type": "string", "minLength": 1},
                "display_type": {"type": "string", "enum": ["OCTAHEDRAL", "STICK", "BBONE", "ENVELOPE", "WIRE"], "default": "OCTAHEDRAL"},
                "show_in_front": {"type": "boolean", "default": True},
                "bones": {"type": "array", "maxItems": 512, "items": bone_spec},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "armature.edit",
        edit_armature,
        description="Plan or apply bounded add, patch, and rename EditBone operations with inverse rollback and full mode/selection restoration.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "operations": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["op"]}},
                "expected_bone_count": {"type": "integer", "minimum": 0, "maximum": 10000},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "operations"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "armature.bone_remove",
        remove_bone,
        description="Remove one dependency-free leaf bone after animation, constraint, driver, and skin-group checks.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "bone": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "bone"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "armature.validate",
        validate_armature,
        description="Validate rest-bone hierarchy, connected joints, pose targets, Action/NLA paths, and bound-mesh deform groups.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "include_animation": {"type": "boolean", "default": True},
                "max_bound_meshes": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 2000},
                "sample_limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 50},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "armature.from_body_graph",
        from_body_graph,
        description="Create a one-root custom armature from a stored body graph, or copy every rest and auxiliary bone from an explicitly mapped imported UE reference armature.",
        input_schema={"type": "object", "properties": {
            "graph_object": {"type": "string", "minLength": 1},
            "output_name": {"type": "string", "minLength": 1},
            "reference_armature_name": {"type": "string", "minLength": 1},
            "bone_map": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}},
            "ue_mannequin": {"type": "boolean", "default": False},
            "expected_state": expected_state,
        }, "required": ["graph_object"], "additionalProperties": False},
    )
    registry.register(
        "armature.validate_against_reference",
        validate_against_reference,
        description="Check one-root hierarchy, nonzero bones, joint continuity, optional closed-mesh containment, and exact rest-bone comparison with an imported reference armature.",
        input_schema={"type": "object", "properties": {
            "armature_name": {"type": "string", "minLength": 1},
            "reference_armature_name": {"type": "string", "minLength": 1},
            "mesh_name": {"type": "string", "minLength": 1},
            "bone_tolerance_m": {"type": "number", "minimum": 0, "default": 0.001},
        }, "required": ["armature_name"], "additionalProperties": False},
    )
    registry.register(
        "armature.export_ue_fbx",
        export_ue_fbx,
        description="Export a UE skeletal mesh or selected Action clips through one shared FBX node, axis, scale and deform-only path. FBX_SCALE_ALL is the default; root_mode auto strips the armature object when a root bone exists, otherwise exports the object as root. Per-action FPS is explicit. Round-trip acceptance checks scale and layout; a failed candidate is not published. Import onto a fresh UE skeleton when replacing an older x100 layout.",
        input_schema={"type": "object", "properties": {
            "armature_name": {"type": "string", "minLength": 1},
            "mesh_name": {"type": "string", "minLength": 1},
            "path": {"type": "string", "minLength": 1},
            "overwrite": {"type": "boolean", "default": False},
            "roundtrip": {"type": "boolean", "default": True},
            "action_names": {"type": "array", "maxItems": 64, "uniqueItems": True,
                             "items": {"type": "string", "minLength": 1}},
            "action_fps": {"type": "object", "maxProperties": 64,
                           "additionalProperties": {"type": "integer", "minimum": 1, "maximum": 32767}},
            "include_mesh": {"type": "boolean", "default": True},
            "root_mode": {"type": "string", "enum": ["auto", "object_is_root", "object_stripped"], "default": "auto"},
            "use_armature_deform_only": {"type": "boolean", "default": False},
            "apply_scale_options": {"type": "string", "enum": ["FBX_SCALE_ALL", "FBX_SCALE_NONE"], "default": "FBX_SCALE_ALL"},
        }, "required": ["armature_name", "path"], "additionalProperties": False},
    )
    registry.register(
        "pose.inspect",
        inspect_pose,
        description="Inspect bounded pose-bone transforms and optional constraint summaries without changing frame or mode.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "bone": {"type": "string", "minLength": 1},
                "include_constraints": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "pose.patch",
        patch_pose,
        description="Dry-run-first pose transform patch with field-level rollback and no mode or frame changes.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "bone": {"type": "string", "minLength": 1},
                "location": vector3,
                "rotation_mode": {"type": "string", "enum": ["QUATERNION", "XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX", "AXIS_ANGLE"]},
                "rotation_euler": vector3,
                "rotation_quaternion": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}},
                "rotation_axis_angle": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}},
                "scale": vector3,
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "bone"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "pose.constraint_patch",
        patch_pose_constraints,
        description="Plan or atomically add, patch, or dependency-check and remove allowlisted pose constraints with confirmation and inverse rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "operations": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["op", "bone", "name"]}},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "operations"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.inspect",
        inspect_animation,
        description="Inspect legacy or Blender 5 layered Action slots, F-Curves, and bounded keyframe pages.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "action_name": {"type": "string", "minLength": 1},
                "include_keyframes": {"type": "boolean", "default": False},
                "max_keys_per_curve": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.action_create",
        create_action,
        description="Dry-run-first exact-name Action creation with optional assignment and guarded replacement of an active Action.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "assign": {"type": "boolean", "default": True},
                "replace": {"type": "boolean", "default": False},
                "fake_user": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.action_assign",
        assign_action,
        description="Assign an existing Action or unassign the active Action with guarded replacement and rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "action_name": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.action_retarget",
        retarget_action,
        description="Copy an Action's F-curves through explicit or same-name bone mapping. This does not solve rest-pose differences, axis changes, mixed units, or unequal chain lengths; verify target poses before export.",
        input_schema={
            "type": "object",
            "properties": {
                "source_object": {"type": "string", "minLength": 1},
                "source_action": {"type": "string", "minLength": 1},
                "target_object": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "bone_map": {"type": "object", "maxProperties": 512, "additionalProperties": {"type": "string", "minLength": 1}},
                "assign": {"type": "boolean", "default": True},
                "replace": {"type": "boolean", "default": False},
                "require_all_bones": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["source_object", "source_action", "target_object", "name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.action_remove",
        remove_action,
        description="Remove one unused Action after active and NLA dependency checks.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.keyframe_edit",
        edit_keyframes,
        description="Plan or atomically edit allowlisted object/pose transform keys through Blender's Action API using an isolated working copy.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "operations": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["op", "property", "index", "frame"]}},
                "expected_action": {"type": "string", "minLength": 1},
                "make_single_user": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "operations"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.pose_bake",
        bake_pose,
        description="Bake evaluated armature pose transforms into a new bounded Action with full context restoration.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "frame_start": {"type": "integer", "minimum": -1048574, "maximum": 1048574},
                "frame_end": {"type": "integer", "minimum": -1048574, "maximum": 1048574},
                "step": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 1},
                "bones": {"type": "array", "minItems": 1, "maxItems": 20000, "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
                "visual_keying": {"type": "boolean", "default": True},
                "max_bones": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 512},
                "max_samples": {"type": "integer", "minimum": 1, "maximum": 500000, "default": 100000},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "name", "frame_start", "frame_end"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.validate",
        validate_animation,
        description="Validate Action assignment, layered slots, F-Curve validity, target paths, duplicate keys, and frame-range coverage.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "action_name": {"type": "string", "minLength": 1},
                "frame_start": {"type": "number", "minimum": -1048574, "maximum": 1048574},
                "frame_end": {"type": "number", "minimum": -1048574, "maximum": 1048574},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.nla_inspect",
        inspect_nla,
        description="Inspect bounded NLA tracks and optional strip details without changing animation state.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "include_strips": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.nla_patch",
        patch_nla,
        description="Plan or atomically add and patch bounded NLA tracks and Action strips with inverse rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "operations": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["op", "track"]}},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "operations"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.nla_remove",
        remove_nla,
        description="Remove one NLA strip or one empty track with confirmation and dependency-preserving preflight.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "track": {"type": "string", "minLength": 1},
                "strip": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "track"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.driver_inspect",
        inspect_drivers,
        description="Inspect bounded driver F-Curves, expressions, variables, and transform targets.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.driver_create",
        create_driver,
        description="Dry-run-first generated linear transform driver creation; arbitrary expressions and RNA paths are not accepted.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "target": {"type": "string", "enum": ["object", "pose_bone"], "default": "object"},
                "bone": {"type": "string", "minLength": 1},
                "property": {"type": "string", "enum": ["location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale"]},
                "index": {"type": "integer", "minimum": 0, "maximum": 3},
                "source_object": {"type": "string", "minLength": 1},
                "source_bone": {"type": "string", "minLength": 1},
                "source_transform": {"type": "string", "enum": ["LOC_X", "LOC_Y", "LOC_Z", "ROT_X", "ROT_Y", "ROT_Z", "SCALE_X", "SCALE_Y", "SCALE_Z"]},
                "source_space": {"type": "string", "enum": ["WORLD_SPACE", "TRANSFORM_SPACE", "LOCAL_SPACE"], "default": "WORLD_SPACE"},
                "multiplier": {"type": "number", "default": 1.0},
                "offset": {"type": "number", "default": 0.0},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "property", "index", "source_object", "source_transform"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.driver_remove",
        remove_driver,
        description="Remove one allowlisted object or pose transform driver with exact target matching.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "target": {"type": "string", "enum": ["object", "pose_bone"], "default": "object"},
                "bone": {"type": "string", "minLength": 1},
                "property": {"type": "string", "enum": ["location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale"]},
                "index": {"type": "integer", "minimum": 0, "maximum": 3},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "property", "index"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.shape_key_inspect",
        inspect_shape_keys,
        description="Inspect bounded mesh shape-key settings, dependencies, vertex groups, and point counts.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.shape_key_create",
        create_shape_key,
        description="Dry-run-first exact-name mesh shape-key creation with optional relative-key and vertex-group binding.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "from_mix": {"type": "boolean", "default": False},
                "relative_key": {"type": "string", "minLength": 1},
                "vertex_group": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.shape_key_patch",
        patch_shape_key,
        description="Plan or patch shape-key settings and explicit point coordinates with dependency checks and exact rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "value": {"type": "number", "minimum": -10, "maximum": 10},
                "slider_min": {"type": "number", "minimum": -10, "maximum": 10},
                "slider_max": {"type": "number", "minimum": -10, "maximum": 10},
                "mute": {"type": "boolean"},
                "interpolation": {"type": "string", "enum": ["KEY_LINEAR", "KEY_CARDINAL", "KEY_CATMULL_ROM", "KEY_BSPLINE"]},
                "relative_key": {"type": "string", "minLength": 1},
                "vertex_group": {"type": "string", "minLength": 1},
                "clear_vertex_group": {"type": "boolean", "default": False},
                "positions": {"type": "array", "maxItems": 250000, "items": {"type": "object", "required": ["index", "co"]}},
                "max_points": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "animation.shape_key_remove",
        remove_shape_key,
        description="Remove one bounded shape key after relative-key and animation/driver dependency checks.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "max_points": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.inspect",
        inspect_skin,
        description="Inspect bounded vertex-group weight statistics and Armature binding without changing mode or selection.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "armature_name": {"type": "string", "minLength": 1},
                "max_vertices": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.bind",
        bind_skin,
        description="Dry-run-first exact-name Armature modifier binding with rollback and complete mode/selection restoration.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "armature_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1, "default": "Armature"},
                "preserve_volume": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "armature_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.unbind",
        unbind_skin,
        description="Remove one exact Armature modifier while preserving vertex groups and reporting its prior settings.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "armature_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.auto_weights",
        automatic_weights,
        description="Dry-run-first Blender automatic bone weights with explicit compute/snapshot budgets, confirmation, rollback, and context restoration.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "armature_name": {"type": "string", "minLength": 1},
                "max_vertices": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "max_weight_entries": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "max_candidate_pairs": {"type": "integer", "minimum": 1, "maximum": 100000000, "default": 5000000},
                "require_all_weighted": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "armature_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.weights_patch",
        patch_weights,
        description="Plan or apply bounded explicit-index vertex-weight changes with per-vertex rollback and destructive confirmation.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "group_name": {"type": "string", "minLength": 1},
                "create_group": {"type": "boolean", "default": False},
                "operations": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["op", "vertices"]}},
                "expected_vertex_count": {"type": "integer", "minimum": 0, "maximum": 5000000},
                "max_vertices": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "group_name", "operations"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.group_remove",
        remove_vertex_group,
        description="Remove one empty vertex group after a bounded membership scan.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "group_name": {"type": "string", "minLength": 1},
                "max_vertices": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "group_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.weights_normalize",
        normalize_weights,
        description="Normalize deform-bone weights, prune small weights, and cap per-vertex influences with exact rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "armature_name": {"type": "string", "minLength": 1},
                "max_influences": {"type": "integer", "minimum": 1, "maximum": 64, "default": 4},
                "min_weight": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0001},
                "require_all_weighted": {"type": "boolean", "default": True},
                "max_vertices": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "max_weight_entries": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.weights_limit",
        weights_limit,
        description="Dry-run-first prune, cap and renormalize deform-bone influences using the existing exact-rollback normalization path.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1},
            "armature_name": {"type": "string", "minLength": 1},
            "max_influences": {"type": "integer", "minimum": 1, "maximum": 64, "default": 4},
            "prune_threshold": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0001},
            "require_all_weighted": {"type": "boolean", "default": True},
            "max_vertices": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
            "max_weight_entries": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
            "dry_run": {"type": "boolean", "default": True},
            "confirm": {"type": "boolean", "const": True},
            "expected_state": expected_state,
        }, "required": ["object_name"], "additionalProperties": False},
    )
    registry.register(
        "skin.weights_transfer_by_index",
        weights_transfer_by_index,
        description="Copy chosen template vertex groups by index after proving identical mesh connectivity; dry-run-first with rollback.",
        input_schema={"type": "object", "properties": {
            "source_name": {"type": "string", "minLength": 1},
            "object_name": {"type": "string", "minLength": 1},
            "group_names": {"type": "array", "minItems": 1,
                            "items": {"type": "string", "minLength": 1}},
            "max_vertices": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
            "max_weight_entries": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 500000},
            "dry_run": {"type": "boolean", "default": True},
            "confirm": {"type": "boolean", "const": True},
            "expected_state": expected_state,
        }, "required": ["source_name", "object_name"], "additionalProperties": False},
    )
    registry.register(
        "skin.validate",
        validate_skin,
        description="Validate Armature modifier targeting, deform-bone group coverage, group references, and weight normalization.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "armature_name": {"type": "string", "minLength": 1},
                "max_vertices": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "normalization_tolerance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0001},
                "sample_limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 50},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "skin.audit",
        audit_skin,
        description="Read-only advisory skin audit: bounded per-bone opposite-side weight bleed and distance spill, plus evaluated edge-stretch and added self-intersection evidence for explicit pose or Action/frame samples. Threshold flags are not an acceptance gate; incomplete scans report unknown counts. Sampling restores frame, Action and pose state, but cannot undo arbitrary frame-handler side effects.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1},
            "armature_name": {"type": "string", "minLength": 1},
            "bone_names": {"type": "array", "minItems": 1, "maxItems": 128, "uniqueItems": True,
                           "items": {"type": "string", "minLength": 1}},
            "side_axis": {"type": "string", "enum": ["X", "Y", "Z"], "default": "X"},
            "midline_offset": {"type": "number", "minimum": -1e12, "maximum": 1e12, "default": 0},
            "side_tolerance": {"type": "number", "minimum": 0, "maximum": 1e12, "default": 0.0001},
            "min_weight": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.01},
            "dominance_threshold": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
            "radius_percentile": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.95},
            "far_multiplier": {"type": "number", "minimum": 1, "maximum": 1000000, "default": 2},
            "samples": {"type": "array", "maxItems": 12, "items": {"type": "object"},
                        "description": "Each sample is {name,bones:[{bone,axis,degrees}]} or {name,action_name,frame}."},
            "max_vertices": {"type": "integer", "minimum": 1, "maximum": 500000, "default": 100000},
            "max_edges": {"type": "integer", "minimum": 1, "maximum": 1500000, "default": 300000},
            "max_triangles": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 20000},
            "max_pair_checks": {"type": "integer", "minimum": 1, "maximum": 2000000, "default": 200000},
            "thresholds": {"type": "object", "description": "Optional advisory thresholds for bleed, spill, stretch, compression and added intersections."},
        }, "required": ["object_name"], "additionalProperties": False},
    )
    modifier_type = {"type": "string", "minLength": 1}
    registry.register(
        "mesh.inspect",
        inspect_mesh,
        description="Inspect bounded mesh counts, bounds, materials, UVs, attributes, and optional budgeted topology diagnostics.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "include_topology": {"type": "boolean", "default": False},
                "max_elements": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "mesh.measure",
        measure_mesh,
        description="Measure bounded local/world mesh distances, angles, edge lengths, face areas, semantic-selection bounds, and volume.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "queries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "VERTEX_DISTANCE", "ANGLE", "EDGE_LENGTH",
                                    "FACE_AREA", "SELECTION_BOUNDS", "VOLUME",
                                ],
                            },
                        },
                        "required": ["type"],
                    },
                },
                "max_elements": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 500000},
            },
            "required": ["object_name", "queries"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "mesh.edit",
        edit_mesh,
        description="Plan or atomically apply bounded BMesh edits, including topology construction, spin/screw, true loop cuts, directional edge slides, and stateless workplanes.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "object", "required": ["op"]},
                },
                "max_elements": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 500000},
                "fragment_small_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1, "default": 0.01},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "operations"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "mesh.join",
        join_meshes,
        description="Plan or apply a mesh join. object_names+name creates a new world-space mesh without selection or visibility and preserves sources; apply with dry_run:false, optional confirm:true is ignored. Legacy default parameters are ignored in named mode. target_object+source_objects is the earlier route and requires a visible selectable target and confirm:true to apply.",
        input_schema={
            "type": "object",
            "properties": {
                "object_names": {"type": "array", "minItems": 2, "maxItems": 128, "uniqueItems": True,
                                 "items": {"type": "string", "minLength": 1}},
                "name": {"type": "string", "minLength": 1},
                "target_object": {"type": "string", "minLength": 1},
                "source_objects": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 200,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "keep_sources": {"type": "boolean", "default": False},
                "max_elements": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 500000},
                "fragment_small_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1, "default": 0.01},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "oneOf": [{"required": ["target_object", "source_objects"]},
                      {"required": ["object_names", "name"]}],
            "additionalProperties": False,
        },
    )
    registry.register(
        "mesh.separate",
        separate_mesh,
        description="Plan or apply bounded loose-part, material, or semantic-selection mesh separation with source restoration on failure.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "mode": {"type": "string", "enum": ["LOOSE", "MATERIAL", "SELECTION"]},
                "selection": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "enum": ["VERT", "EDGE", "FACE"]},
                        "predicate": {"type": "object"},
                    },
                    "required": ["domain", "predicate"],
                    "additionalProperties": False,
                },
                "name_prefix": {"type": "string", "minLength": 1},
                "max_parts": {"type": "integer", "minimum": 2, "maximum": 2000, "default": 100},
                "max_elements": {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 500000},
                "fragment_small_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1, "default": 0.01},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "mode"],
            "additionalProperties": False,
        },
    )
    curve_settings = {
        "dimensions": {"type": "string", "enum": ["2D", "3D"]},
        "resolution_u": {"type": "integer", "minimum": 1, "maximum": 1024},
        "render_resolution_u": {"type": "integer", "minimum": 0, "maximum": 1024},
        "fill_mode": {"type": "string", "enum": ["FULL", "HALF", "BACK", "FRONT"]},
        "bevel_depth": {"type": "number", "minimum": 0, "maximum": 1000000},
        "bevel_resolution": {"type": "integer", "minimum": 0, "maximum": 32},
        "twist_smooth": {"type": "integer", "minimum": 0, "maximum": 100},
    }
    spline_schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["POLY", "BEZIER", "NURBS"]},
            "points": {"type": "array", "minItems": 2, "maxItems": 100000},
            "cyclic": {"type": "boolean", "default": False},
            "resolution_u": {"type": "integer", "minimum": 1, "maximum": 1024},
            "order_u": {"type": "integer", "minimum": 2, "maximum": 6},
            "use_endpoint_u": {"type": "boolean"},
            "use_bezier_u": {"type": "boolean"},
        },
        "required": ["type", "points"],
        "additionalProperties": False,
    }
    registry.register(
        "curve.inspect",
        inspect_curve,
        description="Inspect bounded Curve objects, spline connectivity/alignment audits, and optional paginated control points.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "object_name": {"type": "string", "minLength": 1},
                "include_points": {"type": "boolean", "default": False},
                "max_points": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 10000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "curve.create",
        create_curve,
        description="Create one Curve object with a typed initial POLY, BEZIER, or NURBS spline.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "collection": {"type": "string", "minLength": 1},
                "location": vector3,
                "rotation": vector3,
                "scale": vector3,
                "spline": spline_schema,
                **curve_settings,
                "expected_state": expected_state,
            },
            "required": ["spline"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "curve.patch",
        patch_curve,
        description="Patch allowlisted Curve datablock settings with field-level rollback.",
        input_schema={
            "type": "object",
            "properties": {"object_name": {"type": "string", "minLength": 1}, **curve_settings, "expected_state": expected_state},
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    for tool_name, handler, description in (
        ("curve.spline_add", add_curve_spline, "Add one bounded typed spline to a single-user Curve datablock."),
        ("curve.spline_patch", patch_curve_spline, "Replace settings and point data in-place without changing spline type or point count."),
    ):
        registry.register(
            tool_name,
            handler,
            description=description,
            input_schema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "minLength": 1},
                    **({"spline_index": {"type": "integer", "minimum": 0, "maximum": 100000}} if tool_name.endswith("patch") else {}),
                    "spline": spline_schema,
                    "expected_state": expected_state,
                },
                "required": ["object_name", "spline"] + (["spline_index"] if tool_name.endswith("patch") else []),
                "additionalProperties": False,
            },
        )
    registry.register(
        "curve.spline_remove",
        remove_curve_spline,
        description="Dry-run-first removal of the final Curve spline with explicit confirmation and recovery metadata.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "spline_index": {"type": "integer", "minimum": 0, "maximum": 100000},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "spline_index"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "geometry_nodes.inspect",
        inspect_geometry_nodes,
        description="Inspect Geometry Nodes modifiers, stable interface identifiers, modifier inputs, and bounded graph pages.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1},
                "include_graph": {"type": "boolean", "default": False},
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 2000},
                "max_links": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 10000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "geometry_nodes.create",
        create_geometry_nodes,
        description="Create an attached Geometry Nodes modifier with a geometry pass-through node group.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1},
                "node_group_name": {"type": "string", "minLength": 1},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "geometry_nodes.patch",
        patch_geometry_nodes,
        description="Dry-run-first atomic node, link, interface, and input-default edits using a validated working copy.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {"type": "object", "required": ["op"]},
                },
                "expected_group": {"type": "string", "minLength": 1},
                "expected_node_count": {"type": "integer", "minimum": 0, "maximum": 10000},
                "expected_link_count": {"type": "integer", "minimum": 0, "maximum": 50000},
                "make_single_user": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "operations"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "geometry_nodes.modifier_inputs",
        patch_geometry_nodes_inputs,
        description="Patch modifier input values by stable interface identifiers with field-level rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1},
                "values": {"type": "object", "minProperties": 1, "maxProperties": 1000},
                "expected_group": {"type": "string", "minLength": 1},
                "expected_state": expected_state,
            },
            "required": ["object_name", "values"],
            "additionalProperties": False,
        },
    )
    vector2 = {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 2,
        "maxItems": 2,
    }
    uv_budget = {"type": "integer", "minimum": 1, "maximum": 5000000, "default": 250000}
    face_selection = {
        "type": "object",
        "properties": {
            "indices": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "minItems": 1,
                "maxItems": 100000,
            },
            "material_index": {"type": "integer", "minimum": 0, "maximum": 32767},
            "normal_direction": vector3,
            "min_dot": {"type": "number", "minimum": -1, "maximum": 1, "default": 0},
        },
        "additionalProperties": False,
    }
    registry.register(
        "uv.inspect",
        inspect_uv,
        description="Inspect bounded UV layers, bounds, selection/pin counts, optional loop pages, and island continuity audits.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "include_loops": {"type": "boolean", "default": False},
                "include_islands": {"type": "boolean", "default": False},
                "max_uv_items": uv_budget,
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "uv.audit", audit_uv,
        description="Audit one UV mesh or a shared multi-object atlas at a stated texture size; report self and cross-object overlap, bounded face-pair evidence and density checks.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1},
            "object_names": {"type": "array", "minItems": 1, "maxItems": 128, "uniqueItems": True,
                             "items": {"type": "string", "minLength": 1}},
            "layer_name": {"type": "string", "minLength": 1},
            "texture_size": {"type": "integer", "minimum": 32, "maximum": 16384},
            "density_floor_px_per_cm": {"type": "number", "minimum": 0, "maximum": 100000, "default": 0},
            "target_px_per_cm": {"type": "number", "minimum": 0, "maximum": 100000},
            "max_overlap_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "raster_size": {"type": "integer", "minimum": 64, "maximum": 4096, "default": 512},
            "max_uv_items": uv_budget,
            "max_overlap_pairs": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
        }, "required": ["texture_size"],
            "oneOf": [{"required": ["object_name"]}, {"required": ["object_names"]}],
            "additionalProperties": False},
    )
    registry.register("uv.seams_from_regions", seams_from_regions, toolset="uv",
        description="Plan or mark UV seams at FACE INT region boundaries and optional mesh boundaries; dry_run defaults true and never depends on selection.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1},
            "attribute_name": {"type": "string", "minLength": 1, "default": "part"},
            "include_boundary": {"type": "boolean", "default": True},
            "dry_run": {"type": "boolean", "default": True},
            "expected_state": expected_state,
        }, "required": ["object_name"], "additionalProperties": False})
    registry.register("uv.cut_strips_by_turn", cut_strips_by_turn, toolset="uv",
        description="Plan or mark whole transverse edge rows of explicit matching quad strips when accumulated centreline turn or segment count reaches a limit; dry_run defaults true.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1},
            "strips": {"type": "array", "minItems": 1, "maxItems": 128, "items": {"type": "object", "properties": {
                "edge_rows": {"type": "array", "minItems": 2, "maxItems": 10000, "items": {
                    "type": "array", "minItems": 1, "maxItems": 1000,
                    "items": {"type": "integer", "minimum": 0}}},
                "closed": {"type": "boolean", "default": False},
            }, "required": ["edge_rows"], "additionalProperties": False}},
            "turn_degrees": {"type": "number", "minimum": 0.001, "maximum": 180, "default": 15},
            "max_segments": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 10},
            "dry_run": {"type": "boolean", "default": True},
            "expected_state": expected_state,
        }, "required": ["object_name", "strips"], "additionalProperties": False})
    registry.register(
        "uv.layer_create",
        create_uv_layer,
        description="Create a bounded UV layer, optionally copying another layer and making it active.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "copy_from": {"type": "string", "minLength": 1},
                "set_active": {"type": "boolean", "default": True},
                "max_uv_items": uv_budget,
                "expected_state": expected_state,
            },
            "required": ["object_name", "name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "uv.layer_rename",
        rename_uv_layer,
        description="Rename one UV layer with collision checks and rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "new_name": {"type": "string", "minLength": 1},
                "expected_state": expected_state,
            },
            "required": ["object_name", "layer_name", "new_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "uv.layer_set_active",
        set_active_uv_layer,
        description="Set the editing UV layer and optionally make it the render or clone layer.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "set_render": {"type": "boolean", "default": False},
                "set_clone": {"type": "boolean", "default": False},
                "expected_state": expected_state,
            },
            "required": ["object_name", "layer_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "uv.layer_remove",
        remove_uv_layer,
        description="Dry-run-first destructive UV-layer removal with checkpoint recovery guidance.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "max_uv_items": uv_budget,
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "layer_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "uv.project",
        project_uv,
        description="Dry-run-first deterministic planar, cube, cylindrical, or spherical UV projection.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "create_layer": {"type": "boolean", "default": False},
                "mode": {"type": "string", "enum": ["PLANAR", "CUBE", "CYLINDER", "SPHERE"]},
                "plane": {"type": "string", "enum": ["XY", "XZ", "YZ"], "default": "XY"},
                "face_selection": face_selection,
                "normalize": {"type": "boolean", "default": False},
                "scale": vector2,
                "offset": vector2,
                "max_uv_items": uv_budget,
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "layer_name", "mode"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "uv.transform",
        transform_uv,
        description="Dry-run-first translation, rotation, and scale of selected UV face loops.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "face_selection": face_selection,
                "translation": vector2,
                "rotation": {"type": "number", "minimum": -6283.185307179586, "maximum": 6283.185307179586},
                "scale": vector2,
                "pivot": {"type": "string", "enum": ["ORIGIN", "CENTER", "MEDIAN"], "default": "MEDIAN"},
                "max_uv_items": uv_budget,
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "layer_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "uv.unwrap",
        unwrap_uv,
        description="Dry-run-first guarded Blender unwrap with UV and object-context rollback.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "create_layer": {"type": "boolean", "default": False},
                "face_selection": face_selection,
                "method": {"type": "string", "enum": ["ANGLE_BASED", "CONFORMAL", "SMART", "MINIMUM_STRETCH"], "default": "ANGLE_BASED"},
                "margin": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.001},
                "angle_limit": {"type": "number", "minimum": 0, "maximum": 1.5707963267948966},
                "island_margin": {"type": "number", "minimum": 0, "maximum": 1},
                "area_weight": {"type": "number", "minimum": 0, "maximum": 1},
                "fill_holes": {"type": "boolean", "default": True},
                "correct_aspect": {"type": "boolean", "default": True},
                "max_uv_items": uv_budget,
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "layer_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "uv.pack",
        pack_uv,
        description="Dry-run-first guarded island packing with full selection and mode restoration.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "layer_name": {"type": "string", "minLength": 1},
                "face_selection": face_selection,
                "rotate": {"type": "boolean", "default": True},
                "scale_to_fit": {"type": "boolean", "default": True},
                "average_island_scale": {"type": "boolean", "default": False},
                "margin": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.001},
                "max_uv_items": uv_budget,
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "layer_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "modifier.inspect",
        inspect_modifier,
        description="Inspect an object's ordered modifier stack and Blender RNA writable settings without mutation.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "modifier.add",
        add_modifier,
        description="Add a modifier valid for the object type with Blender RNA-validated initial settings.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "type": modifier_type,
                "name": {"type": "string", "minLength": 1},
                "settings": {"type": "object", "minProperties": 1},
                "expected_state": expected_state,
            },
            "required": ["object_name", "type"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "modifier.patch",
        patch_modifier,
        description="Patch Blender RNA writable modifier settings and visibility flags with field-level rollback metadata.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1},
                "settings": {"type": "object", "minProperties": 1},
                "show_viewport": {"type": "boolean"},
                "show_render": {"type": "boolean"},
                "show_in_editmode": {"type": "boolean"},
                "expected_state": expected_state,
            },
            "required": ["object_name", "modifier_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "modifier.reorder",
        reorder_modifier,
        description="Dry-run-first modifier stack reordering with rollback to the original index on failure.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "modifier_name": {"type": "string", "minLength": 1},
                "index": {"type": "integer", "minimum": 0},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "modifier_name", "index"],
            "additionalProperties": False,
        },
    )
    for tool_name, handler, description in (
        ("modifier.remove", remove_modifier, "Dry-run-first removal of one modifier; apply requires explicit confirmation."),
        ("modifier.apply", apply_modifier, "Dry-run-first destructive application of one mesh modifier; checkpoint and confirmation required."),
    ):
        registry.register(
            tool_name,
            handler,
            description=description,
            input_schema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string", "minLength": 1},
                    "modifier_name": {"type": "string", "minLength": 1},
                    "dry_run": {"type": "boolean", "default": True},
                    "confirm": {"type": "boolean", "const": True},
                    "expected_state": expected_state,
                    **({"fragment_small_ratio": {"type": "number", "exclusiveMinimum": 0,
                                                  "maximum": 1, "default": 0.01}}
                       if tool_name == "modifier.apply" else {}),
                },
                "required": ["object_name", "modifier_name"],
                "additionalProperties": False,
            },
        )
    registry.register(
        "object.convert_evaluated",
        convert_evaluated,
        description="Create a named mesh from evaluated geometry, retaining the source by default.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1},
            "keep_original": {"type": "boolean", "default": True},
            "fragment_small_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1, "default": 0.01},
            "dry_run": {"type": "boolean", "default": True},
            "confirm": {"type": "boolean", "const": True},
            "expected_state": expected_state,
        }, "required": ["object_name", "name"], "additionalProperties": False},
    )
    registry.register(
        "object.inspect",
        inspect_object,
        description="List a bounded filtered page of scene objects and optionally return full detail for one object.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "name_regex": {"type": "string", "minLength": 1},
                "type": {"type": "string", "minLength": 1},
                "collection": {"type": "string", "minLength": 1},
                "visible": {"type": "boolean"},
                "selected": {"type": "boolean"},
                "parent": {"type": "string", "minLength": 1},
                "material": {"type": "string", "minLength": 1},
                "custom_property": {"type": "string", "minLength": 1},
                "custom_value": {
                    "anyOf": [
                        {"type": "string"}, {"type": "number"},
                        {"type": "boolean"},
                    ]
                },
                "object_name": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.create",
        create_object,
        description="Create one allowlisted mesh primitive, empty, camera, or light without exposing arbitrary operators.",
        input_schema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["PLANE", "CUBE", "UV_SPHERE", "ICO_SPHERE", "CYLINDER", "CONE", "TORUS", "CIRCLE", "GRID", "MONKEY", "EMPTY", "CAMERA", "LIGHT"]},
                "name": {"type": "string", "minLength": 1},
                "location": vector3,
                "rotation": vector3,
                "scale": vector3,
                "collection": {"type": "string", "minLength": 1},
                "light_type": {"type": "string", "enum": ["POINT", "SUN", "SPOT", "AREA"]},
                "expected_state": expected_state,
            },
            "required": ["type"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.duplicate",
        duplicate_object,
        description="Duplicate one scene object with copied data by default or shared data when linked is true.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "linked": {"type": "boolean", "default": False},
                "collection": {"type": "string", "minLength": 1},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.rename",
        rename_object,
        description="Rename one object exactly, rejecting collisions unless allow_suffix is true.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "allow_suffix": {"type": "boolean", "default": False},
                "expected_state": expected_state,
            },
            "required": ["object_name", "name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.set_visibility",
        set_object_visibility,
        description="Patch viewport, render, or selection visibility flags for a bounded object set.",
        input_schema={
            "type": "object",
            "properties": {
                "objects": object_names,
                "hide_viewport": {"type": "boolean"},
                "hide_render": {"type": "boolean"},
                "hide_select": {"type": "boolean"},
                "expected_state": expected_state,
            },
            "required": ["objects"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.set_selection",
        set_object_selection,
        description="Select or deselect bounded scene objects, optionally replacing selection and assigning the active object.",
        input_schema={
            "type": "object",
            "properties": {
                "objects": object_names,
                "selected": {"type": "boolean"},
                "exclusive": {"type": "boolean", "default": False},
                "active": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
                "set_active": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true with active omitted/null to clear the active object.",
                },
                "expected_state": expected_state,
            },
            "required": ["objects", "selected"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.parent",
        parent_object,
        description="Dry-run-first hierarchy change that sets or clears one object's parent, preserving world transform by default.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "parent": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
                "keep_transform": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "parent"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.mode_set",
        set_object_mode,
        description="Dry-run-first mode switch for the already-active object; it does not choose a window or change active-object ownership.",
        input_schema={"type": "object", "properties": {"mode": {"type": "string", "enum": ["OBJECT", "EDIT", "POSE", "SCULPT", "VERTEX_PAINT", "WEIGHT_PAINT", "TEXTURE_PAINT"]}, "object_name": {"type": "string", "minLength": 1}, "dry_run": {"type": "boolean", "default": True}, "expected_state": expected_state}, "required": ["mode"], "additionalProperties": False},
    )
    registry.register(
        "object.align_to_workplane",
        align_object_to_workplane,
        description="Dry-run-first placement of an object on a face-derived or explicit reference-object workplane without using viewport state or the 3D Cursor.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "reference_object": {"type": "string", "minLength": 1},
                "plane": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": ["FACE", "EXPLICIT"]},
                    },
                    "required": ["source"],
                },
                "offset": {**vector3, "default": [0.0, 0.0, 0.0]},
                "axis_map": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 3,
                    "items": {"type": "string", "enum": ["U", "V", "N", "-U", "-V", "-N"]},
                    "default": ["U", "V", "N"],
                },
                "align_location": {"type": "boolean", "default": True},
                "align_rotation": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": True},
                "expected_state": expected_state,
            },
            "required": ["object_name", "reference_object", "plane"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.transform",
        transform_object,
        description="Read local and world transforms or patch selected transform fields in local or world space.",
        input_schema={
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "minLength": 1},
                "location": vector3,
                "rotation_euler": vector3,
                "scale": vector3,
                "space": {"type": "string", "enum": ["local", "world"], "default": "local"},
                "expected_state": expected_state,
            },
            "required": ["object_name"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "object.delete",
        delete_object,
        description="Dry-run-first object deletion. A real run requires dry_run false and confirm true; object data is retained for orphan management.",
        input_schema={
            "type": "object",
            "properties": {
                "objects": object_names,
                "dry_run": {"type": "boolean", "default": True},
                "confirm": {"type": "boolean", "const": True, "description": "Required with dry_run false to delete."},
                "expected_state": expected_state,
            },
            "required": ["objects"],
            "additionalProperties": False,
        },
    )
    registry.register(
        "scene.save_version",
        save_version,
        description="Non-overwriting active save-as for the current .blend; unlike checkpoint.save, subsequent calls use the new version.",
        input_schema={
            "type": "object",
            "properties": {
                "base": {"type": "string", "minLength": 1},
                "tag": {"type": "string", "minLength": 1},
                "expected_state": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional state_token that must still identify the active document state.",
                },
            },
            "additionalProperties": False,
        },
    )
    registry.register(
        "scene.save_as",
        save_as,
        description="Dry-run-first save of the active document to an explicit .blend path, including first save.",
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1},
            "dry_run": {"type": "boolean", "default": True},
            "overwrite": {"type": "boolean", "default": False},
            "confirm": {"type": "boolean", "const": True},
            "expected_state": expected_state,
        }, "required": ["path"], "additionalProperties": False},
    )
    registry.register(
        "scene.save_current",
        save_current,
        description="Save the active .blend in one call when its disk file is unchanged since load or last save. If disk identity changed, confirm:true plus matching expected_state saves by default; dry_run:true still plans only.",
        input_schema={"type": "object", "properties": {"dry_run": {"type": "boolean"}, "confirm": {"type": "boolean", "const": True}, "expected_state": expected_state}, "additionalProperties": False},
    )
    registry.register(
        "system.capabilities",
        capabilities_handler(registry.toolsets, registry.specs, registry.specs_for_toolset),
        description="List compact toolsets or inspect one toolset's authoritative add-on schemas with a stable capability digest.",
        input_schema={
            "type": "object",
            "properties": {
                "toolset": {"type": "string", "minLength": 1},
                "include_schemas": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"},
            },
            "additionalProperties": False,
        },
        toolset="system",
    )
    registry.register(
        "system.doctor",
        doctor_handler(registry.specs),
        description="Report non-secret add-on, Blender, document, bridge, and capability health, with per-tool input schema digests. Restart the MCP client after an adapter upgrade to refresh cached parameters of existing tools.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        toolset="system",
    )
    from .tools.asset_quality import quality_gate
    from .tools.asset_source_compare import compare_to_source
    from .tools.ai_source_bake import bake_from_ai_source
    from .tools.game_asset_export import export_game_asset
    registry.register(
        "asset.quality_gate", quality_gate, toolset="asset",
        description="Read-only bounded all-quad, loop, sliver, symmetry and self-intersection gate on a base mesh; incomplete scans fail. Pole count is complete; examples are capped at 20.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1},
            "part_attribute": {"type": "string", "minLength": 1},
            "require_quads": {"type": "boolean", "default": True},
            "triangle_budget": {"type": "integer", "minimum": 1},
            "sliver_min_edge": {"type": "number", "minimum": 0, "default": 0},
            "sliver_min_angle_deg": {"type": "number", "minimum": 0, "maximum": 180, "default": 1},
            "symmetry_plane_x": {"type": "number"},
            "symmetry_tolerance_m": {"type": "number", "minimum": 0},
            "allowed_loop_break_vertices": {"type": "array", "items": {"type": "integer", "minimum": 0}, "maxItems": 256},
        }, "required": ["object_name"], "additionalProperties": False},
    )
    registry.register(
        "asset.compare_to_source", compare_to_source, toolset="asset",
        description="Read-only CPU silhouette IoU, sampled deviation and outer-shell radial comparison against a source mesh; radial grid is omitted by default and can be requested or written as JSON with output_dir. Radial regions use paired absolute delta p90 unless radial_max_mm is explicit.",
        input_schema={"type": "object", "properties": {
            "low": {"type": "string", "minLength": 1}, "high": {"type": "string", "minLength": 1},
            "views": {"type": "array", "items": {"type": "string", "enum": ["front", "side", "top", "back", "left", "bottom"]}, "minItems": 1, "maxItems": 6},
            "resolution": {"type": "integer", "minimum": 32, "maximum": 2048, "default": 512},
            "sample_count": {"type": "integer", "minimum": 100, "maximum": 100000, "default": 10000},
            "spherical": {"type": "boolean", "default": False},
            "include_radial_grid": {"type": "boolean", "default": False},
            "center": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            "radial_theta_steps": {"type": "integer", "minimum": 4, "maximum": 360, "default": 72},
            "radial_phi_steps": {"type": "integer", "minimum": 3, "maximum": 181, "default": 37},
            "output_dir": {"type": "string", "minLength": 1},
            "thresholds": {"type": "object", "properties": {
                "iou_min": {"type": "number", "minimum": 0, "maximum": 1},
                "deviation_p99_mm": {"type": "number", "minimum": 0},
                "radial_max_mm": {"type": "number", "minimum": 0},
            }, "additionalProperties": False},
        }, "required": ["low", "high"], "additionalProperties": False},
    )
    registry.register(
        "bake.from_ai_source", bake_from_ai_source, toolset="bake",
        description="CPU Cycles AI-high to clean-low bake. Live calls queue a background job; snapshot occurs when it starts, then operation.status reports progress. Ray misses use nearest source-UV fallback bounded by fallback_max_distance (default: cage_extrusion + max_ray_distance). fill_parts assigns fixed linear base_color, roughness, metallic, AO and flat normal by FACE INT id; filled texels, per-part misses and sampled potential low-part occlusion are reported separately.",
        input_schema={"type": "object", "properties": {
            "low": {"type": "string", "minLength": 1}, "high": {"type": "string", "minLength": 1},
            "output_dir": {"type": "string", "minLength": 1},
            "resolution": {"type": "integer", "minimum": 16, "maximum": 4096, "default": 2048},
            "part_attribute": {"type": "string", "minLength": 1},
            "shell_parts": {"type": "array", "items": {"type": "integer"}},
            "fill_face_attribute": {"type": "string", "minLength": 1},
            "fill_parts": {"type": "object", "maxProperties": 64, "additionalProperties": {
                "type": "object", "properties": {
                    "base_color": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1}, "minItems": 3, "maxItems": 3},
                    "roughness": {"type": "number", "minimum": 0, "maximum": 1},
                    "metallic": {"type": "number", "minimum": 0, "maximum": 1},
                    "ao": {"type": "number", "minimum": 0, "maximum": 1},
                    "normal": {"type": "string", "const": "FLAT", "default": "FLAT"},
                }, "required": ["base_color", "roughness", "metallic", "ao"], "additionalProperties": False}},
            "cage_extrusion": {"type": "number", "minimum": 0, "maximum": 1000, "default": 0.01},
            "max_ray_distance": {"type": "number", "minimum": 0, "maximum": 1000, "default": 0},
            "fallback_max_distance": {"type": "number", "minimum": 0, "maximum": 1000,
                                      "description": "Maximum low-to-high nearest fallback distance in scene units; default is cage_extrusion + max_ray_distance, capped at 1000."},
            "highpass_sigma_px": {"type": "number", "minimum": 0, "maximum": 256, "default": 16},
            "normal_convention": {"type": "string", "enum": ["OPENGL", "DIRECTX"], "default": "OPENGL"},
            "pack": {"type": "string", "enum": ["ORM"]},
            "margin": {"type": "integer", "minimum": 0, "maximum": 128, "default": 16},
            "ao_samples": {"type": "integer", "minimum": 1, "maximum": 1024, "default": 64},
            "max_uv_overlap_pct": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.01},
            "expected_state": {"type": "string", "minLength": 1},
        }, "required": ["low", "high", "output_dir"], "additionalProperties": False},
    )
    registry.register(
        "export.game_asset", export_game_asset, toolset="export",
        description="Dry-run-first triangulated FBX and Unreal sidecar. OBJECT_ORIGIN preserves each object's pivot and assembly placement; ORIGIN and BOUNDS_CENTER use shared pivots. overwrite replaces only this call's exact output files with rollback; unrelated files remain. auto_convex may fill cavities; verify Unreal collision.",
        input_schema={"type": "object", "properties": {
            "object_names": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 128},
            "asset_name": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"},
            "output_dir": {"type": "string", "minLength": 1},
            "texture_paths": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}},
            "pivot": {"type": "string", "enum": ["ORIGIN", "BOUNDS_CENTER", "OBJECT_ORIGIN"], "default": "ORIGIN"},
            "overwrite": {"type": "boolean", "default": False},
            "lod_group": {"type": "string", "minLength": 1, "default": "SmallProp"},
            "collision": {"type": "string", "enum": ["auto_convex", "complex_as_simple", "ucx"], "default": "complex_as_simple"},
            "import_yaw": {"type": "number", "minimum": -360, "maximum": 360, "default": 0},
            "detail_normal": {"type": "string", "minLength": 1},
            "detail_tiling": {"type": "number", "minimum": 0.001, "maximum": 1000, "default": 1},
            "dry_run": {"type": "boolean", "default": True},
            "expected_state": {"type": "string", "minLength": 1},
        }, "required": ["object_names", "asset_name", "output_dir"], "additionalProperties": False},
    )
    from .tools.hard_surface_build import build_lathe, build_sweep, build_plate, build_slab, clear_of, fair_fit
    build_common = {
        "name": {"type": "string", "minLength": 1, "maxLength": 63},
        "part_attribute": {"type": "string", "minLength": 1, "maxLength": 63, "default": "part_id"},
        "part_id": {"type": "integer", "minimum": 0, "maximum": 2147483647, "default": 0},
        "expected_state": {"type": "string", "minLength": 1},
    }
    vec3_build = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
    slab_loop = {"type": "array", "minItems": 4, "maxItems": 64,
                 "items": {"anyOf": [vector2, vec3_build]}}
    registry.register("mesh.build_slab", build_slab, toolset="mesh",
        description="Build a bounded all-quad planar slab, through opening or blind pocket from matching ordered even-length outline/opening loops. The outline is the top face; thickness extends opposite its normal (negative Z for a counter-clockwise XY outline). FACE INT fregion labels distinguish top, walls, bottom and pocket floor. Unsupported or folded geometry refuses before mutation.",
        input_schema={"type": "object", "properties": {**build_common,
            "outline": slab_loop, "opening": slab_loop,
            "thickness": {"type": "number", "exclusiveMinimum": 0},
            "pocket_depth": {"type": "number", "exclusiveMinimum": 0},
        }, "required": ["name", "outline", "thickness"], "additionalProperties": False})
    registry.register("mesh.goal_deform", goal_deform, toolset="mesh",
                      description=GOAL_DEFORM_DESCRIPTION, input_schema=GOAL_DEFORM_SCHEMA)
    contour_common = ("object_name", "region", "max_displacement_mm", "pinned_vertex_group",
                      "symmetry", "clearance", "dry_run", "confirm", "expected_state")
    contour_schema = {
        "type": "object", "additionalProperties": False,
        "required": ["object_name", "region", "max_displacement_mm", "views"],
        "properties": {**{key: GOAL_DEFORM_SCHEMA["properties"][key] for key in contour_common},
                       "views": {"type": "array", "minItems": 2, "maxItems": 2,
                                 "description": "Exactly two perpendicular orthographic camera references, each with {camera_name, contour, width, height} or {camera_name, mask_path, threshold?, width?, height?}.",
                                 "items": {"type": "object"}}},
    }
    registry.register("mesh.fit_contours", fit_contours, toolset="mesh",
                      description=CONTOUR_FIT_DESCRIPTION, input_schema=contour_schema)
    registry.register("mesh.build_lathe", build_lathe, toolset="mesh",
        description="Construct an all-quad revolved profile with tangent 2D fillets, smooth faces, explicit or over-35-degree sharp edges and disk-mapped quad-grid caps; unsupported shapes do not mutate.",
        input_schema={"type": "object", "properties": {**build_common,
            "profile": {"type": "array", "items": {"type": "object", "properties": {
                "radius": {"type": "number", "exclusiveMinimum": 0}, "z": {"type": "number"},
                "sharp": {"type": "boolean"}, "round_radius": {"type": "number", "minimum": 0},
            }, "required": ["radius", "z"], "additionalProperties": False}, "minItems": 2, "maxItems": 128},
            "segments": {"type": "integer", "minimum": 4, "maximum": 512, "default": 32},
            "closed_profile": {"type": "boolean", "default": False},
            "cap_ends": {"type": "boolean", "default": True},
            "fillet_segments": {"type": "integer", "minimum": 1, "maximum": 32, "default": 4},
        }, "required": ["name", "profile"], "additionalProperties": False})
    registry.register("mesh.build_sweep", build_sweep, toolset="mesh",
        description="Construct a bounded all-quad transported profile sweep with smooth faces, over-35-degree sharp edges and miter checks; closed paths must be planar.",
        input_schema={"type": "object", "properties": {**build_common,
            "path": {"type": "array", "items": {"type": "object", "properties": {
                "point": vec3_build, "width": {"type": "number", "exclusiveMinimum": 0},
                "height": {"type": "number", "exclusiveMinimum": 0}, "chamfer": {"type": "number", "minimum": 0},
            }, "required": ["point", "width", "height"], "additionalProperties": False}, "minItems": 2, "maxItems": 512},
            "closed": {"type": "boolean", "default": False}, "up": vec3_build,
        }, "required": ["name", "path"], "additionalProperties": False})
    registry.register("mesh.build_plate", build_plate, toolset="mesh",
        description="Construct a smooth-shaded quad grid from 3-64 convex coplanar corners, with over-35-degree sharp edges; target-projected thickness grows outward and a bounded clearance search rejects unresolved penetration.",
        input_schema={"type": "object", "properties": {**build_common,
            "outline": {"type": "array", "items": {"anyOf": [vector2, vec3_build]}, "minItems": 3, "maxItems": 64},
            "subdivisions": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 64}, "minItems": 2, "maxItems": 2},
            "thickness": {"type": "number", "minimum": 0, "default": 0},
            "open_back": {"type": "boolean", "default": False},
            "target": {"type": "string", "minLength": 1},
            "projection_direction": vec3_build,
            "max_projection_distance": {"type": "number", "exclusiveMinimum": 0, "default": 10},
        }, "required": ["name", "outline"], "additionalProperties": False})
    registry.register("mesh.clear_of", clear_of, toolset="mesh",
        description="Analyze or apply bounded normal clearance of one face part from a planar or closed curved target with BVH and gap checks; hidden-face deletion is unsupported.",
        input_schema={"type": "object", "properties": {
            "object_name": {"type": "string", "minLength": 1}, "target": {"type": "string", "minLength": 1},
            "part_attribute": build_common["part_attribute"], "part_id": build_common["part_id"],
            "gap": {"type": "array", "items": {"type": "number", "minimum": 0}, "minItems": 2, "maxItems": 2},
            "max_iterations": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "step": {"type": "number", "exclusiveMinimum": 0, "default": 0.001},
            "apply": {"type": "boolean", "default": False},
            "delete_hidden": {"type": "boolean", "default": False},
            "expected_state": build_common["expected_state"],
        }, "required": ["object_name", "target", "gap"], "additionalProperties": False})
    registry.register("mesh.fair_fit", fair_fit, toolset="mesh",
        description="Analyze a robust symmetric low-order radial fit and optional all-quad nonpolar thick-band build; missing rays remain unsupported.",
        input_schema={"type": "object", "properties": {**build_common,
            "source": {"type": "string", "minLength": 1}, "center": vec3_build,
            "theta_samples": {"type": "integer", "minimum": 8, "maximum": 128, "default": 72},
            "phi_samples": {"type": "integer", "minimum": 3, "maximum": 64, "default": 25},
            "phi_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
            "order_m": {"type": "integer", "minimum": 0, "maximum": 8, "default": 4},
            "order_k": {"type": "integer", "minimum": 0, "maximum": 7, "default": 3},
            "iterations": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6},
            "rejection_sigma": {"type": "number", "exclusiveMinimum": 0, "default": 2.5},
            "thickness": {"type": "number", "exclusiveMinimum": 0},
            "apply": {"type": "boolean", "default": False},
        }, "required": ["source", "center"], "additionalProperties": False})
    return registry


_REGISTRY = create_registry()


def dispatch(
    tool_name: str,
    params: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a named tool. The returned envelope is safe to JSON-encode."""
    return _REGISTRY.dispatch(tool_name, params, context)


def tool_names() -> tuple[str, ...]:
    """Return the registered public tool names."""
    return _REGISTRY.names()


def tool_specs() -> tuple[dict[str, Any], ...]:
    """Return descriptions and JSON input schemas for transport adapters."""
    return _REGISTRY.specs()
