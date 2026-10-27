"""PLY header and record decoding for both text and binary payloads."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.tools.interchange import _ply_header, _ply_records


class PlyReaderTests(unittest.TestCase):
    def test_ascii_vertex_colors_normals_and_face(self) -> None:
        stream = BytesIO(
            b"ply\nformat ascii 1.0\nelement vertex 3\n"
            b"property float x\nproperty float y\nproperty float z\n"
            b"property float nx\nproperty uchar red\n"
            b"element face 1\nproperty list uchar int vertex_indices\nend_header\n"
            b"0 0 0 1 255\n1 0 0 1 0\n0 1 0 1 0\n3 0 1 2\n"
        )
        encoding, elements = _ply_header(stream)
        vertices = list(_ply_records(stream, encoding, elements[0][1], elements[0][2]))
        faces = list(_ply_records(stream, encoding, elements[1][1], elements[1][2]))
        self.assertEqual(vertices[0]["red"], 255)
        self.assertEqual(vertices[0]["nx"], 1.0)
        self.assertEqual(faces[0]["vertex_indices"], [0, 1, 2])

    def test_binary_big_endian_face_list(self) -> None:
        header = (
            b"ply\nformat binary_big_endian 1.0\nelement vertex 3\n"
            b"property float x\nproperty float y\nproperty float z\n"
            b"element face 1\nproperty list uchar int vertex_indices\nend_header\n"
        )
        payload = b"".join(struct.pack(">fff", *point) for point in ((0, 0, 0), (1, 0, 0), (0, 1, 0)))
        payload += struct.pack(">Biii", 3, 0, 1, 2)
        stream = BytesIO(header + payload)
        encoding, elements = _ply_header(stream)
        vertices = list(_ply_records(stream, encoding, elements[0][1], elements[0][2]))
        faces = list(_ply_records(stream, encoding, elements[1][1], elements[1][2]))
        self.assertEqual(vertices[1]["x"], 1.0)
        self.assertEqual(faces[0]["vertex_indices"], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
