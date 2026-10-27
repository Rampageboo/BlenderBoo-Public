"""Pure-Python tests for render guards, review params, and pixel comparison."""

from pathlib import Path
from array import array
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools.render import (
    DEFAULT_PIXEL_BUDGET,
    HARD_PIXEL_CAP,
    REVIEW_VIEWS,
    _boolean_param,
    _compare_rgba_pixels,
    _draw_label,
    _manifest_entry,
    _name_list_param,
    _review_output_paths,
    _review_views,
    _threshold,
    _validate_overwrite,
    _validate_pixel_budget,
    render_review_set,
)


class RenderGuardTests(unittest.TestCase):
    def test_boolean_params_are_strict(self) -> None:
        self.assertFalse(_boolean_param({}, "overwrite"))
        self.assertTrue(_boolean_param({"overwrite": True}, "overwrite"))
        with self.assertRaises(InvalidParamsError):
            _boolean_param({"overwrite": 1}, "overwrite")

    def test_default_pixel_budget(self) -> None:
        _validate_pixel_budget(3840, 2160, False)
        with self.assertRaisesRegex(InvalidParamsError, "allow_large: true"):
            _validate_pixel_budget(4096, 2160, False)
        _validate_pixel_budget(4096, 2160, True)

    def test_hard_pixel_cap_always_applies(self) -> None:
        self.assertEqual(DEFAULT_PIXEL_BUDGET, 8_294_400)
        self.assertEqual(HARD_PIXEL_CAP, 67_108_864)
        with self.assertRaisesRegex(InvalidParamsError, "hard pixel cap"):
            _validate_pixel_budget(8193, 8192, True)

    def test_existing_output_requires_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "preview.png"
            path.touch()
            with self.assertRaisesRegex(InvalidParamsError, str(path.name)):
                _validate_overwrite(path, False)
            _validate_overwrite(path, True)
            _validate_overwrite(path.parent / "new.png", False)


class ReviewParameterTests(unittest.TestCase):
    def test_default_and_subset_views_use_canonical_order(self) -> None:
        self.assertEqual(_review_views({}), list(REVIEW_VIEWS))
        self.assertEqual(
            _review_views({"views": ["three_quarter", "camera", "side"]}),
            ["camera", "side", "three_quarter"],
        )

    def test_invalid_view_subsets_are_rejected(self) -> None:
        for value in ([], "top", ["top", "top"], ["rear"], [True]):
            with self.subTest(value=value), self.assertRaises(InvalidParamsError):
                _review_views({"views": value})

    def test_collection_name_lists_are_strict(self) -> None:
        self.assertIsNone(_name_list_param({}, "hide"))
        self.assertEqual(_name_list_param({"hide": []}, "hide"), [])
        self.assertEqual(_name_list_param({"show_only": ["Shrub"]}, "show_only"), ["Shrub"])
        for value in ("Shrub", [""], [1], ["Shrub", "Shrub"]):
            with self.subTest(value=value), self.assertRaises(InvalidParamsError):
                _name_list_param({"hide": value}, "hide")

    def test_output_location_arguments_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(InvalidParamsError, "mutually exclusive"):
            _review_output_paths(
                None,
                {"output_dir": "C:/tmp/reviews", "output_prefix": "C:/tmp/review"},
                ["top"],
            )

    def test_manifest_entry_has_stable_shape(self) -> None:
        entry = _manifest_entry(Path("review_top.png"), "TopCamera", 960, 540, ["note"])
        self.assertEqual(set(entry), {"path", "camera", "width", "height", "warnings"})
        self.assertEqual(entry["warnings"], ["note"])

    def test_review_set_reuses_pixel_budget_before_loading_bpy(self) -> None:
        with self.assertRaisesRegex(InvalidParamsError, "allow_large: true"):
            render_review_set({"width": 4096, "height": 2160})


class ComparePixelTests(unittest.TestCase):
    @staticmethod
    def _rgba(values):
        pixels = []
        for red, green, blue, alpha in values:
            pixels.extend((red, green, blue, alpha))
        return pixels

    def test_identical_images_have_no_changed_pixels(self) -> None:
        pixels = self._rgba([(0.2, 0.4, 0.6, 1.0), (0.0, 0.0, 0.0, 0.0)])
        metrics = _compare_rgba_pixels(pixels, list(pixels), 2, 1, 8)
        self.assertEqual(metrics["changed_pixels"], 0)
        self.assertEqual(metrics["changed_fraction"], 0.0)
        self.assertEqual(metrics["mean_abs_rgb_delta"], 0.0)
        self.assertIsNone(metrics["bbox"])

    def test_known_differences_have_exact_metrics_and_bbox(self) -> None:
        zeros = self._rgba([(0.0, 0.0, 0.0, 1.0)] * 4)
        changed = self._rgba(
            [
                (10 / 255, 0.0, 0.0, 1.0),
                (0.0, 8 / 255, 0.0, 1.0),
                (0.0, 0.0, 9 / 255, 1.0),
                (1 / 255, 1 / 255, 1 / 255, 1.0),
            ]
        )
        metrics = _compare_rgba_pixels(zeros, changed, 2, 2, 8)
        self.assertEqual(metrics["total_pixels"], 4)
        self.assertEqual(metrics["changed_pixels"], 2)
        self.assertEqual(metrics["changed_fraction"], 0.5)
        self.assertAlmostEqual(metrics["mean_abs_rgb_delta"], 2.5)
        self.assertEqual(metrics["bbox"], [0, 0, 0, 1])

    def test_alpha_is_ignored_and_threshold_is_strictly_greater(self) -> None:
        base = self._rgba([(0.0, 0.0, 0.0, 0.0)])
        alpha_only = self._rgba([(0.0, 0.0, 0.0, 1.0)])
        self.assertEqual(_compare_rgba_pixels(base, alpha_only, 1, 1, 0)["changed_pixels"], 0)
        exactly = self._rgba([(8 / 255, 0.0, 0.0, 0.0)])
        self.assertEqual(_compare_rgba_pixels(base, exactly, 1, 1, 8)["changed_pixels"], 0)

    def test_invalid_threshold_and_pixel_lengths_are_rejected(self) -> None:
        for value in (-1, 256, True, 1.5):
            with self.subTest(value=value), self.assertRaises(InvalidParamsError):
                _threshold({"threshold": value})
        with self.assertRaisesRegex(InvalidParamsError, "must each contain 8"):
            _compare_rgba_pixels([0.0] * 4, [0.0] * 8, 2, 1, 8)


class ContactSheetPixelTests(unittest.TestCase):
    def test_label_draws_visible_glyph_within_cell(self) -> None:
        pixels = array("f", [0]) * (20 * 10 * 4)
        _draw_label(pixels, 20, 10, 1, 1, "A", 2)
        self.assertGreater(sum(pixels), 0)
        self.assertEqual(list(pixels[0:4]), [0, 0, 0, 0])
        # A's top row is stored at the highest Y in Blender's bottom-up image.
        self.assertEqual(list(pixels[(7 * 20 + 3) * 4:(7 * 20 + 3) * 4 + 4]),
                         [1, 1, 1, 1])
        self.assertEqual(list(pixels[(1 * 20 + 3) * 4:(1 * 20 + 3) * 4 + 4]),
                         [0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
