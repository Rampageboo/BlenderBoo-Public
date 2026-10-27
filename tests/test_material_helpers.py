"""Pure-Python guards for typed material operations without importing bpy."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError, LockedError, MutationApplyError, StaleStateError
from blenderboo.tools.material import _socket_lookup, _validate_precondition, inspect_material, patch_material
from blenderboo.tools import session_state
from blenderboo.tools.transaction import new_transaction


class FakeSockets(list):
    def get(self, name):
        return next((socket for socket in self if socket.name == name), None)


class FakeSocket:
    def __init__(self, name, value=0.0):
        self.name = name
        self.identifier = name
        self.default_value = value
        self.is_linked = False
        self.links = []


class FakeNode:
    def __init__(self, name, node_type="ShaderNodeBsdfPrincipled"):
        self.name = name
        self.bl_idname = node_type
        self.inputs = FakeSockets([FakeSocket("Roughness", 0.5)])
        self.outputs = FakeSockets([FakeSocket("BSDF")])


class FakeNodes(list):
    def get(self, name):
        return next((node for node in self if node.name == name), None)


class FakeMaterial:
    def __init__(self):
        self.name = "Leaf"
        self.users = 3
        self.use_nodes = True
        self.use_backface_culling = False
        self.node_tree = type("Tree", (), {"nodes": FakeNodes([FakeNode("Principled")]), "links": []})()


class MaterialHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.material = FakeMaterial()
        self.bpy = type(
            "Bpy",
            (),
            {"data": type("Data", (), {"materials": {"Leaf": self.material}, "filepath": "fake.blend"})},
        )()

    def test_unknown_operation_is_rejected_before_mutation(self) -> None:
        with patch("blenderboo.tools.material.get_bpy", return_value=self.bpy):
            with self.assertRaisesRegex(InvalidParamsError, "op must be one of"):
                patch_material({"material": "Leaf", "operations": [{"op": "arbitrary_python"}]})
        self.assertFalse(self.material.use_backface_culling)

    def test_precondition_mismatch_aborts_before_mutation(self) -> None:
        with patch("blenderboo.tools.material.get_bpy", return_value=self.bpy):
            with self.assertRaisesRegex(InvalidParamsError, "Precondition failed"):
                patch_material({
                    "material": "Leaf",
                    "preconditions": [{"kind": "flag_equals", "flag": "use_backface_culling", "value": True}],
                    "operations": [{"op": "set_material_flag", "flag": "use_backface_culling", "value": True}],
                })
        self.assertFalse(self.material.use_backface_culling)

    def test_full_operation_validation_aborts_before_the_first_apply(self) -> None:
        with patch("blenderboo.tools.material.get_bpy", return_value=self.bpy):
            with self.assertRaises(InvalidParamsError):
                patch_material({
                    "material": "Leaf", "dry_run": False,
                    "operations": [
                        {"op": "set_material_flag", "flag": "use_backface_culling", "value": True},
                        {"op": "not_allowlisted"},
                    ],
                })
        self.assertFalse(self.material.use_backface_culling)

    def test_stale_state_aborts_before_material_lookup_or_mutation(self) -> None:
        with (
            patch.object(session_state, "_epoch", 0),
            patch.object(session_state, "_last_filepath", None),
            patch("blenderboo.tools.material.get_bpy", return_value=self.bpy),
            self.assertRaises(StaleStateError),
        ):
            patch_material({
                "material": "Leaf",
                "operations": [
                    {"op": "set_material_flag", "flag": "use_backface_culling", "value": True}
                ],
                "dry_run": False,
                "expected_state": "stale-token",
            })
        self.assertFalse(self.material.use_backface_culling)

    def test_live_apply_checks_document_lease_before_material_lookup(self) -> None:
        lock = LockedError(
            "held",
            {"active": True, "scope": "document", "held_by": "other", "expires_in": 30},
        )
        with (
            patch("blenderboo.tools.material.get_bpy", return_value=self.bpy),
            patch(
                "blenderboo.tools.material.require_document_write_access",
                side_effect=lock,
            ) as guard,
            self.assertRaises(LockedError),
        ):
            patch_material({
                "material": "Leaf",
                "operations": [
                    {"op": "set_material_flag", "flag": "use_backface_culling", "value": True}
                ],
                "dry_run": False,
            })
        guard.assert_called_once_with(self.bpy)
        self.assertFalse(self.material.use_backface_culling)

    def test_expected_state_must_be_a_non_empty_string(self) -> None:
        with self.assertRaises(InvalidParamsError):
            patch_material({
                "material": "Leaf",
                "operations": [{"op": "set_material_flag"}],
                "expected_state": None,
            })

    def test_partial_apply_failure_advances_state_and_preserves_journal(self) -> None:
        operations = [
            {"op": "set_material_flag", "flag": "use_backface_culling", "value": True},
            {"op": "set_material_flag", "flag": "use_backface_culling", "value": False},
        ]
        with (
            patch.object(session_state, "_epoch", 0),
            patch.object(session_state, "_last_filepath", None),
            patch("blenderboo.tools.material.get_bpy", return_value=self.bpy),
        ):
            before = session_state.current_state_token(self.bpy)
            with (
                patch(
                    "blenderboo.tools.material._apply_operation",
                    side_effect=["applied", RuntimeError("apply failed")],
                ),
                self.assertRaises(MutationApplyError) as raised,
            ):
                patch_material({
                    "material": "Leaf",
                    "operations": operations,
                    "dry_run": False,
                    "expected_state": before,
                })
            partial = raised.exception.data
            self.assertTrue(partial["changed"])
            self.assertEqual(
                [entry["status"] for entry in partial["journal"]],
                ["applied", "failed"],
            )
            self.assertNotEqual(partial["state_token"], before)

    def test_inspect_rejects_unknown_parameters_without_bpy(self) -> None:
        with self.assertRaises(InvalidParamsError):
            inspect_material({"unexpected": True})

    def test_dry_run_defaults_true_and_returns_planned_journal(self) -> None:
        with (
            patch("blenderboo.tools.material.get_bpy", return_value=self.bpy),
            patch(
                "blenderboo.tools.material.require_document_write_access"
            ) as guard,
        ):
            result = patch_material({
                "material": "Leaf",
                "operations": [
                    {"op": "set_input", "node": "Principled", "input": "Roughness", "value": 0.2},
                    {"op": "set_material_flag", "flag": "use_backface_culling", "value": True},
                ],
            })
        guard.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["changed"])
        self.assertEqual([entry["status"] for entry in result["journal"]], ["planned", "planned"])
        self.assertEqual(set(result["journal"][0]), {"op", "target", "before", "after", "status"})
        self.assertEqual(self.material.node_tree.nodes[0].inputs[0].default_value, 0.5)

    def test_already_present_value_does_not_report_a_change(self) -> None:
        with patch("blenderboo.tools.material.get_bpy", return_value=self.bpy):
            result = patch_material({
                "material": "Leaf", "dry_run": False,
                "operations": [{"op": "set_material_flag", "flag": "use_backface_culling", "value": False}],
            })
        self.assertFalse(result["changed"])
        self.assertEqual(result["journal"][0]["status"], "present")

    def test_precondition_helper_checks_node_presence(self) -> None:
        _validate_precondition({"kind": "node_present", "name": "Principled", "present": True}, self.material.node_tree.nodes, self.material)
        with self.assertRaises(InvalidParamsError):
            _validate_precondition({"kind": "node_present", "name": "Missing", "present": True}, self.material.node_tree.nodes, self.material)

    def test_disconnect_dry_run_and_apply_report_actual_removed_link(self) -> None:
        source = FakeNode("Source")
        target = self.material.node_tree.nodes[0]
        link = type("Link", (), {"from_node": source, "from_socket": source.outputs[0]})()
        target.inputs[0].links.append(link)
        target.inputs[0].is_linked = True
        self.material.node_tree.links = type("Links", (list,), {"remove": lambda self, item: (list.remove(self, item), item.to_socket.links.remove(item))})()
        link.to_socket = target.inputs[0]
        self.material.node_tree.links.append(link)
        operation = {"op": "disconnect", "to_node": "Principled", "to_input": "Roughness"}
        with patch("blenderboo.tools.material.get_bpy", return_value=self.bpy):
            preview = patch_material({"material": "Leaf", "operations": [operation]})
            self.assertEqual(preview["journal"][0]["before"], [{"from_node": "Source", "from_socket": "BSDF"}])
            self.assertEqual(preview["journal"][0]["after"], [])
            applied = patch_material({"material": "Leaf", "operations": [operation], "dry_run": False})
            self.assertEqual(applied["journal"][0]["status"], "applied")
            self.assertEqual(patch_material({"material": "Leaf", "operations": [operation], "dry_run": False})["journal"][0]["status"], "absent")

    def test_identifier_resolves_duplicate_display_names(self) -> None:
        first, second = FakeSocket("Weight"), FakeSocket("Weight")
        first.identifier, second.identifier = "Weight_0", "Weight_1"
        sockets = FakeSockets([first, second])
        self.assertIs(_socket_lookup(sockets, "Weight_1", "input"), second)
        with self.assertRaisesRegex(InvalidParamsError, "ambiguous"):
            _socket_lookup(sockets, "Weight", "input")

    def test_transaction_ids_are_deterministic_and_monotonic(self) -> None:
        first = new_transaction([{"type": "Material", "name": "Leaf"}])
        second = new_transaction([{"type": "Material", "name": "Leaf"}])
        self.assertRegex(first["operation_id"], r"^op-\d{8}$")
        self.assertEqual(int(second["operation_id"][-8:]), int(first["operation_id"][-8:]) + 1)
        self.assertFalse(first["changed"])


if __name__ == "__main__":
    unittest.main()
