"""Pure-Python tests for bounded architectural wall planning."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError, InvalidStateError
from blenderboo.tools.architecture import (
    build_walls,
    _build_wall_mesh,
    _plan_walls,
    _remove_created,
)


def _request(**overrides):
    request = {
        "name": "Floor_A_Walls",
        "wall_lines": [{"start": [0, 0], "end": [5, 0]}],
        "height": 3,
        "thickness": 0.2,
    }
    request.update(overrides)
    return request


class ArchitectureWallPlanningTests(unittest.TestCase):
    def test_plain_wall_is_one_watertight_box(self) -> None:
        plan = _plan_walls(_request())

        self.assertEqual(plan["total_counts"], {
            "vertices": 8,
            "edges": 12,
            "faces": 6,
            "loops": 24,
        })
        self.assertEqual(plan["plans"][0]["name"], "Floor_A_Walls")
        self.assertEqual(plan["total_openings"], 0)
        geometry = plan["plans"][0]["geometry"]
        signed_volume = 0.0
        for face in geometry["faces"]:
            first = geometry["vertices"][face[0]]
            for index in range(1, len(face) - 1):
                second = geometry["vertices"][face[index]]
                third = geometry["vertices"][face[index + 1]]
                cross = (
                    second[1] * third[2] - second[2] * third[1],
                    second[2] * third[0] - second[0] * third[2],
                    second[0] * third[1] - second[1] * third[0],
                )
                signed_volume += sum(first[axis] * cross[axis] for axis in range(3)) / 6
        self.assertAlmostEqual(signed_volume, 5 * 0.2 * 3)

    def test_multiple_door_and_window_openings_are_real_mesh_voids(self) -> None:
        plan = _plan_walls(_request(
            wall_lines=[{
                "start": [0, 0],
                "end": [8, 0],
                "openings": [
                    {"id": "Door_A", "kind": "DOOR", "offset": 1, "width": 1, "height": 2.1},
                    {"id": "Window_A", "kind": "WINDOW", "offset": 4, "width": 2, "bottom": 1, "height": 1.2},
                ],
            }],
        ))
        geometry = plan["plans"][0]["geometry"]

        self.assertEqual(plan["total_openings"], 2)
        self.assertGreater(geometry["counts"]["faces"], 6)
        for face in geometry["faces"]:
            points = [geometry["vertices"][index] for index in face]
            if len({round(point[1], 8) for point in points}) != 1:
                continue
            center_x = sum(point[0] for point in points) / 4
            center_z = sum(point[2] for point in points) / 4
            self.assertFalse(1 < center_x < 2 and 0 < center_z < 2.1)
            self.assertFalse(4 < center_x < 6 and 1 < center_z < 2.2)

        edge_uses = Counter()
        for face in geometry["faces"]:
            for left, right in zip(face, face[1:] + face[:1]):
                edge_uses[tuple(sorted((left, right)))] += 1
        self.assertEqual(set(edge_uses.values()), {2})

    def test_rotated_wall_preserves_length_and_thickness(self) -> None:
        line = _plan_walls(_request(
            wall_lines=[{"start": [1, 2], "end": [4, 6]}],
            thickness=0.4,
            base_z=2,
        ))["plans"][0]["line"]
        geometry = _build_wall_mesh(line, thickness=0.4, height=3, base_z=2)

        self.assertAlmostEqual(line["length"], 5)
        self.assertEqual({round(vertex[2], 8) for vertex in geometry["vertices"]}, {2, 5})
        start_vertices = [vertex for vertex in geometry["vertices"] if abs(
            (vertex[0] - 1) * 0.6 + (vertex[1] - 2) * 0.8
        ) < 1e-7]
        self.assertEqual(len(start_vertices), 4)

    def test_many_lines_receive_deterministic_names(self) -> None:
        plan = _plan_walls(_request(wall_lines=[
            {"start": [0, 0], "end": [4, 0]},
            {"start": [4, 0], "end": [4, 3]},
        ]))
        self.assertEqual(
            [item["name"] for item in plan["plans"]],
            ["Floor_A_Walls_001", "Floor_A_Walls_002"],
        )

    def test_openings_must_be_bounded_non_overlapping_and_semantic(self) -> None:
        with self.assertRaisesRegex(InvalidParamsError, "must not overlap"):
            _plan_walls(_request(wall_lines=[{
                "start": [0, 0], "end": [5, 0],
                "openings": [
                    {"offset": 1, "width": 2, "height": 2},
                    {"offset": 2, "width": 1, "height": 1},
                ],
            }]))
        with self.assertRaisesRegex(InvalidParamsError, "extends beyond"):
            _plan_walls(_request(wall_lines=[{
                "start": [0, 0], "end": [5, 0],
                "openings": [{"offset": 4.5, "width": 1, "height": 2}],
            }]))
        with self.assertRaisesRegex(InvalidParamsError, "bottom must be 0"):
            _plan_walls(_request(wall_lines=[{
                "start": [0, 0], "end": [5, 0],
                "openings": [{"kind": "DOOR", "offset": 1, "width": 1, "bottom": 0.2, "height": 2}],
            }]))
        with self.assertRaisesRegex(InvalidParamsError, "duplicate ids"):
            _plan_walls(_request(wall_lines=[{
                "start": [0, 0], "end": [5, 0],
                "openings": [
                    {"id": "Door_A", "offset": 1, "width": 1, "height": 2},
                    {"id": "Door_A", "offset": 3, "width": 1, "height": 2},
                ],
            }]))

    def test_vertically_separated_openings_may_share_wall_offset(self) -> None:
        plan = _plan_walls(_request(height=6, wall_lines=[{
            "start": [0, 0], "end": [5, 0],
            "openings": [
                {"id": "Lower", "offset": 1, "width": 2, "bottom": 0, "height": 2},
                {"id": "Upper", "offset": 1, "width": 2, "bottom": 3, "height": 2},
            ],
        }]))
        self.assertEqual(
            [item["id"] for item in plan["plans"][0]["line"]["openings"]],
            ["Lower", "Upper"],
        )

    def test_openings_cannot_remove_an_entire_wall(self) -> None:
        with self.assertRaisesRegex(InvalidParamsError, "entire wall"):
            _plan_walls(_request(wall_lines=[{
                "start": [0, 0], "end": [5, 0],
                "openings": [{"offset": 0, "width": 5, "height": 3}],
            }]))

    def test_element_budget_rejects_before_blender_mutation(self) -> None:
        with self.assertRaisesRegex(InvalidStateError, "max_elements 1"):
            _plan_walls(_request(max_elements=1))

    def test_invalid_dry_run_is_rejected_before_blender_access(self) -> None:
        with patch(
            "blenderboo.tools.architecture.get_bpy",
            side_effect=AssertionError("Blender must not be loaded"),
        ):
            with self.assertRaisesRegex(InvalidParamsError, "dry_run must be a boolean"):
                build_walls(_request(dry_run="false"))

    def test_live_apply_checks_lease_before_expected_state(self) -> None:
        events = []
        fake_bpy = object()

        def lease(_bpy):
            events.append("lease")

        def state(_expected, _bpy):
            events.append("state")
            raise RuntimeError("stop after ordering check")

        with (
            patch("blenderboo.tools.architecture.get_bpy", return_value=fake_bpy),
            patch("blenderboo.tools.architecture.require_document_write_access", side_effect=lease),
            patch("blenderboo.tools.architecture.check_expected_state", side_effect=state),
        ):
            with self.assertRaisesRegex(RuntimeError, "ordering check"):
                build_walls(_request(dry_run=False))
        self.assertEqual(events, ["lease", "state"])

    def test_cleanup_continues_after_an_object_removal_failure(self) -> None:
        attempts = []

        class Item:
            def __init__(self, name):
                self.name = name
                self.users = 0

        class Items:
            def __init__(self, values, fail_name=None):
                self.values = {item.name: item for item in values}
                self.fail_name = fail_name

            def get(self, name):
                return self.values.get(name)

            def remove(self, item, **_kwargs):
                attempts.append(item.name)
                if item.name == self.fail_name:
                    raise RuntimeError("injected cleanup failure")
                self.values.pop(item.name, None)

        first_object, second_object = Item("Wall_A"), Item("Wall_B")
        first_mesh, second_mesh = Item("Wall_A_Mesh"), Item("Wall_B_Mesh")
        fake_bpy = type("Bpy", (), {"data": type("Data", (), {
            "objects": Items([first_object, second_object], fail_name="Wall_B"),
            "meshes": Items([first_mesh, second_mesh]),
        })()})()

        errors = _remove_created(
            fake_bpy,
            [(first_object, first_mesh), (second_object, second_mesh)],
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("Wall_B", errors[0])
        self.assertEqual(
            attempts,
            ["Wall_B", "Wall_B_Mesh", "Wall_A", "Wall_A_Mesh"],
        )


if __name__ == "__main__":
    unittest.main()
