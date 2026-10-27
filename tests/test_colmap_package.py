"""Portable parser checks for Camera Refinement Workbench camera handoffs.

The pixel-level Blender checks live in run_colmap_package_headless.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.tools.colmap_import import parse_camera_package


def write_text_model(folder: Path, *, model: str = "PINHOLE") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    if model == "PINHOLE":
        camera = "1 PINHOLE 2560 1070 2751.875833 2751.875833 1291.854682 489.236294\n"
    elif model == "SIMPLE_PINHOLE":
        camera = "1 SIMPLE_PINHOLE 2560 1070 2751.875833 1291.854682 489.236294\n"
    else:
        camera = f"1 {model} 2560 1070 2751.875833 1291.854682 489.236294 0.1\n"
    (folder / "cameras.txt").write_text("# synthetic camera\n" + camera, encoding="utf-8")
    (folder / "images.txt").write_text(
        "# two lines per image\n"
        "1 1 0 0 0 0 0 0 1 frame_000001.png\n\n",
        encoding="utf-8",
    )


class ColmapPackageParserTests(unittest.TestCase):
    def test_pinhole_retains_nonzero_principal_and_camera_axes(self):
        with tempfile.TemporaryDirectory() as temp:
            write_text_model(Path(temp))
            parsed = parse_camera_package(temp, kind="reproduction")
        self.assertEqual(len(parsed["cameras"]), 1)
        row = parsed["cameras"][0]
        self.assertEqual(row["name"], "frame_000001.png")
        self.assertEqual(row["kind"], "reproduction")
        self.assertEqual((row["width"], row["height"]), (2560, 1070))
        self.assertAlmostEqual(row["fx"], 2751.875833)
        self.assertAlmostEqual(row["fy"], 2751.875833)
        self.assertAlmostEqual(row["cx"], 1291.854682)
        self.assertAlmostEqual(row["cy"], 489.236294)
        self.assertAlmostEqual(row["shift_x"], (1280 - 1291.854682) / 2560)
        self.assertAlmostEqual(row["shift_y"], (489.236294 - 535) / 2560)
        self.assertAlmostEqual(row["matrix_world"][0][0], 1)
        self.assertAlmostEqual(row["matrix_world"][1][1], -1)
        self.assertAlmostEqual(row["matrix_world"][2][2], -1)

    def test_simple_pinhole_duplicates_focal_without_recentering(self):
        with tempfile.TemporaryDirectory() as temp:
            write_text_model(Path(temp), model="SIMPLE_PINHOLE")
            row = parse_camera_package(temp, kind="constrained")["cameras"][0]
        self.assertEqual(row["fx"], row["fy"])
        self.assertAlmostEqual(row["cx"], 1291.854682)
        self.assertAlmostEqual(row["cy"], 489.236294)

    def test_distortion_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            write_text_model(Path(temp), model="SIMPLE_RADIAL")
            with self.assertRaisesRegex(Exception, "(?i)distort|SIMPLE_RADIAL|unsupported"):
                parse_camera_package(temp, kind="reproduction")

    def test_nested_colmap_model_inherits_outer_run_kind_and_mesh_transform(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "synthetic_plateau_run"
            model = run / "robust_ba" / "text_model"
            write_text_model(model)
            mesh_transform = [[1, 0, 0, 0.004], [0, 1, 0, 0.006],
                              [0, 0, 1, 0.034], [0, 0, 0, 1]]
            (run / "run.json").write_text(json.dumps({
                "kind": "constrained", "mesh_transform": mesh_transform,
            }), encoding="utf-8")
            parsed = parse_camera_package(str(model))
        self.assertEqual(parsed["kind"], "constrained")
        self.assertEqual(parsed["run_id"], "synthetic_plateau_run")
        self.assertEqual(parsed["mesh_transform"], mesh_transform)
        self.assertEqual(parsed["cameras"][0]["kind"], "constrained")

    def test_json_only_workbench_package_preserves_kind_metrics_and_world(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = {
                "name": "frame_000001.png", "source_run": "synthetic_a",
                "kind": "reproduction", "width": 2560, "height": 1070,
                "fx": 2751.875833, "fy": 2751.875833,
                "cx": 1291.854682, "cy": 489.236294,
                "matrix_world": [[1, 0, 0, 2], [0, -1, 0, 3],
                                 [0, 0, -1, 4], [0, 0, 0, 1]],
                "camera_frame": "source_world", "mesh_transform":
                    [[1, 0, 0, 10], [0, 1, 0, 20], [0, 0, 1, 30], [0, 0, 0, 1]],
                "metrics": {"iou": 0.977, "mean_boundary_px": 1.1},
            }
            (root / "cameras.json").write_text(json.dumps([source]), encoding="utf-8")
            row = parse_camera_package(temp)["cameras"][0]
        self.assertEqual(row["kind"], "reproduction")
        self.assertEqual(row["source_run"], "synthetic_a")
        self.assertEqual(row["metrics"]["iou"], 0.977)
        self.assertEqual(row["matrix_world"][0][3], 2)

    def test_real_m4a4_json_packages_when_available(self):
        examples_dir = os.environ.get("BLENDERBOO_COLMAP_EXAMPLES_DIR")
        if not examples_dir:
            self.skipTest("BLENDERBOO_COLMAP_EXAMPLES_DIR is not set")
        examples = Path(examples_dir)
        names = ("m4a4_track_a_source_world_export_v2",
                 "m4a4_track_b_square_plateau_export")
        paths = [examples / name for name in names]
        if any(not (path / "cameras.json").is_file() for path in paths):
            self.skipTest("M4A4 workbench export packages are unavailable")
        for path, kind in zip(paths, ("reproduction", "constrained")):
            with self.subTest(path=path):
                parsed = parse_camera_package(str(path))
                rows = parsed["cameras"]
                self.assertEqual(len(rows), 404)
                self.assertEqual({row["kind"] for row in rows}, {kind})
                self.assertEqual(len({row["name"] for row in rows}), 404)
                self.assertEqual({(row["width"], row["height"]) for row in rows},
                                 {(2560, 1070)})
                self.assertTrue(all(row["source_run"] for row in rows))


if __name__ == "__main__":
    unittest.main()
