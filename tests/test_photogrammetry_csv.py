"""Sanitized standard-GUI RealityScan CSV parser fixtures."""

from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools.photogrammetry_csv import (
    HEADER, parse_realityscan_csv, read_png_dimensions, timestamp_seconds,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "photogrammetry"


def _row(name, *, focal=45.0, k2=0.0):
    values = [name, 1, 2, 3, 10, 20, 30, focal, 0.01, -0.02,
              0, k2, 0, 0, 0, 0]
    return ",".join(map(str, values))


def _png(path: Path, width: int, height: int):
    body = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(body)) + b"IHDR" +
                     body + struct.pack(">I", zlib.crc32(b"IHDR" + body)))


class RealityScanCSVTests(unittest.TestCase):
    def _write(self, folder: Path, *rows: str, header=HEADER):
        path = folder / "cameras.csv"
        path.write_text(",".join(header) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
        return path

    def test_preserves_per_image_fields_and_reports_review_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            images = root / "Images"
            images.mkdir()
            names = [f"sample_seg01_{index:06d}_00-00-0{index}-000.png" for index in (1, 2, 3)]
            _png(images / names[0], 1920, 1080)
            path = self._write(root, _row(names[0], focal=45),
                               _row(names[1], focal=60, k2=5),
                               _row(names[2], focal=46))
            result = parse_realityscan_csv(path, images)
            self.assertEqual(result["camera_count"], 3)
            self.assertEqual([item["f_35mm"] for item in result["cameras"]], [45, 60, 46])
            self.assertEqual(result["cameras"][0]["image_size"], [1920, 1080])
            self.assertEqual(result["cameras"][0]["timestamp_seconds"], 1)
            codes = [item["code"] for item in result["warnings"]]
            self.assertIn("large_distortion", codes)
            self.assertIn("abrupt_focal_jump", codes)
            self.assertEqual(codes.count("image_missing"), 2)
            self.assertEqual(result["diagnostic_thresholds"]["focal_jump_fraction"], 0.30)

    def test_sanitized_source_fixture(self):
        result = parse_realityscan_csv(FIXTURES / "cameras.csv")
        self.assertEqual(result["camera_count"], 5)
        self.assertEqual(result["cameras"][0]["f_35mm"], 34.6502371865815)
        self.assertEqual(result["cameras"][4]["timestamp_seconds"], 3.233)
        self.assertEqual({camera["timestamp_group"] for camera in result["cameras"]},
                         {"clip_seg01"})
        points = [[float(value) for value in line.split()]
                  for line in (FIXTURES / "points.xyz").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(points), 5)
        self.assertTrue(all(len(point) == 3 for point in points))

    def test_rejects_header_duplicate_nonfinite_and_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            name = "sample_00-00-01-000.png"
            for rows, header in [
                ([_row(name)], ("name",) + HEADER[1:]),
                ([_row(name), _row(name.upper())], HEADER),
                ([_row(name, focal=float("nan"))], HEADER),
                ([_row(name, focal=0)], HEADER),
            ]:
                with self.subTest(rows=rows, header=header):
                    path = self._write(root, *rows, header=header)
                    with self.assertRaises(InvalidParamsError):
                        parse_realityscan_csv(path)

    def test_focal_jump_does_not_compare_independent_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._write(
                root,
                _row("clip_seg01_000001_00-00-01-000.png", focal=45),
                _row("clip_seg02_000001_00-00-01-500.png", focal=100),
                _row("clip_seg01_000002_00-00-02-000.png", focal=46),
                _row("clip_seg02_000002_00-00-02-500.png", focal=102),
            )
            result = parse_realityscan_csv(path)
            self.assertEqual(result["warning_counts"], {})
            self.assertEqual([item["timestamp_group"] for item in result["cameras"]],
                             ["clip_seg01", "clip_seg02", "clip_seg01", "clip_seg02"])

    def test_png_dimensions_and_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "sample.png"
            _png(image, 2560, 1440)
            self.assertEqual(read_png_dimensions(image), (2560, 1440))
            image.write_bytes(b"not a PNG")
            with self.assertRaises(InvalidParamsError):
                read_png_dimensions(image)
        self.assertEqual(timestamp_seconds("sample_seg01_00-01-02-345.png"), 62.345)
        self.assertIsNone(timestamp_seconds("sample.png"))


if __name__ == "__main__":
    unittest.main()
