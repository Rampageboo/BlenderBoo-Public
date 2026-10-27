"""Bounded read-only inspection of Blender Annotation datablocks."""

from __future__ import annotations

from typing import Any, Callable

from ..errors import InvalidParamsError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .session_state import current_state_token


_FRAME_MIN = -1_048_574
_FRAME_MAX = 1_048_574
_STROKE_INDEX_MAX = 100_000_000


def _optional_name(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidParamsError(f"{key} must be a non-empty string")
    return value


def _optional_integer(
    params: dict[str, Any], key: str, minimum: int, maximum: int
) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise InvalidParamsError(
            f"{key} must be an integer from {minimum} to {maximum}"
        )
    return value


def _pagination(params: dict[str, Any]) -> tuple[int, int]:
    limit = params.get("limit", 200)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 2000
    ):
        raise InvalidParamsError("limit must be an integer from 1 to 2000")
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if (
        isinstance(cursor, str)
        and 1 <= len(cursor) <= 18
        and cursor.isascii()
        and cursor.isdecimal()
    ):
        return limit, int(cursor)
    raise InvalidParamsError(
        "cursor must be a non-negative decimal string returned by annotation.inspect"
    )


def _request_scope(
    params: dict[str, Any],
) -> tuple[str, str | None, str | None, int | None, int | None]:
    annotation_name = _optional_name(params, "annotation_name")
    layer_name = _optional_name(params, "layer_name")
    frame_number = _optional_integer(
        params, "frame_number", _FRAME_MIN, _FRAME_MAX
    )
    stroke_index = _optional_integer(
        params, "stroke_index", 0, _STROKE_INDEX_MAX
    )
    if annotation_name is None and layer_name is not None:
        raise InvalidParamsError("layer_name requires annotation_name")
    if layer_name is None and frame_number is not None:
        raise InvalidParamsError("frame_number requires layer_name")
    if frame_number is None and stroke_index is not None:
        raise InvalidParamsError("stroke_index requires frame_number")
    if annotation_name is None:
        scope = "DATABLOCKS"
    elif layer_name is None:
        scope = "LAYERS"
    elif frame_number is None:
        scope = "FRAMES"
    elif stroke_index is None:
        scope = "STROKES"
    else:
        scope = "POINTS"
    return scope, annotation_name, layer_name, frame_number, stroke_index


def _page_records(
    collection: Any,
    limit: int,
    offset: int,
    record: Callable[[Any, int], dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    count = len(collection)
    start = min(offset, count)
    stop = min(start + limit, count)
    items = [record(collection[index], index) for index in range(start, stop)]
    next_cursor = str(stop) if stop < count else None
    return items, {
        "offset": offset,
        "limit": limit,
        "returned": len(items),
        "total": count,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
    }


def _active_context_annotation(bpy: Any) -> tuple[Any | None, Any | None]:
    try:
        annotation = getattr(bpy.context, "annotation_data", None)
    except (AttributeError, RuntimeError):
        annotation = None
    try:
        owner = getattr(bpy.context, "annotation_data_owner", None)
    except (AttributeError, RuntimeError):
        owner = None
    if getattr(getattr(annotation, "bl_rna", None), "identifier", None) != "Annotation":
        annotation = None
        owner = None
    return annotation, owner


def _owner_summary(owner: Any | None) -> dict[str, Any] | None:
    if owner is None:
        return None
    result: dict[str, Any] = {
        "type": getattr(
            getattr(owner, "bl_rna", None), "identifier", type(owner).__name__
        )
    }
    name = getattr(owner, "name", None)
    if isinstance(name, str) and name:
        result["name"] = name
    return result


def _annotation_summary(
    annotation: Any,
    active_annotation: Any | None,
    active_owner: Any | None,
) -> dict[str, Any]:
    active = annotation == active_annotation
    return {
        "name": annotation.name,
        "layer_count": len(annotation.layers),
        "users": int(annotation.users),
        "linked": annotation.library is not None,
        "editable": bool(annotation.is_editable),
        "active_in_context": active,
        "active_owner": _owner_summary(active_owner) if active else None,
    }


def _layer_summary(layer: Any, index: int) -> dict[str, Any]:
    active_frame = getattr(layer, "active_frame", None)
    return {
        "index": index,
        "name": layer.info,
        "frame_count": len(layer.frames),
        "active_frame_number": (
            int(active_frame.frame_number) if active_frame is not None else None
        ),
        "color": [float(value) for value in layer.color],
        "opacity": float(layer.annotation_opacity),
        "thickness": int(layer.thickness),
        "hidden": bool(layer.annotation_hide),
        "locked": bool(layer.lock),
        "frame_locked": bool(layer.lock_frame),
        "selected": bool(layer.select),
        "show_in_front": bool(layer.show_in_front),
        "is_ruler": bool(layer.is_ruler),
    }


def _frame_summary(frame: Any, index: int, active_frame: Any | None) -> dict[str, Any]:
    return {
        "index": index,
        "frame_number": int(frame.frame_number),
        "stroke_count": len(frame.strokes),
        "selected": bool(frame.select),
        "active": frame == active_frame,
    }


def _stroke_summary(stroke: Any, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "display_mode": str(stroke.display_mode),
        "point_count": len(stroke.points),
    }


def _point_summary(point: Any, index: int) -> dict[str, Any]:
    return {"index": index, "co": [float(value) for value in point.co]}


def _exact_layer(annotation: Any, name: str) -> Any:
    for layer in annotation.layers:
        if layer.info == name:
            return layer
    raise NotFoundError(f"Annotation layer not found in {annotation.name}: {name}")


def _exact_frame(layer: Any, frame_number: int) -> Any:
    for frame in layer.frames:
        if int(frame.frame_number) == frame_number:
            return frame
    raise NotFoundError(
        f"Annotation frame not found in layer {layer.info}: {frame_number}"
    )


def inspect_annotation(params: dict[str, Any]) -> ToolResult:
    """Inspect Annotation datablocks through bounded hierarchical pages."""
    allowed = {
        "annotation_name",
        "layer_name",
        "frame_number",
        "stroke_index",
        "limit",
        "cursor",
    }
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    limit, offset = _pagination(params)
    scope, annotation_name, layer_name, frame_number, stroke_index = _request_scope(
        params
    )
    bpy = get_bpy()
    active_annotation, active_owner = _active_context_annotation(bpy)
    data: dict[str, Any] = {
        "scope": scope,
        "dirty": bool(bpy.data.is_dirty),
        "frame_current": int(bpy.context.scene.frame_current),
        "state_token": current_state_token(bpy),
        "annotation": None,
        "layer": None,
        "frame": None,
        "stroke": None,
    }

    if scope == "DATABLOCKS":
        annotations = sorted(bpy.data.annotations, key=lambda item: item.name.casefold())
        items, page = _page_records(
            annotations,
            limit,
            offset,
            lambda item, _index: _annotation_summary(
                item, active_annotation, active_owner
            ),
        )
        data.update(items=items, page=page)
        return ToolResult(data=data)

    assert annotation_name is not None
    annotation = bpy.data.annotations.get(annotation_name)
    if annotation is None:
        raise NotFoundError(f"Annotation datablock not found: {annotation_name}")
    data["annotation"] = _annotation_summary(
        annotation, active_annotation, active_owner
    )
    if scope == "LAYERS":
        items, page = _page_records(
            annotation.layers, limit, offset, _layer_summary
        )
        data.update(items=items, page=page)
        return ToolResult(data=data)

    assert layer_name is not None
    layer = _exact_layer(annotation, layer_name)
    data["layer"] = _layer_summary(
        layer,
        next(index for index, item in enumerate(annotation.layers) if item == layer),
    )
    if scope == "FRAMES":
        active_frame = getattr(layer, "active_frame", None)
        items, page = _page_records(
            layer.frames,
            limit,
            offset,
            lambda item, index: _frame_summary(item, index, active_frame),
        )
        data.update(items=items, page=page)
        return ToolResult(data=data)

    assert frame_number is not None
    frame = _exact_frame(layer, frame_number)
    data["frame"] = _frame_summary(
        frame,
        next(index for index, item in enumerate(layer.frames) if item == frame),
        getattr(layer, "active_frame", None),
    )
    if scope == "STROKES":
        items, page = _page_records(frame.strokes, limit, offset, _stroke_summary)
        data.update(items=items, page=page)
        return ToolResult(data=data)

    assert stroke_index is not None
    if stroke_index >= len(frame.strokes):
        raise NotFoundError(
            f"Annotation stroke index {stroke_index} is out of range for "
            f"frame {frame_number}"
        )
    stroke = frame.strokes[stroke_index]
    data["stroke"] = _stroke_summary(stroke, stroke_index)
    items, page = _page_records(stroke.points, limit, offset, _point_summary)
    data.update(items=items, page=page)
    return ToolResult(data=data)
