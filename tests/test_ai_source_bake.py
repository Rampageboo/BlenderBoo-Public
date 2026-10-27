"""Pure numerical bake tests; run with python -m unittest discover -s tests -p test_ai_source_bake.py."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.ai_source_bake import (
    _bilinear, _dilate, _highpass_normal, _options, _uv_masks, _color_metrics,
    bake_from_ai_source,
)


def uv_mesh(triangles, polygon_indices=None, parts=None):
    """Minimal mesh surface for the production UV raster, without Blender."""
    flat = np.asarray(triangles, dtype=np.float64).reshape(-1)
    uv_data = SimpleNamespace(foreach_get=lambda key, target: target.__setitem__(slice(None), flat))
    faces = polygon_indices if polygon_indices is not None else list(range(len(triangles)))
    attributes = {}
    if parts is not None:
        attributes["part"] = SimpleNamespace(domain="FACE", data_type="INT",
                                              data=[SimpleNamespace(value=v) for v in parts])
    return SimpleNamespace(
        uv_layers=SimpleNamespace(active=SimpleNamespace(data=uv_data)),
        loops=[None] * (3 * len(triangles)), attributes=attributes,
        calc_loop_triangles=lambda: None,
        loop_triangles=[SimpleNamespace(index=i, polygon_index=face, loops=tuple(range(i * 3, i * 3 + 3)))
                        for i, face in enumerate(faces)],
    )


class AISourceBakeTests(unittest.TestCase):
    def test_live_call_queues_background_job_without_forwarding_live_state_token(self):
        params = {"low": "Low", "high": "High", "output_dir": "textures", "expected_state": "state-a"}
        live = SimpleNamespace(app=SimpleNamespace(background=False))
        with patch("blenderboo.tools.ai_source_bake.get_bpy", return_value=live), \
             patch("blenderboo.tools.ai_source_bake.require_document_write_access"), \
             patch("blenderboo.tools.ai_source_bake.check_expected_state") as state, \
             patch("blenderboo.tools.background_render._manager") as manager:
            manager.start.return_value = SimpleNamespace(data={"operation_id": "job-a"})
            result = bake_from_ai_source(params)
        state.assert_called_once_with("state-a", live)
        self.assertEqual(result.data["operation_id"], "job-a")
        self.assertEqual(manager.start.call_args.args[0]["options"],
                         {"low": "Low", "high": "High", "output_dir": "textures"})

    def test_tiny_border_overlap_is_measured_and_owned_by_first_triangle(self):
        # A 0.02-pixel border incursion covers one texel among >30k occupied.
        triangles = np.array([[(0, 0), (128.51, 0), (128.51, 256)],
                              [(128.49, 128.5), (256, 0), (256, 256)]]) / 256
        mesh = uv_mesh(triangles, parts=[0, 1])
        inside, shell, owner, ambiguous, stats = _uv_masks(mesh, 256, "part", {0})
        self.assertEqual(stats["ambiguous_texels"], 1)
        self.assertLess(stats["uv_overlap_pct"], .01)
        self.assertEqual(stats["cross_face_overlap_texels"], 1)
        self.assertEqual(stats["same_face_overlap_texels"], 0)
        self.assertTrue(ambiguous[128, 128])
        self.assertTrue(shell[128, 128])
        self.assertEqual(owner[128, 128], 0)
        self.assertEqual(stats["occupied_texels"], int(inside.sum()))
        with self.assertRaisesRegex(InvalidStateError, "max_uv_overlap_pct=0"):
            _uv_masks(mesh, 256, max_uv_overlap_pct=0)

    def test_shared_diagonal_boundary_is_not_an_interior_overlap(self):
        mesh = uv_mesh([[(0, 0), (1, 0), (1, 1)], [(0, 0), (1, 1), (0, 1)]])
        _, _, owner, ambiguous, stats = _uv_masks(mesh, 64, max_uv_overlap_pct=0)
        self.assertEqual(stats["ambiguous_texels"], 0)
        self.assertEqual(stats["shared_boundary_texels"], 64)
        self.assertFalse(ambiguous.any())
        self.assertTrue((np.diag(owner) == 0).all())

    def test_true_stacked_overlap_and_same_face_fold_are_both_rejected(self):
        triangle = [(0, 0), (1, 0), (0, 1)]
        for faces, category in (([0, 1], "cross_face_overlap_texels"), ([0, 0], "same_face_overlap_texels")):
            mesh = uv_mesh([triangle, triangle], polygon_indices=faces)
            with self.subTest(faces=faces), self.assertRaisesRegex(InvalidStateError, "UV overlap"):
                _uv_masks(mesh, 64)
            _, _, _, _, stats = _uv_masks(mesh, 64, max_uv_overlap_pct=100)
            self.assertGreater(stats["uv_overlap_pct"], 90)
            self.assertEqual(stats[category], stats["ambiguous_texels"])

    def test_valid_black_albedo_is_not_a_missing_color_texel(self):
        rgb = np.array([[[0., 0., 0.], [1., 1., 1.]],
                        [[.005, .005, .005], [0., 0., 0.]]])
        inside = np.array([[True, True], [True, False]])
        hits = np.array([[True, False], [True, False]])
        metrics = _color_metrics(rgb, inside, hits)
        # Even a stale bright pixel is a defect if coverage says it was unfilled;
        # black or dark albedo with valid coverage must not fail the defect bar.
        self.assertEqual(metrics["black_texels"], 1)
        self.assertAlmostEqual(metrics["black_texels_pct"], 100 / 3)
        self.assertEqual(metrics["raw_dark_texels"], 2)
        self.assertEqual(metrics["valid_dark_texels"], 2)
        self.assertAlmostEqual(metrics["raw_dark_pct"], 200 / 3)
        all_hit = _color_metrics(rgb, inside, inside)
        self.assertEqual(all_hit["black_texels_pct"], 0)
        self.assertEqual(all_hit["raw_dark_texels"], 2)

    def test_parameter_errors_do_not_require_blender(self):
        base = {"low": "low", "high": "high", "output_dir": "textures"}
        for change in ({"shell_parts": [1]}, {"resolution": True}, {"normal_convention": "VULKAN"},
                       {"highpass_sigma_px": float("nan")}, {"pack": "RMA"}, {"high": "low"},
                       {"max_uv_overlap_pct": -1}, {"fallback_max_distance": -1},
                       {"fill_parts": {"2": {"base_color": [1, 0, 0], "roughness": .5, "metallic": 0, "ao": 1}}},
                       {"fill_face_attribute": "part", "fill_parts": {"2": {"base_color": [1, 0, 0], "roughness": 2, "metallic": 0, "ao": 1}}},
                       {"fill_face_attribute": "part", "fill_parts": {"2": {"base_color": [1, 0, 0], "roughness": .5, "metallic": 0, "ao": 1, "normal": "SOURCE"}}}):
            with self.subTest(change=change), self.assertRaises(InvalidParamsError):
                _options({**base, **change})
        self.assertEqual(_options({**base, "cage_extrusion": .02, "max_ray_distance": .03})["fallback_max_distance"], .05)
        fixed = _options({**base, "fill_face_attribute": "part", "fill_parts": {"2": {
            "base_color": [.7, .1, .2], "roughness": .4, "metallic": .8, "ao": .43}}})
        self.assertEqual(fixed["fill_parts"][2]["ao"], .43)

    def test_padding_does_not_wrap_or_fill_recorded_misses(self):
        pixels = np.zeros((5, 5, 3), dtype=np.float32)
        pixels[2, 0] = (1, 0.5, 0.25)
        valid = np.zeros((5, 5), dtype=bool)
        valid[2, 0] = True
        misses = np.zeros_like(valid)
        misses[2, 1] = True
        padded = _dilate(pixels, valid, 1, forbidden=misses)
        np.testing.assert_array_equal(padded[2, -1], (0, 0, 0))
        np.testing.assert_array_equal(padded[2, 1], (0, 0, 0))
        np.testing.assert_allclose(padded[1, 0], (1, 0.5, 0.25))

    def test_highpass_removes_constant_tilt_and_preserves_fine_detail(self):
        y, x = np.mgrid[:32, :32]
        normals = np.zeros((32, 32, 3), dtype=np.float32)
        normals[..., 0] = 0.35 + 0.06 * np.where(x % 2, 1, -1)
        normals[..., 1] = 0.2
        normals[..., 2] = np.sqrt(1 - np.sum(normals[..., :2] ** 2, axis=-1))
        valid = np.zeros((32, 32), dtype=bool)
        valid[4:28, 4:28] = True
        source = (normals + 1) * 0.5
        gl = _highpass_normal(source, valid, 4, "OPENGL")
        dx = _highpass_normal(source, valid, 4, "DIRECTX")
        result = gl * 2 - 1
        self.assertLess(abs(float(result[valid, 0].mean())), 0.01)
        self.assertGreater(float(result[valid, 0].std()), 0.04)
        np.testing.assert_allclose(np.linalg.norm(result[valid], axis=-1), 1, atol=1e-6)
        np.testing.assert_array_equal(gl[0, 0], (0.5, 0.5, 1))
        np.testing.assert_allclose(dx[..., 1], 1 - gl[..., 1])
        np.testing.assert_allclose(dx[..., (0, 2)], gl[..., (0, 2)])

    def test_bilinear_samples_pixel_centers_without_tile_wrap(self):
        pixels = np.array([[[1., 0, 0], [0, 1, 0]], [[0, 0, 1], [1, 1, 1]]])
        values = _bilinear(pixels, np.array([[.25, .25], [.5, .5], [1, 1]]))
        np.testing.assert_allclose(values, [[1, 0, 0], [.5, .5, .5], [1, 1, 1]])


if __name__ == "__main__":
    unittest.main()
