"""Pure geometry/receipt checks for asset.compare_to_source; no bpy required."""
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.asset_source_compare import (
    _acceptance, _frame, _options, _quantiles, _radial_regions, _raster_mask,
    _silhouette_stats, _surface_samples, _write_png,
)


def mesh(vertices, triangles):
    vertices, triangles = np.array(vertices, dtype=float), np.array(triangles, dtype=int)
    tri = vertices[triangles]
    areas = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1) * .5
    return {"vertices": vertices, "triangles": triangles, "areas": areas,
            "bounds": (vertices.min(axis=0), vertices.max(axis=0))}


class SourceCompareHelpersTests(unittest.TestCase):
    def test_options_reject_ambiguous_or_unbounded_inputs(self):
        base = {"low": "low", "high": "high"}
        self.assertEqual(_options(base)["views"], ["front", "side", "top", "back"])
        for extra in ({"views": []}, {"views": ["front", "front"]}, {"views": [1]},
                      {"resolution": True}, {"resolution": 4096}, {"sample_count": 0},
                      {"center": [float("nan"), 0, 0]}, {"spherical": "true"},
                      {"thresholds": {"iou_min": 1.1}}, {"thresholds": {"unknown": 0}},
                      {"thresholds": {"radial_max_mm": 1}}, {"output_dir": "relative/path"},
                      {"include_radial_grid": "yes"}):
            with self.subTest(extra=extra), self.assertRaises(InvalidParamsError):
                _options({**base, **extra})
        with tempfile.TemporaryDirectory() as directory:
            _options({**base, "output_dir": directory})
            (Path(directory) / "keep.txt").write_text("keep")
            with self.assertRaises(InvalidParamsError):
                _options({**base, "output_dir": directory})

    def test_shared_frame_preserves_translation_and_scale_loss(self):
        square = mesh([(0, 0, 0), (2, 0, 0), (2, 0, 2), (0, 0, 2)], [(0, 1, 2), (0, 2, 3)])
        shifted = mesh(square["vertices"] + (1, 0, 0), square["triangles"])
        left, right, frame = _frame(square, shifted, "front", 256)
        self.assertAlmostEqual(frame["ortho_scale"], 3 * 1.05)
        low = _raster_mask(left, square["triangles"], 256, [0])
        high = _raster_mask(right, shifted["triangles"], 256, [0])
        self.assertAlmostEqual(_silhouette_stats(low, high)["iou"], 1 / 3, delta=.012)
        small = mesh(square["vertices"] * .5 + (.5, 0, .5), square["triangles"])
        left, right, _ = _frame(small, square, "front", 256)
        low = _raster_mask(left, small["triangles"], 256, [0])
        high = _raster_mask(right, square["triangles"], 256, [0])
        self.assertAlmostEqual(_silhouette_stats(low, high)["iou"], .25, delta=.012)

    def test_raster_winding_and_shared_edges(self):
        points = np.array([(0, 0), (8, 0), (8, 8), (0, 8)], dtype=float)
        triangles = np.array([(0, 1, 2), (0, 2, 3)])
        first = _raster_mask(points, triangles, 32, [0])
        reverse = _raster_mask(points, triangles[:, ::-1], 32, [0])
        np.testing.assert_array_equal(first, reverse)
        self.assertEqual(int(first.sum()), 64)
        self.assertEqual(_silhouette_stats(first, reverse)["iou"], 1)
        with self.assertRaises(InvalidStateError):
            _raster_mask(points, triangles, 32, [100_000_000])

    def test_empty_coverage_cannot_pass(self):
        empty = np.zeros((32, 32), dtype=bool)
        stats = _silhouette_stats(empty, empty)
        self.assertIsNone(stats["iou"])
        self.assertEqual(stats["status"], "unavailable")
        self.assertFalse(_acceptance({"front": stats}, {}, None, {"iou_min": 0})["pass"])
        self.assertIsNone(_acceptance({}, {}, None, {})["pass"])
        self.assertFalse(_acceptance({}, {}, {"status": "partial"}, {"radial_max_mm": 10})["pass"])

    def test_area_weighted_interior_samples_not_vertex_counts(self):
        geometry = mesh([(0, 0, 0), (2, 0, 0), (0, 1, 0),
                         (10, 0, 0), (16, 0, 0), (10, 1, 0)], [(0, 1, 2), (3, 4, 5)])
        samples, triangles = _surface_samples(geometry, 1000)
        repeat, _ = _surface_samples(geometry, 1000)
        np.testing.assert_array_equal(samples, repeat)
        self.assertEqual(triangles, 2)
        self.assertEqual(int((samples[:, 0] < 5).sum()), 250)
        self.assertTrue((samples[:, 1] > 0).all())
        self.assertTrue((samples[:, 1] < 1).all())
        self.assertEqual(_quantiles([0, 1, 2, 3])["median"], 1.5)
        self.assertIsNone(_quantiles([])["p99"])

    def test_radial_regions_wrap_and_ignore_missing_rays(self):
        delta = np.array([[2., 0., np.nan, -3.], [0., 0., 0., 0.]])
        result = _radial_regions(delta, np.array([0, 90, 180, 270]), np.array([-45, 45]), 1, 10)
        self.assertEqual(result["over_threshold_cells"], 2)
        self.assertEqual(result["region_count"], 1)
        region = result["regions"][0]
        self.assertTrue(region["wraps_theta_seam"])
        self.assertEqual(region["peak_abs_delta_mm"], 30)
        self.assertEqual(region["peak_signed_delta_scene_units"], -3)

    def test_png_pixels_and_no_overwrite(self):
        pixels = np.array([[[1, 2, 3], [255, 0, 0]], [[0, 255, 0], [0, 0, 255]]], dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.png"
            _write_png(path, pixels)
            content = path.read_bytes()
            self.assertEqual(content[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">IIBBBBB", content[16:29]), (2, 2, 8, 2, 0, 0, 0))
            offset, compressed = 8, b""
            while offset < len(content):
                length = struct.unpack(">I", content[offset:offset + 4])[0]
                tag = content[offset + 4:offset + 8]
                payload = content[offset + 8:offset + 8 + length]
                self.assertEqual(struct.unpack(">I", content[offset + 8 + length:offset + 12 + length])[0], zlib.crc32(tag + payload) & 0xffffffff)
                if tag == b"IDAT":
                    compressed += payload
                offset += length + 12
            self.assertEqual(zlib.decompress(compressed), b"\0" + pixels[0].tobytes() + b"\0" + pixels[1].tobytes())
            with self.assertRaises(FileExistsError):
                _write_png(path, pixels)
            self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
