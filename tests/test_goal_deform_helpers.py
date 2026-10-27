"""Pure numerical tests for bounded deformation; no Blender process required."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon'))
from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.goal_deform import landmark_candidate, region_candidate, displacement_receipt


def fixture():
    world = np.array([[x * .01, y * .01, .001 if x == 0 and y == 0 else 0]
                      for y in range(-2, 3) for x in range(-2, 3)])
    faces = [(y * 5 + x, y * 5 + x + 1, (y + 1) * 5 + x + 1)
             for y in range(4) for x in range(4)]
    faces += [(y * 5 + x, (y + 1) * 5 + x + 1, (y + 1) * 5 + x)
              for y in range(4) for x in range(4)]
    edges = sorted({tuple(sorted((a, b))) for f in faces for a, b in zip(f, (f[1], f[2], f[0]))})
    mask = (abs(world[:, 0]) < .02) & (abs(world[:, 1]) < .02)
    return dict(world=world, triangles=np.array(faces), edges=np.array(edges),
                mask=mask, weights=mask.astype(float), pinned=np.zeros(len(world), dtype=bool))


class GoalDeformNumerics(unittest.TestCase):
    def test_rbf_exact_landmarks_compact_falloff_mask_and_pins(self):
        context = fixture()
        context['pinned'][7] = True
        args = {'landmarks': [{'vertex_index': 12, 'target': [0, 0, .003]}], 'falloff_radius_mm': 15}
        result = landmark_candidate(context, args)
        np.testing.assert_allclose(result[12], [0, 0, .003], atol=1e-14)
        self.assertEqual(result[~context['mask']].tobytes(), context['world'][~context['mask']].tobytes())
        np.testing.assert_array_equal(result[7], context['world'][7])
        context['pinned'][12] = True
        with self.assertRaises(InvalidStateError):
            landmark_candidate(context, args)

    def test_harmonic_preserves_pins_and_exact_anchor(self):
        context = fixture()
        context['pinned'][7] = True
        result = landmark_candidate(context, {'landmarks': [{'vertex_index': 12, 'target': [0, 0, .003]}],
                                              'method': 'harmonic', 'iterations': 30, 'falloff_radius_mm': 50})
        np.testing.assert_allclose(result[12], [0, 0, .003], atol=1e-14)
        np.testing.assert_array_equal(result[~context['mask']], context['world'][~context['mask']])
        np.testing.assert_array_equal(result[7], context['world'][7])

    def test_all_region_operations_preserve_mask_and_pin(self):
        for args in ({'operation': 'relax', 'iterations': 3},
                     {'operation': 'normal_offset', 'offset_mm': .2},
                     {'operation': 'crease', 'offset_mm': -.2, 'band_width_mm': 15,
                      'curve': [[-.02, 0, 0], [.02, 0, 0]]},
                     {'operation': 'seam_smooth', 'iterations': 3, 'band_width_mm': 15,
                      'curve': [[-.02, 0, 0], [.02, 0, 0]]}):
            with self.subTest(operation=args['operation']):
                context = fixture()
                context['pinned'][7] = True
                result = region_candidate(context, args)
                self.assertEqual(result[~context['mask']].tobytes(), context['world'][~context['mask']].tobytes())
                np.testing.assert_array_equal(result[7], context['world'][7])
                self.assertGreater(np.linalg.norm(result - context['world']), 0)

    def test_receipt_matches_independent_displacement_and_closed_volume(self):
        before = np.array([[0., 0, 0], [1., 0, 0], [0., 1, 0], [0., 0, 1]])
        triangles = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
        after = before.copy()
        after[3, 2] = 2
        receipt = displacement_receipt(before, after, np.ones(4, dtype=bool), triangles)
        self.assertEqual(receipt['moved_vertex_count'], 1)
        self.assertEqual(receipt['displacement_mm']['p50'], 0)
        self.assertAlmostEqual(receipt['displacement_mm']['p99'], 970)
        self.assertAlmostEqual(receipt['volume_change_m3'], 1/6)
        self.assertAlmostEqual(receipt['volume_before_m3'], 1/6)
        self.assertIsNone(displacement_receipt(before, after, np.ones(4, dtype=bool), triangles[:1])['volume_change_m3'])

    def test_invalid_duplicate_and_nonfinite_landmarks_refuse(self):
        point = {'vertex_index': 12, 'target': [0, 0, .003]}
        for landmarks in ([point, point], [{'vertex_index': True, 'target': [0, 0, 0]}],
                          [{'vertex_index': 12, 'target': [float('nan'), 0, 0]}]):
            with self.assertRaises(InvalidParamsError):
                landmark_candidate(fixture(), {'landmarks': landmarks, 'falloff_radius_mm': 20})


if __name__ == '__main__':
    unittest.main()
