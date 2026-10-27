"""Stable-v1 MCP stdio server exposing typed BlenderBoo tools."""

from __future__ import annotations

import copy
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import time
import tomllib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mcp.server.fastmcp import Image
from mcp.types import CallToolResult, ImageContent, TextContent

from .instances import InstanceRouter
from .call_log import CallLog, summarize
from .proxy import ToolkitProxy, supplied_tool_arguments
from .response_profiles import (
    DEFAULT_RETRIEVAL_CHARS,
    MAX_RETRIEVAL_CHARS,
    ResponseMode,
    ResponseArtifactStore,
    apply_response_profile,
    response_mode,
)
from .schema_audit import audit_schemas, schema_digest
from .workflow import run_workflow
from .workflow_batch import plan_batch, WorkflowBatchError
from .workflow_jobs import WorkflowJobManager
from .workflow_templates import (
    describe_template,
    expand_template,
    list_templates,
    WorkflowTemplateError,
)


_MAX_INLINE_ENCODED_BYTES = 8 * 1024 * 1024
_MAX_INLINE_RAW_BYTES = (_MAX_INLINE_ENCODED_BYTES // 4) * 3
_TEXT_CHANNELS = frozenset({"summary", "full"})
_SERVER_INSTRUCTIONS = (
    "Work against one selected Blender instance; name it if several are registered. "
    "Typed tools cover common work; python.exec covers custom and bulk operations. "
    "Check version-sensitive bpy paths with api.lookup. After a timeout or disconnect, "
    "inspect its operation and scene state before retrying a mutation. One client needs "
    "no document lease; concurrent writers should acquire one. For reference work, prefer "
    "camera.import_colmap_package handoffs. RealityScan CSV via camera.import_photogrammetry "
    "uses csv_principal and needs pose verification. Calibrate unsolved cameras from known "
    "points; check pixel residuals and contours before changing form. Fast captures and "
    "close-ups support frequent visual checks. Retrieve omitted response chunks with "
    "response.artifact_get before eviction. After an adapter upgrade, restart the MCP "
    "client to refresh cached parameters; system.doctor reports per-tool schema digests."
)
_REVIEW_VIEWS = Literal["camera", "top", "side", "three_quarter"]
_ORTHO_VIEW = Literal["front", "back", "left", "right", "top", "bottom"]
_DETAIL_DIRECTION = Literal["front", "back", "left", "right", "top", "bottom", "three_quarter"]
_VIEW_MODE = Literal["lit", "solid", "normal", "matcap", "curvature", "depth", "wireframe_on_shaded"]
_OBJECT_CREATE_TYPE = Literal[
    "PLANE", "CUBE", "UV_SPHERE", "ICO_SPHERE", "CYLINDER", "CONE",
    "TORUS", "CIRCLE", "GRID", "MONKEY", "EMPTY", "CAMERA", "LIGHT",
]
_LIGHT_TYPE = Literal["POINT", "SUN", "SPOT", "AREA"]
_CAMERA_TYPE = Literal["PERSP", "ORTHO", "PANO"]
_AREA_SHAPE = Literal["SQUARE", "RECTANGLE", "DISK", "ELLIPSE"]
_IMAGE_FORMAT = Literal["PNG", "JPEG", "OPEN_EXR", "TIFF"]
_CURVE_DIMENSIONS = Literal["2D", "3D"]
_CURVE_FILL_MODE = Literal["FULL", "HALF", "BACK", "FRONT"]
_UV_PROJECTION_MODE = Literal["PLANAR", "CUBE", "CYLINDER", "SPHERE"]
_UV_PLANE = Literal["XY", "XZ", "YZ"]
_UV_UNWRAP_METHOD = Literal["ANGLE_BASED", "CONFORMAL", "SMART", "MINIMUM_STRETCH"]
_MODIFIER_TYPE = Annotated[str, Field(min_length=1)]
_WORKPLANE_AXIS = Literal["U", "V", "N", "-U", "-V", "-N"]
_OBJECT_NAME = Annotated[str, Field(min_length=1)]
_DETAIL_OBJECT_NAMES = Annotated[list[_OBJECT_NAME], Field(min_length=1, json_schema_extra={"uniqueItems": True})]
_ASSEMBLY_OBJECT_NAMES = Annotated[list[_OBJECT_NAME], Field(min_length=2, json_schema_extra={"uniqueItems": True})]
_ASSEMBLY_PAIRS = Annotated[list[tuple[_OBJECT_NAME, _OBJECT_NAME]], Field(min_length=1)]
_ORTHO_VIEWS = Annotated[list[_ORTHO_VIEW], Field(min_length=1, json_schema_extra={"uniqueItems": True})]
_FAST_VIEWS = Annotated[list[Literal["current", "front", "back", "left", "right", "top", "bottom", "three_quarter"]], Field(min_length=1, max_length=6, json_schema_extra={"uniqueItems": True})]
_OBJECT_NAMES = _OBJECT_NAME | Annotated[list[_OBJECT_NAME], Field(min_length=1, max_length=2000)]
_VECTOR3 = tuple[float, float, float]
_VECTOR4 = tuple[float, float, float, float]
_VECTOR2 = tuple[float, float]
_COLOR_COMPONENT = Annotated[float, Field(ge=0, le=1)]
_COLOR3 = tuple[_COLOR_COMPONENT, _COLOR_COMPONENT, _COLOR_COMPONENT]
_CUSTOM_VALUE = str | float | bool
_INSTANCE_SELECTOR = Annotated[
    str | int | None,
    Field(description="Optional Blender registry id or loopback bridge port."),
]
_TOOL_MODES = frozenset({"eager", "search"})
_MCP_ONLY_TOOLS = frozenset(
    {
        "session.instances",
        "workflow.run",
        "workflow.template",
        "workflow.batch_plan",
        "workflow.job_start",
        "workflow.job_control",
        "toolsets.list",
        "toolsets.describe",
        "toolsets.call",
        "adapter.schema_audit",
        "response.artifact_get",
        "session.call_log_summary",
        "operation.start",
        "operation.status",
    }
)
_SEARCH_TOOLS = frozenset(
    {
        "toolsets.list",
        "toolsets.describe",
        "toolsets.call",
        "workflow.run",
        "workflow.template",
        "workflow.batch_plan",
        "workflow.job_start",
        "workflow.job_control",
        "adapter.schema_audit",
        "response.artifact_get",
        "session.call_log_summary",
        "operation.start",
        "operation.status",
    }
)


def _tool_mode(value: str | None) -> str:
    resolved = os.environ.get("CBT_MCP_TOOL_MODE", "eager") if value is None else value
    if not isinstance(resolved, str) or resolved.lower() not in _TOOL_MODES:
        raise ValueError("tool mode must be 'eager' or 'search'")
    return resolved.lower()


def _adapter_version() -> str:
    source_project = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if source_project.is_file():
        try:
            project = tomllib.loads(source_project.read_text(encoding="utf-8")).get("project")
            if isinstance(project, dict) and project.get("name") == "blenderboo-mcp" and isinstance(project.get("version"), str):
                return project["version"]
        except (OSError, ValueError):
            pass
    try:
        return version("blenderboo-mcp")
    except PackageNotFoundError:
        return "source-tree"


def _release_version(value: Any) -> str | None:
    match = re.match(r"^(\d+\.\d+\.\d+)(?:[.+-]|$)", value) if isinstance(value, str) else None
    return match.group(1) if match else None


class RomBoneRotation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bone: _OBJECT_NAME
    axis: Literal["X", "Y", "Z"]
    degrees: Annotated[float, Field(ge=-180, le=180)]


class RomPose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: _OBJECT_NAME
    bones: Annotated[list[RomBoneRotation], Field(min_length=1, max_length=8)]


class GoalRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vertex_group: str | None = None
    face_indices: Annotated[list[Annotated[int, Field(ge=0)]] | None,
                            Field(min_length=1, max_length=500000)] = None


class GoalSymmetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    normal: _VECTOR3
    offset_m: float = 0
    pair_tolerance_mm: Annotated[float, Field(gt=0, le=0.01)] = 0.001


class GoalClearance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_name: str
    min_mm: Annotated[float, Field(gt=0, le=10000)]


class GoalLandmark(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vertex_index: Annotated[int, Field(ge=0)]
    target: _VECTOR3


class ScreenshotView(BaseModel):
    """Typed, non-mutating view selection for viewport screenshots."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["current", "orbit"] = "current"
    object_name: Annotated[
        str | None, Field(min_length=1, description="Optional orbit target object.")
    ] = None
    target: Annotated[
        tuple[float, float, float] | None,
        Field(description="Optional world-space orbit target [x, y, z]."),
    ] = None
    azimuth_degrees: float = 45.0
    elevation_degrees: Annotated[float, Field(ge=-89.9, le=89.9)] = 30.0
    distance: Annotated[float | None, Field(gt=0)] = None


class DetailBounds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: _VECTOR3
    max: _VECTOR3


class BendZone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: _OBJECT_NAME
    point: _VECTOR3
    radius: Annotated[float, Field(gt=0)]


class LandmarkPair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: _VECTOR3
    target: _VECTOR3


class ReferenceCamera(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: _VECTOR3
    rotation_euler: _VECTOR3
    projection: Literal["perspective", "orthographic"] | None = None
    lens: Annotated[float | None, Field(gt=0)] = None
    ortho_scale: Annotated[float | None, Field(gt=0)] = None


class CalibrationVertex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: _OBJECT_NAME
    vertex_index: Annotated[int, Field(ge=0)]


class CalibrationEmpty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    empty: _OBJECT_NAME


class CalibrationCorrespondence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pixel: _VECTOR2
    world: _VECTOR3 | CalibrationVertex | CalibrationEmpty


class ContactSheetImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Annotated[str, Field(min_length=1)]
    label: Annotated[str | None, Field(min_length=1)] = None


class AssemblyExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair: tuple[_OBJECT_NAME, _OBJECT_NAME]
    contact: bool | None = None
    gap_between: tuple[Annotated[float, Field(ge=0)], Annotated[float, Field(ge=0)]] | None = None
    no_intersection: bool | None = None
    intersection_allowed: bool | None = None


class FixedPartChannels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_color: Annotated[list[Annotated[float, Field(ge=0, le=1)]], Field(min_length=3, max_length=3)]
    roughness: Annotated[float, Field(ge=0, le=1)]
    metallic: Annotated[float, Field(ge=0, le=1)]
    ao: Annotated[float, Field(ge=0, le=1)]
    normal: Literal["FLAT"] = "FLAT"


class StripSeamRows(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge_rows: Annotated[list[Annotated[list[Annotated[int, Field(ge=0)]], Field(min_length=1, max_length=1000)]],
                         Field(min_length=2, max_length=10000)]
    closed: bool = False


class _OpenOperation(BaseModel):
    """Keep operation payloads intact while advertising required discriminator fields."""

    model_config = ConfigDict(extra="allow")


class KeyframeOperation(_OpenOperation):
    op: Any
    property: Any
    index: Any
    frame: Any
    interpolation: Literal["CONSTANT", "LINEAR", "BEZIER", "SINE", "QUAD", "CUBIC", "QUART", "QUINT", "EXPO", "CIRC", "BACK", "BOUNCE", "ELASTIC"] | None = None
    easing: Literal["AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"] | None = None
    handle_left_type: Literal["FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"] | None = None
    handle_right_type: Literal["FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"] | None = None


class NlaOperation(_OpenOperation):
    op: Any
    track: Any


class NamedOperation(_OpenOperation):
    op: Any


class WorkplanePlane(_OpenOperation):
    source: Literal["FACE", "EXPLICIT"]


class MeshMeasureQuery(_OpenOperation):
    type: Literal[
        "VERTEX_DISTANCE", "ANGLE", "EDGE_LENGTH", "FACE_AREA",
        "SELECTION_BOUNDS", "VOLUME",
    ]


class MeshSeparateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Literal["VERT", "EDGE", "FACE"]
    predicate: dict[str, Any]


class ArchitectureOpening(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str | None, Field(min_length=1, max_length=63)] = None
    kind: Literal["DOOR", "WINDOW", "OPENING"] = "OPENING"
    offset: Annotated[float, Field(ge=0)]
    width: Annotated[float, Field(gt=0)]
    bottom: Annotated[float, Field(ge=0)] = 0
    height: Annotated[float, Field(gt=0)]


class ArchitectureWallLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: _VECTOR2
    end: _VECTOR2
    openings: Annotated[
        list[ArchitectureOpening],
        Field(max_length=32),
    ] = Field(default_factory=list)


class PoseConstraintOperation(_OpenOperation):
    op: Any
    bone: Any
    name: Any


class SkinWeightOperation(_OpenOperation):
    op: Any
    vertices: Any


class ShapeKeyPosition(_OpenOperation):
    index: Any
    co: Any


class ArmatureBone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: _OBJECT_NAME
    head: _VECTOR3
    tail: _VECTOR3
    roll: float | None = None
    parent: _OBJECT_NAME | None = None
    use_connect: bool = False
    use_deform: bool = True
    inherit_scale: Literal["FULL", "FIX_SHEAR", "ALIGNED", "AVERAGE", "NONE", "NONE_LEGACY"] | None = None


class CurveSpline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["POLY", "BEZIER", "NURBS"]
    points: Annotated[list[Any], Field(min_length=2, max_length=100000)]
    cyclic: bool = False
    resolution_u: Annotated[int | None, Field(ge=1, le=1024)] = None
    order_u: Annotated[int | None, Field(ge=2, le=6)] = None
    use_endpoint_u: bool | None = None
    use_bezier_u: bool | None = None


class BakeChannelPack(str, Enum):
    ORM = "ORM"


class SourceCompareThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iou_min: Annotated[float | None, Field(ge=0, le=1)] = None
    deviation_p99_mm: Annotated[float | None, Field(ge=0)] = None
    radial_max_mm: Annotated[float | None, Field(ge=0)] = None


class LatheProfilePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    radius: Annotated[float, Field(gt=0)]
    z: float
    sharp: bool | None = None
    round_radius: Annotated[float | None, Field(ge=0)] = None


class SweepPathPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point: _VECTOR3
    width: Annotated[float, Field(gt=0)]
    height: Annotated[float, Field(gt=0)]
    chamfer: Annotated[float | None, Field(ge=0)] = None


class UVFaceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indices: Annotated[list[Annotated[int, Field(ge=0)]] | None, Field(min_length=1, max_length=100000)] = None
    material_index: Annotated[int | None, Field(ge=0, le=32767)] = None
    normal_direction: _VECTOR3 | None = None
    min_dot: Annotated[float, Field(ge=-1, le=1)] = 0


def _model_payload(value: BaseModel | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(exclude_none=True, exclude_unset=True)


def _model_payloads(values: list[BaseModel] | None) -> list[dict[str, Any]] | None:
    if values is None:
        return None
    return [value.model_dump(exclude_none=True, exclude_unset=True) for value in values]


def _detail_target_params(
    object_name: str | None,
    object_names: list[str] | None,
    point: _VECTOR3 | None,
    radius: float | None,
    bounds: DetailBounds | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in (("object_name", object_name), ("object_names", object_names),
                       ("point", point), ("radius", radius)):
        if value is not None:
            params[key] = value
    if bounds is not None:
        params["bounds"] = bounds.model_dump()
    return params


def _warning_for_path(path: str, reason: str) -> str:
    return f"MCP adapter did not inline PNG '{path}': {reason}; use the returned path instead."


def _structured_text_mirror(result: dict[str, Any], *, channel: str = "summary") -> str:
    """Keep a short text receipt alongside the authoritative structured result."""
    if channel == "full":
        return json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    summary: dict[str, Any] = {
        "ok": result.get("ok") is True,
        "tool": result.get("tool"),
        "message": "See structuredContent for the result.",
    }
    error = result.get("error")
    if isinstance(error, dict) and isinstance(error.get("type"), str):
        summary["error_type"] = error["type"]
        if isinstance(error.get("message"), str):
            summary["error_message"] = error["message"][:500]
    if isinstance(result.get("reason"), str):
        summary["reason"] = result["reason"][:500]
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("changed", "state_token", "partial_change_possible", "status",
                    "dry_run", "action", "coverage", "rays", "hits",
                    "new_seam_count", "residual_p95_scene_units", "artifact"):
            if key in data:
                summary[key] = data[key]
        if isinstance(data.get("reason"), str):
            summary["reason"] = data["reason"][:500]
        if isinstance(data.get("coefficients"), list):
            summary["coefficients"] = data["coefficients"][:16]
        if isinstance(data.get("source_objects"), list):
            summary["source_objects"] = data["source_objects"][:8]
        if isinstance(data.get("operation_hint"), dict):
            summary["operation_hint"] = data["operation_hint"]
        if isinstance(data.get("changes_summary"), dict):
            summary["changes_summary"] = data["changes_summary"]
    return json.dumps(
        summary,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _text_channel(value: str | None = None) -> str:
    resolved = os.environ.get("CBT_MCP_TEXT_CHANNEL", "summary") if value is None else value
    if not isinstance(resolved, str) or resolved.lower() not in _TEXT_CHANNELS:
        raise ValueError("text channel must be 'summary' or 'full'")
    return resolved.lower()


def _profile_mcp_result(
    result: Any,
    *,
    mode: ResponseMode,
    store: ResponseArtifactStore,
    bypass_profile: bool = False,
    text_channel: str = "summary",
    requested: dict[str, Any] | None = None,
) -> Any:
    """Apply a response profile and avoid duplicating structured data as text."""
    if isinstance(result, CallToolResult):
        structured = result.structuredContent
        if not isinstance(structured, dict):
            return result
        public = structured if bypass_profile else apply_response_profile(
            structured,
            mode=mode,
            store=store,
            requested=requested,
        )
        non_text = [item for item in result.content if not isinstance(item, TextContent)]
        return result.model_copy(
            update={
                "content": [
                    TextContent(type="text", text=_structured_text_mirror(public, channel=text_channel)),
                    *non_text,
                ],
                "structuredContent": public,
            }
        )
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[1], dict)
    ):
        structured = result[1]
        public = structured if bypass_profile else apply_response_profile(
            structured,
            mode=mode,
            store=store,
            requested=requested,
        )
        return (
            [TextContent(type="text", text=_structured_text_mirror(public, channel=text_channel))],
            public,
        )
    if isinstance(result, dict):
        public = result if bypass_profile else apply_response_profile(
            result,
            mode=mode,
            store=store,
            requested=requested,
        )
        return (
            [TextContent(type="text", text=_structured_text_mirror(public, channel=text_channel))],
            public,
        )
    return result


def _inline_png(path_value: Any) -> tuple[ImageContent | None, str | None]:
    if not isinstance(path_value, str) or not path_value:
        return None, "MCP adapter received an empty or non-string image path."

    path = Path(path_value)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, _warning_for_path(path_value, f"file is unavailable ({exc})")

    if path.suffix.lower() != ".png":
        return None, _warning_for_path(path_value, "path is not a PNG")
    if size > _MAX_INLINE_RAW_BYTES:
        return None, _warning_for_path(
            path_value,
            f"base64 content would exceed the {_MAX_INLINE_ENCODED_BYTES}-byte inline cap",
        )

    try:
        with path.open("rb") as handle:
            payload = handle.read(_MAX_INLINE_RAW_BYTES + 1)
    except OSError as exc:
        return None, _warning_for_path(path_value, f"file is unreadable ({exc})")

    if len(payload) > _MAX_INLINE_RAW_BYTES:
        return None, _warning_for_path(
            path_value,
            f"base64 content would exceed the {_MAX_INLINE_ENCODED_BYTES}-byte inline cap",
        )
    return Image(data=payload, format="png").to_image_content(), None


def _render_result(envelope: dict[str, Any], paths: list[Any]) -> CallToolResult:
    result = copy.deepcopy(envelope)
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
        result["warnings"] = warnings

    images: list[ImageContent] = []
    for path in paths:
        image, warning = _inline_png(path)
        if image is not None:
            images.append(image)
        elif warning is not None:
            warnings.append(warning)

    content = [
        TextContent(type="text", text=_structured_text_mirror(result)),
        *images,
    ]
    return CallToolResult(content=content, structuredContent=result)


def _review_paths(envelope: dict[str, Any]) -> list[Any]:
    data = envelope.get("data")
    if not isinstance(data, dict):
        return []
    manifest = data.get("manifest")
    if not isinstance(manifest, dict):
        return []

    ordered_views = data.get("views")
    if not isinstance(ordered_views, list):
        ordered_views = list(manifest)
    paths: list[Any] = []
    for view in ordered_views:
        entry = manifest.get(view)
        if isinstance(entry, dict) and "path" in entry:
            paths.append(entry["path"])
    return paths


def _addon_schema_catalog(
    proxy: ToolkitProxy,
    instance: str | int | None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    catalog = proxy.system_capabilities(None, False, 200, None, instance)
    if catalog.get("ok") is not True:
        error = catalog.get("error")
        message = error.get("message") if isinstance(error, dict) else "capability catalog failed"
        raise RuntimeError(str(message))
    data = catalog.get("data")
    toolsets = data.get("toolsets") if isinstance(data, dict) else None
    if not isinstance(toolsets, list):
        raise RuntimeError("system.capabilities returned no toolset list")
    digest_value = data.get("capability_digest")
    expected_digest = digest_value if isinstance(digest_value, str) else None
    schemas: dict[str, dict[str, Any]] = {}
    for record in toolsets:
        toolset = record.get("name") if isinstance(record, dict) else None
        if not isinstance(toolset, str) or not toolset:
            raise RuntimeError("system.capabilities returned an invalid toolset record")
        cursor = None
        while True:
            envelope = proxy.system_capabilities(toolset, True, 200, cursor, instance)
            if envelope.get("ok") is not True:
                error = envelope.get("error")
                message = error.get("message") if isinstance(error, dict) else f"toolset {toolset} failed"
                raise RuntimeError(str(message))
            page = envelope.get("data")
            tools = page.get("tools") if isinstance(page, dict) else None
            if not isinstance(tools, list):
                raise RuntimeError(f"system.capabilities returned no tools for {toolset}")
            for spec in tools:
                name = spec.get("name") if isinstance(spec, dict) else None
                schema = spec.get("input_schema") if isinstance(spec, dict) else None
                if not isinstance(name, str) or not isinstance(schema, dict):
                    raise RuntimeError(f"toolset {toolset} returned an invalid tool schema")
                if name in schemas:
                    raise RuntimeError(f"duplicate add-on capability: {name}")
                schemas[name] = schema
            next_cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or next_cursor == cursor:
                raise RuntimeError(f"toolset {toolset} returned an invalid pagination cursor")
            cursor = next_cursor
    return schemas, expected_digest


def create_server(
    proxy: ToolkitProxy | None = None,
    tool_mode: str | None = None,
    response_profile: str | None = None,
):
    # Keep the SDK outside Blender and import it only when the standalone server starts.
    from mcp.server.fastmcp import FastMCP

    class _CatalogFastMCP(FastMCP):
        """Track public names so search mode can use the SDK's removal API."""

        def __init__(self, *args, **kwargs):
            self.cbt_registered_tools: list[str] = []
            super().__init__(*args, **kwargs)

        def tool(self, *args, **kwargs):
            decorator = super().tool(*args, **kwargs)

            def tracked(function):
                result = decorator(function)
                name = kwargs.get("name") or function.__name__
                manager = getattr(self, "_tool_manager", None)
                record = manager.get_tool(name) if manager is not None else None
                if record is None:
                    raise RuntimeError("Installed MCP SDK does not expose the registered tool model")
                argument_model = record.fn_metadata.arg_model
                argument_model.model_config = {**argument_model.model_config, "extra": "forbid"}
                argument_model.model_rebuild(force=True)
                record.parameters = argument_model.model_json_schema(by_alias=True)
                self.cbt_registered_tools.append(name)
                return result

            return tracked

        async def call_tool(self, name: str, arguments: dict[str, Any]):
            seq = call_log.next_seq()
            started = time.perf_counter()
            target_instance, target_blend = call_log.target_context(arguments.get("instance"), proxy.client)
            supplied = supplied_tool_arguments.set((name, frozenset(arguments) - {"instance"}))
            try:
                result = await super().call_tool(name, arguments)
                public = _profile_mcp_result(
                    result,
                    mode=resolved_response_mode,
                    store=response_store,
                    bypass_profile=name == "response.artifact_get",
                    text_channel=resolved_text_channel,
                    requested=arguments,
                )
                raw_content = result.structuredContent if isinstance(result, CallToolResult) else result[1] if isinstance(result, tuple) and len(result) == 2 else result
                public_content = public.structuredContent if isinstance(public, CallToolResult) else public[1] if isinstance(public, tuple) and len(public) == 2 else public
                serialize_started = time.perf_counter()
                try:
                    json.dumps(public_content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
                    serialize_ms = (time.perf_counter() - serialize_started) * 1000
                except (TypeError, ValueError):
                    serialize_ms = None
                recorded_tool = arguments.get("tool", name) if name == "toolsets.call" else name
                recorded_params = arguments.get("arguments", {}) if name == "toolsets.call" else arguments
                call_log.record(seq=seq, tool=recorded_tool, params=recorded_params, result=raw_content, public=public_content, elapsed_ms=(time.perf_counter() - started) * 1000, serialize_ms=serialize_ms, instance=target_instance, blend_file=target_blend)
                if isinstance(raw_content, dict):
                    data = raw_content.get("data")
                    if isinstance(data, dict):
                        operation_id = data.get("operation_id")
                        failure = raw_content.get("error")
                        pending_timeout = isinstance(failure, dict) and failure.get("type") == "BridgeExecutionPendingError"
                        if isinstance(operation_id, str) and pending_timeout:
                            pending_operations[operation_id] = (seq, arguments.get("tool", name), arguments.get("arguments", arguments), target_instance)
                        if name == "operation.start" and isinstance(operation_id, str) and raw_content.get("ok") is True:
                            pending_operations[operation_id] = (seq, arguments["tool"], arguments["arguments"], target_instance)
                        if name == "operation.status" and isinstance(operation_id, str) and data.get("status") in {"succeeded", "failed", "cancelled"}:
                            pending = pending_operations.pop(operation_id, None)
                            if pending is not None and isinstance(data.get("result"), dict):
                                late_seq, target, params, instance = pending
                                call_log.record(seq=late_seq, tool=target, params=params, result=data["result"], public=data["result"], instance=instance, blend_file=target_blend, late=True)
                return public
            except Exception as exc:
                call_log.record(seq=seq, tool=name, params=arguments, error=exc, elapsed_ms=(time.perf_counter() - started) * 1000, instance=target_instance, blend_file=target_blend)
                raise
            finally:
                supplied_tool_arguments.reset(supplied)

    proxy = proxy or ToolkitProxy(InstanceRouter.from_env())
    if isinstance(proxy, ToolkitProxy):
        proxy.expected_addon_version = _adapter_version()
    job_manager = WorkflowJobManager()
    resolved_tool_mode = _tool_mode(tool_mode)
    resolved_response_mode = response_mode(response_profile)
    resolved_text_channel = _text_channel()
    response_store = ResponseArtifactStore()
    call_log = CallLog(token=getattr(getattr(proxy, "client", None), "_token", None), adapter_version=_adapter_version())
    proxy.call_log = call_log
    pending_operations: dict[str, tuple[int, str, dict[str, Any], str | int | None]] = {}
    mcp = _CatalogFastMCP(
        "BlenderBoo",
        instructions=_SERVER_INSTRUCTIONS,
        json_response=True,
    )

    @mcp.tool(name="python.exec")
    def python_exec(
        code: str,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        checkpoint: bool | str = False,
        diff: Literal["none", "summary", "detailed"] = "summary",
        timeout_hint_s: Annotated[float | None, Field(gt=0)] = None,
        collapse_face_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.25,
        collapse_volume_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.25,
        bbox_jump_ratio: Annotated[float, Field(gt=1)] = 2,
        fragment_small_ratio: Annotated[float, Field(gt=0, le=1)] = 0.01,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Run unrestricted Python once on Blender's main thread with bpy, bmesh, mathutils, Vector, Matrix, C, D, and view3d_override. Assign result to return a value; stdout/stderr are bounded to 8 KiB inline. changes is observational: changed:false means no_changes_detected. checkpoint saves before execution when the file has a path. A running script cannot be cancelled. GPU/render calls in the bridge timer can crash Blender; use deferred capture/render tools. File switches or add-on reloads can invalidate bridge state. Document-local cbt_modules imports persist across calls; the receipt lists their source hashes. Store reusable helpers with python.module_put. Check unfamiliar or version-sensitive bpy paths with api.lookup before writing exec code. diff:summary (default) uses depsgraph updates and an ID inventory; diff:none skips detection; diff:detailed is slow, for targeted forensics. A recognized missing-member error adds api_hint. new_islands flags fragments on changed meshes for review; it is not a failure. A windowless FBX operator-context failure names interchange.import as the typed route. Default MCP responses summarize large change lists with full detail in an artifact."""
        arguments: dict[str, Any] = {"code": code, "checkpoint": checkpoint, "diff": diff,
                                     "collapse_face_ratio": collapse_face_ratio,
                                     "collapse_volume_ratio": collapse_volume_ratio, "bbox_jump_ratio": bbox_jump_ratio}
        arguments["fragment_small_ratio"] = fragment_small_ratio
        if expected_state is not None:
            arguments["expected_state"] = expected_state
        if timeout_hint_s is not None:
            arguments["timeout_hint_s"] = timeout_hint_s
        return proxy.call_named("python.exec", arguments, instance)

    @mcp.tool(name="api.lookup")
    def api_lookup(
        path: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
        search: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        detail: Literal["summary", "full"] = "summary",
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,12}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect the installed Blender Python API by dotted path or paged search without reading or changing the scene."""
        if (path is None) == (search is None):
            raise ValueError("provide exactly one of path or search")
        arguments: dict[str, Any] = {"detail": detail, "limit": limit}
        if path is not None:
            arguments["path"] = path
        if search is not None:
            arguments["search"] = search
        if cursor is not None:
            arguments["cursor"] = cursor
        return proxy.call_named("api.lookup", arguments, instance)

    @mcp.tool(name="python.module_put")
    def python_module_put(
        name: _OBJECT_NAME,
        code: str | None = None,
        path: Annotated[str | None, Field(min_length=1)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Store a Python helper in this .blend. Saved source reappears when the same file is reopened; a different .blend has its own helpers. A path is copied once, not linked. Immutable versions remain for replay."""
        params: dict[str, Any] = {"name": name}
        if code is not None:
            params["code"] = code
        if path is not None:
            params["path"] = path
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("python.module_put", params, instance)

    @mcp.tool(name="python.module_list")
    def python_module_list(instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """List active document-local Python helpers and their source hashes."""
        return proxy.call_named("python.module_list", {}, instance)

    @mcp.tool(name="python.module_remove")
    def python_module_remove(
        name: _OBJECT_NAME,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Remove an active document-local Python helper; retained source versions remain in the .blend."""
        params: dict[str, Any] = {"name": name}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("python.module_remove", params, instance)

    @mcp.tool(name="scene.inspect")
    def scene_inspect(
        object_name: Annotated[str | None, Field(description="Optional object to inspect.")] = None,
        include_names: bool = False,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[
            str | None,
            Field(pattern=r"^[0-9]{1,18}$", description="Cursor returned by the previous page."),
        ] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect a bounded scene summary or one named object."""
        return proxy.scene_inspect(object_name, include_names, limit, cursor, instance)

    @mcp.tool(name="assembly.analyze")
    def assembly_analyze(
        pairs: _ASSEMBLY_PAIRS | None = None,
        object_names: _ASSEMBLY_OBJECT_NAMES | None = None,
        collection_name: _OBJECT_NAME | None = None,
        search_margin: Annotated[float | None, Field(ge=0)] = None,
        contact_tolerance: Annotated[float, Field(ge=0)] = 0.0001,
        borderline_relative_band: Annotated[float, Field(ge=0, le=1)] = 0.02,
        max_faces_per_object: Annotated[int, Field(ge=1, le=2_000_000)] = 250_000,
        max_pairs: Annotated[int, Field(ge=1, le=10_000)] = 200,
        evaluated: bool = True,
        expectations: list[AssemblyExpectation] | None = None,
        expect_isolated: Annotated[list[_OBJECT_NAME], Field(json_schema_extra={"uniqueItems": True})] | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Measure candidate part interfaces on evaluated world-space mesh surfaces; distances and penetration are sampled estimates. Face budgets refuse oversized meshes, and max_pairs caps candidate work. Returns contact, gap, intersection, expectation status, and focus. Penetration is undetermined when a target mesh is open. isolated_parts lists parts touching nothing for review; name intentionally free parts in expect_isolated."""
        params: dict[str, Any] = {"contact_tolerance": contact_tolerance, "borderline_relative_band": borderline_relative_band,
                                  "max_faces_per_object": max_faces_per_object,
                                  "max_pairs": max_pairs, "evaluated": evaluated}
        for key, value in (("pairs", pairs), ("object_names", object_names),
                           ("collection_name", collection_name), ("search_margin", search_margin)):
            if value is not None:
                params[key] = value
        if expectations is not None:
            params["expectations"] = _model_payloads(expectations)
        if expect_isolated is not None:
            params["expect_isolated"] = expect_isolated
        return proxy.assembly_analyze(params, instance)

    @mcp.tool(name="source.inspect")
    def source_inspect(object_name: _OBJECT_NAME, instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Inspect source mesh topology, islands, symmetry, bounds, textures, and pose hints without changing it."""
        return proxy.call_named("source.inspect", {"object_name": object_name}, instance)

    @mcp.tool(name="source.prepare")
    def source_prepare(
        object_name: _OBJECT_NAME, voxel_size: Annotated[float, Field(gt=0)],
        min_island_vertices: Annotated[int | None, Field(ge=1)] = None,
        output_name: _OBJECT_NAME | None = None,
        symmetrize: bool | None = None,
        symmetry_plane_x: float | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create a separate watertight projection proxy from a source mesh while preserving the original."""
        params: dict[str, Any] = {"object_name": object_name, "voxel_size": voxel_size}
        for key, value in (("min_island_vertices", min_island_vertices), ("output_name", output_name),
                           ("symmetrize", symmetrize), ("symmetry_plane_x", symmetry_plane_x),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("source.prepare", params, instance)

    @mcp.tool(name="creature.graph_suggest")
    def creature_graph_suggest(body_plan: Literal["humanoid", "quadruped", "reptile"],
                               proxy_name: _OBJECT_NAME, instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Suggest a body graph from body-plan landmarks and a projection proxy; inspect confidence before applying."""
        return proxy.call_named("creature.graph_suggest", {"body_plan": body_plan, "proxy_name": proxy_name}, instance)

    @mcp.tool(name="creature.landmark_set")
    def creature_landmark_set(name: _OBJECT_NAME, position: _VECTOR3,
                              object_name: _OBJECT_NAME | None = None,
                              expected_state: Annotated[str | None, Field(min_length=1)] = None,
                              instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Create or move a named landmark Empty for body-graph suggestions."""
        params: dict[str, Any] = {"name": name, "position": position}
        if object_name is not None:
            params["object_name"] = object_name
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("creature.landmark_set", params, instance)

    @mcp.tool(name="creature.graph_set")
    def creature_graph_set(object_name: _OBJECT_NAME, graph: dict[str, Any],
                           expected_state: Annotated[str | None, Field(min_length=1)] = None,
                           instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Validate and store an explicit body graph on an object for mesh, region, and skeleton generation."""
        params: dict[str, Any] = {"object_name": object_name, "graph": graph}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("creature.graph_set", params, instance)

    @mcp.tool(name="creature.graph_inspect")
    def creature_graph_inspect(object_name: _OBJECT_NAME, instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Read the validated body graph attached to an object."""
        return proxy.call_named("creature.graph_inspect", {"object_name": object_name}, instance)

    @mcp.tool(name="creature.base_from_graph")
    def creature_base_from_graph(graph_object: _OBJECT_NAME, output_name: _OBJECT_NAME | None = None,
                                 expected_state: Annotated[str | None, Field(min_length=1)] = None,
                                 instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Build a quad-dominant region-labeled creature base from a body graph as a new mesh."""
        params: dict[str, Any] = {"graph_object": graph_object}
        if output_name is not None:
            params["output_name"] = output_name
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("creature.base_from_graph", params, instance)

    @mcp.tool(name="armature.from_body_graph")
    def armature_from_body_graph(graph_object: _OBJECT_NAME,
                                 output_name: _OBJECT_NAME | None = None,
                                 reference_armature_name: _OBJECT_NAME | None = None,
                                 bone_map: dict[str, _OBJECT_NAME] | None = None,
                                 ue_mannequin: bool = False,
                                 expected_state: Annotated[str | None, Field(min_length=1)] = None,
                                 instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Create a one-root custom armature from a stored body graph, or copy every rest and auxiliary bone from an explicitly mapped imported UE reference armature."""
        params: dict[str, Any] = {"graph_object": graph_object, "ue_mannequin": ue_mannequin}
        for key, value in (("output_name", output_name), ("reference_armature_name", reference_armature_name),
                           ("bone_map", bone_map), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("armature.from_body_graph", params, instance)

    @mcp.tool(name="armature.validate_against_reference")
    def armature_validate_against_reference(armature_name: _OBJECT_NAME,
                                             reference_armature_name: _OBJECT_NAME | None = None,
                                             mesh_name: _OBJECT_NAME | None = None,
                                             bone_tolerance_m: Annotated[float, Field(ge=0)] = 0.001,
                                             instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Check one-root hierarchy, nonzero bones, joint continuity, optional closed-mesh containment, and exact rest-bone comparison with an imported reference armature."""
        params: dict[str, Any] = {"armature_name": armature_name, "bone_tolerance_m": bone_tolerance_m}
        if reference_armature_name is not None:
            params["reference_armature_name"] = reference_armature_name
        if mesh_name is not None:
            params["mesh_name"] = mesh_name
        return proxy.call_named("armature.validate_against_reference", params, instance)

    @mcp.tool(name="armature.export_ue_fbx")
    def armature_export_ue_fbx(armature_name: _OBJECT_NAME,
                                path: Annotated[str, Field(min_length=1)],
                                mesh_name: _OBJECT_NAME | None = None,
                                overwrite: bool = False, roundtrip: bool = True,
                                action_names: Annotated[list[_OBJECT_NAME] | None,
                                    Field(max_length=64, json_schema_extra={"uniqueItems": True})] = None,
                                action_fps: Annotated[dict[str, Annotated[int, Field(ge=1, le=32767)]] | None,
                                    Field(max_length=64)] = None,
                                include_mesh: bool = True,
                                root_mode: Literal["auto", "object_is_root", "object_stripped"] = "auto",
                                use_armature_deform_only: bool = False,
                                apply_scale_options: Literal["FBX_SCALE_ALL", "FBX_SCALE_NONE"] = "FBX_SCALE_ALL",
                                instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Export a UE skeletal mesh or selected Action clips through one shared FBX node, axis, scale and deform-only path. FBX_SCALE_ALL is the default; root_mode auto strips the armature object when a root bone exists, otherwise exports the object as root. Per-action FPS is explicit. Round-trip acceptance checks scale and layout; a failed candidate is not published. Import onto a fresh UE skeleton when replacing an older x100 layout."""
        params: dict[str, Any] = {"armature_name": armature_name, "path": path,
                                  "overwrite": overwrite, "roundtrip": roundtrip,
                                  "include_mesh": include_mesh, "root_mode": root_mode,
                                  "use_armature_deform_only": use_armature_deform_only,
                                  "apply_scale_options": apply_scale_options}
        for key, value in (("mesh_name", mesh_name), ("action_names", action_names),
                           ("action_fps", action_fps)):
            if value is not None:
                params[key] = value
        return proxy.call_named("armature.export_ue_fbx", params, instance)

    @mcp.tool(name="creature.bake_from_source")
    def creature_bake_from_source(sources: Annotated[list[_OBJECT_NAME], Field(min_length=1, json_schema_extra={"uniqueItems": True})], target: _OBJECT_NAME,
                                   uv_layer: _OBJECT_NAME, output_dir: Annotated[str, Field(min_length=1)],
                                   maps: Annotated[list[Literal["normal", "ao", "base_color"]], Field(min_length=1, max_length=3, json_schema_extra={"uniqueItems": True})] = ["normal", "ao", "base_color"],
                                   resolution: Annotated[int, Field(ge=16, le=4096)] = 1024,
                                   image_prefix: _OBJECT_NAME | None = None,
                                   margin: Annotated[int, Field(ge=0, le=128)] = 16,
                                   overwrite: bool = False, cage_object: _OBJECT_NAME | None = None,
                                   cage_extrusion: Annotated[float, Field(ge=0, le=1000)] = 0,
                                   max_ray_distance: Annotated[float, Field(ge=0, le=1000)] = 0,
                                   normal_g: Literal["POS_Y", "NEG_Y"] = "POS_Y",
                                   expected_state: Annotated[str | None, Field(min_length=1)] = None,
                                   instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Bake normal, AO, and base-color maps from unchanged original source meshes to one fitted UV mesh; report coverage and possible baked lighting."""
        params: dict[str, Any] = {"sources": sources, "target": target, "uv_layer": uv_layer,
                                  "output_dir": output_dir, "maps": maps, "overwrite": overwrite}
        for key, value in (("resolution", resolution), ("image_prefix", image_prefix),
                           ("margin", margin), ("cage_object", cage_object),
                           ("cage_extrusion", cage_extrusion), ("max_ray_distance", max_ray_distance),
                           ("normal_g", normal_g), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("creature.bake_from_source", params, instance)

    @mcp.tool(name="rig.rom_test")
    def rig_rom_test(object_name: _OBJECT_NAME, armature_name: _OBJECT_NAME,
                     output_prefix: Annotated[str, Field(min_length=1)],
                     body_plan: _OBJECT_NAME | None = None,
                     poses: Annotated[list[RomPose] | None, Field(min_length=1, max_length=12)] = None,
                     width: Annotated[int, Field(ge=128, le=2048)] = 256,
                     height: Annotated[int, Field(ge=128, le=2048)] = 256,
                     view_mode: _VIEW_MODE = "matcap",
                     direction: _DETAIL_DIRECTION | _VECTOR3 = "three_quarter",
                     overwrite: bool = False, allow_large: bool = False,
                     max_triangles: Annotated[int, Field(ge=1, le=100000)] = 20000,
                     min_volume_ratio: Annotated[float, Field(ge=0.01, le=1)] = 0.6,
                     max_volume_ratio: Annotated[float, Field(ge=1, le=10)] = 1.4,
                     max_edge_stretch: Annotated[float, Field(ge=1, le=10)] = 1.5,
                     min_edge_compression: Annotated[float, Field(ge=0.01, le=1)] = 0.67,
                     min_twist_area_ratio: Annotated[float, Field(ge=0.01, le=1)] = 0.5,
                     expected_state: Annotated[str | None, Field(min_length=1)] = None,
                     instance: _INSTANCE_SELECTOR = None) -> CallToolResult:
        """Measure editable creature range-of-motion poses for local volume, edge stretch, intersections, and twist; render deferred pose PNGs and a contact sheet, then restore the pose."""
        params: dict[str, Any] = {"object_name": object_name, "armature_name": armature_name,
                                  "output_prefix": output_prefix, "width": width, "height": height,
                                  "view_mode": view_mode, "direction": direction,
                                  "overwrite": overwrite, "allow_large": allow_large,
                                  "max_triangles": max_triangles, "min_volume_ratio": min_volume_ratio,
                                  "max_volume_ratio": max_volume_ratio, "max_edge_stretch": max_edge_stretch,
                                  "min_edge_compression": min_edge_compression,
                                  "min_twist_area_ratio": min_twist_area_ratio}
        for key, value in (("body_plan", body_plan), ("poses", _model_payloads(poses)),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        envelope = proxy.call_named("rig.rom_test", params, instance)
        data = envelope.get("data") if isinstance(envelope, dict) else None
        images = []
        if isinstance(data, dict):
            images = [data.get("contact_sheet")]
        return _render_result(envelope, images)

    @mcp.tool(name="mesh.wrap_to_target")
    def mesh_wrap_to_target(object_name: _OBJECT_NAME, target_name: _OBJECT_NAME,
                            output_name: _OBJECT_NAME | None = None,
                            iterations: Annotated[int | None, Field(ge=1)] = None,
                            projection_strength: Annotated[float | None, Field(ge=0, le=1)] = None,
                            smooth_strength: Annotated[float | None, Field(ge=0, le=1)] = None,
                            landmark_pairs: Annotated[list[LandmarkPair] | None, Field(min_length=3)] = None,
                            region_stiffness: dict[str, Any] | None = None,
                            symmetry_plane_x: float | None = None,
                            coverage_tolerance: Annotated[float | None, Field(gt=0)] = None,
                            expected_state: Annotated[str | None, Field(min_length=1)] = None,
                            instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Fit a copy of a fixed-topology mesh to a target surface and report distance and topology proof."""
        params: dict[str, Any] = {"object_name": object_name, "target_name": target_name}
        for key, value in (("output_name", output_name), ("iterations", iterations),
                           ("projection_strength", projection_strength), ("smooth_strength", smooth_strength),
                           ("landmark_pairs", _model_payloads(landmark_pairs)), ("region_stiffness", region_stiffness),
                           ("symmetry_plane_x", symmetry_plane_x),
                           ("coverage_tolerance", coverage_tolerance),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("mesh.wrap_to_target", params, instance)

    @mcp.tool(name="mesh.topology_report")
    def mesh_topology_report(object_name: _OBJECT_NAME,
                             triangle_budget: Annotated[int | None, Field(ge=1)] = None,
                             tiny_face_area: Annotated[float | None, Field(ge=0)] = None,
                             symmetry_plane_x: float | None = None,
                             bend_zones: list[BendZone] | None = None,
                             instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Report face quality, poles, symmetry, bend zones, budget, and connectivity of a mesh."""
        params: dict[str, Any] = {"object_name": object_name}
        for key, value in (("triangle_budget", triangle_budget), ("tiny_face_area", tiny_face_area),
                           ("symmetry_plane_x", symmetry_plane_x),
                           ("bend_zones", _model_payloads(bend_zones))):
            if value is not None:
                params[key] = value
        return proxy.call_named("mesh.topology_report", params, instance)

    @mcp.tool(name="pointcloud.section")
    def pointcloud_section(pointcloud_name: _OBJECT_NAME, plane_point: _VECTOR3,
                           plane_normal: _VECTOR3, band_width: Annotated[float, Field(gt=0)],
                           mesh_name: _OBJECT_NAME | None = None, roi: DetailBounds | None = None,
                           max_points: Annotated[int, Field(ge=1, le=1_000_000)] = 100_000,
                           output_path: Annotated[str | None, Field(min_length=1)] = None,
                           overwrite: bool = False, instance: _INSTANCE_SELECTOR = None) -> CallToolResult:
        """Slice a point cloud and optional mesh in a bounded ROI and return 2D evidence with a section image."""
        params: dict[str, Any] = {"pointcloud_name": pointcloud_name, "plane_point": plane_point,
                                  "plane_normal": plane_normal, "band_width": band_width,
                                  "max_points": max_points, "overwrite": overwrite}
        if mesh_name is not None:
            params["mesh_name"] = mesh_name
        if roi is not None:
            params["roi"] = roi.model_dump()
        if output_path is not None:
            params["output_path"] = output_path
        envelope = proxy.call_named("pointcloud.section", params, instance)
        data = envelope.get("data")
        return _render_result(envelope, [data.get("image_path")] if isinstance(data, dict) else [])

    @mcp.tool(name="reference.crop_grid")
    def reference_crop_grid(reference_path: Annotated[str, Field(min_length=1)],
                            center: tuple[int, int],
                            radius: Annotated[int, Field(ge=8, le=512)] = 64,
                            zoom: Annotated[int, Field(ge=1, le=16)] = 4,
                            grid_spacing: Annotated[int, Field(ge=1, le=128)] = 10,
                            output_path: Annotated[str | None, Field(min_length=1)] = None,
                            overwrite: bool = False,
                            instance: _INSTANCE_SELECTOR = None) -> CallToolResult:
        """Return a zoomed pixel-addressable crop with grid coordinates for manual reference correspondences."""
        params: dict[str, Any] = {"reference_path": reference_path, "center": center,
                                  "radius": radius, "zoom": zoom, "grid_spacing": grid_spacing,
                                  "overwrite": overwrite}
        if output_path is not None:
            params["output_path"] = output_path
        envelope = proxy.call_named("reference.crop_grid", params, instance)
        data = envelope.get("data")
        return _render_result(envelope, [data.get("path")] if isinstance(data, dict) else [])

    @mcp.tool(name="mesh.compare_to_points")
    def mesh_compare_to_points(pointcloud_name: _OBJECT_NAME, mesh_name: _OBJECT_NAME,
                               roi: DetailBounds | None = None,
                               max_points: Annotated[int, Field(ge=1, le=1_000_000)] = 100_000,
                               min_points: Annotated[int, Field(ge=1)] = 50,
                               outlier_sigma: Annotated[float, Field(gt=0)] = 3.0,
                               max_p95: Annotated[float | None, Field(ge=0)] = None,
                               borderline_band: Annotated[float, Field(ge=0, le=1)] = 0.02,
                               output_path: Annotated[str | None, Field(min_length=1)] = None,
                               overwrite: bool = False,
                               instance: _INSTANCE_SELECTOR = None) -> CallToolResult:
        """Measure point-to-mesh distances in an ROI with outlier filtering, sparse-state evidence, a heat map, and focus."""
        params: dict[str, Any] = {"pointcloud_name": pointcloud_name, "mesh_name": mesh_name,
                                  "max_points": max_points, "min_points": min_points,
                                  "outlier_sigma": outlier_sigma, "borderline_band": borderline_band,
                                  "overwrite": overwrite}
        if max_p95 is not None:
            params["max_p95"] = max_p95
        if roi is not None:
            params["roi"] = roi.model_dump()
        if output_path is not None:
            params["output_path"] = output_path
        envelope = proxy.call_named("mesh.compare_to_points", params, instance)
        data = envelope.get("data")
        return _render_result(envelope, [data.get("image_path")] if isinstance(data, dict) else [])

    @mcp.tool(name="annotation.inspect")
    def annotation_inspect(
        annotation_name: Annotated[str | None, Field(min_length=1)] = None,
        layer_name: Annotated[str | None, Field(min_length=1)] = None,
        frame_number: Annotated[
            int | None, Field(ge=-1_048_574, le=1_048_574)
        ] = None,
        stroke_index: Annotated[
            int | None, Field(ge=0, le=100_000_000)
        ] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[
            str | None,
            Field(
                pattern=r"^[0-9]{1,18}$",
                description="Cursor returned by the previous page.",
            ),
        ] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded Annotation datablocks through strokes and points."""
        return proxy.annotation_inspect(
            annotation_name,
            layer_name,
            frame_number,
            stroke_index,
            limit,
            cursor,
            instance,
        )

    @mcp.tool(name="scene.screenshot")
    def scene_screenshot(
        output_path: Annotated[str | None, Field(min_length=1)] = None,
        overwrite: bool = False,
        width: Annotated[int | None, Field(ge=1, le=16384)] = None,
        height: Annotated[int | None, Field(ge=1, le=16384)] = None,
        view: ScreenshotView | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Capture the current viewport or an independent orbit view without changing the UI."""
        view_params = (
            view.model_dump(exclude_none=True, exclude_unset=True)
            if view is not None
            else None
        )
        envelope = proxy.scene_screenshot(
            output_path, overwrite, width, height, instance, view_params
        )
        data = envelope.get("data")
        path = data.get("path") if isinstance(data, dict) else None
        return _render_result(envelope, [path] if path is not None else [])

    @mcp.tool(name="session.instances")
    def session_instances() -> dict[str, Any]:
        """Enumerate registered local Blender instances without exposing tokens."""
        return proxy.session_instances()

    @mcp.tool(name="session.call_log_summary")
    def session_call_log_summary(
        since: Annotated[str | None, Field(description="Optional ISO-8601 UTC timestamp.")] = None,
        since_seq: Annotated[int | None, Field(ge=0)] = None,
        detail: Literal["compact", "full"] = "compact",
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Summarize local adapter call logs without sending their contents elsewhere."""
        from datetime import datetime
        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
            if parsed is not None and parsed.tzinfo is None:
                raise ValueError("since requires a timezone")
            target, _ = call_log.target_context(instance, proxy.client)
            data = summarize([call_log.directory], since=parsed, instance=target if instance is not None else None,
                             since_seq=since_seq, session=call_log.session if since_seq is not None else None,
                             detail=detail)
            return {"ok": True, "tool": "session.call_log_summary", "data": data, "error": None, "warnings": []}
        except (ValueError, OSError) as exc:
            return {"ok": False, "tool": "session.call_log_summary", "data": {}, "error": {"type": type(exc).__name__, "message": str(exc), "traceback": ""}, "warnings": []}

    @mcp.tool(name="operation.start")
    def operation_start(
        tool: Literal["python.exec", "render.preview", "render.review_set", "render.detail", "render.ortho_set", "render.reference_match", "render.contact_sheet", "scene.save_current", "scene.save_as", "bake.run"],
        arguments: dict[str, Any],
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Queue a long Blender operation and return its recovery id immediately."""
        return proxy._call("bridge.operation_start", {"tool": tool, "arguments": arguments}, instance)

    @mcp.tool(name="operation.status")
    def operation_status(
        operation_id: Annotated[str, Field(min_length=1)],
        wait_ms: Annotated[int, Field(ge=0, le=30000)] = 0,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Read queued, running, or completed result of a long Blender operation; wait_ms long-polls until it finishes. A successful status call can carry a failed inner result: check the contained ok and error."""
        return proxy._call("bridge.operation_status", {"operation_id": operation_id, "wait_ms": wait_ms}, instance)

    @mcp.tool(name="response.artifact_get")
    def response_artifact_get(
        artifact_id: Annotated[
            str,
            Field(pattern=r"^response-[0-9]{8}-[0-9a-f]{12}$"),
        ],
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        max_chars: Annotated[
            int,
            Field(ge=1, le=MAX_RETRIEVAL_CHARS),
        ] = DEFAULT_RETRIEVAL_CHARS,
        path: Annotated[str | None, Field(min_length=1)] = None,
    ) -> dict[str, Any]:
        """Retrieve a bounded chunk from a process-local full response artifact; entries can be evicted and do not survive adapter restart (64 entries, 4 MiB each, 32 MiB total)."""
        try:
            data = response_store.get(
                artifact_id,
                cursor=0 if cursor is None else int(cursor),
                max_chars=max_chars,
                path=path,
            )
            return {
                "ok": True,
                "tool": "response.artifact_get",
                "data": data,
                "error": None,
                "warnings": [],
            }
        except KeyError:
            return {
                "ok": False,
                "tool": "response.artifact_get",
                "data": {"artifact_id": artifact_id},
                "error": {
                    "type": "ResponseArtifactNotFoundError",
                    "message": "Response artifact is unavailable or was evicted from this MCP process.",
                    "traceback": "",
                },
                "warnings": [],
            }
        except ValueError as exc:
            return {
                "ok": False,
                "tool": "response.artifact_get",
                "data": {"artifact_id": artifact_id},
                "error": {
                    "type": "InvalidParamsError",
                    "message": str(exc),
                    "traceback": "",
                },
                "warnings": [],
            }

    @mcp.tool(name="collection.inspect")
    def collection_inspect(
        name: Annotated[str | None, Field(min_length=1)] = None,
        name_regex: Annotated[str | None, Field(min_length=1)] = None,
        parent: Annotated[str | None, Field(min_length=1)] = None,
        hidden_viewport: bool | None = None,
        hidden_render: bool | None = None,
        custom_property: Annotated[str | None, Field(min_length=1)] = None,
        custom_value: _CUSTOM_VALUE | None = None,
        collection_name: Annotated[str | None, Field(min_length=1)] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """List a bounded collection page and optionally detail one collection."""
        return proxy.collection_inspect(
            name, name_regex, parent, hidden_viewport, hidden_render,
            custom_property, custom_value, collection_name, limit, cursor, instance,
        )

    @mcp.tool(name="collection.create")
    def collection_create(
        name: _OBJECT_NAME,
        parent: Annotated[str | None, Field(min_length=1)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create one collection under the scene root or a named parent."""
        return proxy.collection_create(name, parent, expected_state, instance)

    @mcp.tool(name="collection.rename")
    def collection_rename(
        collection_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        allow_suffix: bool = False,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Rename one collection, rejecting collisions unless suffixing is allowed."""
        return proxy.collection_rename(
            collection_name, name, allow_suffix, expected_state, instance
        )

    @mcp.tool(name="collection.set_visibility")
    def collection_set_visibility(
        collections: _OBJECT_NAMES,
        hide_viewport: bool | None = None,
        hide_render: bool | None = None,
        hide_select: bool | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch viewport, render, or selection visibility for collections."""
        return proxy.collection_set_visibility(
            collections, hide_viewport, hide_render, hide_select, expected_state, instance
        )

    @mcp.tool(name="collection.link_objects")
    def collection_link_objects(
        collection: _OBJECT_NAME,
        objects: _OBJECT_NAMES,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Link existing scene objects directly into one collection."""
        return proxy.collection_link_objects(
            collection, objects, expected_state, instance
        )

    @mcp.tool(name="collection.unlink_objects")
    def collection_unlink_objects(
        collection: _OBJECT_NAME,
        objects: _OBJECT_NAMES,
        dry_run: Annotated[bool, Field(description="Defaults true; pass false to apply unlinking.")] = True,
        confirm: Annotated[Literal[True] | None, Field(description="Must be true when dry_run is false.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or explicitly confirm removal of object-collection links."""
        return proxy.collection_unlink_objects(
            collection, objects, dry_run, confirm, expected_state, instance
        )

    @mcp.tool(name="collection.parent_objects")
    def collection_parent_objects(collection: _OBJECT_NAME, parent: _OBJECT_NAME | None, recursive: bool = False, keep_transform: bool = True, max_objects: Annotated[int, Field(ge=1, le=2000)] = 2000, dry_run: bool = True, expected_state: Annotated[str | None, Field(min_length=1)] = None, instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Dry-run-first parenting of at most 2,000 direct or recursive collection members in one operation."""
        return proxy.collection_parent_objects(collection, parent, recursive, keep_transform, max_objects, dry_run, expected_state, instance)

    @mcp.tool(name="collection.transform")
    def collection_transform(collection: _OBJECT_NAME, translation: _VECTOR3 = (0, 0, 0), rotation_euler: _VECTOR3 = (0, 0, 0), scale: _VECTOR3 = (1, 1, 1), recursive: bool = False, max_objects: Annotated[int, Field(ge=1, le=2000)] = 2000, dry_run: bool = True, expected_state: Annotated[str | None, Field(min_length=1)] = None, instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Dry-run-first world-space transform of at most 2,000 direct or recursive collection members; refuses scopes that would move external descendants indirectly."""
        return proxy.collection_transform(collection, translation, rotation_euler, scale, recursive, max_objects, dry_run, expected_state, instance)

    @mcp.tool(name="collection.link_child")
    def collection_link_child(
        parent: _OBJECT_NAME,
        child: _OBJECT_NAME,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Add a direct collection hierarchy link after cycle validation."""
        return proxy.collection_link_child(parent, child, expected_state, instance)

    @mcp.tool(name="collection.unlink_child")
    def collection_unlink_child(
        parent: _OBJECT_NAME,
        child: _OBJECT_NAME,
        dry_run: Annotated[bool, Field(description="Defaults true; pass false to apply unlinking.")] = True,
        confirm: Annotated[Literal[True] | None, Field(description="Must be true when dry_run is false.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or explicitly confirm removal of a hierarchy link."""
        return proxy.collection_unlink_child(
            parent, child, dry_run, confirm, expected_state, instance
        )

    @mcp.tool(name="collection.delete")
    def collection_delete(
        collections: _OBJECT_NAMES,
        recursive: bool = False,
        dry_run: Annotated[bool, Field(description="Defaults true; reports the full deletion set.")] = True,
        confirm: Annotated[Literal[True] | None, Field(description="Must be true when dry_run is false.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or explicitly confirm safe collection deletion."""
        return proxy.collection_delete(
            collections, recursive, dry_run, confirm, expected_state, instance
        )

    @mcp.tool(name="camera.inspect")
    def camera_inspect(
        name: Annotated[str | None, Field(min_length=1)] = None,
        active: bool | None = None,
        object_name: Annotated[str | None, Field(min_length=1)] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded camera details and active-camera assignment."""
        return proxy.camera_inspect(name, active, object_name, limit, cursor, instance)

    @mcp.tool(name="camera.patch")
    def camera_patch(
        object_name: _OBJECT_NAME,
        type: _CAMERA_TYPE | None = None,
        lens: Annotated[float | None, Field(ge=1, le=5000)] = None,
        ortho_scale: Annotated[float | None, Field(gt=0, le=1000000)] = None,
        clip_start: Annotated[float | None, Field(gt=0, le=1000000)] = None,
        clip_end: Annotated[float | None, Field(gt=0, le=1000000000)] = None,
        shift_x: Annotated[float | None, Field(ge=-10, le=10)] = None,
        shift_y: Annotated[float | None, Field(ge=-10, le=10)] = None,
        sensor_width: Annotated[float | None, Field(ge=1, le=1000)] = None,
        sensor_height: Annotated[float | None, Field(ge=1, le=1000)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch allowlisted camera data fields with rollback metadata."""
        return proxy.camera_patch(
            object_name, type, lens, ortho_scale, clip_start, clip_end,
            shift_x, shift_y, sensor_width, sensor_height, expected_state, instance,
        )

    @mcp.tool(name="camera.set_active")
    def camera_set_active(
        object_name: _OBJECT_NAME | None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Assign or clear the active scene camera."""
        return proxy.camera_set_active(object_name, expected_state, instance)

    @mcp.tool(name="light.inspect")
    def light_inspect(
        name: Annotated[str | None, Field(min_length=1)] = None,
        type: _LIGHT_TYPE | None = None,
        object_name: Annotated[str | None, Field(min_length=1)] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded common and type-specific light details."""
        return proxy.light_inspect(name, type, object_name, limit, cursor, instance)

    @mcp.tool(name="light.patch")
    def light_patch(
        object_name: _OBJECT_NAME,
        type: _LIGHT_TYPE | None = None,
        energy: Annotated[float | None, Field(ge=0, le=1000000000)] = None,
        color: _COLOR3 | None = None,
        use_shadow: bool | None = None,
        shadow_soft_size: Annotated[float | None, Field(ge=0, le=1000000)] = None,
        shape: _AREA_SHAPE | None = None,
        size: Annotated[float | None, Field(gt=0, le=1000000)] = None,
        size_y: Annotated[float | None, Field(gt=0, le=1000000)] = None,
        spot_size: Annotated[float | None, Field(gt=0, le=3.141592653589793)] = None,
        spot_blend: Annotated[float | None, Field(ge=0, le=1)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch allowlisted light fields with type-aware validation."""
        return proxy.light_patch(
            object_name, type, energy, color, use_shadow, shadow_soft_size,
            shape, size, size_y, spot_size, spot_blend, expected_state, instance,
        )

    @mcp.tool(name="world.inspect")
    def world_inspect(
        name: Annotated[str | None, Field(min_length=1)] = None,
        active: bool | None = None,
        world_name: Annotated[str | None, Field(min_length=1)] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded World datablock and Background details."""
        return proxy.world_inspect(name, active, world_name, limit, cursor, instance)

    @mcp.tool(name="world.create")
    def world_create(
        name: _OBJECT_NAME,
        color: _COLOR3 | None = None,
        use_nodes: bool = False,
        background_color: _COLOR3 | None = None,
        background_strength: Annotated[float | None, Field(ge=0, le=1000000)] = None,
        set_active: bool = False,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create a World and optionally assign it to the current scene."""
        return proxy.world_create(
            name, color, use_nodes, background_color, background_strength,
            set_active, expected_state, instance,
        )

    @mcp.tool(name="world.patch")
    def world_patch(
        world_name: _OBJECT_NAME,
        color: _COLOR3 | None = None,
        use_nodes: bool | None = None,
        background_color: _COLOR3 | None = None,
        background_strength: Annotated[float | None, Field(ge=0, le=1000000)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch basic World and unambiguous Background-node values."""
        return proxy.world_patch(
            world_name, color, use_nodes, background_color,
            background_strength, expected_state, instance,
        )

    @mcp.tool(name="world.set_active")
    def world_set_active(
        world_name: _OBJECT_NAME | None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Assign or clear the current scene World."""
        return proxy.world_set_active(world_name, expected_state, instance)

    @mcp.tool(name="render.settings")
    def render_settings(
        engine: Annotated[str | None, Field(min_length=1)] = None,
        resolution_x: Annotated[int | None, Field(ge=1, le=16384)] = None,
        resolution_y: Annotated[int | None, Field(ge=1, le=16384)] = None,
        resolution_percentage: Annotated[int | None, Field(ge=1, le=100)] = None,
        film_transparent: bool | None = None,
        fps: Annotated[int | None, Field(ge=1, le=240)] = None,
        fps_base: Annotated[float | None, Field(gt=0, le=1000)] = None,
        frame_start: Annotated[int | None, Field(ge=-1048574, le=1048574)] = None,
        frame_end: Annotated[int | None, Field(ge=-1048574, le=1048574)] = None,
        frame_step: Annotated[int | None, Field(ge=1, le=1048574)] = None,
        image_format: _IMAGE_FORMAT | None = None,
        filepath: Annotated[str | None, Field(min_length=1)] = None,
        cycles_samples: Annotated[int | None, Field(ge=1, le=65536)] = None,
        cycles_use_denoising: bool | None = None,
        eevee_use_raytracing: bool | None = None,
        view_transform: Annotated[str | None, Field(min_length=1)] = None,
        look: Annotated[str | None, Field(min_length=1)] = None,
        exposure: Annotated[float | None, Field(ge=-32, le=32)] = None,
        gamma: Annotated[float | None, Field(ge=0.001, le=5)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect or patch persistent render and frame-range settings."""
        return proxy.render_settings(
            engine, resolution_x, resolution_y, resolution_percentage,
            film_transparent, fps, fps_base, frame_start, frame_end, frame_step,
            image_format, filepath, expected_state, instance,
            cycles_samples=cycles_samples,
            cycles_use_denoising=cycles_use_denoising,
            eevee_use_raytracing=eevee_use_raytracing,
            view_transform=view_transform,
            look=look,
            exposure=exposure,
            gamma=gamma,
        )

    @mcp.tool(name="compositor.inspect")
    def compositor_inspect(
        scene: Annotated[str | None, Field(min_length=1)] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect a bounded page of compositor nodes, links, and File Output slots."""
        return proxy.compositor_inspect(scene, limit, cursor, instance)

    @mcp.tool(name="compositor.create")
    def compositor_create(
        scene: Annotated[str | None, Field(min_length=1)] = None,
        include_viewer: bool = False,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or create a non-destructive Render Layers to Composite graph."""
        return proxy.compositor_create(scene, include_viewer, dry_run, expected_state, instance)

    @mcp.tool(name="compositor.patch")
    def compositor_patch(
        operations: Annotated[list[NamedOperation], Field(min_length=1, max_length=100)],
        scene: Annotated[str | None, Field(min_length=1)] = None,
        expected_node_count: Annotated[int | None, Field(ge=0, le=10000)] = None,
        expected_link_count: Annotated[int | None, Field(ge=0, le=50000)] = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or apply rollback-backed allowlisted compositor graph edits."""
        return proxy.compositor_patch(
            _model_payloads(operations), scene, expected_node_count, expected_link_count,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="render.manifest")
    def render_manifest(
        scene: Annotated[str | None, Field(min_length=1)] = None,
        include_paths: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Describe render settings, view-layer passes, and compositor outputs."""
        return proxy.render_manifest(scene, include_paths, instance)

    @mcp.tool(name="render.manifest_validate")
    def render_manifest_validate(
        scene: Annotated[str | None, Field(min_length=1)] = None,
        include_paths: bool = False,
        require_composite: bool = True,
        require_file_output: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Preflight camera, passes, compositor outputs, and path collisions."""
        return proxy.render_manifest_validate(
            scene, include_paths, require_composite, require_file_output, instance,
        )

    @mcp.tool(name="interchange.inspect")
    def interchange_inspect(
        format: Literal["OBJ", "FBX", "GLTF", "USD", "ALEMBIC", "PLY"] | None = None,
        direction: Literal["import", "export"] | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect core-format operator availability and allowlisted options."""
        return proxy.interchange_inspect(format, direction, instance)

    @mcp.tool(name="interchange.import")
    def interchange_import(
        filepath: Annotated[str, Field(min_length=1)],
        format: Literal["OBJ", "FBX", "GLTF", "USD", "ALEMBIC", "PLY"] | None = None,
        options: dict[str, Any] | None = None,
        max_bytes: Annotated[int, Field(ge=1, le=17179869184)] = 2147483648,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or confirm an explicit-file core-format import with postflight."""
        return proxy.interchange_import(
            filepath, format, options, max_bytes, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="interchange.blend_import")
    def interchange_blend_import(
        filepath: Annotated[str, Field(min_length=1)],
        names: _OBJECT_NAMES,
        data_type: Literal["COLLECTION", "OBJECT"] = "COLLECTION",
        link: bool = False,
        target_collection: _OBJECT_NAME | None = None,
        max_bytes: Annotated[int, Field(ge=1, le=17179869184)] = 2147483648,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Dry-run-first append or link of named Collection/Object datablocks from one .blend file, subject to confirmation and a byte budget."""
        return proxy.interchange_blend_import(
            filepath, names, data_type, link, target_collection, max_bytes,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="interchange.export")
    def interchange_export(
        filepath: Annotated[str, Field(min_length=1)],
        format: Literal["OBJ", "FBX", "GLTF", "USD", "ALEMBIC"] | None = None,
        scope: Literal["selected", "scene", "collection"] = "selected",
        collection: Annotated[str | None, Field(min_length=1)] = None,
        options: dict[str, Any] | None = None,
        overwrite: bool = False,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or confirm a staged single-file core-format export with postflight."""
        return proxy.interchange_export(
            filepath, format, scope, collection, options, overwrite,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="armature.inspect")
    def armature_inspect(
        object_name: _OBJECT_NAME,
        include_pose: bool = True,
        include_constraints: bool = False,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded armature hierarchy, pose transforms, and constraints."""
        return proxy.armature_inspect(
            object_name, include_pose, include_constraints, limit, cursor, instance,
        )

    @mcp.tool(name="armature.create")
    def armature_create(
        name: _OBJECT_NAME,
        data_name: Annotated[str | None, Field(min_length=1)] = None,
        collection: Annotated[str | None, Field(min_length=1)] = None,
        display_type: Literal["OCTAHEDRAL", "STICK", "BBONE", "ENVELOPE", "WIRE"] = "OCTAHEDRAL",
        show_in_front: bool = True,
        bones: Annotated[list[ArmatureBone] | None, Field(max_length=512)] = None,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or create an armature with bounded initial bones and context restoration."""
        return proxy.armature_create(
            name, data_name, collection, display_type, show_in_front,
            _model_payloads(bones), dry_run, expected_state, instance,
        )

    @mcp.tool(name="armature.edit")
    def armature_edit(
        object_name: _OBJECT_NAME,
        operations: Annotated[list[NamedOperation], Field(min_length=1, max_length=100)],
        expected_bone_count: Annotated[int | None, Field(ge=0, le=10000)] = None,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or apply reversible EditBone add, patch, and rename operations."""
        return proxy.armature_edit(
            object_name, _model_payloads(operations), expected_bone_count, dry_run, expected_state, instance,
        )

    @mcp.tool(name="armature.bone_remove")
    def armature_bone_remove(
        object_name: _OBJECT_NAME,
        bone: _OBJECT_NAME,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Remove one dependency-free leaf bone."""
        return proxy.armature_bone_remove(
            object_name, bone, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="armature.validate")
    def armature_validate(
        object_name: _OBJECT_NAME,
        include_animation: bool = True,
        max_bound_meshes: Annotated[int, Field(ge=1, le=10000)] = 2000,
        sample_limit: Annotated[int, Field(ge=1, le=2000)] = 50,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Validate bone hierarchy, pose targets, animation paths, and bound meshes."""
        return proxy.armature_validate(
            object_name, include_animation, max_bound_meshes, sample_limit, instance,
        )

    @mcp.tool(name="pose.inspect")
    def pose_inspect(
        object_name: _OBJECT_NAME,
        bone: Annotated[str | None, Field(min_length=1)] = None,
        include_constraints: bool = True,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded pose-bone transforms and constraints."""
        return proxy.pose_inspect(
            object_name, bone, include_constraints, limit, cursor, instance,
        )

    @mcp.tool(name="pose.patch")
    def pose_patch(
        object_name: _OBJECT_NAME,
        bone: _OBJECT_NAME,
        location: _VECTOR3 | None = None,
        rotation_mode: Literal["QUATERNION", "XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX", "AXIS_ANGLE"] | None = None,
        rotation_euler: _VECTOR3 | None = None,
        rotation_quaternion: _VECTOR4 | None = None,
        rotation_axis_angle: _VECTOR4 | None = None,
        scale: _VECTOR3 | None = None,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or patch one pose-bone transform with field-level rollback."""
        return proxy.pose_patch(
            object_name, bone, location, rotation_mode, rotation_euler,
            rotation_quaternion, rotation_axis_angle, scale,
            dry_run, expected_state, instance,
        )

    @mcp.tool(name="pose.constraint_patch")
    def pose_constraint_patch(
        object_name: _OBJECT_NAME,
        operations: Annotated[list[PoseConstraintOperation], Field(min_length=1, max_length=100)],
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or atomically add, patch, or guarded-remove pose constraints."""
        return proxy.pose_constraint_patch(
            object_name, _model_payloads(operations), dry_run,
            confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.inspect")
    def animation_inspect(
        object_name: _OBJECT_NAME,
        action_name: Annotated[str | None, Field(min_length=1)] = None,
        include_keyframes: bool = False,
        max_keys_per_curve: Annotated[int, Field(ge=1, le=2000)] = 200,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect legacy or layered Actions, slots, F-Curves, and bounded keys."""
        return proxy.animation_inspect(
            object_name, action_name, include_keyframes,
            max_keys_per_curve, limit, cursor, instance,
        )

    @mcp.tool(name="animation.action_create")
    def animation_action_create(
        object_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        assign: bool = True,
        replace: bool = False,
        fake_user: bool = False,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or create and optionally assign an exact-name Action."""
        return proxy.animation_action_create(
            object_name, name, assign, replace, fake_user,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.action_assign")
    def animation_action_assign(
        object_name: _OBJECT_NAME,
        action_name: Annotated[str | None, Field(min_length=1)] = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Assign an existing Action, or omit action_name to unassign the active Action."""
        return proxy.animation_action_assign(
            object_name, action_name, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.action_retarget")
    def animation_action_retarget(
        source_object: _OBJECT_NAME,
        source_action: _OBJECT_NAME,
        target_object: _OBJECT_NAME,
        name: _OBJECT_NAME,
        bone_map: Annotated[dict[str, _OBJECT_NAME] | None, Field(max_length=512)] = None,
        assign: bool = True,
        replace: bool = False,
        require_all_bones: bool = True,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Copy an Action's F-curves through explicit or same-name bone mapping. This does not solve rest-pose differences, axis changes, mixed units, or unequal chain lengths; verify target poses before export."""
        return proxy.animation_action_retarget(
            source_object, source_action, target_object, name, bone_map,
            assign, replace, require_all_bones, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.action_remove")
    def animation_action_remove(
        name: _OBJECT_NAME,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Remove one unused Action after active and NLA dependency checks."""
        return proxy.animation_action_remove(
            name, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.keyframe_edit")
    def animation_keyframe_edit(
        object_name: _OBJECT_NAME,
        operations: Annotated[list[KeyframeOperation], Field(min_length=1, max_length=100)],
        expected_action: Annotated[str | None, Field(min_length=1)] = None,
        make_single_user: bool = False,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or atomically edit allowlisted object or pose transform keys."""
        return proxy.animation_keyframe_edit(
            object_name, _model_payloads(operations), expected_action, make_single_user,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.pose_bake")
    def animation_pose_bake(
        object_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        frame_start: Annotated[int, Field(ge=-1048574, le=1048574)],
        frame_end: Annotated[int, Field(ge=-1048574, le=1048574)],
        step: Annotated[int, Field(ge=1, le=1000)] = 1,
        bones: Annotated[list[_OBJECT_NAME] | None, Field(min_length=1, max_length=20000)] = None,
        visual_keying: bool = True,
        max_bones: Annotated[int, Field(ge=1, le=20000)] = 512,
        max_samples: Annotated[int, Field(ge=1, le=500000)] = 100000,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Bake evaluated armature pose transforms into a new Action."""
        return proxy.animation_pose_bake(
            object_name, name, frame_start, frame_end, step, bones,
            visual_keying, max_bones, max_samples, dry_run, expected_state, instance,
        )

    @mcp.tool(name="animation.validate")
    def animation_validate(
        object_name: _OBJECT_NAME,
        action_name: Annotated[str | None, Field(min_length=1)] = None,
        frame_start: Annotated[float | None, Field(ge=-1048574, le=1048574)] = None,
        frame_end: Annotated[float | None, Field(ge=-1048574, le=1048574)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Validate Action slots, F-Curve targets, duplicate keys, and frame range."""
        return proxy.animation_validate(
            object_name, action_name, frame_start, frame_end, instance,
        )

    @mcp.tool(name="animation.nla_inspect")
    def animation_nla_inspect(
        object_name: _OBJECT_NAME,
        include_strips: bool = True,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded NLA tracks and optional Action-strip details."""
        return proxy.animation_nla_inspect(
            object_name, include_strips, limit, cursor, instance,
        )

    @mcp.tool(name="animation.nla_patch")
    def animation_nla_patch(
        object_name: _OBJECT_NAME,
        operations: Annotated[list[NlaOperation], Field(min_length=1, max_length=100)],
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or atomically add and patch NLA tracks and strips."""
        return proxy.animation_nla_patch(
            object_name, _model_payloads(operations), dry_run, expected_state, instance,
        )

    @mcp.tool(name="animation.nla_remove")
    def animation_nla_remove(
        object_name: _OBJECT_NAME,
        track: _OBJECT_NAME,
        strip: Annotated[str | None, Field(min_length=1)] = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Remove one NLA strip, or omit strip to remove one empty track."""
        return proxy.animation_nla_remove(
            object_name, track, strip, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.driver_inspect")
    def animation_driver_inspect(
        object_name: _OBJECT_NAME,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded driver F-Curves, expressions, variables, and targets."""
        return proxy.animation_driver_inspect(object_name, limit, cursor, instance)

    @mcp.tool(name="animation.driver_create")
    def animation_driver_create(
        object_name: _OBJECT_NAME,
        property: Literal["location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale"],
        index: Annotated[int, Field(ge=0, le=3)],
        source_object: _OBJECT_NAME,
        source_transform: Literal["LOC_X", "LOC_Y", "LOC_Z", "ROT_X", "ROT_Y", "ROT_Z", "SCALE_X", "SCALE_Y", "SCALE_Z"],
        target: Literal["object", "pose_bone"] = "object",
        bone: Annotated[str | None, Field(min_length=1)] = None,
        source_bone: Annotated[str | None, Field(min_length=1)] = None,
        source_space: Literal["WORLD_SPACE", "TRANSFORM_SPACE", "LOCAL_SPACE"] = "WORLD_SPACE",
        multiplier: float = 1.0,
        offset: float = 0.0,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or create a generated linear transform driver without arbitrary input expressions."""
        return proxy.animation_driver_create(
            object_name, property, index, source_object, source_transform,
            target, bone, source_bone, source_space, multiplier, offset,
            dry_run, expected_state, instance,
        )

    @mcp.tool(name="animation.driver_remove")
    def animation_driver_remove(
        object_name: _OBJECT_NAME,
        property: Literal["location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale"],
        index: Annotated[int, Field(ge=0, le=3)],
        target: Literal["object", "pose_bone"] = "object",
        bone: Annotated[str | None, Field(min_length=1)] = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Remove one exact allowlisted object or pose transform driver."""
        return proxy.animation_driver_remove(
            object_name, property, index, target, bone,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.shape_key_inspect")
    def animation_shape_key_inspect(
        object_name: _OBJECT_NAME,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded mesh shape-key settings and dependencies."""
        return proxy.animation_shape_key_inspect(object_name, limit, cursor, instance)

    @mcp.tool(name="animation.shape_key_create")
    def animation_shape_key_create(
        object_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        from_mix: bool = False,
        relative_key: Annotated[str | None, Field(min_length=1)] = None,
        vertex_group: Annotated[str | None, Field(min_length=1)] = None,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or create an exact-name mesh shape key."""
        return proxy.animation_shape_key_create(
            object_name, name, from_mix, relative_key,
            vertex_group, dry_run, expected_state, instance,
        )

    @mcp.tool(name="animation.shape_key_patch")
    def animation_shape_key_patch(
        object_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        value: Annotated[float | None, Field(ge=-10, le=10)] = None,
        slider_min: Annotated[float | None, Field(ge=-10, le=10)] = None,
        slider_max: Annotated[float | None, Field(ge=-10, le=10)] = None,
        mute: bool | None = None,
        interpolation: Literal["KEY_LINEAR", "KEY_CARDINAL", "KEY_CATMULL_ROM", "KEY_BSPLINE"] | None = None,
        relative_key: Annotated[str | None, Field(min_length=1)] = None,
        vertex_group: Annotated[str | None, Field(min_length=1)] = None,
        clear_vertex_group: bool = False,
        positions: Annotated[list[ShapeKeyPosition] | None, Field(max_length=250000)] = None,
        max_points: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or patch shape-key settings and explicit point coordinates."""
        return proxy.animation_shape_key_patch(
            object_name, name, value, slider_min, slider_max, mute,
            interpolation, relative_key, vertex_group, clear_vertex_group,
            _model_payloads(positions), max_points, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="animation.shape_key_remove")
    def animation_shape_key_remove(
        object_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        max_points: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Remove one bounded shape key after dependency checks."""
        return proxy.animation_shape_key_remove(
            object_name, name, max_points, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="skin.inspect")
    def skin_inspect(
        object_name: _OBJECT_NAME,
        armature_name: Annotated[str | None, Field(min_length=1)] = None,
        max_vertices: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded vertex-group weights and Armature binding."""
        return proxy.skin_inspect(
            object_name, armature_name, max_vertices, limit, cursor, instance,
        )

    @mcp.tool(name="skin.bind")
    def skin_bind(
        object_name: _OBJECT_NAME,
        armature_name: _OBJECT_NAME,
        modifier_name: _OBJECT_NAME = "Armature",
        preserve_volume: bool = False,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or add an exact-name Armature modifier with context restoration."""
        return proxy.skin_bind(
            object_name, armature_name, modifier_name,
            preserve_volume, dry_run, expected_state, instance,
        )

    @mcp.tool(name="skin.unbind")
    def skin_unbind(
        object_name: _OBJECT_NAME,
        armature_name: Annotated[str | None, Field(min_length=1)] = None,
        modifier_name: Annotated[str | None, Field(min_length=1)] = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Remove one exact Armature modifier while preserving vertex groups."""
        return proxy.skin_unbind(
            object_name, armature_name, modifier_name,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="skin.auto_weights")
    def skin_auto_weights(
        object_name: _OBJECT_NAME,
        armature_name: _OBJECT_NAME,
        max_vertices: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        max_weight_entries: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        max_candidate_pairs: Annotated[int, Field(ge=1, le=100000000)] = 5000000,
        require_all_weighted: bool = True,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or calculate Blender automatic bone weights with exact rollback."""
        return proxy.skin_auto_weights(
            object_name, armature_name, max_vertices, max_weight_entries,
            max_candidate_pairs, require_all_weighted, dry_run,
            confirm, expected_state, instance,
        )

    @mcp.tool(name="skin.weights_patch")
    def skin_weights_patch(
        object_name: _OBJECT_NAME,
        group_name: _OBJECT_NAME,
        operations: Annotated[list[SkinWeightOperation], Field(min_length=1, max_length=100)],
        create_group: bool = False,
        expected_vertex_count: Annotated[int | None, Field(ge=0, le=5000000)] = None,
        max_vertices: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or patch explicit-index vertex weights with exact rollback."""
        return proxy.skin_weights_patch(
            object_name, group_name, _model_payloads(operations), create_group,
            expected_vertex_count, max_vertices, dry_run,
            confirm, expected_state, instance,
        )

    @mcp.tool(name="skin.group_remove")
    def skin_group_remove(
        object_name: _OBJECT_NAME,
        group_name: _OBJECT_NAME,
        max_vertices: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Remove one empty vertex group after a bounded membership scan."""
        return proxy.skin_group_remove(
            object_name, group_name, max_vertices,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="skin.weights_normalize")
    def skin_weights_normalize(
        object_name: _OBJECT_NAME,
        armature_name: Annotated[str | None, Field(min_length=1)] = None,
        max_influences: Annotated[int, Field(ge=1, le=64)] = 4,
        min_weight: Annotated[float, Field(ge=0, le=1)] = 0.0001,
        require_all_weighted: bool = True,
        max_vertices: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        max_weight_entries: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Normalize deform weights and cap influences with exact rollback."""
        return proxy.skin_weights_normalize(
            object_name, armature_name, max_influences, min_weight,
            require_all_weighted, max_vertices, max_weight_entries,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="skin.weights_limit")
    def skin_weights_limit(object_name: _OBJECT_NAME,
                           armature_name: _OBJECT_NAME | None = None,
                           max_influences: Annotated[int, Field(ge=1, le=64)] = 4,
                           prune_threshold: Annotated[float, Field(ge=0, le=1)] = 0.0001,
                           require_all_weighted: bool = True,
                           max_vertices: Annotated[int, Field(ge=1, le=5000000)] = 250000,
                           max_weight_entries: Annotated[int, Field(ge=1, le=5000000)] = 250000,
                           dry_run: bool = True, confirm: Literal[True] | None = None,
                           expected_state: Annotated[str | None, Field(min_length=1)] = None,
                           instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Dry-run-first prune, cap and renormalize deform-bone influences using the existing exact-rollback normalization path."""
        params: dict[str, Any] = {"object_name": object_name, "max_influences": max_influences,
                                  "prune_threshold": prune_threshold,
                                  "require_all_weighted": require_all_weighted,
                                  "max_vertices": max_vertices, "max_weight_entries": max_weight_entries,
                                  "dry_run": dry_run}
        for key, value in (("armature_name", armature_name), ("confirm", confirm),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("skin.weights_limit", params, instance)

    @mcp.tool(name="skin.weights_transfer_by_index")
    def skin_weights_transfer_by_index(source_name: _OBJECT_NAME, object_name: _OBJECT_NAME,
                                        group_names: Annotated[list[_OBJECT_NAME] | None, Field(min_length=1)] = None,
                                        max_vertices: Annotated[int, Field(ge=1, le=5000000)] = 250000,
                                        max_weight_entries: Annotated[int, Field(ge=1, le=5000000)] = 500000,
                                        dry_run: bool = True, confirm: Literal[True] | None = None,
                                        expected_state: Annotated[str | None, Field(min_length=1)] = None,
                                        instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Copy chosen template vertex groups by index after proving identical mesh connectivity; dry-run-first with rollback."""
        params: dict[str, Any] = {"source_name": source_name, "object_name": object_name,
                                  "max_vertices": max_vertices, "max_weight_entries": max_weight_entries,
                                  "dry_run": dry_run}
        for key, value in (("group_names", group_names), ("confirm", confirm),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("skin.weights_transfer_by_index", params, instance)

    @mcp.tool(name="skin.validate")
    def skin_validate(
        object_name: _OBJECT_NAME,
        armature_name: Annotated[str | None, Field(min_length=1)] = None,
        max_vertices: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        normalization_tolerance: Annotated[float, Field(ge=0, le=1)] = 0.0001,
        sample_limit: Annotated[int, Field(ge=1, le=2000)] = 50,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Validate Armature targeting, group coverage, and weight normalization."""
        return proxy.skin_validate(
            object_name, armature_name, max_vertices,
            normalization_tolerance, sample_limit, instance,
        )

    @mcp.tool(name="skin.audit")
    def skin_audit(
        object_name: _OBJECT_NAME,
        armature_name: _OBJECT_NAME | None = None,
        bone_names: Annotated[list[_OBJECT_NAME] | None,
                              Field(min_length=1, max_length=128,
                                    json_schema_extra={"uniqueItems": True})] = None,
        side_axis: Literal["X", "Y", "Z"] = "X",
        midline_offset: Annotated[float, Field(ge=-1e12, le=1e12)] = 0,
        side_tolerance: Annotated[float, Field(ge=0, le=1e12)] = 0.0001,
        min_weight: Annotated[float, Field(ge=0, le=1)] = 0.01,
        dominance_threshold: Annotated[float, Field(ge=0, le=1)] = 0.5,
        radius_percentile: Annotated[float, Field(ge=0, le=1)] = 0.95,
        far_multiplier: Annotated[float, Field(ge=1, le=1e6)] = 2,
        samples: Annotated[list[dict[str, Any]] | None,
                           Field(max_length=12, description="Each sample is {name,bones:[{bone,axis,degrees}]} or {name,action_name,frame}.")] = None,
        max_vertices: Annotated[int, Field(ge=1, le=500000)] = 100000,
        max_edges: Annotated[int, Field(ge=1, le=1500000)] = 300000,
        max_triangles: Annotated[int, Field(ge=1, le=100000)] = 20000,
        max_pair_checks: Annotated[int, Field(ge=1, le=2000000)] = 200000,
        thresholds: Annotated[dict[str, Any] | None,
                              Field(description="Optional advisory thresholds for bleed, spill, stretch, compression and added intersections.")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Read-only advisory skin audit: bounded per-bone opposite-side weight bleed and distance spill, plus evaluated edge-stretch and added self-intersection evidence for explicit pose or Action/frame samples. Threshold flags are not an acceptance gate; incomplete scans report unknown counts. Sampling restores frame, Action and pose state, but cannot undo arbitrary frame-handler side effects."""
        params: dict[str, Any] = {
            "object_name": object_name, "side_axis": side_axis,
            "midline_offset": midline_offset, "side_tolerance": side_tolerance,
            "min_weight": min_weight, "dominance_threshold": dominance_threshold,
            "radius_percentile": radius_percentile, "far_multiplier": far_multiplier,
            "max_vertices": max_vertices, "max_edges": max_edges,
            "max_triangles": max_triangles, "max_pair_checks": max_pair_checks,
        }
        for key, value in (("armature_name", armature_name), ("bone_names", bone_names),
                           ("samples", samples), ("thresholds", thresholds)):
            if value is not None:
                params[key] = value
        return proxy.call_named("skin.audit", params, instance)

    @mcp.tool(name="mesh.inspect")
    def mesh_inspect(
        object_name: _OBJECT_NAME,
        include_topology: bool = False,
        max_elements: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded mesh data and optional budgeted topology diagnostics."""
        return proxy.mesh_inspect(
            object_name, include_topology, max_elements, limit, cursor, instance
        )

    @mcp.tool(name="mesh.measure")
    def mesh_measure(
        object_name: _OBJECT_NAME,
        queries: Annotated[list[MeshMeasureQuery], Field(min_length=1, max_length=100)],
        max_elements: Annotated[int, Field(ge=1, le=5000000)] = 500000,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Measure bounded local/world mesh dimensions and semantic regions."""
        return proxy.mesh_measure(
            object_name, _model_payloads(queries), max_elements, instance
        )

    @mcp.tool(name="mesh.edit")
    def mesh_edit(
        object_name: _OBJECT_NAME,
        operations: Annotated[list[NamedOperation], Field(min_length=1, max_length=100)],
        max_elements: Annotated[int, Field(ge=1, le=5000000)] = 500000,
        dry_run: bool = True,
        confirm: Annotated[Literal[True] | None, Field(description="Required for topology-changing apply operations.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        fragment_small_ratio: Annotated[float, Field(gt=0, le=1)] = 0.01,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Apply bounded atomic BMesh edits, including spin/screw, topology construction, and workplanes."""
        return proxy.mesh_edit(
            object_name, _model_payloads(operations), max_elements, dry_run,
            confirm, expected_state, instance,
            fragment_small_ratio=fragment_small_ratio,
        )

    @mcp.tool(name="mesh.join")
    def mesh_join(
        target_object: _OBJECT_NAME | None = None,
        source_objects: Annotated[
            list[_OBJECT_NAME] | None,
            Field(min_length=1, max_length=200, json_schema_extra={"uniqueItems": True}),
        ] = None,
        object_names: Annotated[
            list[_OBJECT_NAME] | None,
            Field(min_length=2, max_length=128, json_schema_extra={"uniqueItems": True}),
        ] = None,
        name: _OBJECT_NAME | None = None,
        keep_sources: bool = False,
        max_elements: Annotated[int, Field(ge=1, le=5000000)] = 500000,
        dry_run: bool = True,
        confirm: Annotated[Literal[True] | None, Field(description="Required to apply the legacy target/source join; optional and ignored for object_names+name.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        fragment_small_ratio: Annotated[float, Field(gt=0, le=1)] = 0.01,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or apply a mesh join. object_names+name creates a new world-space mesh without selection or visibility and preserves sources; apply with dry_run:false, optional confirm:true is ignored. Legacy default parameters are ignored in named mode. target_object+source_objects is the earlier route and requires a visible selectable target and confirm:true to apply."""
        if object_names is not None or name is not None:
            params: dict[str, Any] = {"object_names": object_names, "name": name, "dry_run": dry_run}
            if expected_state is not None:
                params["expected_state"] = expected_state
            if keep_sources or max_elements != 500000 or fragment_small_ratio != 0.01:
                raise ValueError("Legacy mesh.join options cannot be combined with object_names+name")
            return proxy.call_named("mesh.join", params, instance)
        return proxy.mesh_join(
            target_object, source_objects, keep_sources, max_elements, dry_run,
            confirm, expected_state, instance,
            fragment_small_ratio=fragment_small_ratio,
        )

    @mcp.tool(name="mesh.separate")
    def mesh_separate(
        object_name: _OBJECT_NAME,
        mode: Literal["LOOSE", "MATERIAL", "SELECTION"],
        selection: MeshSeparateSelection | None = None,
        name_prefix: Annotated[str | None, Field(min_length=1)] = None,
        max_parts: Annotated[int, Field(ge=2, le=2000)] = 100,
        max_elements: Annotated[int, Field(ge=1, le=5000000)] = 500000,
        dry_run: bool = True,
        confirm: Annotated[Literal[True] | None, Field(description="Required to apply separation.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        fragment_small_ratio: Annotated[float, Field(gt=0, le=1)] = 0.01,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or apply recoverable loose, material, or semantic separation."""
        return proxy.mesh_separate(
            object_name,
            mode,
            selection.model_dump(exclude_none=True) if selection is not None else None,
            name_prefix,
            max_parts,
            max_elements,
            dry_run,
            confirm,
            expected_state,
            instance,
            fragment_small_ratio=fragment_small_ratio,
        )

    @mcp.tool(name="curve.inspect")
    def curve_inspect(
        name: Annotated[str | None, Field(min_length=1)] = None,
        object_name: Annotated[str | None, Field(min_length=1)] = None,
        include_points: bool = False,
        max_points: Annotated[int, Field(ge=1, le=100000)] = 10000,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded Curve objects and optional paginated point data."""
        return proxy.curve_inspect(
            name, object_name, include_points, max_points, limit, cursor, instance
        )

    @mcp.tool(name="curve.create")
    def curve_create(
        spline: CurveSpline,
        name: Annotated[str | None, Field(min_length=1)] = None,
        collection: Annotated[str | None, Field(min_length=1)] = None,
        location: _VECTOR3 | None = None,
        rotation: _VECTOR3 | None = None,
        scale: _VECTOR3 | None = None,
        dimensions: _CURVE_DIMENSIONS | None = None,
        resolution_u: Annotated[int | None, Field(ge=1, le=1024)] = None,
        render_resolution_u: Annotated[int | None, Field(ge=0, le=1024)] = None,
        fill_mode: _CURVE_FILL_MODE | None = None,
        bevel_depth: Annotated[float | None, Field(ge=0, le=1000000)] = None,
        bevel_resolution: Annotated[int | None, Field(ge=0, le=32)] = None,
        twist_smooth: Annotated[int | None, Field(ge=0, le=100)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create one Curve object with a typed initial spline."""
        return proxy.curve_create(
            _model_payload(spline), name, collection, location, rotation, scale, dimensions,
            resolution_u, render_resolution_u, fill_mode, bevel_depth,
            bevel_resolution, twist_smooth, expected_state, instance,
        )

    @mcp.tool(name="curve.patch")
    def curve_patch(
        object_name: _OBJECT_NAME,
        dimensions: _CURVE_DIMENSIONS | None = None,
        resolution_u: Annotated[int | None, Field(ge=1, le=1024)] = None,
        render_resolution_u: Annotated[int | None, Field(ge=0, le=1024)] = None,
        fill_mode: _CURVE_FILL_MODE | None = None,
        bevel_depth: Annotated[float | None, Field(ge=0, le=1000000)] = None,
        bevel_resolution: Annotated[int | None, Field(ge=0, le=32)] = None,
        twist_smooth: Annotated[int | None, Field(ge=0, le=100)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch allowlisted Curve datablock settings with rollback."""
        return proxy.curve_patch(
            object_name, dimensions, resolution_u, render_resolution_u,
            fill_mode, bevel_depth, bevel_resolution, twist_smooth,
            expected_state, instance,
        )

    @mcp.tool(name="curve.spline_add")
    def curve_spline_add(
        object_name: _OBJECT_NAME,
        spline: CurveSpline,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Add one bounded typed spline to a single-user Curve."""
        return proxy.curve_spline_add(object_name, _model_payload(spline), expected_state, instance)

    @mcp.tool(name="curve.spline_patch")
    def curve_spline_patch(
        object_name: _OBJECT_NAME,
        spline_index: Annotated[int, Field(ge=0, le=100000)],
        spline: CurveSpline,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Replace same-type, same-size spline point data with rollback."""
        return proxy.curve_spline_patch(
            object_name, spline_index, _model_payload(spline), expected_state, instance
        )

    @mcp.tool(name="curve.spline_remove")
    def curve_spline_remove(
        object_name: _OBJECT_NAME,
        spline_index: Annotated[int, Field(ge=0, le=100000)],
        dry_run: bool = True,
        confirm: Annotated[Literal[True] | None, Field(description="Must be true when dry_run is false.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or confirm removal of the final Curve spline."""
        return proxy.curve_spline_remove(
            object_name, spline_index, dry_run, confirm, expected_state, instance
        )

    @mcp.tool(name="geometry_nodes.inspect")
    def geometry_nodes_inspect(
        object_name: Annotated[str | None, Field(min_length=1)] = None,
        modifier_name: Annotated[str | None, Field(min_length=1)] = None,
        include_graph: bool = False,
        max_nodes: Annotated[int, Field(ge=1, le=10000)] = 2000,
        max_links: Annotated[int, Field(ge=1, le=50000)] = 10000,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded Geometry Nodes modifiers, interfaces, nodes, links, and inputs."""
        return proxy.geometry_nodes_inspect(
            object_name, modifier_name, include_graph, max_nodes,
            max_links, limit, cursor, instance,
        )

    @mcp.tool(name="geometry_nodes.create")
    def geometry_nodes_create(
        object_name: _OBJECT_NAME,
        modifier_name: Annotated[str | None, Field(min_length=1)] = None,
        node_group_name: Annotated[str | None, Field(min_length=1)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create an attached geometry pass-through node group and modifier."""
        return proxy.geometry_nodes_create(
            object_name, modifier_name, node_group_name, expected_state, instance
        )

    @mcp.tool(name="geometry_nodes.patch")
    def geometry_nodes_patch(
        object_name: _OBJECT_NAME,
        operations: Annotated[list[NamedOperation], Field(min_length=1, max_length=500)],
        modifier_name: Annotated[str | None, Field(min_length=1)] = None,
        expected_group: Annotated[str | None, Field(min_length=1)] = None,
        expected_node_count: Annotated[int | None, Field(ge=0, le=10000)] = None,
        expected_link_count: Annotated[int | None, Field(ge=0, le=50000)] = None,
        make_single_user: bool = False,
        dry_run: bool = True,
        confirm: Annotated[Literal[True] | None, Field(description="Required when removing nodes, links, or sockets.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or atomically apply a bounded Geometry Nodes graph patch."""
        return proxy.geometry_nodes_patch(
            object_name, _model_payloads(operations), modifier_name, expected_group,
            expected_node_count, expected_link_count, make_single_user,
            dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="geometry_nodes.modifier_inputs")
    def geometry_nodes_modifier_inputs(
        object_name: _OBJECT_NAME,
        values: Annotated[dict[str, Any], Field(min_length=1, max_length=1000)],
        modifier_name: Annotated[str | None, Field(min_length=1)] = None,
        expected_group: Annotated[str | None, Field(min_length=1)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch Geometry Nodes modifier inputs by stable interface identifier."""
        return proxy.geometry_nodes_modifier_inputs(
            object_name, values, modifier_name, expected_group, expected_state, instance
        )

    @mcp.tool(name="uv.inspect")
    def uv_inspect(
        object_name: _OBJECT_NAME,
        layer_name: Annotated[str | None, Field(min_length=1)] = None,
        include_loops: bool = False,
        include_islands: bool = False,
        max_uv_items: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect bounded UV layers, loops, bounds, pins, and island continuity."""
        return proxy.uv_inspect(
            object_name, layer_name, include_loops, include_islands,
            max_uv_items, limit, cursor, instance,
        )

    @mcp.tool(name="uv.layer_create")
    def uv_layer_create(
        object_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        copy_from: Annotated[str | None, Field(min_length=1)] = None,
        set_active: bool = True,
        max_uv_items: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create a bounded UV layer, optionally copying another layer."""
        return proxy.uv_layer_create(
            object_name, name, copy_from, set_active, max_uv_items,
            expected_state, instance,
        )

    @mcp.tool(name="uv.layer_rename")
    def uv_layer_rename(
        object_name: _OBJECT_NAME,
        layer_name: _OBJECT_NAME,
        new_name: _OBJECT_NAME,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Rename one UV layer with collision checks and rollback."""
        return proxy.uv_layer_rename(
            object_name, layer_name, new_name, expected_state, instance
        )

    @mcp.tool(name="uv.layer_set_active")
    def uv_layer_set_active(
        object_name: _OBJECT_NAME,
        layer_name: _OBJECT_NAME,
        set_render: bool = False,
        set_clone: bool = False,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Set editing, render, and clone UV layer roles."""
        return proxy.uv_layer_set_active(
            object_name, layer_name, set_render, set_clone, expected_state, instance
        )

    @mcp.tool(name="uv.layer_remove")
    def uv_layer_remove(
        object_name: _OBJECT_NAME,
        layer_name: _OBJECT_NAME,
        max_uv_items: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        confirm: Annotated[Literal[True] | None, Field(description="Must be true when dry_run is false.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or explicitly confirm destructive UV-layer removal."""
        return proxy.uv_layer_remove(
            object_name, layer_name, max_uv_items, dry_run,
            confirm, expected_state, instance,
        )

    @mcp.tool(name="uv.project")
    def uv_project(
        object_name: _OBJECT_NAME,
        layer_name: _OBJECT_NAME,
        mode: _UV_PROJECTION_MODE,
        create_layer: bool = False,
        plane: _UV_PLANE = "XY",
        face_selection: UVFaceSelection | None = None,
        normalize: bool = False,
        scale: _VECTOR2 | None = None,
        offset: _VECTOR2 | None = None,
        max_uv_items: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or apply deterministic bounded UV projection."""
        return proxy.uv_project(
            object_name, layer_name, mode, create_layer, plane, _model_payload(face_selection),
            normalize, scale, offset, max_uv_items, dry_run, expected_state, instance,
        )

    @mcp.tool(name="uv.transform")
    def uv_transform(
        object_name: _OBJECT_NAME,
        layer_name: _OBJECT_NAME,
        face_selection: UVFaceSelection | None = None,
        translation: _VECTOR2 | None = None,
        rotation: Annotated[float | None, Field(ge=-6283.185307179586, le=6283.185307179586)] = None,
        scale: _VECTOR2 | None = None,
        pivot: Literal["ORIGIN", "CENTER", "MEDIAN"] = "MEDIAN",
        max_uv_items: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or apply a bounded UV translation, rotation, and scale."""
        return proxy.uv_transform(
            object_name, layer_name, _model_payload(face_selection), translation, rotation,
            scale, pivot, max_uv_items, dry_run, expected_state, instance,
        )

    @mcp.tool(name="uv.unwrap")
    def uv_unwrap(
        object_name: _OBJECT_NAME,
        layer_name: _OBJECT_NAME,
        create_layer: bool = False,
        face_selection: UVFaceSelection | None = None,
        method: _UV_UNWRAP_METHOD = "ANGLE_BASED",
        margin: Annotated[float, Field(ge=0, le=1)] = 0.001,
        angle_limit: Annotated[float | None, Field(ge=0, le=1.5707963267948966)] = None,
        island_margin: Annotated[float | None, Field(ge=0, le=1)] = None,
        area_weight: Annotated[float | None, Field(ge=0, le=1)] = None,
        fill_holes: bool = True,
        correct_aspect: bool = True,
        max_uv_items: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Dry-run-first guarded Blender unwrap with UV and object-context rollback."""
        return proxy.uv_unwrap(
            object_name, layer_name, create_layer, _model_payload(face_selection), method,
            margin, fill_holes, correct_aspect, max_uv_items,
            dry_run, expected_state, instance,
            angle_limit=angle_limit, island_margin=island_margin, area_weight=area_weight,
        )

    @mcp.tool(name="uv.pack")
    def uv_pack(
        object_name: _OBJECT_NAME,
        layer_name: _OBJECT_NAME,
        face_selection: UVFaceSelection | None = None,
        rotate: bool = True,
        scale_to_fit: bool = True,
        average_island_scale: bool = False,
        margin: Annotated[float, Field(ge=0, le=1)] = 0.001,
        max_uv_items: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Dry-run-first guarded island packing with full selection and mode restoration."""
        params: dict[str, Any] = {"object_name": object_name, "layer_name": layer_name,
                                  "rotate": rotate, "scale_to_fit": scale_to_fit,
                                  "average_island_scale": average_island_scale,
                                  "margin": margin, "max_uv_items": max_uv_items, "dry_run": dry_run}
        if face_selection is not None:
            params["face_selection"] = _model_payload(face_selection)
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("uv.pack", params, instance)

    @mcp.tool(name="modifier.inspect")
    def modifier_inspect(
        object_name: _OBJECT_NAME,
        modifier_name: Annotated[str | None, Field(min_length=1)] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect the ordered modifier stack and supported settings."""
        return proxy.modifier_inspect(object_name, modifier_name, limit, cursor, instance)

    @mcp.tool(name="modifier.add")
    def modifier_add(
        object_name: _OBJECT_NAME,
        type: _MODIFIER_TYPE,
        name: Annotated[str | None, Field(min_length=1)] = None,
        settings: Annotated[dict[str, Any] | None, Field(min_length=1)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Add a Blender modifier with optional typed settings."""
        return proxy.modifier_add(
            object_name, type, name, settings, expected_state, instance
        )

    @mcp.tool(name="modifier.patch")
    def modifier_patch(
        object_name: _OBJECT_NAME,
        modifier_name: _OBJECT_NAME,
        settings: Annotated[dict[str, Any] | None, Field(min_length=1)] = None,
        show_viewport: bool | None = None,
        show_render: bool | None = None,
        show_in_editmode: bool | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch modifier fields with rollback metadata."""
        return proxy.modifier_patch(
            object_name, modifier_name, settings, show_viewport,
            show_render, show_in_editmode, expected_state, instance,
        )

    @mcp.tool(name="modifier.reorder")
    def modifier_reorder(
        object_name: _OBJECT_NAME,
        modifier_name: _OBJECT_NAME,
        index: Annotated[int, Field(ge=0)],
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or apply a modifier stack reorder."""
        return proxy.modifier_reorder(
            object_name, modifier_name, index, dry_run, expected_state, instance
        )

    @mcp.tool(name="modifier.remove")
    def modifier_remove(
        object_name: _OBJECT_NAME,
        modifier_name: _OBJECT_NAME,
        dry_run: bool = True,
        confirm: Annotated[Literal[True] | None, Field(description="Must be true when dry_run is false.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or explicitly confirm modifier removal."""
        return proxy.modifier_remove(
            object_name, modifier_name, dry_run, confirm, expected_state, instance
        )

    @mcp.tool(name="modifier.apply")
    def modifier_apply(
        object_name: _OBJECT_NAME,
        modifier_name: _OBJECT_NAME,
        dry_run: bool = True,
        confirm: Annotated[Literal[True] | None, Field(description="Must be true when dry_run is false.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        fragment_small_ratio: Annotated[float, Field(gt=0, le=1)] = 0.01,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or explicitly confirm destructive modifier application."""
        return proxy.modifier_apply(
            object_name, modifier_name, dry_run, confirm, expected_state, instance,
            fragment_small_ratio=fragment_small_ratio,
        )

    @mcp.tool(name="object.inspect")
    def object_inspect(
        name: Annotated[str | None, Field(min_length=1)] = None,
        name_regex: Annotated[str | None, Field(min_length=1)] = None,
        type: Annotated[str | None, Field(min_length=1)] = None,
        collection: Annotated[str | None, Field(min_length=1)] = None,
        visible: bool | None = None,
        selected: bool | None = None,
        parent: Annotated[str | None, Field(min_length=1)] = None,
        material: Annotated[str | None, Field(min_length=1)] = None,
        custom_property: Annotated[str | None, Field(min_length=1)] = None,
        custom_value: _CUSTOM_VALUE | None = None,
        object_name: Annotated[str | None, Field(min_length=1)] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """List a bounded filtered object page and optionally detail one object."""
        return proxy.object_inspect(
            name, name_regex, type, collection, visible, selected,
            parent, material, custom_property, custom_value,
            object_name, limit, cursor, instance,
        )

    @mcp.tool(name="object.create")
    def object_create(
        type: _OBJECT_CREATE_TYPE,
        name: Annotated[str | None, Field(min_length=1)] = None,
        location: _VECTOR3 | None = None,
        rotation: _VECTOR3 | None = None,
        scale: _VECTOR3 | None = None,
        collection: Annotated[str | None, Field(min_length=1)] = None,
        light_type: _LIGHT_TYPE | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create one allowlisted primitive or typed object."""
        return proxy.object_create(
            type, name, location, rotation, scale, collection, light_type, expected_state, instance
        )

    @mcp.tool(name="architecture.wall_build")
    def architecture_wall_build(
        wall_lines: Annotated[list[ArchitectureWallLine], Field(min_length=1, max_length=256)],
        height: Annotated[float, Field(gt=0, le=10_000)],
        thickness: Annotated[float, Field(gt=0, le=1_000)],
        name: Annotated[str, Field(min_length=1, max_length=63)] = "Walls",
        base_z: Annotated[float, Field(ge=-1_000_000, le=1_000_000)] = 0,
        collection: Annotated[str | None, Field(min_length=1, max_length=63)] = None,
        material: Annotated[str | None, Field(min_length=1, max_length=63)] = None,
        max_elements: Annotated[int, Field(ge=1, le=5_000_000)] = 250_000,
        dry_run: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or create up to 256 editable world-XY wall meshes with 512 rectangular openings. Junctions remain separate objects without mitering or Boolean union."""
        return proxy.architecture_wall_build(
            _model_payloads(wall_lines) or [],
            height,
            thickness,
            name,
            base_z,
            collection,
            material,
            max_elements,
            dry_run,
            expected_state,
            instance,
        )

    @mcp.tool(name="object.duplicate")
    def object_duplicate(
        object_name: _OBJECT_NAME,
        name: Annotated[str | None, Field(min_length=1)] = None,
        linked: bool = False,
        collection: Annotated[str | None, Field(min_length=1)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Duplicate one object, copying its data unless linked is true."""
        return proxy.object_duplicate(
            object_name, name, linked, collection, expected_state, instance
        )

    @mcp.tool(name="object.convert_evaluated")
    def object_convert_evaluated(
        object_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        keep_original: bool = True,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        fragment_small_ratio: Annotated[float, Field(gt=0, le=1)] = 0.01,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Copy the evaluated mesh into a new object; optionally remove its source."""
        params: dict[str, Any] = {"object_name": object_name, "name": name, "keep_original": keep_original, "dry_run": dry_run}
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        params["fragment_small_ratio"] = fragment_small_ratio
        return proxy.call_named("object.convert_evaluated", params, instance)

    @mcp.tool(name="object.rename")
    def object_rename(
        object_name: _OBJECT_NAME,
        name: _OBJECT_NAME,
        allow_suffix: bool = False,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Rename one object, rejecting collisions unless suffixing is allowed."""
        return proxy.object_rename(
            object_name, name, allow_suffix, expected_state, instance
        )

    @mcp.tool(name="object.set_visibility")
    def object_set_visibility(
        objects: _OBJECT_NAMES,
        hide_viewport: bool | None = None,
        hide_render: bool | None = None,
        hide_select: bool | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch viewport, render, or selection visibility for bounded objects."""
        return proxy.object_set_visibility(
            objects, hide_viewport, hide_render, hide_select, expected_state, instance
        )

    @mcp.tool(name="object.set_selection")
    def object_set_selection(
        objects: _OBJECT_NAMES,
        selected: bool,
        exclusive: bool = False,
        active: Annotated[str | None, Field(min_length=1)] = None,
        set_active: Annotated[bool, Field(description="Set true with active omitted to clear the active object.")] = False,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Select/deselect objects and optionally replace selection or active object."""
        return proxy.object_set_selection(
            objects, selected, exclusive, active, set_active, expected_state, instance
        )

    @mcp.tool(name="object.parent")
    def object_parent(
        object_name: _OBJECT_NAME,
        parent: _OBJECT_NAME | None,
        keep_transform: bool = True,
        dry_run: Annotated[bool, Field(description="Defaults true; pass false to apply the hierarchy change.")] = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or apply a parent/unparent hierarchy change."""
        return proxy.object_parent(
            object_name, parent, keep_transform, dry_run, expected_state, instance
        )

    @mcp.tool(name="object.mode_set")
    def object_mode_set(mode: Literal["OBJECT", "EDIT", "POSE", "SCULPT", "VERTEX_PAINT", "WEIGHT_PAINT", "TEXTURE_PAINT"], object_name: _OBJECT_NAME | None = None, dry_run: bool = True, expected_state: Annotated[str | None, Field(min_length=1)] = None, instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Dry-run-first mode switch for the already-active object; it does not choose a window or change active-object ownership."""
        return proxy.object_mode_set(mode, object_name, dry_run, expected_state, instance)

    @mcp.tool(name="object.align_to_workplane")
    def object_align_to_workplane(
        object_name: _OBJECT_NAME,
        reference_object: _OBJECT_NAME,
        plane: WorkplanePlane,
        offset: _VECTOR3 = (0.0, 0.0, 0.0),
        axis_map: Annotated[list[_WORKPLANE_AXIS], Field(min_length=3, max_length=3)] = ["U", "V", "N"],
        align_location: bool = True,
        align_rotation: bool = True,
        dry_run: Annotated[bool, Field(description="Defaults true; pass false to apply the object transform.")] = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or place an object on a reference-object workplane without viewport state."""
        return proxy.object_align_to_workplane(
            object_name,
            reference_object,
            plane.model_dump(exclude_none=True),
            offset,
            axis_map,
            align_location,
            align_rotation,
            dry_run,
            expected_state,
            instance,
        )

    @mcp.tool(name="object.transform")
    def object_transform(
        object_name: _OBJECT_NAME,
        location: _VECTOR3 | None = None,
        rotation_euler: _VECTOR3 | None = None,
        scale: _VECTOR3 | None = None,
        space: Literal["local", "world"] = "local",
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Read transforms or patch selected fields in local or world space."""
        return proxy.object_transform(
            object_name, location, rotation_euler, scale, space, expected_state, instance
        )

    @mcp.tool(name="object.delete")
    def object_delete(
        objects: _OBJECT_NAMES,
        dry_run: Annotated[bool, Field(description="Defaults true; returns affected objects without deleting.")] = True,
        confirm: Annotated[Literal[True] | None, Field(description="Must be true when dry_run is false.")] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan deletion or execute it through the dry-run plus confirmation gate."""
        return proxy.object_delete(objects, dry_run, confirm, expected_state, instance)

    @mcp.tool(name="checkpoint.save")
    def checkpoint_save(
        tag: str | None = None,
        instance: _INSTANCE_SELECTOR = None,
        directory: Annotated[str | None, Field(min_length=1)] = None,
        name_template: Annotated[str | None, Field(min_length=1)] = None,
        keep_latest: Annotated[int | None, Field(ge=1, le=10000)] = None,
        preview_only: bool = False,
        prune: bool = False,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        asset: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
    ) -> dict[str, Any]:
        """Save a checkpoint copy. Fixed filenames require confirm:true to overwrite; retention without an index requires an explicit asset identity."""
        return proxy.checkpoint_save(tag, instance, directory, name_template, keep_latest, preview_only, prune, confirm, expected_state, asset)

    @mcp.tool(name="checkpoint.list")
    def checkpoint_list(instance: _INSTANCE_SELECTOR = None, directory: Annotated[str | None, Field(min_length=1)] = None, name_template: Annotated[str | None, Field(min_length=1)] = None, asset: Annotated[str | None, Field(min_length=1, max_length=128)] = None) -> dict[str, Any]:
        """List checkpoints for the current document or an explicit asset identity."""
        return proxy.checkpoint_list(instance, directory, name_template, asset)

    @mcp.tool(name="session.whoami")
    def session_whoami(instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Report the active Blender process, document identity, and bridge health."""
        return proxy.session_whoami(instance)

    @mcp.tool(name="session.acquire_lease")
    def session_acquire_lease(
        scope: Literal["document"] = "document",
        ttl: Annotated[int, Field(ge=5, le=3600)] = 300,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Acquire a document lease when several clients edit one document; single-client work needs no lease."""
        return proxy.session_acquire_lease(scope, ttl, instance)

    @mcp.tool(name="session.renew_lease")
    def session_renew_lease(
        lease_id: Annotated[str, Field(min_length=1)],
        ttl: Annotated[int | None, Field(ge=5, le=3600)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Renew this client's active document lease."""
        return proxy.session_renew_lease(lease_id, ttl, instance)

    @mcp.tool(name="session.release_lease")
    def session_release_lease(
        lease_id: Annotated[str, Field(min_length=1)],
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Release this client's active document lease."""
        return proxy.session_release_lease(lease_id, instance)

    @mcp.tool(name="checkpoint.restore")
    def checkpoint_restore(
        name: _OBJECT_NAME,
        confirm: Literal[True],
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
        directory: Annotated[str | None, Field(min_length=1)] = None,
        name_template: Annotated[str | None, Field(min_length=1)] = None,
        asset: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
    ) -> dict[str, Any]:
        """Open a named checkpoint after explicit confirmation, discarding unsaved state; asset selects fixed-name checkpoint history."""
        return proxy.checkpoint_restore(name, confirm, expected_state, instance, directory, name_template, asset)

    @mcp.tool(name="asset.quality_gate")
    def asset_quality_gate(
        object_name: _OBJECT_NAME,
        part_attribute: _OBJECT_NAME | None = None,
        require_quads: bool = True,
        triangle_budget: Annotated[int | None, Field(ge=1)] = None,
        sliver_min_edge: Annotated[float, Field(ge=0)] = 0,
        sliver_min_angle_deg: Annotated[float, Field(ge=0, le=180)] = 1,
        symmetry_plane_x: float | None = None,
        symmetry_tolerance_m: Annotated[float | None, Field(ge=0)] = None,
        allowed_loop_break_vertices: Annotated[list[Annotated[int, Field(ge=0)]] | None, Field(max_length=256)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Read-only bounded all-quad, loop, sliver, symmetry and self-intersection gate on a base mesh; incomplete scans fail. Pole count is complete; examples are capped at 20."""
        params: dict[str, Any] = {"object_name": object_name, "require_quads": require_quads,
                                  "sliver_min_edge": sliver_min_edge, "sliver_min_angle_deg": sliver_min_angle_deg}
        for key, value in (("part_attribute", part_attribute), ("triangle_budget", triangle_budget),
                           ("symmetry_plane_x", symmetry_plane_x), ("symmetry_tolerance_m", symmetry_tolerance_m),
                           ("allowed_loop_break_vertices", allowed_loop_break_vertices)):
            if value is not None:
                params[key] = value
        return proxy.call_named("asset.quality_gate", params, instance)

    @mcp.tool(name="asset.compare_to_source")
    def asset_compare_to_source(
        low: _OBJECT_NAME,
        high: _OBJECT_NAME,
        views: Annotated[list[Literal["front", "side", "top", "back", "left", "bottom"]] | None, Field(min_length=1, max_length=6)] = None,
        resolution: Annotated[int, Field(ge=32, le=2048)] = 512,
        sample_count: Annotated[int, Field(ge=100, le=100000)] = 10000,
        spherical: bool = False,
        include_radial_grid: bool = False,
        center: _VECTOR3 | None = None,
        radial_theta_steps: Annotated[int, Field(ge=4, le=360)] = 72,
        radial_phi_steps: Annotated[int, Field(ge=3, le=181)] = 37,
        output_dir: _OBJECT_NAME | None = None,
        thresholds: SourceCompareThresholds | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Read-only CPU silhouette IoU, sampled deviation and outer-shell radial comparison against a source mesh; radial grid is omitted by default and can be requested or written as JSON with output_dir. Radial regions use paired absolute delta p90 unless radial_max_mm is explicit."""
        params: dict[str, Any] = {"low": low, "high": high, "resolution": resolution, "sample_count": sample_count,
                                  "spherical": spherical, "include_radial_grid": include_radial_grid,
                                  "radial_theta_steps": radial_theta_steps,
                                  "radial_phi_steps": radial_phi_steps}
        for key, value in (("views", views), ("center", center), ("output_dir", output_dir)):
            if value is not None:
                params[key] = value
        if thresholds is not None:
            params["thresholds"] = _model_payload(thresholds)
        envelope = proxy.call_named("asset.compare_to_source", params, instance)
        data = envelope.get("data")
        artifacts = data.get("artifacts", []) if isinstance(data, dict) else []
        paths = [artifact.get("path") for artifact in artifacts
                 if isinstance(artifact, dict) and artifact.get("kind") != "radial_delta"]
        return _render_result(envelope, paths)

    @mcp.tool(name="bake.from_ai_source")
    def bake_from_ai_source(
        low: _OBJECT_NAME,
        high: _OBJECT_NAME,
        output_dir: _OBJECT_NAME,
        resolution: Annotated[int, Field(ge=16, le=4096)] = 2048,
        part_attribute: _OBJECT_NAME | None = None,
        shell_parts: list[int] | None = None,
        fill_face_attribute: _OBJECT_NAME | None = None,
        fill_parts: Annotated[dict[str, FixedPartChannels] | None, Field(max_length=64)] = None,
        cage_extrusion: Annotated[float, Field(ge=0, le=1000)] = 0.01,
        max_ray_distance: Annotated[float, Field(ge=0, le=1000)] = 0,
        fallback_max_distance: Annotated[float | None, Field(ge=0, le=1000,
            description="Maximum low-to-high nearest fallback distance in scene units; default is cage_extrusion + max_ray_distance, capped at 1000.")] = None,
        highpass_sigma_px: Annotated[float, Field(ge=0, le=256)] = 16,
        normal_convention: Literal["OPENGL", "DIRECTX"] = "OPENGL",
        pack: BakeChannelPack | None = None,
        margin: Annotated[int, Field(ge=0, le=128)] = 16,
        ao_samples: Annotated[int, Field(ge=1, le=1024)] = 64,
        max_uv_overlap_pct: Annotated[float, Field(ge=0, le=100)] = 0.01,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """CPU Cycles AI-high to clean-low bake. Live calls queue a background job; snapshot occurs when it starts, then operation.status reports progress. Ray misses use nearest source-UV fallback bounded by fallback_max_distance (default: cage_extrusion + max_ray_distance). fill_parts assigns fixed linear base_color, roughness, metallic, AO and flat normal by FACE INT id; filled texels, per-part misses and sampled potential low-part occlusion are reported separately."""
        params: dict[str, Any] = {"low": low, "high": high, "output_dir": output_dir, "resolution": resolution,
                                  "cage_extrusion": cage_extrusion, "max_ray_distance": max_ray_distance,
                                  "highpass_sigma_px": highpass_sigma_px, "normal_convention": normal_convention,
                                  "margin": margin, "ao_samples": ao_samples,
                                  "max_uv_overlap_pct": max_uv_overlap_pct}
        for key, value in (("part_attribute", part_attribute), ("shell_parts", shell_parts),
                           ("fill_face_attribute", fill_face_attribute),
                           ("fallback_max_distance", fallback_max_distance),
                           ("pack", pack), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        if fill_parts is not None:
            params["fill_parts"] = {key: _model_payload(value) for key, value in fill_parts.items()}
        return proxy.call_named("bake.from_ai_source", params, instance)

    @mcp.tool(name="export.game_asset")
    def export_game_asset(
        object_names: Annotated[list[_OBJECT_NAME], Field(min_length=1, max_length=128)],
        asset_name: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")],
        output_dir: _OBJECT_NAME,
        texture_paths: dict[str, _OBJECT_NAME] | None = None,
        pivot: Literal["ORIGIN", "BOUNDS_CENTER", "OBJECT_ORIGIN"] = "ORIGIN",
        overwrite: bool = False,
        lod_group: _OBJECT_NAME = "SmallProp",
        collision: Literal["auto_convex", "complex_as_simple", "ucx"] = "complex_as_simple",
        import_yaw: Annotated[float, Field(ge=-360, le=360)] = 0,
        detail_normal: _OBJECT_NAME | None = None,
        detail_tiling: Annotated[float, Field(ge=0.001, le=1000)] = 1,
        dry_run: bool = True,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Dry-run-first triangulated FBX and Unreal sidecar. OBJECT_ORIGIN preserves each object's pivot and assembly placement; ORIGIN and BOUNDS_CENTER use shared pivots. overwrite replaces only this call's exact output files with rollback; unrelated files remain. auto_convex may fill cavities; verify Unreal collision."""
        params: dict[str, Any] = {"object_names": object_names, "asset_name": asset_name, "output_dir": output_dir,
                                  "pivot": pivot, "lod_group": lod_group, "collision": collision,
                                  "import_yaw": import_yaw, "detail_tiling": detail_tiling,
                                  "dry_run": dry_run, "overwrite": overwrite}
        for key, value in (("texture_paths", texture_paths), ("detail_normal", detail_normal), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("export.game_asset", params, instance)

    @mcp.tool(name="uv.audit")
    def uv_audit(
        texture_size: Annotated[int, Field(ge=32, le=16384)],
        object_name: _OBJECT_NAME | None = None,
        object_names: Annotated[list[_OBJECT_NAME] | None, Field(min_length=1, max_length=128,
                     json_schema_extra={"uniqueItems": True})] = None,
        layer_name: _OBJECT_NAME | None = None,
        density_floor_px_per_cm: Annotated[float, Field(ge=0, le=100000)] = 0,
        target_px_per_cm: Annotated[float | None, Field(ge=0, le=100000)] = None,
        max_overlap_pct: Annotated[float | None, Field(ge=0, le=100)] = None,
        raster_size: Annotated[int, Field(ge=64, le=4096)] = 512,
        max_uv_items: Annotated[int, Field(ge=1, le=5000000)] = 250000,
        max_overlap_pairs: Annotated[int, Field(ge=1, le=2000)] = 200,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Audit one UV mesh or a shared multi-object atlas at a stated texture size; report self and cross-object overlap, bounded face-pair evidence and density checks."""
        params: dict[str, Any] = {"texture_size": texture_size,
                                  "density_floor_px_per_cm": density_floor_px_per_cm,
                                  "raster_size": raster_size, "max_uv_items": max_uv_items,
                                  "max_overlap_pairs": max_overlap_pairs}
        for key, value in (("object_name", object_name), ("object_names", object_names),
                           ("layer_name", layer_name), ("target_px_per_cm", target_px_per_cm),
                           ("max_overlap_pct", max_overlap_pct)):
            if value is not None:
                params[key] = value
        return proxy.call_named("uv.audit", params, instance)

    @mcp.tool(name="uv.seams_from_regions")
    def uv_seams_from_regions(
        object_name: _OBJECT_NAME,
        attribute_name: _OBJECT_NAME = "part",
        include_boundary: bool = True,
        dry_run: bool = True,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or mark UV seams at FACE INT region boundaries and optional mesh boundaries; dry_run defaults true and never depends on selection."""
        params = {"object_name": object_name, "attribute_name": attribute_name,
                  "include_boundary": include_boundary, "dry_run": dry_run}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("uv.seams_from_regions", params, instance)

    @mcp.tool(name="uv.cut_strips_by_turn")
    def uv_cut_strips_by_turn(
        object_name: _OBJECT_NAME,
        strips: Annotated[list[StripSeamRows], Field(min_length=1, max_length=128)],
        turn_degrees: Annotated[float, Field(ge=0.001, le=180)] = 15,
        max_segments: Annotated[int, Field(ge=1, le=10000)] = 10,
        dry_run: bool = True,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or mark whole transverse edge rows of explicit matching quad strips when accumulated centreline turn or segment count reaches a limit; dry_run defaults true."""
        params = {"object_name": object_name, "strips": _model_payloads(strips),
                  "turn_degrees": turn_degrees, "max_segments": max_segments, "dry_run": dry_run}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("uv.cut_strips_by_turn", params, instance)

    @mcp.tool(name="mesh.build_slab")
    def mesh_build_slab(
        name: Annotated[str, Field(min_length=1, max_length=63)],
        outline: Annotated[list[_VECTOR2 | _VECTOR3], Field(min_length=4, max_length=64)],
        thickness: Annotated[float, Field(gt=0)],
        opening: Annotated[list[_VECTOR2 | _VECTOR3] | None, Field(min_length=4, max_length=64)] = None,
        pocket_depth: Annotated[float | None, Field(gt=0)] = None,
        part_attribute: Annotated[str, Field(min_length=1, max_length=63)] = "part_id",
        part_id: Annotated[int, Field(ge=0, le=2147483647)] = 0,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Build a bounded all-quad planar slab, through opening or blind pocket from matching ordered even-length outline/opening loops. The outline is the top face; thickness extends opposite its normal (negative Z for a counter-clockwise XY outline). FACE INT fregion labels distinguish top, walls, bottom and pocket floor. Unsupported or folded geometry refuses before mutation."""
        params: dict[str, Any] = {"name": name, "outline": outline, "thickness": thickness,
                                  "part_attribute": part_attribute, "part_id": part_id}
        for key, value in (("opening", opening), ("pocket_depth", pocket_depth),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("mesh.build_slab", params, instance)

    @mcp.tool(name="mesh.goal_deform")
    def mesh_goal_deform(
        object_name: str,
        operation: Literal["landmark_warp", "relax", "normal_offset", "crease", "seam_smooth"],
        region: GoalRegion,
        max_displacement_mm: Annotated[float, Field(gt=0, le=10000)],
        pinned_vertex_group: str | None = None,
        symmetry: GoalSymmetry | None = None,
        clearance: GoalClearance | None = None,
        landmarks: Annotated[list[GoalLandmark] | None, Field(min_length=1, max_length=100000)] = None,
        falloff_radius_mm: Annotated[float | None, Field(gt=0, le=10000)] = None,
        method: Literal["rbf", "harmonic"] = "rbf",
        iterations: Annotated[int, Field(ge=1, le=500)] = 20,
        strength: Annotated[float, Field(gt=0, le=1)] = 0.5,
        offset_mm: Annotated[float | None, Field(ge=-10000, le=10000)] = None,
        curve: Annotated[list[_VECTOR3] | None, Field(min_length=2, max_length=256)] = None,
        band_width_mm: Annotated[float | None, Field(gt=0, le=10000)] = None,
        landmark_tolerance_mm: Annotated[float, Field(ge=0.001, le=1)] = 0.01,
        dry_run: bool = True,
        confirm: bool = False,
        expected_state: str | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Bounded base-mesh landmark warp, region relax, normal offset, curve crease or seam smoothing. Explicit vertex group (weights scale deformation) or face_indices region; vertices outside it remain byte-identical. Coordinates/curves/landmark targets are WORLD METRES (matrix_world times scene.unit_settings.scale_length); distances are millimetres. Dry-run first by default; apply needs dry_run=false and confirm=true. Pins stay fixed; symmetry requires paired masks and pins; incompatible landmarks, displacement limits or clearance refuse before mutation. Clearance certifies unsigned distance of the entire base surface to the named BASE mesh, including unselected faces; it does not assert outside/inside. Unresolved certificates refuse. Receipts measure float32-realizable positions, report region displacement percentiles and enclosed-volume change only for closed consistently wound meshes. Modifiers are not evaluated. No automatic self-intersection acceptance; validate fitted assets independently. 250000 vertices, 500000 triangles, 500 iterations; RBF max 256 landmarks; harmonic max 100000."""
        params: dict[str, Any] = {
            "object_name": object_name, "operation": operation,
            "region": region.model_dump(exclude_none=True),
            "max_displacement_mm": max_displacement_mm, "method": method,
            "iterations": iterations, "strength": strength,
            "landmark_tolerance_mm": landmark_tolerance_mm,
            "dry_run": dry_run, "confirm": confirm,
        }
        for key, value in (("pinned_vertex_group", pinned_vertex_group),
                           ("symmetry", symmetry.model_dump() if symmetry else None),
                           ("clearance", clearance.model_dump() if clearance else None),
                           ("landmarks", _model_payloads(landmarks) if landmarks else None),
                           ("falloff_radius_mm", falloff_radius_mm), ("offset_mm", offset_mm),
                           ("curve", curve), ("band_width_mm", band_width_mm),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("mesh.goal_deform", params, instance)

    @mcp.tool(name="mesh.fit_contours")
    def mesh_fit_contours(
        object_name: str,
        region: GoalRegion,
        max_displacement_mm: Annotated[float, Field(gt=0, le=10000)],
        views: Annotated[list[dict[str, Any]], Field(min_length=2, max_length=2,
            description="Exactly two perpendicular orthographic camera references, each with {camera_name, contour, width, height} or {camera_name, mask_path, threshold?, width?, height?}.")],
        pinned_vertex_group: str | None = None,
        symmetry: GoalSymmetry | None = None,
        clearance: GoalClearance | None = None,
        dry_run: bool = True,
        confirm: bool = False,
        expected_state: str | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Fit a selected mesh region to exactly two perpendicular orthographic front/side reference silhouettes sharing the same vertical axis. Each view names an existing camera and supplies a closed contour in top-left pixel coordinates with width/height, or a light-foreground mask_path. Targets must have one connected span per occupied row; holes, disjoint spans, perspective, camera shifts, visible modifiers and height changes are unsupported. Remaps horizontal cross-section envelopes using 257 height samples, scaled by region group weights; preserves vertical coordinates, topology and all unselected/pinned vertices. Uses world millimetre displacement bounds and shared symmetry/clearance constraints; conflicts and raster contour regression refuse before writes, never silently clamp. Reports both views' CPU-raster symmetric contour error and IoU before/after on final quantized coordinates. Dry run defaults true; applying requires dry_run=false and confirm=true. No render, camera, selection, material or output-file changes. This fits base mesh silhouettes, not anatomy or unseen concavities."""
        params: dict[str, Any] = {
            "object_name": object_name, "region": region.model_dump(exclude_none=True),
            "max_displacement_mm": max_displacement_mm, "views": views,
            "dry_run": dry_run, "confirm": confirm,
        }
        for key, value in (("pinned_vertex_group", pinned_vertex_group),
                           ("symmetry", symmetry.model_dump() if symmetry else None),
                           ("clearance", clearance.model_dump() if clearance else None),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("mesh.fit_contours", params, instance)

    @mcp.tool(name="mesh.build_lathe")
    def mesh_build_lathe(
        name: Annotated[str, Field(min_length=1, max_length=63)],
        profile: Annotated[list[LatheProfilePoint], Field(min_length=2, max_length=128)],
        segments: Annotated[int, Field(ge=4, le=512)] = 32,
        closed_profile: bool = False,
        cap_ends: bool = True,
        fillet_segments: Annotated[int, Field(ge=1, le=32)] = 4,
        part_attribute: Annotated[str, Field(min_length=1, max_length=63)] = "part_id",
        part_id: Annotated[int, Field(ge=0, le=2147483647)] = 0,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Construct an all-quad revolved profile with tangent 2D fillets, smooth faces, explicit or over-35-degree sharp edges and disk-mapped quad-grid caps; unsupported shapes do not mutate."""
        params: dict[str, Any] = {"name": name, "profile": _model_payloads(profile), "segments": segments,
                                  "closed_profile": closed_profile, "cap_ends": cap_ends,
                                  "fillet_segments": fillet_segments, "part_attribute": part_attribute, "part_id": part_id}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("mesh.build_lathe", params, instance)

    @mcp.tool(name="mesh.build_sweep")
    def mesh_build_sweep(
        name: Annotated[str, Field(min_length=1, max_length=63)],
        path: Annotated[list[SweepPathPoint], Field(min_length=2, max_length=512)],
        closed: bool = False,
        up: _VECTOR3 | None = None,
        part_attribute: Annotated[str, Field(min_length=1, max_length=63)] = "part_id",
        part_id: Annotated[int, Field(ge=0, le=2147483647)] = 0,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Construct a bounded all-quad transported profile sweep with smooth faces, over-35-degree sharp edges and miter checks; closed paths must be planar."""
        params: dict[str, Any] = {"name": name, "path": _model_payloads(path), "closed": closed,
                                  "part_attribute": part_attribute, "part_id": part_id}
        for key, value in (("up", up), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("mesh.build_sweep", params, instance)

    @mcp.tool(name="mesh.build_plate")
    def mesh_build_plate(
        name: Annotated[str, Field(min_length=1, max_length=63)],
        outline: Annotated[list[_VECTOR2 | _VECTOR3], Field(min_length=3, max_length=64)],
        subdivisions: tuple[Annotated[int, Field(ge=1, le=64)], Annotated[int, Field(ge=1, le=64)]] | None = None,
        thickness: Annotated[float, Field(ge=0)] = 0,
        open_back: bool = False,
        target: _OBJECT_NAME | None = None,
        projection_direction: _VECTOR3 | None = None,
        max_projection_distance: Annotated[float, Field(gt=0)] = 10,
        part_attribute: Annotated[str, Field(min_length=1, max_length=63)] = "part_id",
        part_id: Annotated[int, Field(ge=0, le=2147483647)] = 0,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Construct a smooth-shaded quad grid from 3-64 convex coplanar corners, with over-35-degree sharp edges; target-projected thickness grows outward and a bounded clearance search rejects unresolved penetration."""
        params: dict[str, Any] = {"name": name, "outline": outline, "thickness": thickness,
                                  "open_back": open_back, "max_projection_distance": max_projection_distance,
                                  "part_attribute": part_attribute, "part_id": part_id}
        for key, value in (("subdivisions", subdivisions), ("target", target),
                           ("projection_direction", projection_direction), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("mesh.build_plate", params, instance)

    @mcp.tool(name="mesh.clear_of")
    def mesh_clear_of(
        object_name: _OBJECT_NAME,
        target: _OBJECT_NAME,
        gap: tuple[Annotated[float, Field(ge=0)], Annotated[float, Field(ge=0)]],
        part_attribute: Annotated[str, Field(min_length=1, max_length=63)] = "part_id",
        part_id: Annotated[int, Field(ge=0, le=2147483647)] = 0,
        max_iterations: Annotated[int, Field(ge=1, le=100)] = 20,
        step: Annotated[float, Field(gt=0)] = 0.001,
        apply: bool = False,
        delete_hidden: bool = False,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Analyze or apply bounded normal clearance of one face part from a planar or closed curved target with BVH and gap checks; hidden-face deletion is unsupported."""
        params: dict[str, Any] = {"object_name": object_name, "target": target, "gap": gap,
                                  "part_attribute": part_attribute, "part_id": part_id,
                                  "max_iterations": max_iterations, "step": step, "apply": apply, "delete_hidden": delete_hidden}
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("mesh.clear_of", params, instance)

    @mcp.tool(name="mesh.fair_fit")
    def mesh_fair_fit(
        source: _OBJECT_NAME,
        center: _VECTOR3,
        name: Annotated[str | None, Field(min_length=1, max_length=63)] = None,
        theta_samples: Annotated[int, Field(ge=8, le=128)] = 72,
        phi_samples: Annotated[int, Field(ge=3, le=64)] = 25,
        phi_range: _VECTOR2 | None = None,
        order_m: Annotated[int, Field(ge=0, le=8)] = 4,
        order_k: Annotated[int, Field(ge=0, le=7)] = 3,
        iterations: Annotated[int, Field(ge=1, le=12)] = 6,
        rejection_sigma: Annotated[float, Field(gt=0)] = 2.5,
        thickness: Annotated[float | None, Field(gt=0)] = None,
        apply: bool = False,
        part_attribute: Annotated[str, Field(min_length=1, max_length=63)] = "part_id",
        part_id: Annotated[int, Field(ge=0, le=2147483647)] = 0,
        expected_state: _OBJECT_NAME | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Analyze a robust symmetric low-order radial fit and optional all-quad nonpolar thick-band build; missing rays remain unsupported."""
        params: dict[str, Any] = {"source": source, "center": center, "theta_samples": theta_samples,
                                  "phi_samples": phi_samples, "order_m": order_m, "order_k": order_k,
                                  "iterations": iterations, "rejection_sigma": rejection_sigma, "apply": apply,
                                  "part_attribute": part_attribute, "part_id": part_id}
        for key, value in (("name", name), ("phi_range", phi_range), ("thickness", thickness), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("mesh.fair_fit", params, instance)

    @mcp.tool(name="bake.run")
    def bake_run(
        type: Literal["NORMAL", "AO", "DIFFUSE", "ROUGHNESS", "EMIT"],
        target: _OBJECT_NAME,
        uv_layer: _OBJECT_NAME,
        image: _OBJECT_NAME,
        output_path: _OBJECT_NAME,
        sources: list[_OBJECT_NAME] | None = None,
        resolution: Annotated[int, Field(ge=16, le=4096)] = 1024,
        margin: Annotated[int, Field(ge=0, le=128)] = 16,
        overwrite: bool = False,
        cage_object: _OBJECT_NAME | None = None,
        cage_extrusion: Annotated[float | None, Field(ge=0, le=1000)] = None,
        max_ray_distance: Annotated[float | None, Field(ge=0, le=1000)] = None,
        normal_g: Literal["POS_Y", "NEG_Y"] = "POS_Y",
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Bake Cycles detail to an explicit PNG path with context restoration."""
        params: dict[str, Any] = {"type": type, "target": target, "uv_layer": uv_layer, "image": image, "output_path": output_path, "resolution": resolution, "margin": margin, "overwrite": overwrite, "normal_g": normal_g}
        for key, value in (("sources", sources), ("cage_object", cage_object), ("cage_extrusion", cage_extrusion), ("max_ray_distance", max_ray_distance), ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        return proxy.call_named("bake.run", params, instance)

    @mcp.tool(name="render.preview")
    def render_preview(
        width: Annotated[int, Field(ge=1, le=16384)] = 960,
        height: Annotated[int, Field(ge=1, le=16384)] = 540,
        output_path: Annotated[str | None, Field(min_length=1)] = None,
        overwrite: bool = False,
        allow_large: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Render a bounded PNG preview and restore temporary render state."""
        envelope = proxy.render_preview(
            width, height, output_path, overwrite, allow_large, instance
        )
        data = envelope.get("data")
        path = data.get("path") if isinstance(data, dict) else None
        return _render_result(envelope, [path] if path is not None else [])

    @mcp.tool(name="render.review_set")
    def render_review_set(
        output_dir: Annotated[str | None, Field(min_length=1)] = None,
        output_prefix: Annotated[str | None, Field(min_length=1)] = None,
        views: Annotated[list[_REVIEW_VIEWS] | None, Field(min_length=1)] = None,
        width: Annotated[int, Field(ge=1, le=16384)] = 960,
        height: Annotated[int, Field(ge=1, le=16384)] = 540,
        overwrite: bool = False,
        allow_large: bool = False,
        hide: list[Annotated[str, Field(min_length=1)]] | None = None,
        show_only: list[Annotated[str, Field(min_length=1)]] | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Render preset review views, returning a manifest and locally inlined PNGs."""
        envelope = proxy.render_review_set(
            output_dir,
            output_prefix,
            views,
            width,
            height,
            overwrite,
            allow_large,
            hide,
            show_only,
            instance,
        )
        return _render_result(envelope, _review_paths(envelope))

    @mcp.tool(name="render.detail")
    def render_detail(
        object_name: _OBJECT_NAME | None = None,
        object_names: _DETAIL_OBJECT_NAMES | None = None,
        point: _VECTOR3 | None = None,
        radius: Annotated[float | None, Field(gt=0)] = None,
        bounds: DetailBounds | None = None,
        direction: _DETAIL_DIRECTION | _VECTOR3 | None = None,
        projection: Literal["perspective", "orthographic"] = "perspective",
        view_mode: _VIEW_MODE = "lit",
        isolate: bool = False,
        clip_to_bounds: bool = False,
        width: Annotated[int, Field(ge=1, le=16384)] = 960,
        height: Annotated[int, Field(ge=1, le=16384)] = 540,
        output_path: Annotated[str | None, Field(min_length=1)] = None,
        overwrite: bool = False,
        allow_large: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Render a reproducible close-up. bounds may fix framing while object_name/object_names select isolation subjects; isolate changes render visibility, and hide_set alone does not affect rendered output. clip_to_bounds requires explicit bounds and clips camera depth to their near/far extent, not the exact 3D box; scene geometry is unchanged. solid uses Workbench lighting; lit warns when no render-visible Light objects exist."""
        params = _detail_target_params(object_name, object_names, point, radius, bounds)
        params.update({"projection": projection, "view_mode": view_mode, "isolate": isolate,
                       "clip_to_bounds": clip_to_bounds, "width": width,
                       "height": height, "overwrite": overwrite, "allow_large": allow_large})
        if direction is not None:
            params["direction"] = direction
        if output_path is not None:
            params["output_path"] = output_path
        envelope = proxy.render_detail(params, instance)
        data = envelope.get("data")
        path = data.get("path") if isinstance(data, dict) else None
        return _render_result(envelope, [path] if path is not None else [])

    @mcp.tool(name="scene.fast_capture")
    def scene_fast_capture(
        object_name: _OBJECT_NAME | None = None,
        object_names: _DETAIL_OBJECT_NAMES | None = None,
        point: _VECTOR3 | None = None,
        radius: Annotated[float | None, Field(gt=0)] = None,
        bounds: DetailBounds | None = None,
        views: _FAST_VIEWS | None = None,
        projection: Literal["perspective", "orthographic"] = "perspective",
        shading: Literal["current", "solid", "material"] = "current",
        view_mode: _VIEW_MODE = "lit",
        overlays: bool | None = None,
        wireframe: bool | None = None,
        width: Annotated[int, Field(ge=1, le=16384)] = 512,
        height: Annotated[int, Field(ge=1, le=16384)] = 512,
        output_path: Annotated[str | None, Field(min_length=1)] = None,
        columns: Annotated[int | None, Field(ge=1, le=6)] = None,
        overwrite: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Capture one to six targeted viewport-quality views through the deferred draw handler and return one PNG sheet. Solid/material shading and overlays are temporary; current view requires no target, named views require one."""
        params = _detail_target_params(object_name, object_names, point, radius, bounds)
        params.update({"projection": projection, "shading": shading, "view_mode": view_mode, "width": width,
                       "height": height, "overwrite": overwrite})
        for key, value in (("views", views), ("overlays", overlays), ("wireframe", wireframe),
                           ("output_path", output_path), ("columns", columns)):
            if value is not None:
                params[key] = value
        envelope = proxy.scene_fast_capture(params, instance)
        data = envelope.get("data")
        path = data.get("path") if isinstance(data, dict) else None
        return _render_result(envelope, [path] if path is not None else [])

    @mcp.tool(name="render.ortho_set")
    def render_ortho_set(
        object_name: _OBJECT_NAME | None = None,
        object_names: _DETAIL_OBJECT_NAMES | None = None,
        point: _VECTOR3 | None = None,
        radius: Annotated[float | None, Field(gt=0)] = None,
        bounds: DetailBounds | None = None,
        views: _ORTHO_VIEWS | None = None,
        shared_scale: bool = True,
        view_mode: _VIEW_MODE = "lit",
        isolate: bool = False,
        width: Annotated[int, Field(ge=1, le=16384)] = 960,
        height: Annotated[int, Field(ge=1, le=16384)] = 540,
        output_dir: Annotated[str | None, Field(min_length=1)] = None,
        output_prefix: Annotated[str | None, Field(min_length=1)] = None,
        overwrite: bool = False,
        allow_large: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Render selected orthographic views with optional shared scale. output_dir plus output_prefix uses a basename inside that directory; explicit names are overwrite-guarded. Isolation uses render visibility, not hide_set."""
        params = _detail_target_params(object_name, object_names, point, radius, bounds)
        params.update({"shared_scale": shared_scale, "view_mode": view_mode, "isolate": isolate, "width": width,
                       "height": height, "overwrite": overwrite, "allow_large": allow_large})
        for key, value in (("views", views), ("output_dir", output_dir), ("output_prefix", output_prefix)):
            if value is not None:
                params[key] = value
        envelope = proxy.render_ortho_set(params, instance)
        return _render_result(envelope, _review_paths(envelope))

    @mcp.tool(name="render.reference_match")
    def render_reference_match(
        reference_path: Annotated[str, Field(min_length=1)],
        camera_name: Annotated[str | None, Field(min_length=1)] = None,
        camera: ReferenceCamera | None = None,
        output_prefix: Annotated[str | None, Field(min_length=1)] = None,
        opacity: Annotated[float, Field(ge=0, le=1)] = 0.5,
        overlay_mode: Literal["blend", "edges"] = "blend",
        overwrite: bool = False,
        allow_large: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Render at a reference image's aspect ratio and write comparison sheets."""
        params: dict[str, Any] = {"reference_path": reference_path, "opacity": opacity,
                                  "overlay_mode": overlay_mode, "overwrite": overwrite,
                                  "allow_large": allow_large}
        if camera_name is not None:
            params["camera_name"] = camera_name
        if camera is not None:
            params["camera"] = camera.model_dump(exclude_none=True)
        if output_prefix is not None:
            params["output_prefix"] = output_prefix
        envelope = proxy.render_reference_match(params, instance)
        data = envelope.get("data")
        paths = [data[key] for key in ("render_path", "side_by_side_path", "overlay_path") if isinstance(data, dict) and key in data]
        return _render_result(envelope, paths)

    @mcp.tool(name="camera.calibrate_from_reference")
    def camera_calibrate_from_reference(
        reference_path: Annotated[str, Field(min_length=1)],
        correspondences: Annotated[list[CalibrationCorrespondence], Field(min_length=4)],
        camera_name: _OBJECT_NAME,
        model: Literal["perspective", "orthographic"] = "perspective",
        focal_length_mm: Annotated[float | None, Field(ge=1)] = None,
        output_path: Annotated[str | None, Field(min_length=1)] = None,
        overwrite: bool = False,
        high_error_fraction: Annotated[float, Field(ge=0, le=1)] = 0.01,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Calibrate a scene camera from image/world correspondences and return pixel residuals with an overlay PNG."""
        params: dict[str, Any] = {"reference_path": reference_path, "correspondences": _model_payloads(correspondences),
                                  "camera_name": camera_name, "model": model, "overwrite": overwrite,
                                  "high_error_fraction": high_error_fraction}
        for key, value in (("focal_length_mm", focal_length_mm), ("output_path", output_path),
                           ("expected_state", expected_state)):
            if value is not None:
                params[key] = value
        envelope = proxy.call_named("camera.calibrate_from_reference", params, instance)
        data = envelope.get("data")
        return _render_result(envelope, [data.get("overlay_path")] if isinstance(data, dict) else [])

    @mcp.tool(name="camera.import_photogrammetry")
    def camera_import_photogrammetry(
        csv_path: Annotated[str, Field(min_length=1)],
        images_dir: Annotated[str, Field(min_length=1)],
        pointcloud_object: _OBJECT_NAME,
        collection_name: Annotated[str, Field(min_length=1)] = "RealityScan Cameras",
        mode: Literal["centered_zero", "csv_principal"] = "csv_principal",
        image_names: Annotated[list[_OBJECT_NAME] | None, Field(min_length=1, max_length=100000, json_schema_extra={"uniqueItems": True})] = None,
        dry_run: bool = True,
        confirm_unverified_orientation: bool = False,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Preview or import RealityScan GUI CSV cameras aligned through a point-cloud object's transform; orientation remains provisional until image correspondences verify it. csv_principal keeps solved intrinsics with their poses; centered_zero is a diagnostic that breaks that fit."""
        params: dict[str, Any] = {
            "csv_path": csv_path, "images_dir": images_dir,
            "pointcloud_object": pointcloud_object,
            "collection_name": collection_name, "mode": mode,
            "dry_run": dry_run,
            "confirm_unverified_orientation": confirm_unverified_orientation,
        }
        if image_names is not None:
            params["image_names"] = image_names
        if expected_state is not None:
            params["expected_state"] = expected_state
        return _render_result(proxy.call_named("camera.import_photogrammetry", params, instance), [])

    @mcp.tool(name="camera.import_colmap_package")
    def camera_import_colmap_package(
        package_dir: Annotated[str, Field(min_length=1)],
        images_dir: Annotated[str | None, Field(min_length=1)] = None,
        transform_object: _OBJECT_NAME | None = None,
        collection_name: Annotated[str | None, Field(min_length=1)] = None,
        dry_run: bool = True,
        import_points: bool = False,
        kind: Literal["reproduction", "constrained"] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Preview or import a Camera Refinement Workbench handoff (cameras.json or undistorted COLMAP text) with solved intrinsics, poses, kind, and scores. Intrinsics and poses are one solution; never mix them across packages or kinds."""
        params: dict[str, Any] = {
            "package_dir": package_dir, "dry_run": dry_run,
            "import_points": import_points,
        }
        if collection_name is not None:
            params["collection_name"] = collection_name
        if images_dir is not None:
            params["images_dir"] = images_dir
        if transform_object is not None:
            params["transform_object"] = transform_object
        if kind is not None:
            params["kind"] = kind
        if expected_state is not None:
            params["expected_state"] = expected_state
        return _render_result(proxy.call_named("camera.import_colmap_package", params, instance), [])

    @mcp.tool(name="render.contour_compare")
    def render_contour_compare(
        reference_path: Annotated[str, Field(min_length=1)],
        camera_name: _OBJECT_NAME,
        object_names: Annotated[list[_OBJECT_NAME], Field(min_length=1, max_length=64, json_schema_extra={"uniqueItems": True})],
        mask_path: Annotated[str | None, Field(min_length=1)] = None,
        threshold: Annotated[float | None, Field(ge=0, le=1)] = None,
        foreground: Literal["dark", "light"] | None = None,
        background_color: _VECTOR3 | None = None,
        color_tolerance: Annotated[float | None, Field(ge=0, le=1)] = None,
        output_path: Annotated[str | None, Field(min_length=1)] = None,
        top_n: Annotated[int | None, Field(ge=1, le=10)] = None,
        min_iou: Annotated[float | None, Field(ge=0, le=1)] = None,
        borderline_band: Annotated[float, Field(ge=0, le=1)] = 0.01,
        overwrite: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Compare a reference silhouette with evaluated target meshes and return deviation statistics, focus points, and a PNG."""
        params: dict[str, Any] = {"reference_path": reference_path, "camera_name": camera_name,
                                  "object_names": object_names, "overwrite": overwrite}
        for key, value in (("mask_path", mask_path), ("threshold", threshold), ("foreground", foreground),
                           ("background_color", background_color), ("color_tolerance", color_tolerance),
                           ("output_path", output_path), ("top_n", top_n),
                           ("min_iou", min_iou), ("borderline_band", borderline_band)):
            if value is not None:
                params[key] = value
        envelope = proxy.call_named("render.contour_compare", params, instance)
        data = envelope.get("data")
        return _render_result(envelope, [data.get("deviation_path")] if isinstance(data, dict) else [])

    @mcp.tool(name="render.contact_sheet")
    def render_contact_sheet(
        images: Annotated[list[ContactSheetImage], Field(min_length=1, max_length=16)],
        output_path: Annotated[str, Field(min_length=1)],
        columns: Annotated[int | None, Field(ge=1, le=16)] = None,
        view_mode: _VIEW_MODE = "lit",
        overwrite: bool = False,
        allow_large: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Compose labeled PNG review images into one contact sheet."""
        params: dict[str, Any] = {"images": _model_payloads(images), "output_path": output_path, "view_mode": view_mode,
                                  "overwrite": overwrite, "allow_large": allow_large}
        if columns is not None:
            params["columns"] = columns
        envelope = proxy.render_contact_sheet(params, instance)
        data = envelope.get("data")
        path = data.get("path") if isinstance(data, dict) else None
        return _render_result(envelope, [path] if path is not None else [])

    @mcp.tool(name="render.background")
    def render_background(
        mode: Literal["preview", "detail", "ortho_set", "review_set", "reference_match"] = "preview",
        options: dict[str, Any] | None = None,
        engine: Literal["CYCLES", "BLENDER_EEVEE_NEXT"] | None = None,
        samples: Annotated[int | None, Field(ge=1, le=100000)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Queue a final-quality render in a separate Blender process and return an operation ID. The copy is saved when the job starts, which may be after queueing; inspect status before editing if exact snapshot contents matter. Later live changes mark the snapshot stale."""
        params: dict[str, Any] = {"mode": mode}
        for key, value in (("options", options), ("engine", engine), ("samples", samples)):
            if value is not None:
                params[key] = value
        return proxy.render_background(params, instance)

    @mcp.tool(name="render.background_status")
    def render_background_status(
        operation_id: Annotated[str, Field(min_length=1)],
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Read queued, running, completed, failed, or cancelled status for a background render ID."""
        return proxy.render_background_status(operation_id, instance)

    @mcp.tool(name="render.background_cancel")
    def render_background_cancel(
        operation_id: Annotated[str, Field(min_length=1)],
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Cancel a queued background render or terminate its separate Blender child process."""
        return proxy.render_background_cancel(operation_id, instance)

    @mcp.tool(name="render.compare")
    def render_compare(
        image_a: Annotated[str, Field(min_length=1)],
        image_b: Annotated[str, Field(min_length=1)],
        threshold: Annotated[int, Field(ge=0, le=255)] = 8,
        write_diff: bool = False,
        diff_output_path: Annotated[str | None, Field(min_length=1)] = None,
        overwrite: bool = False,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Compare same-size PNGs and optionally return an inlined difference image."""
        envelope = proxy.render_compare(
            image_a,
            image_b,
            threshold,
            write_diff,
            diff_output_path,
            overwrite,
            instance,
        )
        data = envelope.get("data")
        diff_path = None
        if isinstance(data, dict):
            diff_path = data.get("diff_path", data.get("diff_output_path"))
        return _render_result(envelope, [diff_path] if diff_path is not None else [])

    @mcp.tool(name="material.inspect")
    def material_inspect(
        material: Annotated[str | None, Field(min_length=1)] = None,
        object_name: Annotated[str | None, Field(min_length=1)] = None,
        collection: Annotated[str | None, Field(min_length=1)] = None,
        include_nodes: bool = False,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect material summaries and optionally paginated node graphs."""
        return proxy.material_inspect(
            material, object_name, collection, include_nodes, limit, cursor, instance
        )

    @mcp.tool(name="material.create")
    def material_create(
        name: Annotated[str, Field(min_length=1)],
        use_nodes: bool = True,
        use_fake_user: bool = False,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create one independent material with a default Principled node tree by default."""
        return proxy.material_create(name, use_nodes, use_fake_user, expected_state, instance)

    @mcp.tool(name="material.patch")
    def material_patch(
        material: Annotated[str, Field(min_length=1)],
        operations: Annotated[
            list[dict[str, Any]],
            Field(min_length=1, description="Allowlisted operations: add_node, remove_node, set_input, connect, disconnect, set_material_flag."),
        ],
        preconditions: list[dict[str, Any]] | None = None,
        dry_run: Annotated[bool, Field(description="Defaults true. Pass false only after reviewing the planned journal.")] = True,
        expected_user_count: Annotated[int | None, Field(ge=0)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Dry-run-first material patch; take a checkpoint before a live apply for rollback."""
        return proxy.material_patch(
            material,
            operations,
            preconditions,
            dry_run,
            expected_user_count,
            expected_state,
            instance,
        )

    @mcp.tool(name="image.inspect")
    def image_inspect(
        image: Annotated[str | None, Field(min_length=1)] = None,
        include_filepath: bool = False,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect image metadata without reading pixel buffers; paths are opt-in."""
        return proxy.image_inspect(image, include_filepath, limit, cursor, instance)

    @mcp.tool(name="image.load")
    def image_load(
        filepath: Annotated[str, Field(min_length=1)],
        name: Annotated[str | None, Field(min_length=1)] = None,
        check_existing: bool = True,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Load one explicit existing allowlisted image file into Blender."""
        return proxy.image_load(filepath, name, check_existing, expected_state, instance)

    @mcp.tool(name="image.create")
    def image_create(
        name: Annotated[str, Field(min_length=1)],
        width: Annotated[int, Field(ge=1, le=32768)],
        height: Annotated[int, Field(ge=1, le=32768)],
        color: Annotated[list[_COLOR_COMPONENT] | None, Field(min_length=4, max_length=4)] = None,
        alpha: bool = True,
        float_buffer: bool = False,
        is_data: bool = False,
        max_pixels: Annotated[int, Field(ge=1, le=268435456)] = 16_777_216,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Create a generated image under an explicit pixel budget."""
        return proxy.image_create(name, width, height, color, alpha, float_buffer, is_data, max_pixels, expected_state, instance)

    @mcp.tool(name="image.patch")
    def image_patch(
        image: Annotated[str, Field(min_length=1)],
        colorspace: Annotated[str | None, Field(min_length=1)] = None,
        alpha_mode: Literal["STRAIGHT", "PREMUL", "CHANNEL_PACKED", "NONE"] | None = None,
        use_fake_user: bool | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Patch allowlisted image metadata with rollback on failure."""
        return proxy.image_patch(image, colorspace, alpha_mode, use_fake_user, expected_state, instance)

    @mcp.tool(name="material.slot_inspect")
    def material_slot_inspect(
        object_name: Annotated[str, Field(min_length=1)],
        limit: Annotated[int, Field(ge=1, le=1000)] = 200,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Inspect an object's material slots and polygon usage counts."""
        return proxy.material_slot_inspect(object_name, limit, cursor, instance)

    @mcp.tool(name="material.slot_assign")
    def material_slot_assign(
        object_name: Annotated[str, Field(min_length=1)],
        material: Annotated[str, Field(min_length=1)],
        slot_index: Annotated[int | None, Field(ge=0, le=999)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Assign or append an existing material on single-user object data."""
        return proxy.material_slot_assign(object_name, material, slot_index, expected_state, instance)

    @mcp.tool(name="material.slot_remove")
    def material_slot_remove(
        object_name: Annotated[str, Field(min_length=1)],
        slot_index: Annotated[int, Field(ge=0, le=999)],
        max_polygons: Annotated[int, Field(ge=1, le=5000000)] = 250_000,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or confirm bounded material-slot removal."""
        return proxy.material_slot_remove(object_name, slot_index, max_polygons, dry_run, confirm, expected_state, instance)

    @mcp.tool(name="material.texture_bind")
    def material_texture_bind(
        material: Annotated[str, Field(min_length=1)],
        image: Annotated[str, Field(min_length=1)],
        image_node: Annotated[str, Field(min_length=1)] = "CBT Image Texture",
        uv_map: Annotated[str | None, Field(min_length=1)] = None,
        mapping: dict[str, Any] | None = None,
        color_target: dict[str, Any] | None = None,
        alpha_target: dict[str, Any] | None = None,
        interpolation: Literal["Closest", "Linear", "Cubic", "Smart"] = "Linear",
        extension: Literal["REPEAT", "EXTEND", "CLIP"] = "REPEAT",
        projection: Literal["FLAT", "BOX", "SPHERE", "TUBE"] = "FLAT",
        replace_existing: bool = False,
        allow_shared: bool = False,
        expected_user_count: Annotated[int | None, Field(ge=0)] = None,
        dry_run: bool = True,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Bind an image texture with optional UV/mapping nodes and safe link replacement."""
        return proxy.material_texture_bind(
            material, image, image_node, uv_map, mapping, color_target, alpha_target,
            interpolation, extension, projection, replace_existing, allow_shared,
            expected_user_count, dry_run, confirm, expected_state, instance,
        )

    @mcp.tool(name="scene.save_version")
    def scene_save_version(
        base: Annotated[str | None, Field(min_length=1)] = None,
        tag: Annotated[str | None, Field(min_length=1)] = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Save a non-overwriting active document version, unlike checkpoint.save's sidecar copy."""
        return proxy.scene_save_version(base, tag, expected_state, instance)

    @mcp.tool(name="scene.save_current")
    def scene_save_current(
        dry_run: bool | None = None,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Save the active .blend in one call when its disk file is unchanged since load or last save. If disk identity changed, confirm:true plus matching expected_state saves by default; dry_run:true still plans only."""
        return proxy.scene_save_current(dry_run, confirm, expected_state, instance)

    @mcp.tool(name="scene.save_as")
    def scene_save_as(
        path: Annotated[str, Field(min_length=1)],
        dry_run: bool = True,
        overwrite: bool = False,
        confirm: Literal[True] | None = None,
        expected_state: Annotated[str | None, Field(min_length=1)] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Plan or save the active Blender document to a named path."""
        params: dict[str, Any] = {"path": path, "dry_run": dry_run, "overwrite": overwrite}
        if confirm is not None:
            params["confirm"] = confirm
        if expected_state is not None:
            params["expected_state"] = expected_state
        return proxy.call_named("scene.save_as", params, instance)

    @mcp.tool(name="system.capabilities")
    def system_capabilities(
        toolset: Annotated[str | None, Field(min_length=1)] = None,
        include_schemas: bool = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Read the authoritative add-on toolset catalog and optional schemas."""
        return proxy.system_capabilities(toolset, include_schemas, limit, cursor, instance)

    @mcp.tool(name="system.doctor")
    def system_doctor(instance: _INSTANCE_SELECTOR = None) -> dict[str, Any]:
        """Report non-secret add-on, Blender, document, bridge, and capability health, with per-tool input schema digests. Restart the MCP client after an adapter upgrade to refresh cached parameters of existing tools."""
        envelope = proxy.system_doctor(instance)
        data = envelope.get("data")
        if isinstance(data, dict):
            adapter_version = _adapter_version()
            data["adapter"] = {
                "version": adapter_version,
                "tool_mode": resolved_tool_mode,
                "response_mode": resolved_response_mode,
                "source_tool_count": len(mcp.cbt_registered_tools),
                "advertised_tool_count": len(_SEARCH_TOOLS) if resolved_tool_mode == "search" else len(mcp.cbt_registered_tools),
            }
            addon_release = _release_version(data.get("addon_version"))
            adapter_release = _release_version(adapter_version)
            if addon_release and adapter_release and addon_release != adapter_release:
                problem = f"adapter {adapter_release} and add-on {addon_release} versions differ; restart clients and Blender after upgrading"
                problems = data.get("problems")
                if isinstance(problems, list):
                    problems.append(problem)
                else:
                    data["problems"] = [problem]
                data["healthy"] = False
                warnings = envelope.get("warnings")
                if isinstance(warnings, list):
                    warnings.append(f"Adapter {adapter_release} and add-on {addon_release} differ; restart Blender to load add-on {adapter_release}")
        return envelope

    @mcp.tool(name="workflow.run")
    def workflow_run(
        steps: Annotated[
            list[dict[str, Any]],
            Field(min_length=1, max_length=32, description="Ordered {id, tool, arguments, rollback?} steps. Use {$ref: 'prior_step.data.path'} for result references."),
        ],
        execute: Annotated[bool, Field(description="False validates and returns a plan without calling Blender.")] = False,
        rollback_on_failure: bool = False,
        confirm_rollback: bool | None = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                description="Optional process-local duplicate-suppression key for execute=true requests.",
            ),
        ] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Validate or synchronously execute at most 32 ordered steps; it stops on first failure, supports explicit compensation, and cannot interrupt a running Blender step."""
        try:
            live_catalog, _ = _addon_schema_catalog(proxy, instance)
        except Exception as exc:
            return {"ok": False, "tool": "workflow.run", "data": {"executed": False, "steps": []}, "error": {"type": "WorkflowPreflightError", "message": str(exc), "traceback": ""}, "warnings": []}
        return run_workflow(
            proxy.call_named,
            steps=steps,
            execute=execute,
            rollback_on_failure=rollback_on_failure,
            confirm_rollback=confirm_rollback,
            instance=instance,
            idempotency_key=idempotency_key,
            catalog=live_catalog,
        )

    @mcp.tool(name="adapter.schema_audit")
    async def adapter_schema_audit(
        max_issues: Annotated[int, Field(ge=1, le=10000)] = 2000,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Compare authoritative Blender schemas with eager MCP field contracts."""
        try:
            addon_schemas, capability_digest = _addon_schema_catalog(proxy, instance)
            if resolved_tool_mode == "search":
                data = {
                    "comparison_mode": "search_authoritative",
                    "aligned": True,
                    "duplicated_schema_surface": False,
                    "addon_tool_count": len(addon_schemas),
                    "advertised_tool_count": len(_SEARCH_TOOLS),
                    "response_mode": resolved_response_mode,
                    "issue_count": 0,
                    "error_count": 0,
                    "warning_count": 0,
                    "issues": [],
                    "capability_digest": capability_digest,
                    "addon_schema_digest": schema_digest(addon_schemas),
                    "note": "Search mode advertises authoritative schemas through toolsets.describe instead of duplicating eager tool fields.",
                }
            else:
                local_schemas: dict[str, dict[str, Any]] = {}
                for tool in await mcp.list_tools():
                    record = tool.model_dump(by_alias=True, exclude_none=True)
                    name = record.get("name")
                    schema = record.get("inputSchema")
                    if isinstance(name, str) and isinstance(schema, dict):
                        local_schemas[name] = schema
                data = audit_schemas(
                    addon_schemas,
                    local_schemas,
                    mcp_only=_MCP_ONLY_TOOLS,
                    max_issues=max_issues,
                )
                data.update(
                    comparison_mode="eager_field_contracts",
                    duplicated_schema_surface=True,
                    capability_digest=capability_digest,
                    response_mode=resolved_response_mode,
                )
            return {
                "ok": True,
                "tool": "adapter.schema_audit",
                "data": data,
                "error": None,
                "warnings": [],
            }
        except Exception as exc:
            return {
                "ok": False,
                "tool": "adapter.schema_audit",
                "data": {"comparison_mode": resolved_tool_mode, "aligned": False},
                "error": {"type": type(exc).__name__, "message": str(exc), "traceback": ""},
                "warnings": [],
            }

    @mcp.tool(name="workflow.template")
    def workflow_template(
        action: Literal["list", "describe", "expand"],
        name: Annotated[str | None, Field(min_length=1, max_length=128)] = None,
        parameters: dict[str, Any] | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """List, describe, or expand a versioned data-only workflow template without calling Blender."""
        try:
            if action == "list":
                if name is not None or parameters:
                    raise WorkflowTemplateError("list does not accept name or parameters")
                data = {"action": action, **list_templates()}
            elif action == "describe":
                if name is None:
                    raise WorkflowTemplateError("describe requires name")
                if parameters:
                    raise WorkflowTemplateError("describe does not accept parameters")
                data = {"action": action, **describe_template(name)}
            else:
                if name is None:
                    raise WorkflowTemplateError("expand requires name")
                expansion = expand_template(name, parameters)
                data = {
                    "action": action,
                    **expansion,
                    "run_request": {
                        "steps": expansion["steps"],
                        "execute": False,
                        "instance": instance,
                        **expansion["run_options"],
                    },
                    "note": "Expansion is data-only. Review the steps, then call workflow.run explicitly; every underlying tool retains its own safety gates.",
                }
            return {
                "ok": True,
                "tool": "workflow.template",
                "data": data,
                "error": None,
                "warnings": [],
            }
        except WorkflowTemplateError as exc:
            return {
                "ok": False,
                "tool": "workflow.template",
                "data": {"action": action, "name": name, "expanded": False},
                "error": {"type": "WorkflowTemplateError", "message": str(exc), "traceback": ""},
                "warnings": [],
            }

    @mcp.tool(name="workflow.batch_plan")
    def workflow_batch_plan(
        jobs: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1,
                max_length=32,
                description="Explicitly attributed jobs containing {id, instance, steps} or {id, instance, template, parameters?}.",
            ),
        ],
        scheduling: Literal["serial", "parallel"] = "serial",
        failure_policy: Literal["stop", "continue"] = "stop",
    ) -> dict[str, Any]:
        """Build a bounded multi-instance batch manifest without contacting Blender or starting work."""
        try:
            data = plan_batch(
                jobs,
                scheduling=scheduling,
                failure_policy=failure_policy,
            )
            return {
                "ok": True,
                "tool": "workflow.batch_plan",
                "data": data,
                "error": None,
                "warnings": [],
            }
        except WorkflowBatchError as exc:
            return {
                "ok": False,
                "tool": "workflow.batch_plan",
                "data": {"planned": False, "job_count": 0},
                "error": {"type": "WorkflowBatchError", "message": str(exc), "traceback": ""},
                "warnings": [],
            }

    @mcp.tool(name="workflow.job_start")
    def workflow_job_start(
        steps: Annotated[
            list[dict[str, Any]],
            Field(min_length=1, max_length=32, description="Ordered resumable workflow steps."),
        ],
        instance: Annotated[
            str | int,
            Field(description="Required exact Blender registry id or loopback bridge port."),
        ],
        execute: Annotated[bool, Field(description="False returns a job plan without reserving state or calling Blender.")] = False,
        step_budget: Annotated[int, Field(ge=1, le=4)] = 1,
        rollback_on_failure: bool = False,
        rollback_on_cancel: bool = False,
        confirm_rollback: bool | None = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                description="Required for execute=true; binds job creation within this MCP process.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Plan or start a bounded process-local job that pauses after a small step budget."""
        return job_manager.start(
            proxy.call_named,
            steps=steps,
            instance=instance,
            execute=execute,
            step_budget=step_budget,
            rollback_on_failure=rollback_on_failure,
            rollback_on_cancel=rollback_on_cancel,
            confirm_rollback=confirm_rollback,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(name="workflow.job_control")
    def workflow_job_control(
        action: Literal["status", "resume", "cancel"],
        job_id: Annotated[str, Field(pattern=r"^workflow-job-[0-9]{8}$")],
        expected_revision: Annotated[int | None, Field(ge=0)] = None,
        step_budget: Annotated[int, Field(ge=1, le=4)] = 1,
        confirm_cancel: Literal[True] | None = None,
    ) -> dict[str, Any]:
        """Inspect, resume, or cooperatively cancel one process-local workflow job."""
        return job_manager.control(
            proxy.call_named,
            action=action,
            job_id=job_id,
            expected_revision=expected_revision,
            step_budget=step_budget,
            confirm_cancel=confirm_cancel,
        )

    @mcp.tool(name="toolsets.list")
    def toolsets_list(
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """List compact Blender capability domains before requesting full schemas."""
        envelope = proxy.system_capabilities(None, False, 200, None, instance)
        data = envelope.get("data")
        if isinstance(data, dict) and envelope.get("ok") is True:
            toolsets = data.get("toolsets")
            if isinstance(toolsets, list):
                toolsets.append({
                    "name": "adapter",
                    "tool_count": 11,
                    "description": "MCP-only instance routing, schema diagnostics, bounded workflows, chunked jobs, templates, and batch planning.",
                })
                toolsets.sort(key=lambda item: item.get("name", ""))
                offset = int(cursor) if cursor is not None else 0
                page = toolsets[offset : offset + limit]
                next_offset = offset + len(page)
                data["toolsets"] = page
                data["count"] = len(toolsets)
                data["truncated"] = next_offset < len(toolsets)
                data["next_cursor"] = str(next_offset) if next_offset < len(toolsets) else None
        return envelope

    @mcp.tool(name="toolsets.describe")
    def toolsets_describe(
        toolset: Annotated[str, Field(min_length=1)],
        include_schemas: bool = True,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Field(pattern=r"^[0-9]{1,18}$")] = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> dict[str, Any]:
        """Describe one capability domain and return its authoritative schemas."""
        if toolset == "adapter":
            tools: list[dict[str, Any]] = [
                {
                    "name": "session.instances",
                    "toolset": "adapter",
                    "description": "List healthy registered Blender instances.",
                    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                },
                {
                    "name": "session.call_log_summary",
                    "toolset": "adapter",
                    "description": "Summarize local adapter call logs.",
                    "input_schema": {"type": "object", "properties": {"since": {"type": "string"}, "instance": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}, "additionalProperties": False},
                },
                {
                    "name": "operation.start",
                    "toolset": "adapter",
                    "description": "Queue a supported long Blender operation.",
                    "input_schema": {"type": "object", "properties": {"tool": {"type": "string", "enum": ["python.exec", "render.preview", "render.review_set", "render.detail", "render.ortho_set", "render.reference_match", "render.contact_sheet", "scene.save_current", "scene.save_as", "bake.run"]}, "arguments": {"type": "object"}, "instance": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}, "required": ["tool", "arguments"], "additionalProperties": False},
                },
                {
                    "name": "operation.status",
                    "toolset": "adapter",
                    "description": "Read the result of a queued Blender operation.",
                    "input_schema": {"type": "object", "properties": {"operation_id": {"type": "string", "minLength": 1}, "instance": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}, "required": ["operation_id"], "additionalProperties": False},
                },
                {
                    "name": "workflow.run",
                    "toolset": "adapter",
                    "description": "Plan or execute at most 32 ordered tool calls with explicit rollback actions.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "steps": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object"}},
                            "execute": {"type": "boolean", "default": False},
                            "rollback_on_failure": {"type": "boolean", "default": False},
                            "confirm_rollback": {"type": "boolean"},
                            "idempotency_key": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 128,
                                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                            },
                        },
                        "required": ["steps"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "adapter.schema_audit",
                    "toolset": "adapter",
                    "description": "Compare authoritative Blender schemas with eager MCP field contracts.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "max_issues": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 2000},
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "workflow.template",
                    "toolset": "adapter",
                    "description": "List, describe, or expand versioned data-only workflow templates.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["list", "describe", "expand"]},
                            "name": {"type": "string", "minLength": 1, "maxLength": 128},
                            "parameters": {"type": "object"},
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "workflow.batch_plan",
                    "toolset": "adapter",
                    "description": "Build a bounded explicitly attributed multi-instance batch plan without executing it.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "jobs": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 32,
                                "items": {"type": "object"},
                            },
                            "scheduling": {
                                "type": "string",
                                "enum": ["serial", "parallel"],
                                "default": "serial",
                            },
                            "failure_policy": {
                                "type": "string",
                                "enum": ["stop", "continue"],
                                "default": "stop",
                            },
                        },
                        "required": ["jobs"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "workflow.job_start",
                    "toolset": "adapter",
                    "description": "Plan or start a bounded process-local workflow that pauses between chunks.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "steps": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 32,
                                "items": {"type": "object"},
                            },
                            "instance": {
                                "anyOf": [{"type": "string"}, {"type": "integer"}],
                            },
                            "execute": {"type": "boolean", "default": False},
                            "step_budget": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                            "rollback_on_failure": {"type": "boolean", "default": False},
                            "rollback_on_cancel": {"type": "boolean", "default": False},
                            "confirm_rollback": {"type": "boolean"},
                            "idempotency_key": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 128,
                                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                            },
                        },
                        "required": ["steps", "instance"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "workflow.job_control",
                    "toolset": "adapter",
                    "description": "Inspect, resume, or cooperatively cancel one process-local workflow job.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["status", "resume", "cancel"]},
                            "job_id": {"type": "string", "pattern": "^workflow-job-[0-9]{8}$"},
                            "expected_revision": {"type": "integer", "minimum": 0},
                            "step_budget": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                            "confirm_cancel": {"type": "boolean", "const": True},
                        },
                        "required": ["action", "job_id"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "response.artifact_get",
                    "toolset": "adapter",
                    "description": "Retrieve one exact bounded chunk from a process-local full response artifact.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "artifact_id": {
                                "type": "string",
                                "pattern": "^response-[0-9]{8}-[0-9a-f]{12}$",
                            },
                            "cursor": {
                                "anyOf": [{"type": "string", "pattern": "^[0-9]{1,18}$"}, {"type": "null"}],
                                "default": None,
                            },
                            "max_chars": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": MAX_RETRIEVAL_CHARS,
                                "default": DEFAULT_RETRIEVAL_CHARS,
                            },
                            "path": {"type": "string", "minLength": 1},
                        },
                        "required": ["artifact_id"],
                        "additionalProperties": False,
                    },
                },
            ]
            if not include_schemas:
                tools = [{key: value for key, value in item.items() if key != "input_schema"} for item in tools]
            return {"ok": True, "tool": "toolsets.describe", "data": {"kind": "tools", "toolset": "adapter", "count": len(tools), "tools": tools, "truncated": False, "next_cursor": None}, "error": None, "warnings": []}
        return proxy.system_capabilities(toolset, include_schemas, limit, cursor, instance)

    @mcp.tool(name="toolsets.call")
    def toolsets_call(
        tool: Annotated[str, Field(min_length=3, pattern=r"^[a-z_]+(?:\.[a-z_]+)+$")],
        arguments: dict[str, Any] | None = None,
        instance: _INSTANCE_SELECTOR = None,
    ) -> CallToolResult:
        """Call one exact capability after discovering its schema."""
        envelope = proxy.call_named(tool, arguments or {}, instance)
        if tool == "system.doctor":
            doctor_data = envelope.get("data")
            if isinstance(doctor_data, dict):
                adapter_version = _adapter_version()
                doctor_data["adapter"] = {
                    "version": adapter_version,
                    "tool_mode": resolved_tool_mode,
                    "response_mode": resolved_response_mode,
                    "source_tool_count": len(mcp.cbt_registered_tools),
                    "advertised_tool_count": len(_SEARCH_TOOLS) if resolved_tool_mode == "search" else len(mcp.cbt_registered_tools),
                }
                addon_release = _release_version(doctor_data.get("addon_version"))
                adapter_release = _release_version(adapter_version)
                if addon_release and adapter_release and addon_release != adapter_release:
                    problem = f"adapter {adapter_release} and add-on {addon_release} versions differ; restart clients and Blender after upgrading"
                    problems = doctor_data.get("problems")
                    if isinstance(problems, list):
                        problems.append(problem)
                    else:
                        doctor_data["problems"] = [problem]
                    doctor_data["healthy"] = False
                    warnings = envelope.get("warnings")
                    if isinstance(warnings, list):
                        warnings.append(f"Adapter {adapter_release} and add-on {addon_release} differ; restart Blender to load add-on {adapter_release}")
        data = envelope.get("data")
        paths: list[Any] = []
        if isinstance(data, dict):
            if tool in {"scene.screenshot", "scene.fast_capture", "render.preview", "render.detail", "render.contact_sheet"}:
                paths = [data.get("path")]
            elif tool in {"render.review_set", "render.ortho_set"}:
                paths = _review_paths(envelope)
            elif tool == "render.reference_match":
                paths = [data.get(key) for key in ("render_path", "side_by_side_path", "overlay_path")]
            elif tool == "camera.calibrate_from_reference":
                paths = [data.get("overlay_path")]
            elif tool == "render.contour_compare":
                paths = [data.get("deviation_path")]
            elif tool in {"pointcloud.section", "mesh.compare_to_points"}:
                paths = [data.get("image_path")]
            elif tool == "reference.crop_grid":
                paths = [data.get("path")]
            elif tool == "rig.rom_test":
                paths = [data.get("contact_sheet")]
            elif tool == "render.compare":
                paths = [data.get("diff_path", data.get("diff_output_path"))]
        return _render_result(envelope, paths)

    if resolved_tool_mode == "search":
        remove_tool = getattr(mcp, "remove_tool", None)
        if not callable(remove_tool):
            raise RuntimeError("Installed MCP SDK does not support search-mode tool removal; install mcp>=1.27,<2")
        for name in tuple(mcp.cbt_registered_tools):
            if name not in _SEARCH_TOOLS:
                remove_tool(name)

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
