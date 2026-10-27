"""Bounded collection inspection and guarded collection graph mutations."""

from __future__ import annotations

import re
from typing import Any

from ..errors import (
    InvalidParamsError,
    InvalidStateError,
    MutationApplyError,
    NameConflictError,
    NotFoundError,
)
from ..registry import ToolResult
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction


MAX_COLLECTION_BATCH = 2000


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    names = sorted(set(params) - allowed)
    if names:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(names)}")


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{label} must be a non-empty string")
    return value


def _optional_name(value: Any, label: str) -> str | None:
    return None if value is None else _name(value, label)


def _strict_bool(value: Any, label: str, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{label} must be a boolean")
    return value


def _validate_expected(params: dict[str, Any]) -> None:
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")


def _page_params(params: dict[str, Any]) -> tuple[int, int]:
    limit = params.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
        raise InvalidParamsError("limit must be an integer from 1 to 2000")
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError(
        "cursor must be a non-negative decimal string returned by collection.inspect"
    )


def _names(value: Any, label: str) -> list[str]:
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, list) or not raw or len(raw) > MAX_COLLECTION_BATCH:
        raise InvalidParamsError(
            f"{label} must be a name or a non-empty list of at most {MAX_COLLECTION_BATCH} names"
        )
    names = [_name(item, label) for item in raw]
    if len(names) != len(set(names)):
        raise InvalidParamsError(f"{label} must not contain duplicate names")
    return names


def _find_scene_collection(root: Any, name: str) -> Any | None:
    if root.name == name:
        return root
    for child in root.children:
        found = _find_scene_collection(child, name)
        if found is not None:
            return found
    return None


def _scene_collections(root: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[int] = set()

    def walk(collection: Any) -> None:
        identity = id(collection)
        if identity in seen:
            return
        seen.add(identity)
        result.append(collection)
        for child in collection.children:
            walk(child)

    walk(root)
    return result


def _parents(root: Any, target: Any) -> list[Any]:
    return sorted(
        # Blender 5.2's bpy_prop_collection.__contains__ accepts a name (or an
        # RNA lookup tuple), not a Collection instance.  Use an explicit lookup
        # so this stays valid across the collection implementations used by
        # supported Blender versions.
        (
            collection
            for collection in _scene_collections(root)
            if collection.children.get(target.name) is target
        ),
        key=lambda item: item.name,
    )


def _descendants(collection: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[int] = set()

    def walk(parent: Any) -> None:
        for child in parent.children:
            identity = id(child)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(child)
            walk(child)

    walk(collection)
    return result


def _collection_detail(root: Any, collection: Any) -> dict[str, Any]:
    descendants = _descendants(collection)
    direct_objects = sorted(obj.name for obj in collection.objects)
    all_objects = sorted({obj.name for item in [collection, *descendants] for obj in item.objects})
    return {
        "name": collection.name,
        "parents": [parent.name for parent in _parents(root, collection)],
        "children": sorted(child.name for child in collection.children),
        "objects": direct_objects,
        "object_count": len(direct_objects),
        "recursive_object_count": len(all_objects),
        "descendant_count": len(descendants),
        "hide_viewport": bool(collection.hide_viewport),
        "hide_render": bool(collection.hide_render),
        "hide_select": bool(collection.hide_select),
        "custom_properties": sorted(
            str(key) for key in collection.keys() if str(key) != "_RNA_UI"
        ),
    }


def _collection_summary(root: Any, collection: Any) -> dict[str, Any]:
    return {
        "name": collection.name,
        "parents": [parent.name for parent in _parents(root, collection)],
        "child_count": len(collection.children),
        "object_count": len(collection.objects),
        "hide_viewport": bool(collection.hide_viewport),
        "hide_render": bool(collection.hide_render),
        "hide_select": bool(collection.hide_select),
    }


def inspect_collection(params: dict[str, Any]) -> ToolResult:
    _unknown(
        params,
        {
            "name", "name_regex", "parent", "hidden_viewport", "hidden_render",
            "custom_property", "custom_value", "limit", "cursor", "collection_name",
        },
    )
    substring = _optional_name(params.get("name"), "name")
    regex_value = _optional_name(params.get("name_regex"), "name_regex")
    try:
        pattern = re.compile(regex_value) if regex_value is not None else None
    except re.error as exc:
        raise InvalidParamsError(f"name_regex is invalid: {exc}") from exc
    parent_name = _optional_name(params.get("parent"), "parent")
    hidden_viewport = (
        None if "hidden_viewport" not in params else _strict_bool(params["hidden_viewport"], "hidden_viewport")
    )
    hidden_render = (
        None if "hidden_render" not in params else _strict_bool(params["hidden_render"], "hidden_render")
    )
    custom_property = _optional_name(params.get("custom_property"), "custom_property")
    if "custom_value" in params and custom_property is None:
        raise InvalidParamsError("custom_value requires custom_property")
    custom_value = params.get("custom_value")
    if "custom_value" in params and not (
        custom_value is None or isinstance(custom_value, (str, int, float, bool))
    ):
        raise InvalidParamsError("custom_value must be a JSON scalar or null")
    detail_name = _optional_name(params.get("collection_name"), "collection_name")
    limit, offset = _page_params(params)
    bpy = get_bpy()
    root = bpy.context.scene.collection
    parent = None
    if parent_name is not None:
        parent = _find_scene_collection(root, parent_name)
        if parent is None:
            raise NotFoundError(f"Collection not found in current scene: {parent_name}")
    candidates = []
    for collection in _scene_collections(root):
        if substring is not None and substring not in collection.name:
            continue
        if pattern is not None and pattern.search(collection.name) is None:
            continue
        if parent is not None and collection not in parent.children:
            continue
        if hidden_viewport is not None and bool(collection.hide_viewport) != hidden_viewport:
            continue
        if hidden_render is not None and bool(collection.hide_render) != hidden_render:
            continue
        if custom_property is not None:
            if custom_property not in collection.keys():
                continue
            if "custom_value" in params and collection[custom_property] != custom_value:
                continue
        candidates.append(collection)
    candidates.sort(key=lambda item: item.name)
    page = candidates[offset : offset + limit]
    next_offset = offset + len(page)
    truncated = next_offset < len(candidates)
    data: dict[str, Any] = {
        "count": len(candidates),
        "collections": [_collection_summary(root, item) for item in page],
        "next_cursor": str(next_offset) if truncated else None,
        "truncated": truncated,
        "state_token": current_state_token(bpy),
    }
    if detail_name is not None:
        detail = _find_scene_collection(root, detail_name)
        if detail is None:
            raise NotFoundError(f"Collection not found in current scene: {detail_name}")
        data["collection_detail"] = _collection_detail(root, detail)
    return ToolResult(data=data)


def _prepare_mutation(params: dict[str, Any]) -> tuple[Any, Any]:
    _validate_expected(params)
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    return bpy, bpy.context.scene.collection


def _scene_collection(root: Any, name: str, *, allow_root: bool = True) -> Any:
    collection = _find_scene_collection(root, name)
    if collection is None:
        raise NotFoundError(f"Collection not found in current scene: {name}")
    if not allow_root and collection is root:
        raise InvalidParamsError("The scene root collection cannot be mutated by this operation")
    return collection


def create_collection(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"name", "parent", "expected_state"})
    requested_name = _name(params.get("name"), "name")
    parent_name = _optional_name(params.get("parent"), "parent")
    bpy, root = _prepare_mutation(params)
    parent = root if parent_name is None else _scene_collection(root, parent_name)
    resolved_name = requested_name
    index = 1
    while bpy.data.collections.get(resolved_name) is not None:
        resolved_name = f"{requested_name}.{index:03d}"
        index += 1
    collection = None
    try:
        collection = bpy.data.collections.new(resolved_name)
        parent.children.link(collection)
        detail = _collection_detail(root, collection)
    except Exception as exc:
        cleanup_errors = []
        if collection is not None and bpy.data.collections.get(collection.name) is collection:
            try:
                bpy.data.collections.remove(collection)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove collection {collection.name}: {cleanup_exc}")
        transaction = new_transaction([{"type": "Collection", "name": resolved_name}])
        transaction.update(
            requested_name=requested_name,
            resolved_name=resolved_name,
            parent=parent.name,
            changed=bool(cleanup_errors),
            warnings=cleanup_errors,
            journal=[{"op": "create", "target": resolved_name, "before": None, "after": None, "status": "failed"}],
            state_token=current_state_token(bpy),
        )
        if cleanup_errors:
            bump_epoch()
            transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"collection.create failed: {exc}", transaction) from exc
    transaction = new_transaction([{"type": "Collection", "name": collection.name}])
    transaction.update(
        collection=collection.name,
        requested_name=requested_name,
        resolved_name=collection.name,
        parent=parent.name,
        detail=detail,
        changed=True,
        journal=[{"op": "create", "target": collection.name, "before": None, "after": detail, "status": "applied"}],
    )
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def rename_collection(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"collection_name", "name", "allow_suffix", "expected_state"})
    old_name = _name(params.get("collection_name"), "collection_name")
    requested_name = _name(params.get("name"), "name")
    allow_suffix = _strict_bool(params.get("allow_suffix"), "allow_suffix", False)
    bpy, root = _prepare_mutation(params)
    collection = _scene_collection(root, old_name, allow_root=False)
    conflict = bpy.data.collections.get(requested_name)
    if conflict is not None and conflict is not collection and not allow_suffix:
        raise NameConflictError(
            f"Collection name already exists: {requested_name}",
            {"conflict": {"type": "Collection", "name": requested_name}},
        )
    before = collection.name
    collection.name = requested_name
    after = collection.name
    transaction = new_transaction([{"type": "Collection", "name": after}])
    transaction.update(
        collection=after,
        requested_name=requested_name,
        resolved_name=after,
        changed=after != before,
        journal=[{"op": "rename", "target": before, "before": before, "after": after, "status": "applied" if after != before else "unchanged"}],
    )
    if after != before:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def set_collection_visibility(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"collections", "hide_viewport", "hide_render", "hide_select", "expected_state"})
    names = _names(params.get("collections"), "collections")
    values = {
        key: _strict_bool(params[key], key)
        for key in ("hide_viewport", "hide_render", "hide_select")
        if key in params
    }
    if not values:
        raise InvalidParamsError("At least one visibility field must be supplied")
    bpy, root = _prepare_mutation(params)
    collections = [_scene_collection(root, name) for name in names]
    before = {item.name: {key: bool(getattr(item, key)) for key in values} for item in collections}
    journal = []
    changed = False
    try:
        for collection in collections:
            target = {key: values[key] for key in values}
            item_changed = before[collection.name] != target
            for key, value in values.items():
                setattr(collection, key, value)
            changed = changed or item_changed
            journal.append({"op": "set_visibility", "target": collection.name, "before": before[collection.name], "after": target, "status": "applied" if item_changed else "unchanged"})
    except Exception as exc:
        rollback_errors = []
        for collection in collections:
            try:
                for key, value in before[collection.name].items():
                    setattr(collection, key, value)
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {collection.name}: {rollback_exc}")
        if not rollback_errors:
            for entry in journal:
                if entry["status"] == "applied":
                    entry["status"] = "rolled_back"
        transaction = new_transaction([{"type": "Collection", "name": item.name} for item in collections])
        transaction.update(collections=names, changed=bool(rollback_errors), journal=journal, warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"collection.set_visibility failed: {exc}", transaction) from exc
    transaction = new_transaction([{"type": "Collection", "name": item.name} for item in collections])
    transaction.update(collections=names, changed=changed, journal=journal)
    if changed:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def link_collection_objects(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"collection", "objects", "expected_state"})
    collection_name = _name(params.get("collection"), "collection")
    object_names = _names(params.get("objects"), "objects")
    bpy, root = _prepare_mutation(params)
    collection = _scene_collection(root, collection_name)
    objects = []
    for name in object_names:
        obj = bpy.context.scene.objects.get(name)
        if obj is None:
            raise NotFoundError(f"Object not found in current scene: {name}")
        objects.append(obj)
    linked = []
    journal = []
    try:
        for obj in objects:
            if collection in obj.users_collection:
                journal.append({"op": "link_object", "target": obj.name, "collection": collection.name, "status": "unchanged"})
                continue
            collection.objects.link(obj)
            linked.append(obj)
            journal.append({"op": "link_object", "target": obj.name, "collection": collection.name, "status": "applied"})
    except Exception as exc:
        rollback_errors = []
        for obj in reversed(linked):
            try:
                collection.objects.unlink(obj)
            except Exception as rollback_exc:
                rollback_errors.append(f"unlink {obj.name}: {rollback_exc}")
        if not rollback_errors:
            for entry in journal:
                if entry["status"] == "applied":
                    entry["status"] = "rolled_back"
        transaction = new_transaction([{"type": "Object", "name": item.name} for item in objects] + [{"type": "Collection", "name": collection.name}])
        transaction.update(collection=collection.name, objects=object_names, changed=bool(rollback_errors), journal=journal, warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"collection.link_objects failed: {exc}", transaction) from exc
    transaction = new_transaction([{"type": "Object", "name": item.name} for item in objects] + [{"type": "Collection", "name": collection.name}])
    transaction.update(collection=collection.name, objects=object_names, changed=bool(linked), journal=journal)
    if linked:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def parent_collection_objects(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"collection", "parent", "recursive", "keep_transform", "max_objects", "dry_run", "expected_state"})
    collection_name = _name(params.get("collection"), "collection")
    parent_name = _optional_name(params.get("parent"), "parent")
    recursive = params.get("recursive", False)
    keep_transform = params.get("keep_transform", True)
    dry_run = params.get("dry_run", True)
    max_objects = params.get("max_objects", 2000)
    if not isinstance(recursive, bool) or not isinstance(keep_transform, bool) or not isinstance(dry_run, bool):
        raise InvalidParamsError("recursive, keep_transform, and dry_run must be booleans")
    if isinstance(max_objects, bool) or not isinstance(max_objects, int) or not 1 <= max_objects <= MAX_COLLECTION_BATCH:
        raise InvalidParamsError(f"max_objects must be an integer from 1 to {MAX_COLLECTION_BATCH}")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    root = bpy.context.scene.collection
    collection = _scene_collection(root, collection_name)
    parent = bpy.context.scene.objects.get(parent_name) if parent_name is not None else None
    if parent_name is not None and parent is None:
        raise NotFoundError(f"Object not found in current scene: {parent_name}")
    collections = [collection, *_descendants(collection)] if recursive else [collection]
    by_name = {obj.name: obj for item in collections for obj in item.objects}
    objects = [by_name[name] for name in sorted(by_name)]
    if not objects:
        raise InvalidStateError(f"Collection contains no target objects: {collection.name}")
    if len(objects) > max_objects:
        raise InvalidStateError(f"collection.parent_objects targets {len(objects)} objects above max_objects {max_objects}")
    from .object import _would_create_parent_cycle
    for obj in objects:
        if _would_create_parent_cycle(obj, parent):
            raise InvalidParamsError(f"parent would create an object hierarchy cycle for {obj.name}")
    changed_objects = [obj for obj in objects if obj.parent is not parent]
    journal = [{"op": "parent", "target": obj.name, "before": obj.parent.name if obj.parent else None, "after": parent.name if parent else None, "status": "planned"} for obj in changed_objects]
    transaction, apply = prepare_destructive_action(
        action="collection.parent_objects",
        affected_datablocks=[{"type": "Object", "name": obj.name} for obj in objects],
        journal=journal,
        dry_run=dry_run,
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"collection": collection.name, "parent": parent.name if parent else None, "recursive": recursive, "object_count": len(objects), "changed_count": len(changed_objects), "keep_transform": keep_transform},
    )
    if not apply or not changed_objects:
        return transaction
    require_document_write_access(bpy)
    snapshots = [(obj, obj.parent, obj.matrix_parent_inverse.copy(), obj.matrix_basis.copy(), obj.matrix_world.copy()) for obj in changed_objects]
    applied = 0
    try:
        for index, (obj, _old_parent, _inverse, _basis, world) in enumerate(snapshots):
            obj.parent = parent
            if keep_transform:
                obj.matrix_world = world
            else:
                obj.matrix_parent_inverse.identity()
            journal[index]["status"] = "applied"
            applied += 1
        bpy.context.view_layer.update()
    except Exception as exc:
        rollback_errors = []
        for obj, old_parent, inverse, basis, _world in reversed(snapshots[:applied]):
            try:
                obj.parent = old_parent
                obj.matrix_parent_inverse = inverse
                obj.matrix_basis = basis
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {obj.name}: {rollback_exc}")
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"collection.parent_objects failed: {exc}", transaction) from exc
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def transform_collection_objects(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"collection", "translation", "rotation_euler", "scale", "recursive", "max_objects", "dry_run", "expected_state"})
    collection_name = _name(params.get("collection"), "collection")
    recursive = params.get("recursive", False)
    dry_run = params.get("dry_run", True)
    max_objects = params.get("max_objects", 2000)
    if not isinstance(recursive, bool) or not isinstance(dry_run, bool):
        raise InvalidParamsError("recursive and dry_run must be booleans")
    if isinstance(max_objects, bool) or not isinstance(max_objects, int) or not 1 <= max_objects <= MAX_COLLECTION_BATCH:
        raise InvalidParamsError(f"max_objects must be an integer from 1 to {MAX_COLLECTION_BATCH}")
    from .object import _vector3
    translation = _vector3(params.get("translation", [0, 0, 0]), "translation")
    rotation = _vector3(params.get("rotation_euler", [0, 0, 0]), "rotation_euler")
    scale = _vector3(params.get("scale", [1, 1, 1]), "scale")
    if any(value <= 0 for value in scale):
        raise InvalidParamsError("scale values must be greater than zero")
    if translation == [0.0, 0.0, 0.0] and rotation == [0.0, 0.0, 0.0] and scale == [1.0, 1.0, 1.0]:
        raise InvalidParamsError("At least one transform component must change")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    root = bpy.context.scene.collection
    collection = _scene_collection(root, collection_name)
    collections = [collection, *_descendants(collection)] if recursive else [collection]
    by_name = {obj.name: obj for item in collections for obj in item.objects}
    objects = [by_name[name] for name in sorted(by_name)]
    if not objects:
        raise InvalidStateError(f"Collection contains no target objects: {collection.name}")
    if len(objects) > max_objects:
        raise InvalidStateError(f"collection.transform targets {len(objects)} objects above max_objects {max_objects}")
    target_ids = {id(obj) for obj in objects}
    external_children = sorted({child.name for obj in objects for child in obj.children_recursive if id(child) not in target_ids})
    if external_children:
        preview = ", ".join(external_children[:10])
        raise InvalidStateError(f"collection.transform would also move descendants outside its target set: {preview}")
    from mathutils import Euler, Matrix, Vector
    delta = Matrix.Translation(Vector(translation)) @ Euler(rotation, "XYZ").to_matrix().to_4x4() @ Matrix.Diagonal((*scale, 1.0))
    snapshots = [(obj, obj.matrix_world.copy()) for obj in objects]
    desired = [(obj, delta @ world) for obj, world in snapshots]
    journal = [{"op": "transform", "target": obj.name, "status": "planned"} for obj in objects]
    transaction, apply = prepare_destructive_action(
        action="collection.transform",
        affected_datablocks=[{"type": "Object", "name": obj.name} for obj in objects],
        journal=journal,
        dry_run=dry_run,
        require_confirm=False,
        state_token=current_state_token(bpy),
        extra={"collection": collection.name, "object_count": len(objects), "recursive": recursive, "translation": translation, "rotation_euler": rotation, "scale": scale},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    def depth(obj: Any) -> int:
        count, current = 0, obj.parent
        while current is not None:
            count += 1
            current = current.parent
        return count
    ordered = sorted(desired, key=lambda item: (depth(item[0]), item[0].name))
    try:
        for obj, matrix in ordered:
            obj.matrix_world = matrix
        bpy.context.view_layer.update()
        for entry in journal:
            entry["status"] = "applied"
    except Exception as exc:
        rollback_errors = []
        for obj, matrix in sorted(snapshots, key=lambda item: (depth(item[0]), item[0].name)):
            try:
                obj.matrix_world = matrix
            except Exception as rollback_exc:
                rollback_errors.append(f"restore {obj.name}: {rollback_exc}")
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"collection.transform failed: {exc}", transaction) from exc
    transaction["changed"] = bool(objects)
    if objects:
        bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def unlink_collection_objects(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"collection", "objects", "dry_run", "confirm", "expected_state"})
    collection_name = _name(params.get("collection"), "collection")
    object_names = _names(params.get("objects"), "objects")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    root = bpy.context.scene.collection
    collection = _scene_collection(root, collection_name)
    objects = []
    for name in object_names:
        obj = bpy.context.scene.objects.get(name)
        if obj is None:
            raise NotFoundError(f"Object not found in current scene: {name}")
        if collection not in obj.users_collection:
            raise InvalidStateError(f"Object {name} is not directly linked to collection {collection_name}")
        objects.append(obj)
    orphaned = sorted(obj.name for obj in objects if len(obj.users_collection) == 1)
    journal = [{"op": "unlink_object", "target": obj.name, "collection": collection.name, "status": "planned"} for obj in objects]
    transaction, apply = prepare_destructive_action(
        action="collection.unlink_objects",
        affected_datablocks=[{"type": "Object", "name": item.name} for item in objects] + [{"type": "Collection", "name": collection.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={"collection": collection.name, "objects": object_names, "scene_unlinked_objects": orphaned},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    unlinked = []
    try:
        for index, obj in enumerate(objects):
            collection.objects.unlink(obj)
            unlinked.append(obj)
            journal[index]["status"] = "applied"
    except Exception as exc:
        rollback_errors = []
        for obj in unlinked:
            try:
                collection.objects.link(obj)
            except Exception as rollback_exc:
                rollback_errors.append(f"relink {obj.name}: {rollback_exc}")
        if not rollback_errors:
            for entry in journal:
                if entry["status"] == "applied":
                    entry["status"] = "rolled_back"
        transaction.update(changed=bool(rollback_errors), warnings=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"collection.unlink_objects failed: {exc}", transaction) from exc
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _would_cycle(parent: Any, child: Any) -> bool:
    return parent is child or parent in _descendants(child)


def link_collection_child(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"parent", "child", "expected_state"})
    parent_name = _name(params.get("parent"), "parent")
    child_name = _name(params.get("child"), "child")
    bpy, root = _prepare_mutation(params)
    parent = _scene_collection(root, parent_name)
    child = _scene_collection(root, child_name, allow_root=False)
    if _would_cycle(parent, child):
        raise InvalidStateError(f"Linking {child.name} under {parent.name} would create a collection cycle")
    changed = child not in parent.children
    journal = [{"op": "link_child", "target": child.name, "parent": parent.name, "status": "unchanged"}]
    if changed:
        try:
            parent.children.link(child)
            journal[0]["status"] = "applied"
        except Exception as exc:
            partial_change = child in parent.children
            transaction = new_transaction([{"type": "Collection", "name": parent.name}, {"type": "Collection", "name": child.name}])
            transaction.update(parent=parent.name, child=child.name, changed=partial_change, journal=journal)
            if partial_change:
                bump_epoch()
            transaction["state_token"] = current_state_token(bpy)
            raise MutationApplyError(f"collection.link_child failed: {exc}", transaction) from exc
        bump_epoch()
    transaction = new_transaction([{"type": "Collection", "name": parent.name}, {"type": "Collection", "name": child.name}])
    transaction.update(parent=parent.name, child=child.name, changed=changed, journal=journal, state_token=current_state_token(bpy))
    return transaction


def unlink_collection_child(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"parent", "child", "dry_run", "confirm", "expected_state"})
    parent_name = _name(params.get("parent"), "parent")
    child_name = _name(params.get("child"), "child")
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    root = bpy.context.scene.collection
    parent = _scene_collection(root, parent_name)
    child = _scene_collection(root, child_name, allow_root=False)
    if child not in parent.children:
        raise InvalidStateError(f"Collection {child.name} is not directly linked under {parent.name}")
    remaining_parents = [item.name for item in _parents(root, child) if item is not parent]
    journal = [{"op": "unlink_child", "target": child.name, "parent": parent.name, "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="collection.unlink_child",
        affected_datablocks=[{"type": "Collection", "name": parent.name}, {"type": "Collection", "name": child.name}],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={"parent": parent.name, "child": child.name, "remaining_scene_parents": remaining_parents, "removes_subtree_from_scene": not remaining_parents},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    try:
        parent.children.unlink(child)
        journal[0]["status"] = "applied"
    except Exception as exc:
        partial_change = child not in parent.children
        transaction["changed"] = partial_change
        if partial_change:
            journal[0]["status"] = "applied"
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"collection.unlink_child failed: {exc}", transaction) from exc
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def _external_delete_uses(bpy: Any, delete_set: set[Any], targets: set[Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    all_parent_candidates = [scene.collection for scene in bpy.data.scenes] + list(bpy.data.collections)
    for collection in sorted(delete_set, key=lambda item: item.name):
        other_scenes = sorted(scene.name for scene in bpy.data.scenes if scene is not bpy.context.scene and collection in _scene_collections(scene.collection))
        external_parents = sorted(
            parent.name
            for parent in all_parent_candidates
            if parent not in delete_set and collection in parent.children
        )
        if other_scenes or (collection not in targets and external_parents):
            conflicts.append({"collection": collection.name, "other_scenes": other_scenes, "external_parents": external_parents})
    return conflicts


def delete_collection(params: dict[str, Any]) -> dict[str, Any]:
    _unknown(params, {"collections", "recursive", "dry_run", "confirm", "expected_state"})
    target_names = _names(params.get("collections"), "collections")
    recursive = _strict_bool(params.get("recursive"), "recursive", False)
    _validate_expected(params)
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    root = bpy.context.scene.collection
    targets = {_scene_collection(root, name, allow_root=False) for name in target_names}
    delete_set = set(targets)
    if recursive:
        for collection in list(targets):
            delete_set.update(_descendants(collection))
    else:
        nonempty = sorted(item.name for item in targets if len(item.children) or len(item.objects))
        if nonempty:
            raise InvalidStateError(
                "Non-recursive collection deletion requires empty leaf collections: " + ", ".join(nonempty)
            )
    conflicts = _external_delete_uses(bpy, delete_set, targets)
    if conflicts:
        error = InvalidStateError("Collection deletion would affect shared collection links or other scenes")
        error.data = {"conflicts": conflicts}
        raise error
    objects = sorted({obj for collection in delete_set for obj in collection.objects}, key=lambda item: item.name)
    scene_unlinked = sorted(
        obj.name for obj in objects if all(collection in delete_set for collection in obj.users_collection)
    )
    ordered = sorted(delete_set, key=lambda item: len(_descendants(item)), reverse=False)
    details = [_collection_detail(root, item) for item in sorted(delete_set, key=lambda entry: entry.name)]
    journal = [{"op": "delete", "target": detail["name"], "before": detail, "after": None, "status": "planned"} for detail in details]
    transaction, apply = prepare_destructive_action(
        action="collection.delete",
        affected_datablocks=[{"type": "Collection", "name": item.name} for item in sorted(delete_set, key=lambda entry: entry.name)] + [{"type": "Object", "name": item.name} for item in objects],
        journal=journal,
        dry_run=params.get("dry_run"),
        confirm=params.get("confirm"),
        require_confirm=True,
        state_token=current_state_token(bpy),
        extra={"collections": sorted(item.name for item in delete_set), "recursive": recursive, "scene_unlinked_objects": scene_unlinked},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    names_before = {item.name for item in delete_set}
    try:
        bpy.data.batch_remove(ids=tuple(ordered))
    except Exception as exc:
        remaining = {name for name in names_before if bpy.data.collections.get(name) is not None}
        changed = remaining != names_before
        for entry in journal:
            if entry["target"] not in remaining:
                entry["status"] = "applied"
        transaction["changed"] = changed
        if changed:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        raise MutationApplyError(f"collection.delete failed: {exc}", transaction) from exc
    for entry in journal:
        entry["status"] = "applied"
    transaction["changed"] = True
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
