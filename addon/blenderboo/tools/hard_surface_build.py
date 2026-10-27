"""Bounded hard-surface construction, clearance planning and radial fair fitting.

Unsupported geometry is rejected before document writes. Constructors create
fresh objects with an integer FACE part attribute; no source is replaced.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from functools import wraps
import math
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NameConflictError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .asset_source_compare import _mesh_data
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token

_MAX_FACES = 20000
_COMMON = {"name", "part_attribute", "part_id", "expected_state"}


class _Unsupported(Exception):
    pass


def _handler(allowed):
    def decorate(fn):
        @wraps(fn)
        def call(params):
            unknown = set(params) - allowed
            if unknown:
                raise InvalidParamsError("Unknown parameters: " + ", ".join(sorted(unknown)))
            try:
                return fn(params)
            except _Unsupported as exc:
                return ToolResult(data={"changed": False, "status": "unsupported", "reason": str(exc)})
        return call
    return decorate


def _num(value, name, minimum=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidParamsError(f"{name} must be finite")
    if minimum is not None and (value <= minimum if positive else value < minimum):
        raise InvalidParamsError(f"{name} must be {'greater than' if positive else 'at least'} {minimum}")
    return float(value)


def _int(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidParamsError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _bool(value, name):
    if not isinstance(value, bool):
        raise InvalidParamsError(f"{name} must be boolean")
    return value


def _name(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 63:
        raise InvalidParamsError(f"{name} must be a non-empty name up to 63 characters")
    return value


def _vec(value, name, size=3):
    import numpy as np
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise InvalidParamsError(f"{name} must have {size} coordinates")
    return np.array([_num(v, name) for v in value], dtype=float)


def _unit(value, name):
    import numpy as np
    length = np.linalg.norm(value)
    if not math.isfinite(length) or length < 1e-12:
        raise _Unsupported(f"{name} has zero or non-finite length")
    return value / length


def _common(params):
    attribute = _name(params.get("part_attribute", "part_id"), "part_attribute")
    if attribute in {"position", "sharp_edge", ".corner_vert", ".edge_verts"} or attribute.startswith("."):
        raise InvalidParamsError("part_attribute must not use a built-in attribute name")
    return (_name(params.get("name"), "name"), attribute,
            _int(params.get("part_id", 0), "part_id", 0, 2147483647))


def _quad_caps(ring, reverse=False):
    if len(ring) % 2:
        raise _Unsupported("All-quad end caps require an even ring vertex count")
    faces = [(ring[0], ring[i], ring[i + 1], ring[i + 2]) for i in range(1, len(ring) - 2, 2)]
    return [face[::-1] if reverse else face for face in faces]


def _disk_grid_cap(vertices, ring, reverse=False):
    """Fill a circular rim with a mapped quad grid and four rim corners."""
    import numpy as np

    count = len(ring)
    if count < 4 or count % 2:
        raise _Unsupported("All-quad disk caps require an even ring of at least four vertices")
    nx = max(1, count // 4)
    ny = count // 2 - nx
    bottom = ring[:nx + 1]
    right = ring[nx:nx + ny + 1]
    top = list(reversed(ring[nx + ny:2 * nx + ny + 1]))
    left = list(reversed(ring[2 * nx + ny:] + [ring[0]]))
    grid = {}
    for i, index in enumerate(bottom):
        grid[i, 0] = index
    for j, index in enumerate(right):
        grid[nx, j] = index
    for i, index in enumerate(top):
        grid[i, ny] = index
    for j, index in enumerate(left):
        grid[0, j] = index
    points = np.asarray(vertices, dtype=float)

    def sample(indices, t):
        position = t * (len(indices) - 1)
        low = min(int(position), len(indices) - 2)
        fraction = position - low
        return points[indices[low]] * (1 - fraction) + points[indices[low + 1]] * fraction

    p00, p10 = points[bottom[0]], points[bottom[-1]]
    p01, p11 = points[top[0]], points[top[-1]]
    for j in range(1, ny):
        v = j / ny
        for i in range(1, nx):
            u = i / nx
            bilinear = (p00 * (1-u) * (1-v) + p10 * u * (1-v)
                        + p01 * (1-u) * v + p11 * u * v)
            point = (sample(bottom, u) * (1-v) + sample(top, u) * v
                     + sample(left, v) * (1-u) + sample(right, v) * u - bilinear)
            grid[i, j] = len(vertices)
            vertices.append(tuple(point))
    faces = [(grid[i, j], grid[i+1, j], grid[i+1, j+1], grid[i, j+1])
             for j in range(ny) for i in range(nx)]
    return [face[::-1] if reverse else face for face in faces]


def _bridge(ring_a, ring_b):
    return [(ring_a[j], ring_a[(j + 1) % len(ring_a)], ring_b[(j + 1) % len(ring_b)], ring_b[j]) for j in range(len(ring_a))]


def _topology(vertices, faces, closed):
    """Pure checks before any Blender allocation; no silent vertex welding."""
    import numpy as np
    if not faces or len(faces) > _MAX_FACES:
        raise _Unsupported(f"Generated mesh needs 1..{_MAX_FACES} faces")
    points = np.asarray(vertices, dtype=float)
    if not np.isfinite(points).all():
        raise _Unsupported("Generated mesh has non-finite coordinates")
    scale = max(float(np.ptp(points, axis=0).max()), 1e-9)
    edges = Counter()
    for face in faces:
        if len(face) != 4 or len(set(face)) != 4:
            raise _Unsupported("Generated topology is not all-quad")
        q = points[list(face)]
        normals = np.cross(np.roll(q, -1, axis=0) - q, np.roll(q, -2, axis=0) - np.roll(q, -1, axis=0))
        lengths = np.linalg.norm(normals, axis=1)
        if min(lengths) <= scale * scale * 1e-12:
            raise _Unsupported("Generated quad has a collapsed corner")
        normal = normals.sum(axis=0)
        if any(normals @ normal <= 0):
            raise _Unsupported("Generated quad folds or is concave; reduce bend, width, thickness or projection distortion")
        edges.update(tuple(sorted((face[j], face[(j + 1) % 4]))) for j in range(4))
    boundary = sum(n == 1 for n in edges.values())
    if any(n > 2 for n in edges.values()) or (closed and boundary):
        raise _Unsupported("Generated topology does not meet the requested manifold boundary condition")
    return {"vertices": len(vertices), "faces": len(faces), "quads": len(faces), "boundary_edges": boundary,
            "closed_manifold": boundary == 0, "all_quads": True}


def _validated_faces(vertices, faces, closed):
    """Detached BMesh normals and conservative non-adjacent triangle overlap veto."""
    import bmesh
    from mathutils.bvhtree import BVHTree
    counts = _topology(vertices, faces, closed)
    bm = bmesh.new()
    try:
        verts = [bm.verts.new(v) for v in vertices]
        for face in faces:
            bm.faces.new([verts[i] for i in face])
        if closed and (any(not edge.is_manifold for edge in bm.edges) or any(not vertex.is_manifold for vertex in bm.verts)):
            raise _Unsupported("Generated vertex or edge fan is not manifold")
        bm.verts.index_update()
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        oriented = [tuple(v.index for v in face.verts) for face in bm.faces]
    finally:
        bm.free()
    triangles = [tri for a, b, c, d in oriented for tri in ((a, b, c), (a, c, d))]
    bvh = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
    for a, b in bvh.overlap(bvh):
        if a < b and not set(triangles[a]).intersection(triangles[b]):
            raise _Unsupported("Generated non-adjacent triangles overlap; reduce section size or simplify the outline/path")
    counts["non_adjacent_triangle_overlaps"] = 0
    return oriented, counts


def _create(params, vertices, faces, closed=True, sharp_edges=(), extra=None, face_regions=None):
    name, attribute, part_id = _common(params)
    if face_regions is not None and (attribute == "fregion" or len(face_regions) != len(faces)):
        raise InvalidParamsError("face region data must match faces and use a different name from part_attribute")
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if bpy.data.objects.get(name) is not None:
        raise NameConflictError(f"Object already exists: {name}", {"name": name})
    faces, counts = _validated_faces(vertices, faces, closed)
    mesh = obj = None
    try:
        mesh = bpy.data.meshes.new(name + "_Mesh")
        mesh.from_pydata(vertices, [], faces)
        mesh.update(calc_edges=True)
        mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
        edge_faces = defaultdict(list)
        for polygon in mesh.polygons:
            for edge in polygon.edge_keys:
                edge_faces[tuple(sorted(edge))].append(polygon.index)
        edge_set = {tuple(sorted(edge)) for edge in sharp_edges}
        smooth_limit = math.cos(math.radians(35))
        for edge, adjacent in edge_faces.items():
            if len(adjacent) == 2 and mesh.polygons[adjacent[0]].normal.dot(mesh.polygons[adjacent[1]].normal) < smooth_limit:
                edge_set.add(edge)
        part = mesh.attributes.new(attribute, "INT", "FACE")
        part.data.foreach_set("value", [part_id] * len(mesh.polygons))
        if face_regions is not None:
            region = mesh.attributes.new("fregion", "INT", "FACE")
            region.data.foreach_set("value", face_regions)
        if edge_set:
            sharp = mesh.attributes.new("sharp_edge", "BOOLEAN", "EDGE")
            sharp.data.foreach_set("value", [tuple(sorted(edge.vertices)) in edge_set for edge in mesh.edges])
        counts["smooth_faces"] = len(mesh.polygons)
        counts["sharp_edges"] = len(edge_set)
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(obj)
    except Exception as exc:
        cleanup = []
        try:
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        except Exception as rollback:
            cleanup.append(str(rollback))
        if cleanup:
            bump_epoch()
        raise MutationApplyError(f"Construction failed: {exc}", {"changed": bool(cleanup), "cleanup_errors": cleanup,
                                                                "state_token": current_state_token(bpy)}) from exc
    bump_epoch()
    return ToolResult(data={"changed": True, "status": "built", "object_name": obj.name,
                            "part_attribute": attribute, "part_id": part_id, "counts": counts,
                            "state_token": current_state_token(bpy), **(extra or {})})


def _fillet_profile(raw, closed, segments):
    import numpy as np
    if not isinstance(raw, list) or not 2 <= len(raw) <= 128:
        raise InvalidParamsError("profile needs 2..128 point objects")
    points, radii, sharp = [], [], []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"radius", "z", "sharp", "round_radius"}:
            raise InvalidParamsError("profile points accept radius, z, sharp, round_radius")
        points.append(np.array([_num(item.get("radius"), "radius", 0, True), _num(item.get("z"), "z")]))
        radii.append(_num(item.get("round_radius", 0), "round_radius", 0))
        sharp.append(_bool(item.get("sharp", False), "sharp"))
        if sharp[-1] and radii[-1] > 0:
            raise InvalidParamsError("A profile corner cannot be both sharp and rounded")
    if closed and len(points) < 3:
        raise InvalidParamsError("Closed profiles need at least three points")
    trims = [0.] * len(points)
    arcs = {}
    for i, point in enumerate(points):
        if radii[i] == 0:
            continue
        if not closed and i in {0, len(points) - 1}:
            raise _Unsupported("Endpoint fillets need an adjoining profile edge")
        u = _unit(point - points[i - 1], "profile edge")
        v = _unit(points[(i + 1) % len(points)] - point, "profile edge")
        turn = math.atan2(u[0] * v[1] - u[1] * v[0], float(u @ v))
        if abs(turn) < 1e-8 or abs(turn) > math.pi - 1e-5:
            raise _Unsupported("Rounded profile corner must have a finite nonzero turn")
        trim = radii[i] * math.tan(abs(turn) * .5)
        trims[i] = trim
        start = point - u * trim
        center = start + np.array([-u[1], u[0]]) * math.copysign(radii[i], turn)
        angle = math.atan2(*(start - center)[::-1])
        arcs[i] = [center + radii[i] * np.array([math.cos(angle + turn * t / segments), math.sin(angle + turn * t / segments)]) for t in range(segments + 1)]
    for i in range(len(points) if closed else len(points) - 1):
        j = (i + 1) % len(points)
        length = float(np.linalg.norm(points[j] - points[i]))
        if length <= trims[i] + trims[j] + 1e-10:
            raise _Unsupported("Profile fillets overlap or profile has duplicate points")
    result, flags = [], []
    for i, point in enumerate(points):
        samples = arcs.get(i, [point])
        result.extend(samples)
        flags.extend([sharp[i] and i not in arcs] * len(samples))
    if min(p[0] for p in result) <= 0:
        raise _Unsupported("Profile fillet crosses the revolution axis")
    return np.array(result), flags


def _lathe_geometry(params):
    import numpy as np
    closed = _bool(params.get("closed_profile", False), "closed_profile")
    caps = _bool(params.get("cap_ends", True), "cap_ends")
    segments = _int(params.get("segments", 32), "segments", 4, 512)
    if caps and not closed and segments % 2:
        raise _Unsupported("Capped all-quad lathe needs an even segment count")
    profile, flags = _fillet_profile(params.get("profile"), closed, _int(params.get("fillet_segments", 4), "fillet_segments", 1, 32))
    if caps and not closed:
        dz = np.diff(profile[:, 1])
        if not ((dz > 1e-10).all() or (dz < -1e-10).all()):
            raise _Unsupported("Capped open lathe profiles must be strictly monotone in Z; use closed_profile for hollow/returning sections")
    cap_faces = 2 * max(1, segments // 4) * (segments // 2 - max(1, segments // 4)) if caps and not closed else 0
    side_faces = (len(profile) if closed else len(profile) - 1) * segments
    if side_faces + cap_faces > _MAX_FACES:
        raise _Unsupported("Lathe exceeds the 20000-face construction budget")
    angles = np.arange(segments) * 2 * math.pi / segments
    vertices = [(float(r * math.cos(t)), float(r * math.sin(t)), float(z)) for r, z in profile for t in angles]
    rings = [list(range(i * segments, (i + 1) * segments)) for i in range(len(profile))]
    faces = []
    for i in range(len(rings) if closed else len(rings) - 1):
        faces.extend(_bridge(rings[i], rings[(i + 1) % len(rings)]))
    if caps and not closed:
        faces.extend(_disk_grid_cap(vertices, rings[0], True))
        faces.extend(_disk_grid_cap(vertices, rings[-1]))
    sharp = [(ring[j], ring[(j + 1) % segments]) for ring, flag in zip(rings, flags) if flag for j in range(segments)]
    if caps and not closed:
        sharp.extend((ring[j], ring[(j + 1) % segments]) for ring in (rings[0], rings[-1]) for j in range(segments))
    return vertices, faces, closed or caps, sharp


@_handler(_COMMON | {"profile", "segments", "closed_profile", "cap_ends", "fillet_segments"})
def build_lathe(params: dict[str, Any]) -> ToolResult:
    vertices, faces, closed, sharp = _lathe_geometry(params)
    return _create(params, vertices, faces, closed, sharp,
                   {"axis": "world Z", "cap_topology": "disk-mapped quad grid; four deliberate rim poles per cap", "closed_profile": params.get("closed_profile", False),
                    "status": "built" if closed else "partial", "limitation": None if closed else "Uncapped lathe has intentional boundary edges"})


def _transport(u, incoming, outgoing):
    import numpy as np
    axis = np.cross(incoming, outgoing)
    cosine = float(incoming @ outgoing)
    if cosine < -.95:
        raise _Unsupported("Sweep turn is too sharp for a bounded miter")
    return u + np.cross(axis, u) + np.cross(axis, np.cross(axis, u)) / (1 + cosine)


def _section(width, height, chamfer):
    w, h = width * .5, height * .5
    if chamfer == 0:
        return [(-w, -h), (w, -h), (w, h), (-w, h)]
    c = chamfer
    if c >= min(w, h):
        raise _Unsupported("Chamfer must be less than half width and height")
    return [(-w + c, -h), (w - c, -h), (w, -h + c), (w, h - c),
            (w - c, h), (-w + c, h), (-w, h - c), (-w, -h + c)]


def _sweep_geometry(params):
    import numpy as np
    raw = params.get("path")
    closed = _bool(params.get("closed", False), "closed")
    if not isinstance(raw, list) or not (3 if closed else 2) <= len(raw) <= 512:
        raise InvalidParamsError("path needs 2..512 points (3 minimum when closed)")
    points, sections = [], []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"point", "width", "height", "chamfer"}:
            raise InvalidParamsError("path points accept point, width, height, chamfer")
        points.append(_vec(item.get("point"), "point"))
        sections.append(_section(_num(item.get("width"), "width", 0, True), _num(item.get("height"), "height", 0, True),
                                 _num(item.get("chamfer", 0), "chamfer", 0)))
    if len({len(section) for section in sections}) != 1:
        raise _Unsupported("Chamfer must be positive at every point or zero at every point to preserve quad connectivity")
    points = np.array(points)
    tangents = [_unit(points[(i + 1) % len(points)] - points[i], "path segment") for i in range(len(points) if closed else len(points) - 1)]
    up = _unit(_vec(params.get("up", [0, 0, 1]), "up"), "up")
    if closed and np.max(abs((points - points[0]) @ up)) > max(np.ptp(points, axis=0).max(), 1e-9) * 1e-7:
        raise _Unsupported("Closed sweeps require a planar path perpendicular to up; non-planar seam twist is unsupported")
    first = tangents[-1] if closed else tangents[0]
    u = _unit(np.cross(up, first), "up cross path (choose a nonparallel up)")
    vertices, rings = [], []
    for i, point in enumerate(points):
        incoming = tangents[(i - 1) % len(tangents)] if i > 0 or closed else tangents[0]
        outgoing = tangents[i % len(tangents)] if i < len(points) - 1 or closed else tangents[-1]
        if float(incoming @ outgoing) < -.95:
            raise _Unsupported("Sweep turn is too sharp for a bounded miter")
        tangent = _unit(incoming + outgoing, "miter")
        v = np.cross(incoming, u)
        ring = []
        for x, y in sections[i]:
            offset = u * x + v * y
            offset -= incoming * float(offset @ tangent) / float(incoming @ tangent)
            ring.append(len(vertices))
            vertices.append(tuple(point + offset))
        rings.append(ring)
        u = _unit(_transport(u, incoming, outgoing), "transported frame")
    faces = []
    for i in range(len(rings) if closed else len(rings) - 1):
        faces.extend(_bridge(rings[i], rings[(i + 1) % len(rings)]))
    if not closed:
        faces.extend(_quad_caps(rings[0], True))
        faces.extend(_quad_caps(rings[-1]))
    return vertices, faces


@_handler(_COMMON | {"path", "closed", "up"})
def build_sweep(params: dict[str, Any]) -> ToolResult:
    vertices, faces = _sweep_geometry(params)
    return _create(params, vertices, faces, extra={"miter": "incoming-section plane intersection", "cap_topology": "quad fan; deliberate cap poles"})


def _subdivide_quads(vertices, faces, steps):
    """Conforming bilinear patches, sharing edge nodes by topology, not rounding."""
    import numpy as np
    points = np.asarray(vertices, dtype=float)
    output, quads, nodes = [], [], {}
    for fi, face in enumerate(faces):
        a, b, c, d = face
        grid = {}
        for j in range(steps + 1):
            for i in range(steps + 1):
                u, v = i / steps, j / steps
                if i in {0, steps} and j in {0, steps}:
                    corner = {(0,0): a, (steps,0): b, (steps,steps): c, (0,steps): d}[(i,j)]
                    key = ("vertex", corner)
                elif j in {0, steps} or i in {0, steps}:
                    start, end, k = (a,b,i) if j == 0 else (d,c,i) if j == steps else (a,d,j) if i == 0 else (b,c,j)
                    key = ("edge", min(start,end), max(start,end), k if start < end else steps-k)
                else:
                    key = ("face", fi, i, j)
                if key not in nodes:
                    nodes[key] = len(output)
                    output.append(points[a]*(1-u)*(1-v) + points[b]*u*(1-v) + points[c]*u*v + points[d]*(1-u)*v)
                grid[i,j] = nodes[key]
        quads.extend((grid[i,j],grid[i+1,j],grid[i+1,j+1],grid[i,j+1]) for j in range(steps) for i in range(steps))
    return np.array(output), quads


def _plate_grid(params):
    import numpy as np
    raw = params.get("outline")
    if not isinstance(raw, list) or not 3 <= len(raw) <= 64:
        raise _Unsupported("Plate needs 3..64 strictly convex coplanar corners; holes and concave outlines are unsupported")
    dimensions = {len(point) if isinstance(point, (list,tuple)) else 0 for point in raw}
    if dimensions not in ({2}, {3}):
        raise InvalidParamsError("outline corners must all be XY pairs or all world XYZ triples")
    outline = np.array([_vec(point, "outline corner", next(iter(dimensions))) for point in raw])
    if dimensions == {2}:
        outline = np.column_stack((outline, np.zeros(len(outline))))
    normal = _unit(np.cross(outline[1] - outline[0], outline[-1] - outline[0]), "outline normal")
    scale = max(float(np.ptp(outline, axis=0).max()), 1e-9)
    if max(abs((outline-outline[0]) @ normal)) > scale * 1e-7:
        raise _Unsupported("Plate outline must be coplanar before projection")
    # Every non-endpoint must lie strictly inside every oriented edge half-plane.
    # This rejects self-crossing star polygons as well as concavity/collinearity.
    for i in range(len(outline)):
        direction = outline[(i+1)%len(outline)] - outline[i]
        for j in range(len(outline)):
            if j not in {i, (i+1)%len(outline)} and np.dot(np.cross(direction, outline[j]-outline[i]),normal) <= scale*scale*1e-12:
                raise _Unsupported("Plate outline must be ordered and strictly convex")
    raw_sub = params.get("subdivisions", [8, 8])
    if not isinstance(raw_sub, (list, tuple)) or len(raw_sub) != 2:
        raise InvalidParamsError("subdivisions must be [u, v]")
    nx, ny = [_int(v, "subdivision", 1, 64) for v in raw_sub]
    if len(outline) == 4:
        vertices = []
        for j in range(ny + 1):
            for i in range(nx + 1):
                u, v = i / nx, j / ny
                vertices.append(outline[0]*(1-u)*(1-v) + outline[1]*u*(1-v) + outline[2]*u*v + outline[3]*(1-u)*v)
        faces = [(j*(nx+1)+i,j*(nx+1)+i+1,(j+1)*(nx+1)+i+1,(j+1)*(nx+1)+i) for j in range(ny) for i in range(nx)]
        return np.array(vertices), faces, normal
    if nx != ny:
        raise _Unsupported("Non-four-sided outlines require equal u/v subdivisions for conforming shared quad edges")
    if 3*len(outline)*nx*ny*2 + 4*len(outline)*nx > _MAX_FACES:
        raise _Unsupported("General outline plate exceeds the construction budget; reduce subdivisions")
    # A convex fan is split into three quads per triangle. Shared edge midpoints
    # preserve a watertight patch for odd/even corner counts without axis poles.
    vertices = outline.tolist() + [outline.mean(axis=0).tolist()]
    center_id = len(outline)
    midpoints = {}
    faces = []
    def midpoint(a, b):
        key = tuple(sorted((a,b)))
        if key not in midpoints:
            midpoints[key] = len(vertices)
            vertices.append(((np.array(vertices[a])+vertices[b])*.5).tolist())
        return midpoints[key]
    for i in range(len(outline)):
        a,b,c = i,(i+1)%len(outline),center_id
        ab,bc,ca = midpoint(a,b),midpoint(b,c),midpoint(c,a)
        centroid = len(vertices)
        vertices.append(((np.array(vertices[a])+vertices[b]+vertices[c])/3).tolist())
        faces.extend(((a,ab,centroid,ca),(b,bc,centroid,ab),(c,ca,centroid,bc)))
    vertices, faces = _subdivide_quads(vertices, faces, nx)
    return vertices, faces, normal


def _thicken(vertices, faces, normals, thickness, open_back):
    import numpy as np
    if thickness == 0:
        return vertices.tolist(), faces
    n = len(vertices)
    result = np.concatenate((vertices, vertices - np.asarray(normals) * thickness)).tolist()
    output = list(faces)
    if not open_back:
        output += [tuple(i + n for i in face[::-1]) for face in faces]
    uses = Counter(tuple(sorted((face[i], face[(i+1)%4]))) for face in faces for i in range(4))
    for face in faces:
        for i in range(4):
            a, b = face[i], face[(i+1)%4]
            if uses[tuple(sorted((a,b)))] == 1:
                output.append((b, a, a+n, b+n))
    if open_back:
        used = sorted({i for face in output for i in face})
        mapping = {old: new for new, old in enumerate(used)}
        result = [result[i] for i in used]
        output = [tuple(mapping[i] for i in face) for face in output]
    return result, output


def _clear_projected_plate(vertices, faces, normals, thickness, open_back, target_bvh):
    """Move a projected patch outward until its triangulated shell clears target."""
    import numpy as np
    from mathutils.bvhtree import BVHTree

    outward = -np.asarray(normals)
    span = max(float(np.ptp(vertices, axis=0).max()), 1e-9)
    max_offset = max(thickness, span * .1)
    offset = 0.0

    def candidate(distance):
        shifted = vertices + outward * distance
        thick_vertices, thick_faces = _thicken(shifted, faces, normals, thickness, open_back)
        triangles = [tri for a, b, c, d in thick_faces for tri in ((a, b, c), (a, c, d))]
        plate_bvh = BVHTree.FromPolygons(thick_vertices, triangles, all_triangles=True, epsilon=0.0)
        return thick_vertices, thick_faces, not plate_bvh.overlap(target_bvh)

    failed_offset = 0.0
    for _ in range(24):
        thick_vertices, thick_faces, clear = candidate(offset)
        if clear:
            if offset:
                for _ in range(12):
                    middle = (failed_offset + offset) * .5
                    trial_vertices, trial_faces, trial_clear = candidate(middle)
                    if trial_clear:
                        offset = middle
                        thick_vertices, thick_faces = trial_vertices, trial_faces
                    else:
                        failed_offset = middle
            return thick_vertices, thick_faces, offset
        failed_offset = offset
        offset = max(span * 1e-6, 1e-8) if offset == 0 else offset * 2
        if offset > max_offset:
            break
    raise _Unsupported("Projected plate still intersects target within the bounded outward offset; increase subdivisions or simplify the outline")


@_handler(_COMMON | {"outline", "subdivisions", "thickness", "open_back", "target", "projection_direction", "max_projection_distance"})
def build_plate(params: dict[str, Any]) -> ToolResult:
    import numpy as np
    vertices, faces, normal = _plate_grid(params)
    thickness = _num(params.get("thickness", 0), "thickness", 0)
    open_back = _bool(params.get("open_back", False), "open_back")
    normals = np.tile(normal, (len(vertices), 1))
    projected = False
    if params.get("target") is not None:
        from mathutils import Vector
        bpy = get_bpy()
        target = bpy.context.scene.objects.get(_name(params["target"], "target"))
        if target is None:
            raise NotFoundError("Plate projection target not found")
        data = _mesh_data(target, bpy.context.evaluated_depsgraph_get())
        direction = _unit(_vec(params.get("projection_direction", [0, 0, -1]), "projection_direction"), "projection_direction")
        max_distance = _num(params.get("max_projection_distance", 10), "max_projection_distance", 0, True)
        for i, point in enumerate(vertices):
            hit, hit_normal, _, _ = data["bvh"].ray_cast(Vector(point), Vector(direction), max_distance)
            if hit is None:
                raise _Unsupported(f"Plate projection missed vertex {i}; no partial plate is created")
            vertices[i] = hit
            outward = np.array(hit_normal)
            if float(outward @ direction) > 0:
                outward = -outward
            normals[i] = -outward  # _thicken subtracts this vector: material grows away from the target.
        projected = True
    if projected:
        vertices, faces, outward_offset = _clear_projected_plate(
            vertices, faces, normals, thickness, open_back, data["bvh"])
    else:
        vertices, faces = _thicken(vertices, faces, normals, thickness, open_back)
        outward_offset = 0.0
    closed = thickness > 0 and not open_back
    return _create(params, vertices, faces, closed,
                   extra={"projection_complete": projected, "boundary_conformance": "exact convex polygon edge segments; projected along requested direction when target supplied",
                          "thickness_scene_units": thickness,
                          "thickness_direction": "away_from_target" if projected else "negative_outline_normal",
                          "projection_outward_offset_scene_units": outward_offset,
                          "open_back": open_back,
                          "status": "built" if closed else "partial", "limitation": None if closed else "Intentional surface/open back has boundary edges; not a closed game mesh"})


def _slab_geometry(params):
    """Plan a planar all-quad slab, optional paired opening and blind pocket."""
    import numpy as np

    def loop(value, name):
        if not isinstance(value, list) or not 4 <= len(value) <= 64 or len(value) % 2:
            raise _Unsupported(f"{name} needs 4..64 ordered vertices and an even count")
        lengths = {len(item) if isinstance(item, (list, tuple)) else 0 for item in value}
        if lengths not in ({2}, {3}):
            raise InvalidParamsError(f"{name} vertices must be all XY pairs or all XYZ triples")
        points = np.array([_vec(item, name, next(iter(lengths))) for item in value])
        return np.column_stack((points, np.zeros(len(points)))) if lengths == {2} else points

    outer = loop(params.get("outline"), "outline")
    inner = loop(params["opening"], "opening") if params.get("opening") is not None else None
    if inner is not None and len(inner) != len(outer):
        raise _Unsupported("outline and opening need matching ordered vertex counts for an all-quad band")
    thickness = _num(params.get("thickness"), "thickness", 0, True)
    depth = None if params.get("pocket_depth") is None else _num(params["pocket_depth"], "pocket_depth", 0, True)
    if depth is not None and (inner is None or depth > thickness):
        raise InvalidParamsError("pocket_depth requires opening and must not exceed thickness")
    normal = _unit(np.cross(outer[1] - outer[0], outer[-1] - outer[0]), "outline normal")
    span = max(float(np.ptp(outer, axis=0).max()), 1e-9)
    if max(abs((outer - outer[0]) @ normal)) > span * 1e-7 or (inner is not None and max(abs((inner - outer[0]) @ normal)) > span * 1e-7):
        raise _Unsupported("outline and opening must share one plane")
    u = _unit(outer[1] - outer[0], "outline edge")
    v = np.cross(normal, u)
    project = lambda points: np.stack(((points - outer[0]) @ u, (points - outer[0]) @ v), axis=-1)
    outer_xy = project(outer)
    inner_xy = project(inner) if inner is not None else None

    def cross(a, b, c):
        ab, ac = b-a, c-a
        return ab[0]*ac[1] - ab[1]*ac[0]

    def simple(points):
        n = len(points)
        area = sum(cross(np.zeros(2), points[i], points[(i+1) % n]) for i in range(n)) * .5
        if area <= span * span * 1e-10:
            return False
        for i in range(n):
            a, b = points[i], points[(i+1) % n]
            if np.linalg.norm(b-a) <= span * 1e-8:
                return False
            for j in range(i+1, n):
                if j in {i, (i+1) % n, (i-1) % n} or i == 0 and j == n-1:
                    continue
                c, d = points[j], points[(j+1) % n]
                if cross(a,b,c)*cross(a,b,d) < 0 and cross(c,d,a)*cross(c,d,b) < 0:
                    return False
        return True

    if not simple(outer_xy) or inner_xy is not None and not simple(inner_xy):
        raise _Unsupported("outline and opening must be simple nondegenerate loops with matching winding")
    if inner_xy is not None:
        for point in inner_xy:
            crossings = sum((a[1] > point[1]) != (b[1] > point[1]) and
                            point[0] < (b[0]-a[0])*(point[1]-a[1])/(b[1]-a[1])+a[0]
                            for a, b in zip(outer_xy, np.roll(outer_xy, -1, axis=0)))
            if crossings % 2 != 1:
                raise _Unsupported("opening must lie strictly inside outline")
    n = len(outer)
    vertices = [tuple(point) for point in outer]
    outer_top = list(range(n))
    inner_top = []
    if inner is not None:
        inner_top = list(range(len(vertices), len(vertices) + n))
        vertices.extend(tuple(point) for point in inner)
    outer_bottom = list(range(len(vertices), len(vertices) + n))
    vertices.extend(tuple(point - normal * thickness) for point in outer)
    inner_bottom = []
    if inner is not None:
        bottom_depth = thickness if depth is None or depth == thickness else depth
        inner_bottom = list(range(len(vertices), len(vertices) + n))
        vertices.extend(tuple(point - normal * bottom_depth) for point in inner)
    faces, regions = [], []

    def add(face, region):
        faces.append(face)
        regions.append(region)

    if inner is None:
        for face in _disk_grid_cap(vertices, outer_top):
            add(face, 0)
    else:
        for i in range(n):
            j = (i+1) % n
            add((outer_top[i], outer_top[j], inner_top[j], inner_top[i]), 0)
    for i in range(n):
        j = (i+1) % n
        add((outer_top[j], outer_top[i], outer_bottom[i], outer_bottom[j]), 1)
    if inner is not None:
        for i in range(n):
            j = (i+1) % n
            add((inner_top[i], inner_top[j], inner_bottom[j], inner_bottom[i]), 2)
    through = inner is not None and (depth is None or depth == thickness)
    if through:
        for i in range(n):
            j = (i+1) % n
            add((outer_bottom[i], inner_bottom[i], inner_bottom[j], outer_bottom[j]), 3)
    else:
        for face in _disk_grid_cap(vertices, outer_bottom, reverse=True):
            add(face, 3)
        if inner is not None:
            for face in _disk_grid_cap(vertices, inner_bottom):
                add(face, 4)
    return vertices, faces, regions, ("solid" if inner is None else "through" if through else "pocket"), thickness, depth


@_handler(_COMMON | {"outline", "opening", "thickness", "pocket_depth"})
def build_slab(params: dict[str, Any]) -> ToolResult:
    vertices, faces, regions, mode, thickness, depth = _slab_geometry(params)
    return _create(params, vertices, faces, True, extra={
        "opening_mode": mode, "thickness_scene_units": thickness,
        "thickness_direction": "negative_outline_normal",
        "pocket_depth_scene_units": depth, "face_region_attribute": "fregion",
        "face_region_ids": {"top": 0, "outer_wall": 1, "inner_wall": 2, "bottom": 3, "pocket_floor": 4},
    }, face_regions=regions)


def _fit_basis(theta, phi, order_m, order_k):
    import numpy as np
    angle = np.radians(theta - 90)
    elevation = np.sin(np.radians(phi))
    return np.stack([np.cos(m * angle) * elevation ** k for m in range(order_m + 1) for k in range(order_k + 1)], axis=-1)


def _robust_fit(basis, observed, iterations, sigma):
    import numpy as np
    valid = np.isfinite(observed)
    active = valid.copy()
    for _ in range(iterations):
        if int(active.sum()) < basis.shape[-1] * 2:
            raise _Unsupported("Not enough radial observations for the requested fair-fit basis")
        coefficients, _, rank, _ = np.linalg.lstsq(basis[active], observed[active], rcond=1e-10)
        if rank < basis.shape[-1]:
            raise _Unsupported("Radial fit is rank-deficient; reduce order or expand angular domain")
        residual = observed - basis @ coefficients
        median = float(np.median(residual[active]))
        mad = float(np.median(abs(residual[active] - median))) * 1.4826
        tolerance = max(mad * sigma, float(np.median(abs(observed[active]))) * 1e-8, 1e-10)
        updated = valid & (abs(residual - median) <= tolerance)
        if np.array_equal(updated, active):
            break
        active = updated
    if active.sum() < basis.shape[-1] * 2:
        raise _Unsupported("Robust rejection left insufficient radial support")
    coefficients, _, rank, _ = np.linalg.lstsq(basis[active], observed[active], rcond=1e-10)
    if rank < basis.shape[-1]:
        raise _Unsupported("Retained radial support is rank-deficient")
    return coefficients, basis @ coefficients, active


@_handler(_COMMON | {"source", "center", "theta_samples", "phi_samples", "phi_range", "order_m", "order_k", "iterations", "rejection_sigma", "apply", "thickness"})
def fair_fit(params: dict[str, Any]) -> ToolResult:
    import numpy as np
    source_name = _name(params.get("source"), "source")
    center = _vec(params.get("center"), "center")
    nt = _int(params.get("theta_samples", 72), "theta_samples", 8, 128)
    np_ = _int(params.get("phi_samples", 25), "phi_samples", 3, 64)
    phi_range = _vec(params.get("phi_range", [-60, 80]), "phi_range", 2)
    if not -90 < phi_range[0] < phi_range[1] < 90:
        raise _Unsupported("Fair-fit supports a nonpolar radial band with -90 < phi_min < phi_max < 90; polar caps are unsupported")
    order_m = _int(params.get("order_m", 4), "order_m", 0, 8)
    order_k = _int(params.get("order_k", 3), "order_k", 0, 7)
    iterations = _int(params.get("iterations", 6), "iterations", 1, 12)
    sigma = _num(params.get("rejection_sigma", 2.5), "rejection_sigma", 0, True)
    apply = _bool(params.get("apply", False), "apply")
    from mathutils import Vector
    bpy = get_bpy()
    source = bpy.context.scene.objects.get(source_name)
    if source is None:
        raise NotFoundError("Fair-fit source not found")
    data = _mesh_data(source, bpy.context.evaluated_depsgraph_get())
    theta, phi = np.arange(nt) * 360 / nt, np.linspace(*phi_range, np_)
    tg, pg = np.meshgrid(theta, phi)
    dirs = np.stack((np.cos(np.radians(pg))*np.cos(np.radians(tg)), np.cos(np.radians(pg))*np.sin(np.radians(tg)), np.sin(np.radians(pg))), axis=-1)
    corners = [(x,y,z) for x in (data["bounds"][0][0],data["bounds"][1][0]) for y in (data["bounds"][0][1],data["bounds"][1][1]) for z in (data["bounds"][0][2],data["bounds"][1][2])]
    radius = max(np.linalg.norm(np.array(corner) - center) for corner in corners) * 1.01
    if not math.isfinite(radius) or radius <= 0:
        raise _Unsupported("Fair-fit ray extent is invalid")
    observed = np.full((np_, nt), np.nan)
    for i in range(np_):
        for j in range(nt):
            direction = Vector(dirs[i,j])
            hit, _, _, distance = data["bvh"].ray_cast(Vector(center) + direction * radius, -direction, radius)
            if hit is not None:
                observed[i,j] = radius - distance
    coefficients, fitted, retained = _robust_fit(_fit_basis(tg, pg, order_m, order_k), observed, iterations, sigma)
    residual = observed - fitted
    valid = np.isfinite(observed)
    result = {"changed": False, "status": "analysis" if valid.all() else "partial", "source": source.name,
              "basis": "cos(m*(theta-90deg))*sin(phi)^k", "mirror_plane": "world X=center.x",
              "theta_deg": theta.tolist(), "phi_deg": phi.tolist(), "center": center.tolist(),
              "coefficients": coefficients.tolist(), "order_m": order_m, "order_k": order_k,
              "rays": nt*np_, "hits": int(valid.sum()), "coverage": float(valid.mean()),
              "retained_samples": int(retained.sum()), "rejected_samples": int((valid & ~retained).sum()),
              "fitted_radius_scene_units": fitted.tolist(),
              "residual_scene_units": [[float(v) if math.isfinite(v) else None for v in row] for row in residual],
              "residual_p95_scene_units": float(np.percentile(abs(residual[valid]),95)),
              "limitation": "Radial band only; no polar caps, concavity reconstruction, or automatic feature/part extraction"}
    if not apply:
        return ToolResult(data=result)
    if not valid.all():
        return ToolResult(data={**result, "status": "unsupported", "reason": "Missing rays: no fitted geometry is created over unobserved domain"})
    thickness = _num(params.get("thickness"), "thickness", 0, True)
    if not np.isfinite(fitted).all() or fitted.min() <= thickness:
        raise _Unsupported("Fitted radius is not positive or thickness reaches the center")
    vertices = center + dirs * fitted[:,:,None]
    normals = dirs.reshape(-1,3)
    faces = [(i*nt+j, i*nt+(j+1)%nt, (i+1)*nt+(j+1)%nt, (i+1)*nt+j) for i in range(np_-1) for j in range(nt)]
    vertices, faces = _thicken(vertices.reshape(-1,3), faces, normals, thickness, False)
    result.pop("changed")
    result.pop("status")
    return _create(params, vertices, faces, extra={**result, "radial_thickness_scene_units": thickness})


def _triangle_gap_certificate(triangles, distance_at, gap, epsilon, max_cells=50000, max_depth=14):
    """Bound distance everywhere using the 1-Lipschitz surface-distance property.

For any triangle point, distance differs from the centroid distance by at
most the triangle's centroid radius. Subdivision tightens the enclosure.
This is a continuous surface bound, not an assertion based on sampled extrema.
"""
    import numpy as np
    pending = [(np.asarray(tri,dtype=float),0) for tri in triangles]
    cells = 0
    lower, upper = float("inf"), 0.
    while pending:
        tri, depth = pending.pop()
        cells += 1
        if cells > max_cells:
            return {"status": "unresolved", "reason": "distance_certificate_cell_budget", "cells": cells-1}
        center = tri.mean(axis=0)
        distance = distance_at(center)
        if distance is None or not math.isfinite(distance):
            return {"status": "unresolved", "reason": "nearest_surface_unavailable", "cells": cells}
        if distance < gap[0]-epsilon or distance > gap[1]+epsilon:
            return {"status": "violated", "witness_world": center.tolist(), "distance_scene_units": float(distance), "cells": cells}
        radius = float(np.linalg.norm(tri-center,axis=1).max())
        lo, hi = max(0., distance-radius-epsilon), distance+radius+epsilon
        if lo >= gap[0] and hi <= gap[1]:
            lower,upper = min(lower,lo),max(upper,hi)
            continue
        if depth >= max_depth:
            return {"status": "unresolved", "reason": "distance_certificate_depth_budget", "cells": cells}
        lengths = [float(np.linalg.norm(tri[(i+1)%3]-tri[i])) for i in range(3)]
        i = int(np.argmax(lengths))
        a,b,c = tri[i],tri[(i+1)%3],tri[(i+2)%3]
        mid = (a+b)*.5
        pending.extend(((np.array((a,mid,c)),depth+1),(np.array((mid,b,c)),depth+1)))
    return {"status": "certified", "minimum_scene_units": lower, "maximum_scene_units": upper,
            "cells": cells, "method": "adaptive_centroid_radius_1_lipschitz", "numeric_margin_scene_units": epsilon}


def _closed_target_orientation(surface):
    """Require closed consistent topology and a non-self-crossing bounded target."""
    import numpy as np
    triangles = surface["triangles"]
    if len(triangles) > 100000:
        raise _Unsupported("Curved clearance target exceeds 100000 triangles; use a validated lower-resolution target")
    edges, signed = Counter(), Counter()
    for a,b,c in triangles:
        for u,v in ((a,b),(b,c),(c,a)):
            key = (min(int(u),int(v)),max(int(u),int(v)))
            edges[key] += 1
            signed[key] += 1 if u < v else -1
    if any(count != 2 for count in edges.values()) or any(signed.values()):
        raise _Unsupported("Nonplanar clearance target must be closed with consistent face winding")
    for a,b in surface["bvh"].overlap(surface["bvh"]):
        if a < b and not set(triangles[a]).intersection(triangles[b]):
            raise _Unsupported("Curved clearance target has non-adjacent triangle intersections")
    points = surface["vertices"][triangles]
    shifted = points - points.reshape(-1,3).mean(axis=0)
    volume6 = float(np.einsum("ij,ij->i",shifted[:,0],np.cross(shifted[:,1],shifted[:,2])).sum())
    if not math.isfinite(volume6) or abs(volume6) < max(float(np.ptp(points.reshape(-1,3),axis=0).max()),1e-9)**3 * 1e-12:
        raise _Unsupported("Curved target has no reliable enclosed volume")
    return 1. if volume6 > 0 else -1.


def _outside_closed_surface(point, triangles):
    """Solid-angle winding gives an outside witness for a connected disjoint part."""
    import numpy as np
    vectors = triangles - point
    a,b,c = vectors[:,0],vectors[:,1],vectors[:,2]
    la,lb,lc = (np.linalg.norm(v,axis=1) for v in (a,b,c))
    numerator = np.einsum("ij,ij->i",a,np.cross(b,c))
    denominator = la*lb*lc + np.einsum("ij,ij->i",a,b)*lc + np.einsum("ij,ij->i",b,c)*la + np.einsum("ij,ij->i",c,a)*lb
    winding = abs(float((2*np.arctan2(numerator,denominator)).sum()))/(4*math.pi)
    if winding < 1e-4:
        return True
    if abs(winding-1) < 1e-4:
        return False
    return None


def _part_representatives(vertex_count, faces):
    roots = list(range(vertex_count))
    def root(i):
        while roots[i] != i:
            roots[i] = roots[roots[i]]
            i = roots[i]
        return i
    for face in faces:
        for i in face[1:]:
            roots[root(i)] = root(face[0])
    representatives = sorted({root(i) for i in range(vertex_count)})
    if len(representatives) > 32:
        raise _Unsupported("Curved clearance supports at most 32 selected connected components")
    return representatives


def _curved_clearance(world, faces, triangles, surface, gap, step, max_iterations):
    import numpy as np
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    orientation = _closed_target_orientation(surface)
    target_triangles = surface["vertices"][surface["triangles"]]
    if not len(triangles):
        raise _Unsupported("Selected part has no triangular surface")
    representatives = _part_representatives(len(world),faces)
    extent = max(float(np.ptp(surface["vertices"],axis=0).max()),float(np.ptp(world,axis=0).max()),1e-9)
    magnitude = max(float(abs(world).max()),float(abs(surface["vertices"]).max()),extent)
    epsilon = magnitude*2e-6
    if gap[1]-gap[0] <= epsilon*4:
        raise _Unsupported("Requested gap interval is narrower than the float mesh clearance margin")
    offset = np.zeros(3)
    reason = "No certified lift within iteration bounds"
    unresolved = False
    for iteration in range(max_iterations+1):
        lifted = world+offset
        lifted_triangles = triangles+offset
        tree = BVHTree.FromPolygons(lifted_triangles.reshape(-1,3).tolist(),
                                    [(3*i,3*i+1,3*i+2) for i in range(len(triangles))], all_triangles=True)
        overlaps = len(tree.overlap(surface["bvh"]))
        samples = np.concatenate((lifted,(triangles+offset).mean(axis=1)))
        signed, distances, normals = [],[],[]
        for point in samples:
            hit,normal,_,distance = surface["bvh"].find_nearest(Vector(point))
            if hit is None or distance is None:
                raise _Unsupported("Curved target nearest-surface query failed")
            outward = np.asarray(normal)*orientation
            signed.append(float((point-np.asarray(hit)) @ outward))
            distances.append(float(distance))
            normals.append(outward)
        signed,distances,normals = np.array(signed),np.array(distances),np.array(normals)
        if overlaps == 0:
            outside = [_outside_closed_surface(lifted[index],target_triangles) for index in representatives]
            if any(value is None for value in outside):
                raise _Unsupported("Curved target outside classification is numerically ambiguous")
            if all(outside):
                def distance_at(point):
                    return surface["bvh"].find_nearest(Vector(point))[3]
                certificate = _triangle_gap_certificate(triangles+offset,distance_at,gap,epsilon)
                if certificate["status"] == "certified":
                    return offset, {**certificate,"intersection_pairs":0,"iterations":iteration,
                                    "outside_components":len(representatives),"target_closed":True}, None
                if certificate["status"] == "unresolved":
                    unresolved = True
                    reason = certificate["reason"]
                if distances.max() > gap[1]+epsilon:
                    reason = "Lift cannot fit the whole part within the requested gap interval"
                    break
        if iteration == max_iterations:
            break
        desired = gap[0]+(gap[1]-gap[0])*.25
        weights = np.maximum(desired-signed,0.)
        if weights.max() <= epsilon:
            reason = "No further outward lift can improve the unresolved gap certificate"
            break
        direction = _unit((normals*weights[:,None]).sum(axis=0),"curved target outward lift direction")
        offset += direction*max(step,float(weights.max()))
    if unresolved:
        raise _Unsupported(reason + "; no geometry changed")
    return offset,None,reason


@_handler({"object_name", "target", "part_attribute", "part_id", "gap", "max_iterations", "step", "apply", "delete_hidden", "expected_state"})
def clear_of(params: dict[str, Any]) -> ToolResult:
    apply = _bool(params.get("apply", False), "apply")
    if _bool(params.get("delete_hidden", False), "delete_hidden"):
        raise _Unsupported("Hidden-face deletion requires explicit visibility/ownership evidence and is not implemented")
    import numpy as np
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    bpy = get_bpy()
    obj = bpy.context.scene.objects.get(_name(params.get("object_name"), "object_name"))
    target = bpy.context.scene.objects.get(_name(params.get("target"), "target"))
    if obj is None or target is None:
        raise NotFoundError("Clearance object or target not found")
    if obj == target or obj.type != "MESH" or obj.mode != "OBJECT" or obj.modifiers or obj.data.shape_keys or obj.data.users != 1:
        raise _Unsupported("Clearance requires a distinct single-user Object Mode mesh without modifiers or shape keys")
    attr = obj.data.attributes.get(_name(params.get("part_attribute", "part_id"), "part_attribute"))
    if attr is None or attr.domain != "FACE" or attr.data_type != "INT":
        raise InvalidStateError("Clearance needs an integer FACE part attribute")
    part_id = _int(params.get("part_id", 0), "part_id", 0, 2147483647)
    selected = [p for p in obj.data.polygons if attr.data[p.index].value == part_id]
    if not selected:
        raise InvalidStateError("No faces have the requested part_id")
    ids = sorted({v for face in selected for v in face.vertices})
    other = {v for face in obj.data.polygons if attr.data[face.index].value != part_id for v in face.vertices}
    if set(ids) & other:
        raise _Unsupported("Selected part shares vertices with unselected faces; separate the part explicitly before rigid lifting")
    if len(ids) + len(selected) > 4096:
        raise _Unsupported("Clearance is bounded to 4096 selected vertices plus face centers")
    gap = _vec(params.get("gap"), "gap", 2)
    if not 0 <= gap[0] <= gap[1] or gap[1] <= 0:
        raise InvalidParamsError("gap must be [nonnegative minimum, positive maximum] in scene units")
    step = _num(params.get("step", .001), "step", 0, True)
    max_iterations = _int(params.get("max_iterations", 20), "max_iterations", 1, 100)
    surface = _mesh_data(target, bpy.context.evaluated_depsgraph_get())
    target_points = surface["vertices"][surface["triangles"]]
    normal = _unit(np.cross(target_points[0,1]-target_points[0,0], target_points[0,2]-target_points[0,0]), "target normal")
    origin = target_points[0,0]
    extent = max(float(np.ptp(target_points.reshape(-1,3), axis=0).max()),1e-9)
    planar = np.max(abs((target_points.reshape(-1,3)-origin) @ normal)) <= extent*1e-7
    world = np.array([tuple(obj.matrix_world @ obj.data.vertices[i].co) for i in ids])
    mapping = {old:new for new,old in enumerate(ids)}
    faces = [tuple(mapping[i] for i in face.vertices) for face in selected]
    offset, clearance, reason = np.zeros(3),None,None
    if planar:
        normals = np.cross(target_points[:,1]-target_points[:,0],target_points[:,2]-target_points[:,0])
        if (normals @ normal <= 0).any():
            raise _Unsupported("Target plane has inconsistent face winding")
        epsilon = max(extent, float(np.ptp(world,axis=0).max())) * 1e-7
        for iteration in range(max_iterations + 1):
            lifted = world + offset
            signed = (lifted-origin) @ normal
            low_gap, high_gap = float(signed.min()), float(signed.max())
            tree = BVHTree.FromPolygons(lifted.tolist(), faces)
            overlaps = len(tree.overlap(surface["bvh"]))
            if low_gap >= gap[0]-epsilon and high_gap <= gap[1]+epsilon and overlaps == 0:
                clearance = {"minimum_scene_units": low_gap, "maximum_scene_units": high_gap,
                             "intersection_pairs": 0, "iterations": iteration, "tolerance_scene_units": epsilon}
                break
            if high_gap > gap[1]+epsilon or high_gap-low_gap > gap[1]-gap[0]+epsilon:
                break
            if iteration < max_iterations:
                offset += normal * max(step, gap[0]-low_gap)
        method = "rigid lift along consistently wound target plane normal"
        limitation = "Gap is to the supporting plane; finite target footprint coverage is not asserted"
    else:
        # Use Blender's actual triangulation on a temporary mesh so the distance
        # certificate covers the same nonplanar-quad surface Blender displays.
        selected_faces = {face.index for face in selected}
        temporary = obj.to_mesh()
        try:
            temporary.calc_loop_triangles()
            triangles = np.array([[tuple(obj.matrix_world @ temporary.vertices[i].co) for i in tri.vertices]
                                  for tri in temporary.loop_triangles if tri.polygon_index in selected_faces])
        finally:
            obj.to_mesh_clear()
        offset,clearance,reason = _curved_clearance(world,faces,triangles,surface,gap,step,max_iterations)
        method = "rigid outward normal lift with BVH crossing, solid-angle outside and adaptive surface-distance bounds"
        limitation = "Closed consistently wound targets only; certificate budgets may reject narrow intervals or complex parts"
    result = {"changed": False, "status": "planned" if clearance else "partial", "object_name": obj.name,
              "target": target.name, "part_id": part_id, "translation_world": offset.tolist(),
              "clearance": clearance, "gap_scene_units": gap.tolist(), "method": method,
              "limitation": limitation,
              "reason": None if clearance else reason or "No solution within gap and iteration bounds; no mesh change"}
    if not apply or clearance is None or not np.any(offset):
        return ToolResult(data=result)
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    if abs(obj.matrix_world.to_3x3().determinant()) < 1e-12:
        raise _Unsupported("Cannot lift a mesh with a singular world transform")
    local_offset = obj.matrix_world.to_3x3().inverted() @ Vector(offset)
    original = {i:obj.data.vertices[i].co.copy() for i in ids}
    try:
        for i in ids:
            obj.data.vertices[i].co += local_offset
        obj.data.update()
    except Exception as exc:
        cleanup = []
        try:
            for i, point in original.items():
                obj.data.vertices[i].co = point
            obj.data.update()
        except Exception as rollback:
            cleanup.append(str(rollback))
        if cleanup:
            bump_epoch()
        raise MutationApplyError(f"Clearance move failed: {exc}", {"changed": bool(cleanup), "cleanup_errors": cleanup,
                                                                 "state_token": current_state_token(bpy)}) from exc
    bump_epoch()
    return ToolResult(data={**result, "changed": True, "status": "applied", "state_token": current_state_token(bpy)})
