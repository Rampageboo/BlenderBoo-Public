"""Pure-Python tests for bounded scene-name pagination."""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools.scene import _paginate_names, _pagination_params


class ScenePaginationTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(_pagination_params({}), (False, 200, 0))

    def test_cursor_round_trip_and_exhaustion(self) -> None:
        names = [{"name": f"Object{i}", "type": "MESH"} for i in range(5)]
        first, cursor, truncated = _paginate_names(names, 2, 0)
        self.assertEqual([item["name"] for item in first], ["Object0", "Object1"])
        self.assertEqual(cursor, "2")
        self.assertTrue(truncated)

        include_names, limit, offset = _pagination_params(
            {"include_names": True, "limit": 2, "cursor": cursor}
        )
        self.assertTrue(include_names)
        second, cursor, truncated = _paginate_names(names, limit, offset)
        self.assertEqual([item["name"] for item in second], ["Object2", "Object3"])
        self.assertEqual(cursor, "4")
        self.assertTrue(truncated)

        _, _, offset = _pagination_params({"cursor": cursor})
        final, cursor, truncated = _paginate_names(names, 2, offset)
        self.assertEqual([item["name"] for item in final], ["Object4"])
        self.assertIsNone(cursor)
        self.assertFalse(truncated)

    def test_invalid_pagination_params(self) -> None:
        invalid_cases = [
            {"include_names": 1},
            {"limit": True},
            {"limit": 0},
            {"limit": 2001},
            {"cursor": -1},
            {"cursor": "-1"},
            {"cursor": "opaque-but-invalid"},
            {"cursor": "9" * 10000},
        ]
        for params in invalid_cases:
            with self.subTest(params=params), self.assertRaises(InvalidParamsError):
                _pagination_params(params)


if __name__ == "__main__":
    unittest.main()
