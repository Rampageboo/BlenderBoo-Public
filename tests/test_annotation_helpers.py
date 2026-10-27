"""Pure-Python tests for bounded Annotation inspection helpers."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools.annotation import (
    inspect_annotation,
    _page_records,
    _pagination,
    _request_scope,
)


class AnnotationInspectionHelperTests(unittest.TestCase):
    def test_unknown_fields_are_rejected_before_bpy_access(self) -> None:
        with self.assertRaises(InvalidParamsError):
            inspect_annotation({"unexpected": True})

    def test_scope_follows_the_annotation_hierarchy(self) -> None:
        cases = [
            ({}, "DATABLOCKS"),
            ({"annotation_name": "ReviewNotes"}, "LAYERS"),
            (
                {"annotation_name": "ReviewNotes", "layer_name": "Issues"},
                "FRAMES",
            ),
            (
                {
                    "annotation_name": "ReviewNotes",
                    "layer_name": "Issues",
                    "frame_number": 12,
                },
                "STROKES",
            ),
            (
                {
                    "annotation_name": "ReviewNotes",
                    "layer_name": "Issues",
                    "frame_number": 12,
                    "stroke_index": 0,
                },
                "POINTS",
            ),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                self.assertEqual(_request_scope(params)[0], expected)

    def test_scope_dependencies_and_bounds_are_rejected(self) -> None:
        invalid_cases = [
            {"layer_name": "Issues"},
            {"annotation_name": "ReviewNotes", "frame_number": 12},
            {
                "annotation_name": "ReviewNotes",
                "layer_name": "Issues",
                "stroke_index": 0,
            },
            {"annotation_name": ""},
            {"annotation_name": "ReviewNotes", "frame_number": True},
            {
                "annotation_name": "ReviewNotes",
                "layer_name": "Issues",
                "frame_number": 1_048_575,
            },
            {
                "annotation_name": "ReviewNotes",
                "layer_name": "Issues",
                "frame_number": 12,
                "stroke_index": -1,
            },
        ]
        for params in invalid_cases:
            with self.subTest(params=params), self.assertRaises(InvalidParamsError):
                _request_scope(params)

    def test_pagination_round_trip(self) -> None:
        records = ["a", "b", "c", "d", "e"]
        first, page = _page_records(
            records, 2, 0, lambda value, index: {"index": index, "value": value}
        )
        self.assertEqual([item["value"] for item in first], ["a", "b"])
        self.assertEqual(page["next_cursor"], "2")
        self.assertTrue(page["truncated"])

        limit, offset = _pagination({"limit": 2, "cursor": page["next_cursor"]})
        second, page = _page_records(
            records,
            limit,
            offset,
            lambda value, index: {"index": index, "value": value},
        )
        self.assertEqual([item["value"] for item in second], ["c", "d"])
        self.assertEqual(page["next_cursor"], "4")

        _, offset = _pagination({"cursor": page["next_cursor"]})
        final, page = _page_records(
            records, 2, offset, lambda value, index: {"index": index, "value": value}
        )
        self.assertEqual([item["value"] for item in final], ["e"])
        self.assertIsNone(page["next_cursor"])
        self.assertFalse(page["truncated"])

    def test_invalid_pagination_is_rejected(self) -> None:
        invalid_cases = [
            {"limit": True},
            {"limit": 0},
            {"limit": 2001},
            {"cursor": -1},
            {"cursor": "-1"},
            {"cursor": "not-a-cursor"},
            {"cursor": "9" * 19},
        ]
        for params in invalid_cases:
            with self.subTest(params=params), self.assertRaises(InvalidParamsError):
                _pagination(params)


if __name__ == "__main__":
    unittest.main()
