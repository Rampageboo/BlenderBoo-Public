"""Import Camera Refinement Workbench JSON packages or COLMAP text models.

The workbench's Blender handoff is currently a cameras.json package. A direct
COLMAP text model is also accepted, with a declared or adjacent run kind.
Neither path replaces a solved intrinsic or pose with another run's values.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import median

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NameConflictError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token


_ALLOWED = {"package_dir", "images_dir", "transform_object", "collection_name",
            "dry_run", "import_points", "kind", "expected_state"}
_KINDS = {"reproduction", "constrained"}
_AXIS = (1.0, -1.0, -1.0)


def _finite(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidParamsError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise InvalidParamsError(f"{label} must be finite")
    return result


def _name(value, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InvalidParamsError(f"{label} must be a non-empty name")
    return value


def _image_name(value) -> str:
    name = _name(value, "image name")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or "\0" in name:
        raise InvalidParamsError(f"Unsafe image name: {name}")
    return name


def _kind(value) -> str:
    if value not in _KINDS:
        raise InvalidParamsError("Camera kind must be reproduction or constrained")
    return value


def _intrinsics(width, height, fx, fy, cx, cy) -> dict:
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError) as exc:
        raise InvalidParamsError("Camera width and height must be integers") from exc
    if not 1 <= width <= 100_000 or not 1 <= height <= 100_000:
        raise InvalidParamsError("Camera dimensions are out of bounds")
    fx, fy, cx, cy = (_finite(value, label) for value, label in
                      ((fx, "fx"), (fy, "fy"), (cx, "cx"), (cy, "cy")))
    if fx <= 0 or fy <= 0:
        raise InvalidParamsError("Camera focal length must be positive")
    if abs(fx - fy) > max(fx, fy) * 1e-6:
        raise InvalidParamsError("Blender camera import requires square pixels (fx = fy)")
    return {"width": width, "height": height, "fx": fx, "fy": fy,
            "cx": cx, "cy": cy, "lens_mm": fx * 36.0 / width,
            "shift_x": (width / 2 - cx) / width,
            "shift_y": (cy - height / 2) / width}


def _matrix(value, label: str, *, rigid: bool = True) -> list[list[float]]:
    if (not isinstance(value, list) or len(value) != 4 or
            any(not isinstance(row, list) or len(row) != 4 for row in value)):
        raise InvalidParamsError(f"{label} must be a 4x4 matrix")
    matrix = [[_finite(item, label) for item in row] for row in value]
    if any(abs(matrix[3][index] - (1.0 if index == 3 else 0.0)) > 1e-8
           for index in range(4)):
        raise InvalidParamsError(f"{label} must be affine")
    if rigid:
        rotation = [row[:3] for row in matrix[:3]]
        for i in range(3):
            for j in range(3):
                dot = sum(rotation[k][i] * rotation[k][j] for k in range(3))
                if abs(dot - (1.0 if i == j else 0.0)) > 1e-5:
                    raise InvalidParamsError(f"{label} rotation must be orthonormal")
        determinant = (rotation[0][0]*(rotation[1][1]*rotation[2][2]-rotation[1][2]*rotation[2][1])
                       - rotation[0][1]*(rotation[1][0]*rotation[2][2]-rotation[1][2]*rotation[2][0])
                       + rotation[0][2]*(rotation[1][0]*rotation[2][1]-rotation[1][1]*rotation[2][0]))
        if abs(determinant - 1.0) > 1e-5:
            raise InvalidParamsError(f"{label} rotation must be proper")
    return matrix


def _read_json(root: Path) -> dict:
    path = root / "cameras.json"
    if path.stat().st_size > 128 * 1024 * 1024:
        raise InvalidParamsError("cameras.json exceeds 128 MiB")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidParamsError(f"Cannot read cameras.json: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise InvalidParamsError("cameras.json must contain a non-empty camera list")
    cameras, seen = [], set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise InvalidParamsError(f"Camera record {index} must be an object")
        name = _image_name(item.get("name"))
        if name.casefold() in seen:
            raise InvalidParamsError(f"Duplicate image name: {name}")
        seen.add(name.casefold())
        intrinsic = _intrinsics(*(item.get(key) for key in
                                  ("width", "height", "fx", "fy", "cx", "cy")))
        matrix = _matrix(item.get("matrix_world"), f"{name} matrix_world")
        kind = _kind(item.get("kind"))
        run_id = _name(item.get("source_run"), "source_run")
        camera_frame = item.get("camera_frame", "source_world")
        if camera_frame != "source_world":
            raise InvalidParamsError(f"{name}: camera_frame must be source_world")
        metrics = item.get("metrics", {})
        if not isinstance(metrics, dict):
            raise InvalidParamsError(f"{name}: metrics must be an object")
        scores = {key: _finite(metrics[key], key) for key in
                  ("iou", "mean_boundary_px", "p95_boundary_px") if key in metrics}
        if "iou" in scores and not 0 <= scores["iou"] <= 1:
            raise InvalidParamsError(f"{name}: IoU must be in [0, 1]")
        if any(value < 0 for key, value in scores.items() if key != "iou"):
            raise InvalidParamsError(f"{name}: boundary error must be non-negative")
        if "lens_mm" in item and abs(_finite(item["lens_mm"], "lens_mm") -
                                     intrinsic["lens_mm"]) > 1e-4:
            raise InvalidParamsError(f"{name}: focal differs between pixels and millimeters")
        transform = item.get("mesh_transform")
        if transform is not None:
            _matrix(transform, f"{name} mesh_transform", rigid=False)
        cameras.append({**intrinsic, "name": name, "matrix_world": matrix,
                        "kind": kind, "source_run": run_id, "metrics": scores,
                        "image_id": item.get("source_image_id", index),
                        "camera_id": item.get("source_camera_id"),
                        "mesh_transform": transform, "camera_frame": camera_frame})
    if len({(camera["kind"], camera["source_run"]) for camera in cameras}) != 1:
        raise InvalidParamsError("One package must contain one camera kind and source run")
    if any(camera["mesh_transform"] != cameras[0]["mesh_transform"] for camera in cameras[1:]):
        raise InvalidParamsError("One package must use one mesh alignment transform")
    return {"format": "workbench_json", "cameras": cameras,
            "kind": cameras[0]["kind"], "run_id": cameras[0]["source_run"],
            "mesh_transform": cameras[0]["mesh_transform"]}


def _rotation_from_qvec(qvec: list[float]) -> list[list[float]]:
    if len(qvec) != 4:
        raise InvalidParamsError("COLMAP pose requires four quaternion values")
    w, x, y, z = (_finite(value, "quaternion") for value in qvec)
    length = math.sqrt(w*w + x*x + y*y + z*z)
    if length < 1e-12:
        raise InvalidParamsError("COLMAP quaternion is zero")
    w, x, y, z = (value / length for value in (w, x, y, z))
    return [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
            [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
            [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]


def _read_text(root: Path, requested_kind: str | None) -> dict:
    intrinsics = {}
    with (root / "cameras.txt").open("r", encoding="utf-8") as stream:
        for raw in stream:
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            parts = raw.split()
            if len(parts) < 7:
                raise InvalidParamsError("Malformed COLMAP cameras.txt row")
            try:
                camera_id, model, width, height = int(parts[0]), parts[1], int(parts[2]), int(parts[3])
            except ValueError as exc:
                raise InvalidParamsError("Malformed COLMAP cameras.txt ID or dimensions") from exc
            if camera_id in intrinsics:
                raise InvalidParamsError(f"Duplicate COLMAP camera ID: {camera_id}")
            values = [_finite(value, "camera parameter") for value in parts[4:]]
            if model == "PINHOLE" and len(values) == 4:
                fx, fy, cx, cy = values
            elif model == "SIMPLE_PINHOLE" and len(values) == 3:
                fx, cx, cy = values
                fy = fx
            else:
                raise InvalidParamsError(f"Unsupported COLMAP model {model}; use undistorted PINHOLE or SIMPLE_PINHOLE")
            intrinsics[camera_id] = _intrinsics(width, height, fx, fy, cx, cy)
    if not intrinsics:
        raise InvalidParamsError("cameras.txt has no cameras")
    run = {}
    run_root = None
    for candidate_root in (root, *list(root.parents)[:3]):
        candidate = candidate_root / "run.json"
        if candidate.is_file():
            try:
                possible = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise InvalidParamsError(f"Cannot read adjacent run.json: {exc}") from exc
            if isinstance(possible, dict) and possible.get("kind") in _KINDS:
                run, run_root = possible, candidate_root
    kind = _kind(requested_kind or run.get("kind"))
    if requested_kind and run.get("kind") and requested_kind != run["kind"]:
        raise InvalidParamsError("Requested kind conflicts with run.json")
    run_id = _name(run.get("id") or run_root.name if run_root else root.name, "run ID")
    mesh_transform = run.get("mesh_transform") or run.get("mesh_output_to_euclidean")
    if mesh_transform is None and run.get("mesh_output_to_euclidean_translation") is not None:
        shift = run["mesh_output_to_euclidean_translation"]
        if not isinstance(shift, list) or len(shift) != 3:
            raise InvalidParamsError("mesh_output_to_euclidean_translation must have three values")
        mesh_transform = [[1.0, 0.0, 0.0, _finite(shift[0], "mesh shift")],
                          [0.0, 1.0, 0.0, _finite(shift[1], "mesh shift")],
                          [0.0, 0.0, 1.0, _finite(shift[2], "mesh shift")],
                          [0.0, 0.0, 0.0, 1.0]]
    if mesh_transform is not None:
        mesh_transform = _matrix(mesh_transform, "mesh_transform", rigid=False)
    cameras, seen, seen_ids = [], set(), set()
    with (root / "images.txt").open("r", encoding="utf-8") as stream:
        while True:
            raw = stream.readline()
            if not raw:
                break
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            parts = raw.strip().split(maxsplit=9)
            if len(parts) != 10:
                raise InvalidParamsError("Malformed COLMAP images.txt pose row")
            try:
                image_id = int(parts[0])
                camera_id = int(parts[8])
            except ValueError as exc:
                raise InvalidParamsError("Malformed COLMAP image or camera ID") from exc
            rotation = _rotation_from_qvec(parts[1:5])
            translation = [_finite(value, "translation") for value in parts[5:8]]
            name = _image_name(parts[9])
            if camera_id not in intrinsics:
                raise InvalidParamsError(f"Unknown COLMAP camera ID {camera_id} for {name}")
            if name.casefold() in seen:
                raise InvalidParamsError(f"Duplicate image name: {name}")
            if image_id in seen_ids:
                raise InvalidParamsError(f"Duplicate COLMAP image ID: {image_id}")
            seen.add(name.casefold())
            seen_ids.add(image_id)
            transpose = [[rotation[j][i] for j in range(3)] for i in range(3)]
            center = [-sum(transpose[i][j] * translation[j] for j in range(3)) for i in range(3)]
            matrix = [[transpose[i][j] * _AXIS[j] for j in range(3)] + [center[i]]
                      for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
            cameras.append({**intrinsics[camera_id], "name": name, "matrix_world": matrix,
                            "kind": kind, "source_run": run_id, "metrics": {},
                            "image_id": image_id, "camera_id": camera_id,
                            "mesh_transform": mesh_transform,
                            "camera_frame": "source_world"})
            if not stream.readline():
                raise InvalidParamsError(f"Missing COLMAP observations line for {name}")
    if not cameras:
        raise InvalidParamsError("images.txt has no registered cameras")
    return {"format": "colmap_text", "cameras": cameras, "kind": kind,
            "run_id": run_id, "mesh_transform": cameras[0]["mesh_transform"]}


def parse_camera_package(package_dir: str | Path, images_dir: str | Path | None = None,
                         kind: str | None = None) -> dict:
    """Parse camera pose/intrinsics and optional score metadata without bpy."""
    root = Path(package_dir)
    if not root.is_absolute() or not root.is_dir():
        raise InvalidParamsError("package_dir must be an existing absolute directory")
    if kind is not None:
        _kind(kind)
    if (root / "cameras.txt").is_file() and (root / "images.txt").is_file():
        result = _read_text(root, kind)
        if (root / "cameras.json").is_file():
            metadata = _read_json(root)
            by_name = {camera["name"]: camera for camera in metadata["cameras"]}
            if set(by_name) != {camera["name"] for camera in result["cameras"]}:
                raise InvalidParamsError("COLMAP text and cameras.json have different image sets")
            if metadata["kind"] != result["kind"]:
                raise InvalidParamsError("COLMAP text and cameras.json have different camera kinds")
            for camera in result["cameras"]:
                other = by_name[camera["name"]]
                if (max(abs(camera[key] - other[key]) for key in ("fx", "fy", "cx", "cy")) > 1e-5 or
                        max(abs(camera["matrix_world"][i][j] - other["matrix_world"][i][j])
                            for i in range(4) for j in range(4)) > 1e-5):
                    raise InvalidParamsError(f"COLMAP text and metadata disagree for {camera['name']}")
                camera["metrics"] = other["metrics"]
                camera["source_run"] = other["source_run"]
            result["run_id"] = metadata["run_id"]
    elif (root / "cameras.json").is_file():
        result = _read_json(root)
        if kind is not None and kind != result["kind"]:
            raise InvalidParamsError("Requested kind conflicts with cameras.json")
    else:
        raise InvalidParamsError("Package needs cameras.json or COLMAP cameras.txt and images.txt")
    image_root = None
    if images_dir is not None:
        image_root = Path(images_dir)
        if not image_root.is_absolute() or not image_root.is_dir():
            raise InvalidParamsError("images_dir must be an existing absolute directory")
    missing = []
    if image_root is not None:
        for camera in result["cameras"]:
            image = (image_root / camera["name"]).resolve()
            if image_root.resolve() not in image.parents or not image.is_file():
                missing.append(camera["name"])
    scored = [camera["metrics"]["iou"] for camera in result["cameras"]
              if "iou" in camera["metrics"]]
    result.update(camera_count=len(result["cameras"]),
                  frame_range=[min(camera["name"] for camera in result["cameras"]),
                               max(camera["name"] for camera in result["cameras"])],
                  scored_camera_count=len(scored), median_iou=median(scored) if scored else None,
                  missing_image_count=len(missing), missing_images=missing[:20],
                  images_dir=str(image_root.resolve()) if image_root else None,
                  package_dir=str(root.resolve()))
    return result


def _transform_object(bpy, name: str | None):
    from mathutils import Matrix

    if name is None:
        return Matrix.Identity(4)
    obj = bpy.context.scene.objects.get(_name(name, "transform_object"))
    if obj is None:
        raise NotFoundError(f"transform_object not found in active scene: {name}")
    transform = obj.matrix_world.copy()
    if any(not math.isfinite(float(value)) for row in transform for value in row):
        raise InvalidStateError("transform_object has a non-finite world transform")
    if any(abs(float(transform[3][i]) - (1.0 if i == 3 else 0.0)) > 1e-7 for i in range(4)):
        raise InvalidStateError("transform_object must be affine")
    basis = transform.to_3x3()
    columns = [basis.col[i].copy() for i in range(3)]
    scale = sum(column.length for column in columns) / 3.0
    if scale <= 1e-9 or any(abs(column.length / scale - 1.0) > 1e-5 for column in columns):
        raise InvalidStateError("transform_object requires nonzero uniform scale")
    if any(abs(columns[i].dot(columns[j])) / scale**2 > 1e-5
           for i in range(3) for j in range(i + 1, 3)):
        raise InvalidStateError("transform_object contains shear")
    if basis.determinant() <= 0:
        raise InvalidStateError("transform_object must not reflect the scene")
    return transform


def _point_vertices(path: Path) -> list[tuple[float, float, float]]:
    if not path.is_file():
        raise InvalidParamsError("import_points requires points3D.txt in the package")
    points = []
    with path.open("r", encoding="utf-8") as stream:
        for raw in stream:
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            parts = raw.split(maxsplit=4)
            if len(parts) < 4:
                raise InvalidParamsError("Malformed COLMAP points3D.txt row")
            points.append(tuple(_finite(value, "point coordinate") for value in parts[1:4]))
    return points


def import_colmap_package(params: dict) -> ToolResult:
    """Preview or import a coupled camera solution in an isolated collection."""
    unknown = sorted(set(params) - _ALLOWED)
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {unknown}")
    package_dir = params.get("package_dir")
    if not isinstance(package_dir, str):
        raise InvalidParamsError("package_dir must be an absolute directory")
    dry_run = params.get("dry_run", True)
    import_points = params.get("import_points", False)
    if not isinstance(dry_run, bool) or not isinstance(import_points, bool):
        raise InvalidParamsError("dry_run and import_points must be boolean")
    parsed = parse_camera_package(package_dir, params.get("images_dir"), params.get("kind"))
    run_key = hashlib.sha1(parsed["run_id"].encode("utf-8")).hexdigest()[:8]
    collection_name = (_name(params["collection_name"], "collection_name")
                       if params.get("collection_name") is not None else
                       f"Workbench {parsed['kind']} {parsed['run_id'][:24]} {run_key}")
    vertices = _point_vertices(Path(package_dir) / "points3D.txt") if import_points else None
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    mapping = _transform_object(bpy, params.get("transform_object"))
    run_slug = "".join(char if char.isalnum() or char == "_" else "_"
                       for char in parsed["run_id"])[:24]
    names = [f"CBT_{run_slug}_{run_key}_{index:06d}"
             for index in range(1, parsed["camera_count"] + 1)]
    conflicts = [name for name in names if bpy.data.objects.get(name) or bpy.data.cameras.get(name)]
    if bpy.data.collections.get(collection_name):
        conflicts.append(collection_name)
    data = {key: parsed[key] for key in ("format", "package_dir", "kind", "run_id", "camera_count",
                                        "frame_range", "scored_camera_count", "median_iou",
                                        "missing_image_count", "missing_images", "images_dir")}
    data.update(dry_run=dry_run, changed=False, collection_name=collection_name,
                transform_object=params.get("transform_object"),
                import_points=import_points, point_count=len(vertices) if vertices is not None else None,
                name_conflicts=conflicts[:10], state_token=current_state_token(bpy))
    warnings = []
    if parsed["missing_image_count"]:
        warnings.append(f"{parsed['missing_image_count']} referenced images are missing")
    if dry_run:
        return ToolResult(data=data, warnings=warnings)
    if conflicts:
        raise NameConflictError("Camera or collection names already exist", data)
    require_document_write_access(bpy)
    from mathutils import Matrix

    scene = bpy.context.scene
    previous_camera = scene.camera
    previous_resolution = (scene.render.resolution_x, scene.render.resolution_y,
                           scene.render.resolution_percentage,
                           scene.render.pixel_aspect_x, scene.render.pixel_aspect_y)
    collection = None
    objects, created_cameras, created_meshes = [], [], []
    try:
        collection = bpy.data.collections.new(collection_name)
        scene.collection.children.link(collection)
        collection["cbt_camera_kind"] = parsed["kind"]
        collection["cbt_camera_run"] = parsed["run_id"]
        if parsed.get("mesh_transform") is not None:
            collection["cbt_mesh_transform_json"] = json.dumps(parsed["mesh_transform"])
        for name, record in zip(names, parsed["cameras"]):
            camera = bpy.data.cameras.new(name)
            created_cameras.append(camera)
            camera.type = "PERSP"
            camera.sensor_fit = "HORIZONTAL"
            camera.sensor_width = 36.0
            camera.lens = record["lens_mm"]
            camera.shift_x = record["shift_x"]
            camera.shift_y = record["shift_y"]
            obj = bpy.data.objects.new(name, camera)
            objects.append(obj)
            collection.objects.link(obj)
            obj.matrix_world = mapping @ Matrix(record["matrix_world"])
            obj["cbt_camera_kind"] = record["kind"]
            obj["cbt_camera_run"] = record["source_run"]
            obj["cbt_source_frame"] = record["name"]
            obj["cbt_image_size"] = [record["width"], record["height"]]
            obj["cbt_focal_px"] = record["fx"]
            obj["cbt_principal_px"] = [record["cx"], record["cy"]]
            obj["cbt_principal_offset_px"] = [record["cx"] - record["width"] / 2,
                                               record["cy"] - record["height"] / 2]
            obj["cbt_camera_frame"] = record["camera_frame"]
            if parsed["images_dir"]:
                obj["cbt_image_path"] = str(Path(parsed["images_dir"]) / record["name"])
            for source_key, prop in (("iou", "cbt_iou"),
                                     ("mean_boundary_px", "cbt_mean_boundary_px"),
                                     ("p95_boundary_px", "cbt_p95_boundary_px")):
                if source_key in record["metrics"]:
                    obj[prop] = record["metrics"][source_key]
        if vertices is not None:
            mesh = bpy.data.meshes.new(f"CBT_Points_{run_key}")
            created_meshes.append(mesh)
            mesh.from_pydata(vertices, [], [])
            mesh.update()
            point_object = bpy.data.objects.new(f"CBT_Points_{run_key}", mesh)
            objects.append(point_object)
            collection.objects.link(point_object)
            point_object.matrix_world = mapping
            point_object["cbt_camera_run"] = parsed["run_id"]
        first = parsed["cameras"][0]
        scene.camera = objects[0]
        scene.render.resolution_x = first["width"]
        scene.render.resolution_y = first["height"]
        scene.render.resolution_percentage = 100
        scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1.0
    except Exception as exc:
        cleanup_errors = []
        for obj in reversed(objects):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove object {obj.name}: {cleanup_exc}")
        for block in reversed(created_meshes):
            try:
                bpy.data.meshes.remove(block, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove mesh datablock {block.name}: {cleanup_exc}")
        for block in reversed(created_cameras):
            try:
                bpy.data.cameras.remove(block, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove camera datablock {block.name}: {cleanup_exc}")
        if collection is not None:
            try:
                bpy.data.collections.remove(collection, do_unlink=True)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"remove collection {collection.name}: {cleanup_exc}")
        try:
            scene.camera = previous_camera
            (scene.render.resolution_x, scene.render.resolution_y,
             scene.render.resolution_percentage, scene.render.pixel_aspect_x,
             scene.render.pixel_aspect_y) = previous_resolution
        except Exception as cleanup_exc:
            cleanup_errors.append(f"restore scene camera/resolution: {cleanup_exc}")
        if cleanup_errors:
            bump_epoch()
        data.update(changed=bool(cleanup_errors), rollback_errors=cleanup_errors,
                    state_token=current_state_token(bpy))
        raise MutationApplyError(f"camera.import_colmap_package failed: {exc}", data) from exc
    bump_epoch()
    data.update(dry_run=False, changed=True, created_camera_count=len(names),
                created_camera_names=names[:10],
                render_resolution=[first["width"], first["height"]],
                active_camera=objects[0].name, state_token=current_state_token(bpy))
    return ToolResult(data=data, warnings=warnings)
