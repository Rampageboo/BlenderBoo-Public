"""Output contract regression coverage for USP orthographic review sets."""
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools.render import _ortho_output_paths, _validate_overwrite


class OrthoOutputPathTests(unittest.TestCase):
    def test_directory_and_prefix_are_deterministic_and_strip_png_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            params = {"output_dir": temp, "output_prefix": "low.PNG"}
            paths = _ortho_output_paths(None, params, ["front", "top"])
            self.assertEqual(paths, {v: Path(temp).resolve() / f"low_{v}.png" for v in ("front", "top")})
            paths["top"].touch()
            self.assertEqual(paths, _ortho_output_paths(None, params, ["front", "top"]))
            with self.assertRaisesRegex(InvalidParamsError, "overwrite is false"):
                _validate_overwrite(paths["top"], False)
            _validate_overwrite(paths["top"], True)
            self.assertEqual(params, {"output_dir": temp, "output_prefix": "low.PNG"})

    def test_combined_prefix_rejects_paths_and_directory_markers(self):
        with tempfile.TemporaryDirectory() as temp:
            for prefix in ("../escape", "sub/name", "sub\\name", "/absolute", "C:relative", ".", ".."):
                with self.subTest(prefix=prefix), self.assertRaisesRegex(InvalidParamsError, "basename"):
                    _ortho_output_paths(None, {"output_dir": temp, "output_prefix": prefix}, ["front"])

    def test_output_directory_must_be_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "file"
            path.touch()
            with self.assertRaisesRegex(InvalidParamsError, "not a directory"):
                _ortho_output_paths(None, {"output_dir": str(path), "output_prefix": "low"}, ["front"])

    def test_prefix_alone_keeps_path_support(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = _ortho_output_paths(None, {"output_prefix": str(Path(temp) / "low.png")}, ["top"])
            self.assertEqual(paths["top"], Path(temp).resolve() / "low_top.png")

    def test_directory_alone_keeps_sequence_allocation(self):
        with tempfile.TemporaryDirectory() as temp:
            first = _ortho_output_paths(None, {"output_dir": temp}, ["front"])
            first["front"].touch()
            second = _ortho_output_paths(None, {"output_dir": temp}, ["front"])
            self.assertEqual(first["front"].name, "review_set_001_front.png")
            self.assertEqual(second["front"].name, "review_set_002_front.png")

    def test_default_location_keeps_preview_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            bpy = SimpleNamespace(data=SimpleNamespace(filepath=str(Path(temp) / "scene.blend")))
            paths = _ortho_output_paths(bpy, {}, ["front"])
            self.assertEqual(paths["front"], Path(temp).resolve() / "previews" / "review_set_001_front.png")


if __name__ == "__main__":
    unittest.main()
