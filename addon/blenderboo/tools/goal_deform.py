"""Bounded base-mesh deformation, with metric coordinates and preflight constraints.

Only vertices in an explicit region are assigned. All algorithms prepare a candidate;
constraints and float32 storage are checked before any Blender coordinate is written.
"""
from __future__ import annotations

import math
from collections import Counter

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .destructive import prepare_destructive_action
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token

MAX_VERTICES = 250_000
MAX_TRIANGLES = 500_000
MAX_WORK = 50_000_000
COMMON_FIELDS = {'object_name', 'region', 'max_displacement_mm', 'pinned_vertex_group',
                 'symmetry', 'clearance', 'dry_run', 'confirm', 'expected_state'}
OP_FIELDS = {'operation', 'landmarks', 'falloff_radius_mm', 'method', 'iterations',
             'strength', 'offset_mm', 'curve', 'band_width_mm', 'landmark_tolerance_mm'}
GOAL_DEFORM_DESCRIPTION = (
    'Bounded base-mesh landmark warp, region relax, normal offset, curve crease or seam smoothing. '
    'Explicit vertex group (weights scale deformation) or face_indices region; vertices outside it '
    'remain byte-identical. Coordinates/curves/landmark targets are WORLD METRES (matrix_world '
    'times scene.unit_settings.scale_length); distances are millimetres. Dry-run first by default; '
    'apply needs dry_run=false and confirm=true. Pins stay fixed; symmetry requires paired masks '
    'and pins; incompatible landmarks, displacement limits or clearance refuse before mutation. '
    'Clearance certifies unsigned distance of the entire base surface to the named BASE mesh, '
    'including unselected faces; it does not assert outside/inside. Unresolved certificates refuse. '
    'Receipts measure float32-realizable positions, report region displacement percentiles and '
    'enclosed-volume change only for closed consistently wound meshes. Modifiers are not evaluated. '
    'No automatic self-intersection acceptance; validate fitted assets independently. '
    '250000 vertices, 500000 triangles, 500 iterations; RBF max 256 landmarks; harmonic max 100000.'
)
_VEC = {'type': 'array', 'items': {'type': 'number'}, 'minItems': 3, 'maxItems': 3}
GOAL_DEFORM_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['object_name', 'operation', 'region', 'max_displacement_mm'],
    'properties': {
        'object_name': {'type': 'string'},
        'operation': {'type': 'string', 'enum': ['landmark_warp', 'relax', 'normal_offset', 'crease', 'seam_smooth']},
        'region': {'type': 'object', 'additionalProperties': False, 'properties': {
            'vertex_group': {'type': 'string'},
            'face_indices': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'minItems': 1, 'maxItems': MAX_TRIANGLES}},
            'oneOf': [{'required': ['vertex_group']}, {'required': ['face_indices']}]},
        'max_displacement_mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 10000},
        'pinned_vertex_group': {'type': 'string'},
        'symmetry': {'type': 'object', 'additionalProperties': False, 'required': ['normal'], 'properties': {
            'normal': _VEC, 'offset_m': {'type': 'number', 'default': 0},
            'pair_tolerance_mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 0.01, 'default': 0.001}}},
        'clearance': {'type': 'object', 'additionalProperties': False, 'required': ['object_name', 'min_mm'], 'properties': {
            'object_name': {'type': 'string'}, 'min_mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 10000}}},
        'landmarks': {'type': 'array', 'minItems': 1, 'maxItems': 100000, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['vertex_index', 'target'],
            'properties': {'vertex_index': {'type': 'integer', 'minimum': 0}, 'target': _VEC}}},
        'falloff_radius_mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 10000},
        'method': {'type': 'string', 'enum': ['rbf', 'harmonic'], 'default': 'rbf'},
        'iterations': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 20},
        'strength': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1, 'default': 0.5},
        'offset_mm': {'type': 'number', 'minimum': -10000, 'maximum': 10000},
        'curve': {'type': 'array', 'minItems': 2, 'maxItems': 256, 'items': _VEC},
        'band_width_mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 10000},
        'landmark_tolerance_mm': {'type': 'number', 'minimum': 0.001, 'maximum': 1, 'default': 0.01},
        'dry_run': {'type': 'boolean', 'default': True}, 'confirm': {'type': 'boolean', 'default': False},
        'expected_state': {'type': 'string'},
    },
}


def _number(value, name, lo=-math.inf, hi=math.inf):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lo <= value <= hi:
        raise InvalidParamsError(f'{name} must be a finite number in [{lo}, {hi}]')
    return float(value)


def _positive(value, name):
    result = _number(value, name, 0, 10000)
    if result == 0:
        raise InvalidParamsError(f'{name} must be positive')
    return result


def _vec(value, name):
    import numpy as np
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise InvalidParamsError(f'{name} must contain three numbers')
    return np.array([_number(v, name) for v in value])


def _fields(value, allowed, name):
    if not isinstance(value, dict) or set(value) - allowed:
        raise InvalidParamsError(f'{name} must be an object with only {sorted(allowed)}')


def _index(value, count, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < count:
        raise InvalidParamsError(f'{name} must be an integer in [0, {count})')
    return value


def _object(bpy, name):
    if not isinstance(name, str) or not name:
        raise InvalidParamsError('object_name must be nonempty')
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        raise NotFoundError(f'Mesh object not found: {name}')
    if obj.type != 'MESH' or obj.mode != 'OBJECT':
        raise InvalidStateError('Deformation requires a mesh in Object mode')
    if not 0 < len(obj.data.vertices) <= MAX_VERTICES:
        raise InvalidStateError(f'Mesh must have 1..{MAX_VERTICES} vertices')
    return obj


def _group(obj, name):
    import numpy as np
    if not isinstance(name, str) or not name:
        raise InvalidParamsError('Vertex group name must be nonempty')
    group = obj.vertex_groups.get(name)
    if group is None:
        raise NotFoundError(f'Vertex group not found: {name}')
    weights = np.zeros(len(obj.data.vertices))
    for vertex in obj.data.vertices:
        weights[vertex.index] = next((entry.weight for entry in vertex.groups if entry.group == group.index), 0.)
    return weights


def _geometry(obj, unit_scale):
    import numpy as np
    local = np.empty((len(obj.data.vertices), 3), dtype=np.float32)
    obj.data.vertices.foreach_get('co', local.ravel())
    matrix = np.array(obj.matrix_world, dtype=float)
    if not np.isfinite(local).all() or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix[:3, :3])) < 1e-15:
        raise InvalidStateError('Mesh coordinates/transform must be finite and nonsingular')
    world = (local.astype(float) @ matrix[:3, :3].T + matrix[:3, 3]) * unit_scale
    obj.data.calc_loop_triangles()
    if len(obj.data.loop_triangles) > MAX_TRIANGLES:
        raise InvalidStateError(f'Mesh exceeds {MAX_TRIANGLES} triangles')
    triangles = np.array([tuple(t.vertices) for t in obj.data.loop_triangles], dtype=int).reshape(-1, 3)
    edges = np.array([tuple(e.vertices) for e in obj.data.edges], dtype=int).reshape(-1, 2)
    return dict(local=local, world=world, matrix=matrix, triangles=triangles, edges=edges)


def read_context(params):
    """Read base geometry and the exact region/pin membership; distances in metres."""
    import numpy as np
    bpy = get_bpy()
    obj = _object(bpy, params.get('object_name'))
    if obj.library or obj.data.library or obj.data.users != 1 or obj.data.shape_keys:
        raise InvalidStateError('Requires local, single-user mesh data without shape keys')
    unit_scale = _positive(float(bpy.context.scene.unit_settings.scale_length), 'scene unit scale')
    region = params.get('region')
    _fields(region, {'vertex_group', 'face_indices'}, 'region')
    if len(region) != 1:
        raise InvalidParamsError('region requires exactly one of vertex_group or face_indices')
    if 'vertex_group' in region:
        weights = _group(obj, region['vertex_group'])
    else:
        faces = region['face_indices']
        if not isinstance(faces, list) or not 1 <= len(faces) <= MAX_TRIANGLES:
            raise InvalidParamsError('face_indices must be a bounded nonempty list')
        weights = np.zeros(len(obj.data.vertices))
        for face in faces:
            weights[list(obj.data.polygons[_index(face, len(obj.data.polygons), 'face index')].vertices)] = 1
    mask = weights > 0
    if not mask.any():
        raise InvalidStateError('Selected region is empty')
    pinned = _group(obj, params['pinned_vertex_group']) > 0 if 'pinned_vertex_group' in params else np.zeros(len(mask), dtype=bool)
    return dict(bpy=bpy, obj=obj, unit_scale=unit_scale, mask=mask, weights=weights,
                pinned=pinned, **_geometry(obj, unit_scale))


def _kernel(distance, radius):
    """Compact Wendland C2 basis, zero outside the explicitly supplied radius."""
    import numpy as np
    ratio = np.minimum(distance / radius, 1.)
    return (1 - ratio) ** 4 * (4 * ratio + 1)


def _landmarks(params, context):
    import numpy as np
    raw = params.get('landmarks')
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100000:
        raise InvalidParamsError('landmarks must have 1..100000 entries')
    indices, targets = [], []
    for item in raw:
        _fields(item, {'vertex_index', 'target'}, 'landmark')
        index = _index(item.get('vertex_index'), len(context['world']), 'landmark vertex_index')
        if not context['mask'][index] or context['pinned'][index]:
            raise InvalidStateError('Landmark is outside region or inside pinned region')
        indices.append(index)
        targets.append(_vec(item.get('target'), 'landmark target'))
    if len(set(indices)) != len(indices):
        raise InvalidParamsError('Landmark vertex indices must be unique')
    return np.array(indices), np.array(targets)


def _neighbor_mean(values, edges):
    import numpy as np
    sums = np.zeros_like(values)
    np.add.at(sums, edges[:, 0], values[edges[:, 1]])
    np.add.at(sums, edges[:, 1], values[edges[:, 0]])
    degree = np.bincount(edges.ravel(), minlength=len(values))
    result = values.copy()
    result[degree > 0] = sums[degree > 0] / degree[degree > 0, None]
    return result


def _graph_distance(world, edges, sources, radius):
    """Bounded Dijkstra distance supplies harmonic falloff without dense N x K storage."""
    import heapq
    import numpy as np
    neighbors = [[] for _ in world]
    for a, b in edges:
        length = float(np.linalg.norm(world[a] - world[b]))
        neighbors[a].append((int(b), length))
        neighbors[b].append((int(a), length))
    distances = np.full(len(world), math.inf)
    pending = [(0., int(i)) for i in sources]
    heapq.heapify(pending)
    distances[sources] = 0
    while pending:
        distance, index = heapq.heappop(pending)
        if distance > distances[index]:
            continue
        for other, length in neighbors[index]:
            candidate = distance + length
            if candidate < min(distances[other], radius):
                distances[other] = candidate
                heapq.heappush(pending, (candidate, other))
    return distances


def landmark_candidate(context, params):
    """Pure numerical candidate builder; leaves all pins and unselected vertices fixed."""
    import numpy as np
    world, weights = context['world'], context['weights']
    indices, targets = _landmarks(params, context)
    radius = _positive(params.get('falloff_radius_mm'), 'falloff_radius_mm') / 1000
    active = context['mask'] & ~context['pinned']
    method = params.get('method', 'rbf')
    displacement = np.zeros_like(world)
    if method == 'rbf':
        if len(indices) > 256 or int(active.sum()) * len(indices) > MAX_WORK:
            raise InvalidStateError('RBF exceeds 256 landmarks / 50000000 vertex-landmark pairs; use harmonic')
        anchors = world[indices]
        kernel = _kernel(np.linalg.norm(anchors[:, None] - anchors[None, :], axis=2), radius)
        if np.linalg.cond(kernel) > 1e10:
            raise InvalidStateError('Landmark basis is ill-conditioned; separate coincident/nearby landmarks')
        coefficients = np.linalg.solve(kernel, (targets - anchors) / weights[indices, None])
        active_indices = np.flatnonzero(active)
        for start in range(0, len(active_indices), 4096):
            chunk = active_indices[start:start + 4096]
            basis = _kernel(np.linalg.norm(world[chunk, None] - anchors[None, :], axis=2), radius)
            displacement[chunk] = (basis @ coefficients) * weights[chunk, None]
    elif method == 'harmonic':
        iterations = _index(params.get('iterations', 20), 501, 'iterations')
        if iterations < 1 or len(context['edges']) * iterations > MAX_WORK:
            raise InvalidParamsError('harmonic iterations must be 1..500 and edge-iterations <=50000000')
        fixed = ~active
        fixed[indices] = True
        anchors = (targets - world[indices]) / weights[indices, None]
        displacement[indices] = anchors
        for _ in range(iterations):
            proposed = _neighbor_mean(displacement, context['edges'])
            displacement[~fixed] = proposed[~fixed]
        distances = _graph_distance(world, context['edges'], indices, radius)
        displacement *= (_kernel(distances, radius) * weights)[:, None]
    else:
        raise InvalidParamsError('method must be rbf or harmonic')
    return world + displacement


def _curve_distance(world, curve):
    import numpy as np
    if not isinstance(curve, list) or not 2 <= len(curve) <= 256:
        raise InvalidParamsError('curve must have 2..256 world-metre points')
    points = np.array([_vec(p, 'curve point') for p in curve])
    if len(world) * len(points) > MAX_WORK:
        raise InvalidStateError('Curve distance exceeds bounded work budget')
    distances = np.full(len(world), math.inf)
    for a, b in zip(points[:-1], points[1:]):
        edge = b - a
        length2 = float(edge @ edge)
        if length2 <= 1e-24:
            raise InvalidParamsError('Curve has a zero-length segment')
        factor = np.clip((world - a) @ edge / length2, 0., 1.)
        distances = np.minimum(distances, np.linalg.norm(world - (a + factor[:, None] * edge), axis=1))
    return distances


def region_candidate(context, params):
    import numpy as np
    world, triangles = context['world'], context['triangles']
    active = context['mask'] & ~context['pinned']
    weights = context['weights'].copy()
    weights[~active] = 0
    operation = params['operation']
    if operation in {'crease', 'seam_smooth'}:
        width = _positive(params.get('band_width_mm'), 'band_width_mm') / 1000
        weights *= _kernel(_curve_distance(world, params.get('curve')), width)
    if operation in {'relax', 'seam_smooth'}:
        iterations = _index(params.get('iterations', 20), 501, 'iterations')
        strength = _number(params.get('strength', .5), 'strength', 0, 1)
        if iterations == 0 or strength == 0 or len(context['edges']) * iterations > MAX_WORK:
            raise InvalidParamsError('Use positive iterations/strength with <=50000000 edge-iterations')
        result = world.copy()
        for _ in range(iterations):
            result += (_neighbor_mean(result, context['edges']) - result) * (weights * strength)[:, None]
        return result
    normals = np.zeros_like(world)
    tri = world[triangles]
    face_normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    for corner in range(3):
        np.add.at(normals, triangles[:, corner], face_normals)
    lengths = np.linalg.norm(normals, axis=1)
    if np.any((weights > 0) & (lengths <= 1e-15)):
        raise InvalidStateError('Selected vertex has an undefined base-surface normal')
    normals /= np.maximum(lengths[:, None], 1e-30)
    offset = _number(params.get('offset_mm'), 'offset_mm', -10000, 10000) / 1000
    return world + normals * (weights * offset)[:, None]


def _symmetrize(context, candidate, value):
    import numpy as np
    from mathutils import Vector
    from mathutils.kdtree import KDTree
    _fields(value, {'normal', 'offset_m', 'pair_tolerance_mm'}, 'symmetry')
    normal = _vec(value.get('normal'), 'symmetry normal')
    length = np.linalg.norm(normal)
    if length < 1e-12:
        raise InvalidParamsError('symmetry normal must be nonzero')
    normal /= length
    offset = _number(value.get('offset_m', 0), 'symmetry offset_m')
    tolerance = _number(value.get('pair_tolerance_mm', .001), 'pair_tolerance_mm', .000001, .01) / 1000
    def reflect(points):
        return points - 2 * (points @ normal - offset)[..., None] * normal
    world = context['world']
    tree = KDTree(len(world))
    for index, point in enumerate(world):
        tree.insert(Vector(point), index)
    tree.balance()
    pairs = np.empty(len(world), dtype=int)
    for index, point in enumerate(reflect(world)):
        _, other, distance = tree.find(Vector(point))
        if distance > tolerance:
            raise InvalidStateError(f'Symmetry has no mirrored partner for vertex {index}')
        pairs[index] = other
    if not np.array_equal(pairs[pairs], np.arange(len(world))):
        raise InvalidStateError('Symmetry correspondence is ambiguous/non-bijective')
    if (not np.array_equal(context['mask'], context['mask'][pairs]) or
            not np.array_equal(context['pinned'], context['pinned'][pairs]) or
            not np.allclose(context['weights'], context['weights'][pairs], atol=1e-7, rtol=0)):
        raise InvalidStateError('Symmetry conflicts with region weights or pinned membership')
    movable = context['mask'] & ~context['pinned']
    result = candidate.copy()
    result[movable] = ((candidate + reflect(candidate[pairs])) * .5)[movable]
    return result, pairs, reflect


def _volume(world, triangles):
    import numpy as np
    edges, signed = Counter(), Counter()
    for face in triangles:
        for a, b in zip(face, (face[1], face[2], face[0])):
            key = (min(a, b), max(a, b))
            edges[key] += 1
            signed[key] += 1 if a < b else -1
    if not edges or any(v != 2 for v in edges.values()) or any(signed.values()):
        return None
    tri = world[triangles] - world.mean(axis=0)
    return float(np.einsum('ij,ij->i', tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6)


def displacement_receipt(before, after, mask, triangles):
    import numpy as np
    displacement = np.linalg.norm(after - before, axis=1)
    region = displacement[mask]
    old_volume, new_volume = _volume(before, triangles), _volume(after, triangles)
    return {'region_vertex_count': int(mask.sum()), 'moved_vertex_count': int(np.count_nonzero(region)),
            'displacement_mm': {'p50': float(np.percentile(region, 50) * 1000),
                                'p99': float(np.percentile(region, 99) * 1000), 'max': float(region.max() * 1000)},
            'outside_mask_moved_count': int(np.count_nonzero(displacement[~mask])),
            'volume_before_m3': None if old_volume is None else abs(old_volume),
            'volume_after_m3': None if new_volume is None else abs(new_volume),
            'volume_change_m3': None if old_volume is None else abs(new_volume) - abs(old_volume),
            'volume_status': 'closed_consistent_signed_tetrahedra' if old_volume is not None else 'undefined_open_or_inconsistent_mesh'}


def _clearance(context, candidate, value):
    import numpy as np
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    from .hard_surface_build import _triangle_gap_certificate
    _fields(value, {'object_name', 'min_mm'}, 'clearance')
    target = _object(context['bpy'], value.get('object_name'))
    if target == context['obj'] or target.data == context['obj'].data:
        raise InvalidParamsError('Clearance target must be a different mesh')
    gap = _positive(value.get('min_mm'), 'clearance min_mm') / 1000
    data = _geometry(target, context['unit_scale'])
    if not len(data['triangles']) or not len(context['triangles']):
        raise InvalidStateError('Clearance requires source and target triangular surfaces')
    tree = BVHTree.FromPolygons(data['world'].tolist(), data['triangles'].tolist(), all_triangles=True)
    magnitude = max(float(abs(candidate).max()), float(abs(data['world']).max()), 1.)
    margin = magnitude * 2e-7
    for point in candidate:
        distance = tree.find_nearest(Vector(point))[3]
        if distance is None or distance < gap + margin:
            raise InvalidStateError('Candidate conflicts with minimum clearance at a source vertex')
    certificate = _triangle_gap_certificate(candidate[context['triangles']],
        lambda p: tree.find_nearest(Vector(p))[3], (gap, math.inf), margin, max_cells=100000, max_depth=20)
    if certificate['status'] != 'certified':
        raise InvalidStateError(f"Minimum clearance refused: {certificate}; no geometry changed")
    return {'status': 'certified', 'minimum_m': certificate['minimum_scene_units'],
            'numeric_margin_m': margin, 'cells': certificate['cells'], 'target': target.name,
            'coverage': 'entire_base_surface_unsigned_distance', 'inside_outside': 'not_classified'}


def apply_candidate(params, candidate_world, *, details=None, validate_candidate=None):
    """Validate/apply absolute world-METRE coordinates keyed by selected vertex index.

    Callers strip operation-specific fields. Missing indices are fixed at their originals.
    Symmetry averages paired proposals; every hard constraint is checked after float32
    conversion. Pins and unselected vertices are never reassigned. No scene save occurs.
    """
    import numpy as np
    _fields(params, COMMON_FIELDS | {'landmarks', 'landmark_tolerance_mm'}, 'candidate parameters')
    context = read_context(params)
    world, local = context['world'], context['local']
    obj, bpy = context['obj'], context['bpy']
    maximum = _positive(params.get('max_displacement_mm'), 'max_displacement_mm') / 1000
    check_expected_state(params.get('expected_state'), bpy)
    if not isinstance(candidate_world, dict) or len(candidate_world) > MAX_VERTICES:
        raise InvalidParamsError('candidate_world must be a bounded index-to-coordinate dictionary')
    candidate = world.copy()
    for key, point in candidate_world.items():
        index = _index(key, len(world), 'candidate index')
        if not context['mask'][index]:
            raise InvalidStateError('Candidate contains a vertex outside the explicit region')
        position = _vec(list(point), 'candidate position')
        if context['pinned'][index] and not np.array_equal(position, world[index]):
            raise InvalidStateError('Candidate conflicts with a pinned vertex')
        candidate[index] = position
    symmetry = None
    if 'symmetry' in params:
        candidate, pairs, reflect = _symmetrize(context, candidate, params['symmetry'])
        symmetry = (pairs, reflect)
    changed = np.any(candidate != world, axis=1)
    stored = local.copy()
    inverse = np.linalg.inv(context['matrix'])
    stored[changed] = ((candidate[changed] / context['unit_scale']) @ inverse[:3, :3].T + inverse[:3, 3]).astype(np.float32)
    candidate = world.copy()
    candidate[changed] = (stored[changed].astype(float) @ context['matrix'][:3, :3].T + context['matrix'][:3, 3]) * context['unit_scale']
    if not np.isfinite(candidate).all():
        raise InvalidStateError('Candidate is not representable as finite mesh coordinates')
    displacement = np.linalg.norm(candidate - world, axis=1)
    if displacement.max() > maximum + 1e-12:
        raise InvalidStateError(f'Maximum displacement {displacement.max()*1000:.9g} mm exceeds bound; no clamping applied')
    if np.any(displacement[context['pinned']] != 0) or np.any(displacement[~context['mask']] != 0):
        raise InvalidStateError('Candidate conflicts with immutable region/pins')
    constraint_receipt = {}
    if symmetry:
        pairs, reflect = symmetry
        error = float(np.linalg.norm(candidate - reflect(candidate[pairs]), axis=1).max())
        if error > 1e-5:
            raise InvalidStateError('Stored coordinates cannot satisfy symmetry within 1e-5 metres')
        constraint_receipt['symmetry_max_error_m'] = error
    if 'clearance' in params:
        constraint_receipt['clearance'] = _clearance(context, candidate, params['clearance'])
    if 'landmarks' in params:
        indices, targets = _landmarks(params, context)
        residual = np.linalg.norm(candidate[indices] - targets, axis=1) * 1000
        tolerance = _number(params.get('landmark_tolerance_mm', .01), 'landmark_tolerance_mm', .001, 1)
        if residual.max() > tolerance:
            raise InvalidStateError(f'Landmark residual {residual.max():.9g} mm conflicts with constraints/tolerance {tolerance} mm')
        constraint_receipt['landmark_residuals'] = [{'vertex_index': int(i), 'residual_mm': float(r)} for i, r in zip(indices, residual)]
    if validate_candidate is not None:
        if not callable(validate_candidate):
            raise InvalidParamsError('validate_candidate must be callable')
        candidate.setflags(write=False)
        extra_details = validate_candidate(candidate)
        if not isinstance(extra_details, dict):
            raise InvalidParamsError('validate_candidate must return a details dictionary')
        details = {**(details or {}), **extra_details}
    receipt = displacement_receipt(world, candidate, context['mask'], context['triangles'])
    transaction, apply = prepare_destructive_action(action='mesh.goal_deform',
        affected_datablocks=[{'type': 'Mesh', 'name': obj.data.name}],
        journal=[{'op': 'set_region_coordinates', 'status': 'planned'}],
        dry_run=params.get('dry_run'), confirm=params.get('confirm'), require_confirm=True,
        state_token=current_state_token(bpy), extra={'changed': False, 'object': obj.name,
            'geometry': 'base_mesh', 'coordinates': 'world_metres', 'scene_unit_scale': context['unit_scale'],
            'details': details or {}, **receipt, **constraint_receipt})
    if not apply:
        return ToolResult(data=transaction)
    require_document_write_access(bpy)
    check_expected_state(params.get('expected_state'), bpy)
    changed_indices = np.flatnonzero(np.any(stored != local, axis=1))
    try:
        for index in changed_indices:
            obj.data.vertices[int(index)].co = stored[index]
        obj.data.update()
    except Exception as exc:
        rollback_errors = []
        for index in changed_indices:
            try:
                obj.data.vertices[int(index)].co = local[index]
            except Exception as rollback:
                rollback_errors.append(str(rollback))
        try:
            obj.data.update()
        except Exception as rollback:
            rollback_errors.append(str(rollback))
        transaction.update(changed=bool(rollback_errors), rollback_errors=rollback_errors)
        if rollback_errors:
            bump_epoch()
        transaction['state_token'] = current_state_token(bpy)
        raise MutationApplyError(f'Deformation apply failed: {exc}', transaction) from exc
    transaction['changed'] = bool(len(changed_indices))
    transaction['journal'][0]['status'] = 'applied'
    if len(changed_indices):
        bump_epoch()
    transaction['state_token'] = current_state_token(bpy)
    return ToolResult(data=transaction)


def goal_deform(params):
    import numpy as np
    _fields(params, COMMON_FIELDS | OP_FIELDS, 'goal_deform parameters')
    operation = params.get('operation')
    fields = {'landmark_warp': {'landmarks', 'falloff_radius_mm', 'method', 'iterations', 'landmark_tolerance_mm'},
              'relax': {'iterations', 'strength'}, 'normal_offset': {'offset_mm'},
              'crease': {'curve', 'band_width_mm', 'offset_mm'},
              'seam_smooth': {'curve', 'band_width_mm', 'iterations', 'strength'}}
    if operation not in fields:
        raise InvalidParamsError('Unknown deformation operation')
    unexpected = (set(params) & OP_FIELDS) - fields[operation] - {'operation'}
    if unexpected:
        raise InvalidParamsError(f'Parameters not used by {operation}: {sorted(unexpected)}')
    context = read_context(params)
    check_expected_state(params.get('expected_state'), context['bpy'])
    candidate = landmark_candidate(context, params) if operation == 'landmark_warp' else region_candidate(context, params)
    common = {key: value for key, value in params.items() if key in COMMON_FIELDS | {'landmarks', 'landmark_tolerance_mm'}}
    return apply_candidate(common, {int(i): candidate[i].tolist() for i in np.flatnonzero(context['mask'])},
                           details={'operation': operation, 'falloff': 'compact_wendland_c2',
                                    'method': params.get('method', 'rbf') if operation == 'landmark_warp' else 'region'})

