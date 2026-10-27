"""Guarded multi-object mesh join and separate workflows."""

from __future__ import annotations

from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .mesh_edit import (
    DEFAULT_ELEMENT_BUDGET,
    MAX_ELEMENT_BUDGET,
    _bm_counts,
    _clear_selection,
    _domain_elements,
    _element_total,
    _predicate_elements,
)
from .mesh_topology import (
    aggregate_components, append_new_island_summaries, capture_component_summary,
    fragment_small_ratio,
)
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


MAX_JOIN_SOURCES = 200
MAX_SEPARATE_PARTS = 2000


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(names)}")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _strict_bool(value: Any, label: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidParamsError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _enum(value: Any, label: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise InvalidParamsError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return value


def _object_names(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_JOIN_SOURCES:
        raise InvalidParamsError(
            f"{label} must be a non-empty list of at most {MAX_JOIN_SOURCES} object names"
        )
    names = [_name(item, label) for item in value]
    if len(names) != len(set(names)):
        raise InvalidParamsError(f"{label} must not contain duplicate names")
    return names


def _mesh_object(bpy: Any, name: str, role: str) -> Any:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f"Object not found in current scene: {name}")
    if obj.type != "MESH" or obj.data is None:
        raise InvalidStateError(f"{role} is not a mesh object: {name}")
    if getattr(obj, "library", None) is not None or getattr(obj, "override_library", None) is not None:
        raise InvalidStateError(f"{role} must be a local editable object: {name}")
    if getattr(obj.data, "library", None) is not None or getattr(obj.data, "override_library", None) is not None:
        raise InvalidStateError(f"{role} mesh data must be local and editable: {name}")
    if obj.data.shape_keys is not None:
        raise InvalidStateError(f"{role} has Shape Keys, which this workflow refuses: {name}")
    return obj


def _mesh_counts(mesh: Any) -> dict[str, int]:
    return {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "faces": len(mesh.polygons),
        "loops": len(mesh.loops),
    }


def _sum_counts(counts: list[dict[str, int]]) -> dict[str, int]:
    return {
        field: sum(item[field] for item in counts)
        for field in ("vertices", "edges", "faces", "loops")
    }


def _ensure_budget(counts: dict[str, int], max_elements: int, label: str) -> None:
    total = _element_total(counts)
    if total > max_elements:
        raise InvalidStateError(
            f"{label} has {total} total elements, above max_elements {max_elements}"
        )


def _object_custom_properties(obj: Any) -> list[str]:
    keys = getattr(obj, "keys", None)
    if not callable(keys):
        return []
    return sorted(str(key) for key in keys() if str(key) != "_RNA_UI")


def _join_source_issues(obj: Any) -> list[str]:
    issues = []
    if obj.parent is not None:
        issues.append("parent")
    if len(obj.children):
        issues.append("children")
    if len(obj.modifiers):
        issues.append("modifiers")
    if len(obj.constraints):
        issues.append("constraints")
    if obj.animation_data is not None:
        issues.append("animation_data")
    if _object_custom_properties(obj):
        issues.append("custom_properties")
    collection_users = len(obj.users_collection)
    if int(getattr(obj, "users", collection_users)) > collection_users:
        issues.append("external_datablock_users")
    if bool(obj.hide_select):
        issues.append("hide_select")
    if not bool(obj.visible_get()):
        issues.append("not_visible_in_view_layer")
    return issues


def _context_snapshot(bpy: Any) -> dict[str, Any]:
    if bpy.context.mode != "OBJECT":
        raise InvalidStateError("mesh.join and mesh.separate require Blender Object mode")
    active = bpy.context.view_layer.objects.active
    return {
        "selected": [obj.name for obj in bpy.context.selected_objects],
        "active": active.name if active is not None else None,
        "mesh_select_mode": tuple(bool(value) for value in bpy.context.tool_settings.mesh_select_mode),
    }


def _restore_context(bpy: Any, snapshot: dict[str, Any]) -> list[str]:
    warnings = []
    try:
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as exc:
        warnings.append(f"mode restoration failed: {exc}")
    try:
        for obj in list(bpy.context.selected_objects):
            obj.select_set(False)
        for name in snapshot["selected"]:
            obj = bpy.context.scene.objects.get(name)
            if obj is not None:
                obj.select_set(True)
        active_name = snapshot["active"]
        bpy.context.view_layer.objects.active = (
            bpy.context.scene.objects.get(active_name) if active_name is not None else None
        )
    except Exception as exc:
        warnings.append(f"selection restoration failed: {exc}")
    try:
        bpy.context.tool_settings.mesh_select_mode = snapshot["mesh_select_mode"]
    except Exception as exc:
        warnings.append(f"mesh select-mode restoration failed: {exc}")
    return warnings


def _select_only(bpy: Any, objects: list[Any], active: Any) -> None:
    for obj in list(bpy.context.selected_objects):
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active


def _snapshot_vertex_groups(obj: Any, max_entries: int) -> dict[str, Any]:
    groups = [
        {"name": str(group.name), "lock_weight": bool(group.lock_weight), "weights": []}
        for group in obj.vertex_groups
    ]
    entry_count = 0
    for vertex in obj.data.vertices:
        for membership in vertex.groups:
            index = int(membership.group)
            if 0 <= index < len(groups):
                groups[index]["weights"].append((int(vertex.index), float(membership.weight)))
                entry_count += 1
                if entry_count > max_entries:
                    raise InvalidStateError(
                        f"Vertex-group recovery has more than max_elements {max_entries} weight entries"
                    )
    active_index = int(obj.vertex_groups.active_index) if groups else 0
    return {"groups": groups, "active_index": active_index, "weight_entries": entry_count}


def _restore_vertex_groups(obj: Any, snapshot: dict[str, Any]) -> None:
    for group in list(obj.vertex_groups):
        obj.vertex_groups.remove(group)
    for record in snapshot["groups"]:
        group = obj.vertex_groups.new(name=record["name"])
        group.lock_weight = record["lock_weight"]
        for vertex_index, weight in record["weights"]:
            group.add([vertex_index], weight, "REPLACE")
    if snapshot["groups"]:
        obj.vertex_groups.active_index = min(
            snapshot["active_index"], len(snapshot["groups"]) - 1
        )


def _remove_orphan_mesh(bpy: Any, mesh: Any) -> None:
    if (
        mesh is not None
        and int(getattr(mesh, "users", 1)) == 0
        and bpy.data.meshes.get(mesh.name) is mesh
    ):
        bpy.data.meshes.remove(mesh)


def _remove_object_record(bpy: Any, record: dict[str, Any], *, remove_orphan_data: bool) -> None:
    obj = bpy.data.objects.get(record["name"])
    data = record.get("data")
    if obj is not None:
        bpy.data.objects.remove(obj, do_unlink=True)
    if remove_orphan_data:
        _remove_orphan_mesh(bpy, data)


def _source_backup(source: Any) -> dict[str, Any]:
    backup = source.copy()
    return {
        "object": backup,
        "name": backup.name,
        "original_name": source.name,
        "data": source.data,
        "collections": list(source.users_collection),
    }


def _restore_source_backups(bpy: Any, records: list[dict[str, Any]]) -> list[str]:
    errors = []
    for record in records:
        original_name = record["original_name"]
        current = bpy.data.objects.get(original_name)
        backup = bpy.data.objects.get(record["name"])
        if current is not None:
            if backup is not None:
                try:
                    bpy.data.objects.remove(backup, do_unlink=True)
                except Exception as exc:
                    errors.append(f"discard duplicate backup for {original_name}: {exc}")
            continue
        if backup is None:
            errors.append(f"source backup disappeared for {original_name}")
            continue
        try:
            backup.name = original_name
            collections = record["collections"] or [bpy.context.scene.collection]
            for collection in collections:
                if collection.objects.get(backup.name) is None:
                    collection.objects.link(backup)
            record["restored"] = True
        except Exception as exc:
            errors.append(f"restore source {original_name}: {exc}")
    return errors


def _restore_target(
    bpy: Any,
    target: Any,
    backup_mesh: Any,
    original_mesh_name: str,
    group_snapshot: dict[str, Any],
) -> list[str]:
    errors = []
    try:
        mutated_mesh = target.data
        target.data = backup_mesh
        _restore_vertex_groups(target, group_snapshot)
        _remove_orphan_mesh(bpy, mutated_mesh)
        backup_mesh.name = original_mesh_name
    except Exception as exc:
        errors.append(f"restore target {target.name}: {exc}")
    return errors


def _planned_names(existing: set[str], prefix: str, count: int) -> list[str]:
    result = []
    reserved = set(existing)
    for index in range(1, count + 1):
        base = f"{prefix}_{index:03d}"
        candidate = base
        suffix = 1
        while candidate in reserved:
            candidate = f"{base}.{suffix:03d}"
            suffix += 1
        reserved.add(candidate)
        result.append(candidate)
    return result


def join_meshes(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(
        params,
        {
            "target_object", "source_objects", "keep_sources", "max_elements",
            "dry_run", "confirm", "expected_state", "fragment_small_ratio",
        },
    )
    target_name = _name(params.get("target_object"), "target_object")
    source_names = _object_names(params.get("source_objects"), "source_objects")
    if target_name in source_names:
        raise InvalidParamsError("target_object must not also appear in source_objects")
    keep_sources = _strict_bool(params.get("keep_sources"), "keep_sources", False)
    max_elements = _integer(
        params.get("max_elements", DEFAULT_ELEMENT_BUDGET),
        "max_elements",
        1,
        MAX_ELEMENT_BUDGET,
    )
    if "dry_run" in params:
        _strict_bool(params["dry_run"], "dry_run")
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm")
    small_ratio = fragment_small_ratio(params.get("fragment_small_ratio", 0.01))
    bpy = get_bpy()
    context_snapshot = _context_snapshot(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    target = _mesh_object(bpy, target_name, "target_object")
    sources = [_mesh_object(bpy, name, "source_object") for name in source_names]
    if int(target.data.users) > 1:
        raise InvalidStateError("mesh.join refuses a target with shared mesh data")
    if bool(target.hide_select) or not bool(target.visible_get()):
        raise InvalidStateError("mesh.join target must be visible and selectable in the active view layer")
    if not keep_sources:
        unsafe = {
            source.name: issues
            for source in sources
            if (issues := _join_source_issues(source))
        }
        if unsafe:
            details = "; ".join(
                f"{name} ({', '.join(issues)})" for name, issues in sorted(unsafe.items())
            )
            raise InvalidStateError(
                "mesh.join can remove only plain, unreferenced source objects; "
                f"use keep_sources: true or simplify them first: {details}"
            )
    source_counts = {obj.name: _mesh_counts(obj.data) for obj in sources}
    target_counts = _mesh_counts(target.data)
    expected_counts = _sum_counts([target_counts, *source_counts.values()])
    _ensure_budget(expected_counts, max_elements, "Joined result")
    journal = [
        {
            "op": "join_into_target",
            "target": target.name,
            "before": target_counts,
            "after": expected_counts,
            "status": "planned",
        },
        *[
            {
                "op": "retain_source" if keep_sources else "remove_source",
                "target": source.name,
                "before": source_counts[source.name],
                "after": source_counts[source.name] if keep_sources else None,
                "status": "planned",
            }
            for source in sources
        ],
    ]
    affected = [
        {"type": "Object", "name": target.name},
        {"type": "Mesh", "name": target.data.name},
    ]
    for source in sources:
        affected.extend(
            [
                {"type": "Object", "name": source.name},
                {"type": "Mesh", "name": source.data.name},
            ]
        )
    transaction, apply = prepare_destructive_action(
        action="mesh.join",
        affected_datablocks=affected,
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={
            "target_object": target.name,
            "source_objects": [source.name for source in sources],
            "keep_sources": keep_sources,
            "before": {"target": target_counts, "sources": source_counts},
            "expected_after": expected_counts,
            "max_elements": max_elements,
        },
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    before_components = aggregate_components([
        capture_component_summary(transaction, item.data, item, small_ratio)
        for item in (target, *sources)
    ])
    group_snapshot = _snapshot_vertex_groups(target, max_elements)
    original_mesh_name = target.data.name
    backup_mesh = None
    source_backups: list[dict[str, Any]] = []
    temporary_records: list[dict[str, Any]] = []
    rollback_errors: list[str] = []
    try:
        backup_mesh = target.data.copy()
        join_sources = sources
        if keep_sources:
            collection = target.users_collection[0] if target.users_collection else bpy.context.scene.collection
            join_sources = []
            for source in sources:
                duplicate = source.copy()
                duplicate.hide_select = False
                duplicate.hide_viewport = False
                collection.objects.link(duplicate)
                try:
                    duplicate.hide_set(False)
                except Exception:
                    pass
                temporary_records.append(
                    {"object": duplicate, "name": duplicate.name, "data": duplicate.data}
                )
                join_sources.append(duplicate)
        else:
            source_backups = [_source_backup(source) for source in sources]
        _select_only(bpy, [target, *join_sources], target)
        result = bpy.ops.object.join()
        if "FINISHED" not in result or bpy.context.view_layer.objects.active is not target:
            raise RuntimeError(f"Blender object.join returned {sorted(result)}")
        if not keep_sources:
            remaining = [name for name in source_names if bpy.context.scene.objects.get(name) is not None]
            if remaining:
                raise RuntimeError(f"Blender retained joined source objects: {', '.join(remaining)}")
        actual_counts = _mesh_counts(target.data)
        _ensure_budget(actual_counts, max_elements, "Joined result")
        if actual_counts != expected_counts:
            raise RuntimeError(
                f"Joined topology differs from preflight: expected {expected_counts}, got {actual_counts}"
            )
        journal[0]["after"] = actual_counts
        journal[0]["status"] = "applied"
        for entry in journal[1:]:
            entry["status"] = "present" if keep_sources else "applied"
        transaction.update(after=actual_counts, changed=True)
        after_components = capture_component_summary(transaction, target.data, target, small_ratio)
        append_new_island_summaries(transaction, before_components, after_components, target.name)
        for record in source_backups:
            try:
                _remove_object_record(bpy, record, remove_orphan_data=True)
            except Exception as exc:
                transaction["warnings"].append(
                    f"source-backup cleanup failed for {record['original_name']}: {exc}"
                )
        if backup_mesh is not None:
            try:
                _remove_orphan_mesh(bpy, backup_mesh)
            except Exception as exc:
                transaction["warnings"].append(f"target-backup cleanup failed: {exc}")
        bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        return transaction
    except Exception as exc:
        for record in temporary_records:
            try:
                _remove_object_record(bpy, record, remove_orphan_data=False)
            except Exception as cleanup_exc:
                rollback_errors.append(f"remove temporary join source {record['name']}: {cleanup_exc}")
        if backup_mesh is not None:
            rollback_errors.extend(
                _restore_target(
                    bpy, target, backup_mesh, original_mesh_name, group_snapshot
                )
            )
        rollback_errors.extend(_restore_source_backups(bpy, source_backups))
        journal[0]["status"] = "failed"
        transaction["changed"] = bool(rollback_errors)
        transaction["warnings"].extend(f"join rollback failed: {item}" for item in rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"mesh.join failed: {exc}", transaction) from exc
    finally:
        transaction["warnings"].extend(_restore_context(bpy, context_snapshot))


def _loose_component_sizes(bm: Any, max_parts: int) -> list[int]:
    vertices = _domain_elements(bm, "VERT")
    visited = set()
    sizes = []
    for seed in vertices:
        if seed in visited:
            continue
        pending = [seed]
        visited.add(seed)
        size = 0
        while pending:
            vert = pending.pop()
            size += 1
            for edge in vert.link_edges:
                neighbor = edge.other_vert(vert)
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        sizes.append(size)
        if len(sizes) > max_parts:
            raise InvalidStateError(f"Loose-part count exceeds max_parts {max_parts}")
    return sorted(sizes, reverse=True)


def _separation_plan(
    bm: Any,
    mode: str,
    selection: Any,
    max_parts: int,
) -> dict[str, Any]:
    counts = _bm_counts(bm)
    if not counts["vertices"]:
        raise InvalidStateError("mesh.separate requires a non-empty mesh")
    if mode == "LOOSE":
        if selection is not None:
            raise InvalidParamsError("selection is only valid when mode is SELECTION")
        sizes = _loose_component_sizes(bm, max_parts)
        return {
            "mode": mode,
            "result_object_count": len(sizes),
            "new_object_count": max(0, len(sizes) - 1),
            "component_vertex_counts": sizes,
            "selection": None,
        }
    if mode == "MATERIAL":
        if selection is not None:
            raise InvalidParamsError("selection is only valid when mode is SELECTION")
        material_indices = sorted({int(face.material_index) for face in bm.faces})
        if not material_indices:
            raise InvalidStateError("MATERIAL separation requires at least one face")
        if len(material_indices) > max_parts:
            raise InvalidStateError(f"Material-part count exceeds max_parts {max_parts}")
        return {
            "mode": mode,
            "result_object_count": len(material_indices),
            "new_object_count": max(0, len(material_indices) - 1),
            "material_indices": material_indices,
            "selection": None,
        }
    if not isinstance(selection, dict):
        raise InvalidParamsError("SELECTION mode requires selection")
    _unknown(selection, {"domain", "predicate"})
    domain = _enum(selection.get("domain"), "selection.domain", {"VERT", "EDGE", "FACE"})
    matches = _predicate_elements(bm, domain, selection.get("predicate"))
    if not matches:
        raise InvalidStateError("SELECTION separation matched no mesh elements")
    selected_vertices = set()
    if domain == "VERT":
        selected_vertices.update(matches)
    else:
        for element in matches:
            selected_vertices.update(element.verts)
    if len(selected_vertices) == len(bm.verts):
        raise InvalidStateError(
            "SELECTION separation would remove every vertex from the source object"
        )
    if max_parts < 2:
        raise InvalidStateError("SELECTION separation requires max_parts of at least 2")
    return {
        "mode": mode,
        "result_object_count": 2,
        "new_object_count": 1,
        "selection": {
            "domain": domain,
            "matched": len(matches),
            "selected_vertices": len(selected_vertices),
        },
    }


def _apply_edit_selection(bm: Any, selection: dict[str, Any]) -> None:
    domain = selection["domain"]
    matches = _predicate_elements(bm, domain, selection.get("predicate"))
    _clear_selection(bm)
    for element in matches:
        element.select_set(True)
    bm.select_flush_mode()


def separate_mesh(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(
        params,
        {
            "object_name", "mode", "selection", "name_prefix", "max_parts",
            "max_elements", "dry_run", "confirm", "expected_state", "fragment_small_ratio",
        },
    )
    object_name = _name(params.get("object_name"), "object_name")
    mode = _enum(params.get("mode"), "mode", {"LOOSE", "MATERIAL", "SELECTION"})
    selection = params.get("selection")
    name_prefix = params.get("name_prefix")
    if name_prefix is not None:
        name_prefix = _name(name_prefix, "name_prefix")
    max_parts = _integer(params.get("max_parts", 100), "max_parts", 2, MAX_SEPARATE_PARTS)
    max_elements = _integer(
        params.get("max_elements", DEFAULT_ELEMENT_BUDGET),
        "max_elements",
        1,
        MAX_ELEMENT_BUDGET,
    )
    if "dry_run" in params:
        _strict_bool(params["dry_run"], "dry_run")
    if "confirm" in params:
        _strict_bool(params["confirm"], "confirm")
    small_ratio = fragment_small_ratio(params.get("fragment_small_ratio", 0.01))
    bpy = get_bpy()
    context_snapshot = _context_snapshot(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    obj = _mesh_object(bpy, object_name, "object_name")
    if int(obj.data.users) > 1:
        raise InvalidStateError("mesh.separate refuses shared mesh data")
    if bool(obj.hide_select) or not bool(obj.visible_get()):
        raise InvalidStateError("mesh.separate target must be visible and selectable in the active view layer")
    before_counts = _mesh_counts(obj.data)
    _ensure_budget(before_counts, max_elements, "Source mesh")
    import bmesh

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        plan = _separation_plan(bm, mode, selection, max_parts)
    finally:
        bm.free()
    prefix = name_prefix or f"{obj.name}_part"
    planned_names = _planned_names(
        {item.name for item in bpy.data.objects}, prefix, plan["new_object_count"]
    )
    changed = plan["new_object_count"] > 0
    journal = [
        {
            "op": "separate",
            "target": obj.name,
            "before": before_counts,
            "after": {
                "result_object_count": plan["result_object_count"],
                "planned_new_objects": planned_names,
            },
            "status": "planned" if changed else "present",
        }
    ]
    transaction, apply = prepare_destructive_action(
        action="mesh.separate",
        affected_datablocks=[
            {"type": "Object", "name": obj.name},
            {"type": "Mesh", "name": obj.data.name},
        ],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={
            "object": obj.name,
            "mode": mode,
            "plan": plan,
            "planned_new_objects": planned_names,
            "before": before_counts,
            "max_parts": max_parts,
            "max_elements": max_elements,
        },
    )
    if not apply or not changed:
        return transaction
    require_document_write_access(bpy)
    before_components = capture_component_summary(transaction, obj.data, obj, small_ratio)
    group_snapshot = _snapshot_vertex_groups(obj, max_elements)
    original_mesh_name = obj.data.name
    backup_mesh = None
    before_objects = set(bpy.context.scene.objects)
    created: list[Any] = []
    rollback_errors: list[str] = []
    try:
        backup_mesh = obj.data.copy()
        _select_only(bpy, [obj], obj)
        if mode == "SELECTION":
            domain = selection["domain"]
            bpy.context.tool_settings.mesh_select_mode = (
                domain == "VERT",
                domain == "EDGE",
                domain == "FACE",
            )
        result = bpy.ops.object.mode_set(mode="EDIT")
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender could not enter Edit mode: {sorted(result)}")
        if mode == "SELECTION":
            edit_bm = bmesh.from_edit_mesh(obj.data)
            _apply_edit_selection(edit_bm, selection)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        result = bpy.ops.mesh.separate(type="SELECTED" if mode == "SELECTION" else mode)
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender mesh.separate returned {sorted(result)}")
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        created = [item for item in bpy.context.scene.objects if item not in before_objects]
        if len(created) != plan["new_object_count"]:
            raise RuntimeError(
                f"Blender created {len(created)} objects; preflight expected {plan['new_object_count']}"
            )
        created.sort(key=lambda item: item.name)
        for created_obj, planned_name in zip(created, planned_names):
            created_obj.name = planned_name
            if created_obj.name != planned_name:
                raise RuntimeError(
                    f"Blender could not assign deterministic result name {planned_name}"
                )
        result_objects = [obj, *created]
        result_counts = {item.name: _mesh_counts(item.data) for item in result_objects}
        combined_counts = _sum_counts(list(result_counts.values()))
        _ensure_budget(combined_counts, max_elements, "Separated result")
        journal[0]["after"] = {
            "result_objects": [item.name for item in result_objects],
            "counts": result_counts,
            "combined": combined_counts,
        }
        journal[0]["status"] = "applied"
        transaction.update(
            objects=[item.name for item in result_objects],
            created_objects=[item.name for item in created],
            after=result_counts,
            combined_after=combined_counts,
            changed=True,
        )
        after_components = aggregate_components([
            capture_component_summary(transaction, item.data, item, small_ratio)
            for item in result_objects
        ])
        append_new_island_summaries(transaction, before_components, after_components, obj.name)
        if backup_mesh is not None:
            try:
                _remove_orphan_mesh(bpy, backup_mesh)
            except Exception as exc:
                transaction["warnings"].append(f"separate-backup cleanup failed: {exc}")
        bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        return transaction
    except Exception as exc:
        try:
            if bpy.context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception as mode_exc:
            rollback_errors.append(f"leave Edit mode: {mode_exc}")
        created = [item for item in bpy.context.scene.objects if item not in before_objects]
        for created_obj in created:
            record = {"name": created_obj.name, "data": created_obj.data}
            try:
                _remove_object_record(bpy, record, remove_orphan_data=True)
            except Exception as cleanup_exc:
                rollback_errors.append(f"remove separated object {record['name']}: {cleanup_exc}")
        if backup_mesh is not None:
            rollback_errors.extend(
                _restore_target(bpy, obj, backup_mesh, original_mesh_name, group_snapshot)
            )
        journal[0]["status"] = "failed"
        transaction["changed"] = bool(rollback_errors)
        transaction["warnings"].extend(
            f"separate rollback failed: {item}" for item in rollback_errors
        )
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"mesh.separate failed: {exc}", transaction) from exc
    finally:
        transaction["warnings"].extend(_restore_context(bpy, context_snapshot))
