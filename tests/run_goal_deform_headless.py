"""Goal deformation qualification on disposable meshes only.

Blender -b --factory-startup --python tests/run_goal_deform_headless.py
Optional: -- --production-fixture <production_rig.blend> --output <scratch JSON>
The optional production attempt copies the file to a temporary directory before
opening it, never runs the reference script, and never saves the production file.
"""
from pathlib import Path
import argparse
import json
import shutil
import sys
import tempfile

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'addon'))
from blenderboo.errors import InvalidParamsError, InvalidStateError, ConfirmationRequiredError
from blenderboo.tools.goal_deform import (
    goal_deform, read_context, apply_candidate, landmark_candidate, COMMON_FIELDS,
)


def coordinates(obj):
    result = np.empty((len(obj.data.vertices), 3), dtype=np.float32)
    obj.data.vertices.foreach_get('co', result.ravel())
    return result


def make_grid(name='DeformGrid'):
    mesh = bpy.data.meshes.new(name)
    verts = [(x * .01, y * .01, .001 if x == 0 and y == 0 else 0.)
             for y in range(-2, 3) for x in range(-2, 3)]
    faces = [(y * 5 + x, y * 5 + x + 1, (y+1) * 5 + x + 1, (y+1) * 5 + x)
             for y in range(4) for x in range(4)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    mask = [i for i, (x, y, z) in enumerate(verts) if abs(x) < .02 and abs(y) < .02]
    obj.vertex_groups.new(name='Region').add(mask, 1., 'REPLACE')
    obj.vertex_groups.new(name='Pins').add([7, 17], 1., 'REPLACE')
    return obj, mask


def fixture_checks():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.unit_settings.scale_length = 1
    operations = [{'operation': 'landmark_warp', 'landmarks': [{'vertex_index': 12, 'target': [0, 0, .003]}],
                   'falloff_radius_mm': 25},
                  {'operation': 'landmark_warp', 'method': 'harmonic', 'iterations': 20,
                   'landmarks': [{'vertex_index': 12, 'target': [0, 0, .003]}], 'falloff_radius_mm': 25},
                  {'operation': 'normal_offset', 'offset_mm': .2},
                  {'operation': 'relax', 'iterations': 3},
                  {'operation': 'crease', 'offset_mm': -.2, 'band_width_mm': 15,
                   'curve': [[-.02, 0, 0], [.02, 0, 0]]},
                  {'operation': 'seam_smooth', 'iterations': 3, 'band_width_mm': 15,
                   'curve': [[-.02, 0, 0], [.02, 0, 0]]}]
    results = []
    for operation in operations:
        obj, selected = make_grid()
        args = {'object_name': obj.name, 'region': {'vertex_group': 'Region'},
                'pinned_vertex_group': 'Pins', 'max_displacement_mm': 5, **operation}
        before = coordinates(obj)
        planned = goal_deform(args).data
        assert planned['dry_run'] and not planned['changed']
        assert before.tobytes() == coordinates(obj).tobytes(), 'dry run modified vertices'
        actual = goal_deform({**args, 'dry_run': False, 'confirm': True}).data
        after = coordinates(obj)
        outside = sorted(set(range(len(before))) - set(selected))
        assert after[outside].tobytes() == before[outside].tobytes()
        assert float(np.linalg.norm((after - before)[outside], axis=1).max()) <= 1e-6
        assert after[[7, 17]].tobytes() == before[[7, 17]].tobytes()
        independent = np.linalg.norm(after.astype(float) - before.astype(float), axis=1)[selected] * 1000
        assert actual['moved_vertex_count'] == int(np.count_nonzero(independent))
        for percentile in (50, 99):
            assert abs(actual['displacement_mm'][f'p{percentile}'] - float(np.percentile(independent, percentile))) < 1e-8
        assert abs(actual['displacement_mm']['max'] - float(independent.max())) < 1e-8
        assert actual['volume_change_m3'] is None  # open patch has no enclosed volume
        results.append({'operation': operation['operation'], 'method': operation.get('method'),
                        'moved': actual['moved_vertex_count'], 'max_mm': actual['displacement_mm']['max']})
    # Reflection needs exact paired geometry, not symmetric triangulation normals.
    obj, mask = make_grid('Symmetry')
    args = {'object_name': obj.name, 'region': {'vertex_group': 'Region'}, 'max_displacement_mm': 5,
            'symmetry': {'normal': [1, 0, 0]}, 'dry_run': False, 'confirm': True}
    context = read_context(args)
    candidate = {int(i): context['world'][i].tolist() for i in mask}
    candidate[11][2] += .002
    receipt = apply_candidate(args, candidate).data
    world = read_context(args)['world']
    mirror_error = max(np.linalg.norm(world[y*5+x] - world[y*5+(4-x)] * [-1, 1, 1])
                       for y in range(5) for x in range(5))
    assert mirror_error <= 1e-5 and receipt['symmetry_max_error_m'] <= 1e-5
    # Incompatible exact landmark goals must not be silently averaged or clamped.
    before = coordinates(obj).tobytes()
    conflicts = [dict(args, landmarks=[{'vertex_index': 11, 'target': [-.01, 0, .004]},
                                     {'vertex_index': 13, 'target': [.01, 0, .001]}]),
                 dict(args, max_displacement_mm=.01)]
    conflict_candidate = {11: [-.01, 0, .004], 13: [.01, 0, .001]}
    for bad in conflicts:
        try:
            apply_candidate(bad, conflict_candidate)
        except InvalidStateError:
            pass
        else:
            raise AssertionError('Conflicting constraints were accepted')
        assert coordinates(obj).tobytes() == before
    # The contour callback observes stored coordinates and may refuse before mutation.
    observed = []
    def reject_final(final):
        observed.append(final[11].tolist())
        raise InvalidStateError('independent acceptance failure')
    try:
        apply_candidate(args, conflict_candidate, validate_candidate=reject_final)
    except InvalidStateError:
        pass
    else:
        raise AssertionError('Precommit acceptance failure was ignored')
    assert observed and coordinates(obj).tobytes() == before
    # Scale_length plus nonuniform object transform: physical 0.2mm offset is invariant.
    obj, mask = make_grid('Metric')
    bpy.context.scene.unit_settings.scale_length = .01
    obj.scale = (2, 3, 4)
    bpy.context.view_layer.update()
    args = {'object_name': obj.name, 'region': {'vertex_group': 'Region'},
            'operation': 'normal_offset', 'offset_mm': .2, 'max_displacement_mm': 1}
    old_world = read_context(args)['world']
    actual = goal_deform({**args, 'dry_run': False, 'confirm': True}).data
    new_world = read_context(args)['world']
    lengths = np.linalg.norm(new_world - old_world, axis=1)[mask] * 1000
    assert np.max(abs(lengths - .2)) < 1e-6
    assert abs(actual['displacement_mm']['max'] - lengths.max()) < 1e-8
    bpy.context.scene.unit_settings.scale_length = 1
    # Continuous-surface clearance certifies a far plane, refuses a crossing plane.
    obj, mask = make_grid('ClearanceSource')
    target_mesh = bpy.data.meshes.new('ClearancePlane')
    target_mesh.from_pydata([(-1,-1,-.1), (1,-1,-.1), (1,1,-.1), (-1,1,-.1)], [], [(0,1,2,3)])
    target = bpy.data.objects.new('ClearancePlane', target_mesh)
    bpy.context.scene.collection.objects.link(target)
    args = {'object_name': obj.name, 'region': {'vertex_group': 'Region'}, 'operation': 'normal_offset',
            'offset_mm': .2, 'max_displacement_mm': 1, 'clearance': {'object_name': target.name, 'min_mm': 1}}
    assert goal_deform(args).data['clearance']['status'] == 'certified'
    target.location.z = .1
    bpy.context.view_layer.update()
    before = coordinates(obj).tobytes()
    try:
        goal_deform({**args, 'dry_run': False, 'confirm': True})
    except InvalidStateError:
        pass
    else:
        raise AssertionError('Clearance violation accepted')
    assert coordinates(obj).tobytes() == before
    # Face selection is explicit; confirmation and shared-data guards are checked.
    args = {'object_name': obj.name, 'region': {'face_indices': [0]}, 'operation': 'normal_offset',
            'offset_mm': .1, 'max_displacement_mm': 1, 'dry_run': False}
    try:
        goal_deform(args)
    except ConfirmationRequiredError:
        pass
    else:
        raise AssertionError('Apply accepted without confirmation')
    clone = obj.copy()
    bpy.context.scene.collection.objects.link(clone)
    try:
        goal_deform({**args, 'dry_run': True})
    except InvalidStateError:
        pass
    else:
        raise AssertionError('Shared mesh accepted')
    return {'status': 'pass', 'operations': results, 'symmetry_error_m': float(mirror_error)}


def self_intersections(world, triangles):
    # Independent BVH broad phase + triangle test; compare pair identities, not counts.
    from blenderboo.tools.skin_audit import _triangles_intersect
    tree = BVHTree.FromPolygons(world.tolist(), triangles.tolist(), all_triangles=True)
    pairs = set()
    candidates = tree.overlap(tree)
    if len(candidates) > 2_000_000:
        raise RuntimeError('Independent intersection check exceeds 2000000 broad-phase pairs')
    for a, b in candidates:
        if a >= b or set(triangles[a]).intersection(triangles[b]):
            continue
        if _triangles_intersect(world[triangles[a]].tolist(), world[triangles[b]].tolist(), 1e-9):
            pairs.add((a, b))
    return pairs


def production_attempt(path):
    """Recreate the documented Quinn-to-fitted-A02 direction on an isolated copy.

    The evidence script calls this 'fit A02 onto Quinn' but deforms Quinn topology
    to the similarity-aligned A02 surface. Source geometry is fresh Quinn rest.
    """
    if not path.is_file():
        return {'status': 'blocked', 'reason': f'Production fixture unavailable: {path}'}
    with tempfile.TemporaryDirectory(prefix='blenderboo-goal-head-') as folder:
        disposable = Path(folder) / 'head-fit-disposable.blend'
        shutil.copyfile(path, disposable)
        bpy.ops.wm.open_mainfile(filepath=str(disposable), load_ui=False)
        q = bpy.data.objects.get('Quinn_SKM_Quinn_LOD0')
        target = bpy.data.objects.get('A02_fit')
        if q is None or target is None:
            return {'status': 'blocked', 'reason': 'Required Quinn_SKM_Quinn_LOD0 or A02_fit object missing',
                    'available_meshes': [o.name for o in bpy.data.objects if o.type == 'MESH']}
        source = q.copy()
        source.data = q.data.copy()
        source.name = 'GOAL_DEFORM_DISPOSABLE_QUINN'
        bpy.context.scene.collection.objects.link(source)
        source.parent = None
        source.modifiers.clear()
        if source.data.shape_keys:
            source.shape_key_clear()
        source.data.transform(Matrix.Scale(1.71 / 1.802, 4) @ q.matrix_world)
        source.matrix_world = Matrix.Identity(4)
        group_indices = {g.name: g.index for g in source.vertex_groups}
        if not {'head', 'neck_01', 'neck_02'} <= set(group_indices):
            return {'status': 'blocked', 'reason': 'Fresh Quinn source lacks head/neck groups'}
        memberships = []
        head_indices = []
        for vertex in source.data.vertices:
            weights = {g.group: g.weight for g in vertex.groups}
            total = sum(weights.values()) or 1
            head = weights.get(group_indices['head'], 0) / total
            neck2 = weights.get(group_indices['neck_02'], 0) / total
            neck1 = weights.get(group_indices['neck_01'], 0) / total
            if head + neck2 + .5 * neck1 > .02:
                memberships.append(vertex.index)
            if head > .5:
                head_indices.append(vertex.index)
        group = source.vertex_groups.new(name='GoalHeadRegion')
        group.add(memberships, 1., 'REPLACE')
        args = {'object_name': source.name, 'region': {'vertex_group': group.name},
                'operation': 'landmark_warp', 'method': 'harmonic', 'iterations': 100,
                'falloff_radius_mm': 300, 'max_displacement_mm': 40}
        context = read_context(args)
        unit = context['unit_scale']
        target_world = np.array([tuple(target.matrix_world @ v.co) for v in target.data.vertices]) * unit
        target.data.calc_loop_triangles()
        target_tris = [tuple(t.vertices) for t in target.data.loop_triangles]
        tree = BVHTree.FromPolygons(target_world.tolist(), target_tris, all_triangles=True)
        center = target_world.mean(axis=0)
        landmarks = []
        for index in head_indices:
            point = context['world'][index]
            direction = Vector(point - center)
            hit = None
            if direction.length > 1e-9:
                direction.normalize()
                hit = tree.ray_cast(Vector(center) + direction * .5, -direction, .5)[0]
            if hit is None or np.linalg.norm(np.array(hit) - point) > .04:
                hit = tree.find_nearest(Vector(point))[0]
            if hit is not None and np.linalg.norm(np.array(hit) - point) <= .04:
                landmarks.append({'vertex_index': index, 'target': list(hit)})
        if not landmarks:
            return {'status': 'blocked', 'reason': 'No bounded source/target head correspondences within 40 mm'}
        original_landmarks = landmarks
        before_pairs = self_intersections(context['world'], context['triangles'])
        before_distance = [tree.find_nearest(Vector(context['world'][i]))[3] for i in head_indices]
        search = []
        chosen = None
        # Strength is explicit fixture authoring, not a public-tool clamp. A full
        # projection can collapse distinct Quinn eye/lip surfaces onto one target.
        # Try a bounded sequence within the stated 0.2mm error budget first.
        pin_group = source.vertex_groups.new(name='HeadCollisionPins')
        args['pinned_vertex_group'] = pin_group.name
        pins = set()
        for attempt in range(8):
            strength = (.94, .96, .98, 1.)[attempt] if attempt < 4 else .98
            current = read_context(args)
            args['landmarks'] = [
                {'vertex_index': item['vertex_index'],
                 'target': (context['world'][item['vertex_index']] + strength *
                            (np.array(item['target']) - context['world'][item['vertex_index']])).tolist()}
                for item in original_landmarks if item['vertex_index'] not in pins]
            if not args['landmarks']:
                break
            numerical = landmark_candidate(current, args)
            candidate = {int(i): numerical[i].tolist() for i in np.flatnonzero(current['mask'])}
            observed = {}
            def inspect_final(final):
                pairs = self_intersections(final, context['triangles'])
                distances = [tree.find_nearest(Vector(final[i]))[3] for i in head_indices]
                observed.update(pairs=pairs, new_pairs=pairs - before_pairs,
                                median_mm=float(np.median(distances) * 1000), world=final.copy())
                return {}
            common = {key: value for key, value in args.items()
                      if key in COMMON_FIELDS | {'landmarks', 'landmark_tolerance_mm'}}
            planned = apply_candidate(common, candidate, validate_candidate=inspect_final).data
            search.append({'strength': strength, 'pinned_vertices': len(pins),
                           'median_mm': observed['median_mm'], 'new_intersections': len(observed['new_pairs'])})
            if observed['median_mm'] <= .2 and not observed['new_pairs']:
                chosen = strength
                break
            if attempt >= 3:
                # Explicitly pin collision-involved moving vertices and remove those
                # landmark goals. Harmonic interpolation blends toward these pins.
                # Samples/acceptance stay the entire original head region.
                moved = np.linalg.norm(observed['world'] - context['world'], axis=1) > 1e-8
                added = {int(v) for pair in observed['new_pairs'] for t in pair
                         for v in context['triangles'][t] if moved[v] and current['mask'][v]}
                if not added - pins:
                    break
                pins.update(added)
                pin_group.add(sorted(pins), 1., 'REPLACE')
        if chosen is None:
            return {'status': 'acceptance_failed', 'production_modified': False,
                    'reason': 'Bounded strength/pinned-region search found no candidate meeting both criteria',
                    'head_samples': len(head_indices), 'search': search}
        # Recompute through the public typed operation and independently measure
        # the actual stored mesh again; no tentative candidate was ever written.
        applied = goal_deform({**args, 'dry_run': False, 'confirm': True}).data
        after = read_context(args)
        after_distance = [tree.find_nearest(Vector(after['world'][i]))[3] for i in head_indices]
        after_pairs = self_intersections(after['world'], after['triangles'])
        new_pairs = after_pairs - before_pairs
        median_mm = float(np.median(after_distance) * 1000)
        outside = ~context['mask']
        assert context['local'][outside].tobytes() == after['local'][outside].tobytes()
        return {'status': 'pass' if median_mm <= .2 and not new_pairs else 'acceptance_failed',
                'direction': 'fresh_scaled_Quinn_topology_to_existing_similarity_aligned_A02_fit',
                'fixture': str(path), 'production_modified': False,
                'head_samples': len(head_indices), 'landmarks': len(args['landmarks']),
                'selected_strength': chosen, 'collision_pinned_vertices': len(pins), 'search': search,
                'median_before_mm': float(np.median(before_distance) * 1000), 'median_after_mm': median_mm,
                'self_intersections_before': len(before_pairs), 'self_intersections_after': len(after_pairs),
                'new_self_intersections': len(new_pairs), 'new_pair_examples': sorted(new_pairs)[:20],
                'max_displacement_mm': applied['displacement_mm']['max'],
                'unselected_coordinates_byte_identical': True, 'dry_run_moved': planned['moved_vertex_count']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--production-fixture', type=Path)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    report = {'fixture': fixture_checks()}
    if options.production_fixture:
        try:
            report['production'] = production_attempt(options.production_fixture)
        except Exception as exc:
            report['production'] = {'status': 'failed', 'reason': f'{type(exc).__name__}: {exc}'}
    if options.output:
        fixture = options.production_fixture
        if fixture and options.output.resolve().is_relative_to(fixture.parent.resolve()):
            raise RuntimeError('Output must be outside the production fixture directory')
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    if report.get('production', {}).get('status') in {'failed', 'acceptance_failed'}:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
