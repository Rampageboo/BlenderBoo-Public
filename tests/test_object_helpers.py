"""Pure-Python tests for object lifecycle guards and shared destructive gating."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import (
    ConfirmationRequiredError,
    InvalidParamsError,
    LockedError,
    MutationApplyError,
    NameConflictError,
    StaleStateError,
)
from blenderboo.tools.destructive import prepare_destructive_action
from blenderboo.tools import object as object_tools


class FakeObject:
    def __init__(
        self,
        name: str,
        object_type: str = "MESH",
        collections: tuple[str, ...] = ("Scene Collection",),
        visible: bool = True,
        selected: bool = False,
    ) -> None:
        self.name = name
        self.type = object_type
        self.users_collection = [SimpleNamespace(name=item) for item in collections]
        self._visible = visible
        self._selected = selected
        self.parent = None

    def visible_get(self):
        return self._visible

    def select_get(self):
        return self._selected


class FakeMatrix:
    def __init__(self, value: float) -> None:
        self.value = value

    def __matmul__(self, other):
        return FakeMatrix(self.value * other.value)

    def inverted_safe(self):
        return FakeMatrix(1.0 / self.value)


class FailingVisibilityObject:
    def __init__(self) -> None:
        self.name = "Fragile"
        self.hide_viewport = False
        self.hide_select = False
        self._hide_render = False

    @property
    def hide_render(self):
        return self._hide_render

    @hide_render.setter
    def hide_render(self, value):
        self._hide_render = value
        if value:
            raise RuntimeError("RNA write failed")


class ObjectHelperTests(unittest.TestCase):
    def test_filters_are_and_combined_and_pagination_is_bounded(self) -> None:
        objects = [
            FakeObject("Hero_Cube", collections=("Stage",), selected=True),
            FakeObject("Hero_Light", "LIGHT", ("Stage",), selected=True),
            FakeObject("Hidden_Cube", collections=("Stage",), visible=False, selected=True),
            FakeObject("Hero_Elsewhere", collections=("Elsewhere",), selected=True),
        ]
        filtered = object_tools._filter_objects(
            objects,
            name="Hero",
            name_pattern=re.compile(r"Cube$"),
            object_type="MESH",
            collection="Stage",
            visible=True,
            selected=True,
        )
        self.assertEqual([item.name for item in filtered], ["Hero_Cube"])
        self.assertEqual(object_tools._page_params({"limit": 2, "cursor": "3"}), (2, 3))
        with self.assertRaises(InvalidParamsError):
            object_tools._page_params({"limit": 2001})

    def test_create_allowlist_rejects_before_bpy_lookup(self) -> None:
        with patch.object(object_tools, "get_bpy") as get_bpy, self.assertRaises(InvalidParamsError):
            object_tools.create_object({"type": "ARBITRARY_OPERATOR"})
        get_bpy.assert_not_called()

    def test_blender_style_name_collision_resolution_is_deterministic(self) -> None:
        existing = {"Cube": object(), "Cube.001": object()}
        self.assertEqual(object_tools._unique_object_name(existing, "Cube"), "Cube.002")
        self.assertEqual(object_tools._unique_object_name(existing, "Lamp"), "Lamp")

    def test_rename_collision_reports_the_conflicting_object(self) -> None:
        source = SimpleNamespace(name="Source")
        conflict = SimpleNamespace(name="Taken")
        objects = {"Source": source, "Taken": conflict}
        bpy = SimpleNamespace(
            context=SimpleNamespace(scene=SimpleNamespace(objects=objects)),
            data=SimpleNamespace(objects=objects),
        )
        with (
            patch.object(object_tools, "get_bpy", return_value=bpy),
            patch.object(object_tools, "require_document_write_access"),
            patch.object(object_tools, "check_expected_state"),
            self.assertRaises(NameConflictError) as raised,
        ):
            object_tools.rename_object({"object_name": "Source", "name": "Taken"})
        self.assertEqual(raised.exception.data["conflict"], {"type": "Object", "name": "Taken"})
        self.assertEqual(source.name, "Source")

    def test_transform_values_reject_wrong_lengths_types_and_non_finite_values(self) -> None:
        for value in ([1, 2], [1, 2, True], [1, 2, float("nan")], "1,2,3"):
            with self.subTest(value=value), self.assertRaises(InvalidParamsError):
                object_tools._vector3(value, "location")
        self.assertEqual(object_tools._vector3((1, 2.5, -3), "location"), [1.0, 2.5, -3.0])

    def test_mode_set_is_dry_run_first_and_rejects_unknown_modes(self) -> None:
        active = SimpleNamespace(name="Cube")
        bpy = SimpleNamespace(
            context=SimpleNamespace(
                mode="OBJECT",
                view_layer=SimpleNamespace(objects=SimpleNamespace(active=active)),
                scene=SimpleNamespace(objects={"Cube": active}),
            )
        )
        with (
            patch.object(object_tools, "get_bpy", return_value=bpy),
            patch.object(object_tools, "check_expected_state"),
            patch.object(object_tools, "current_state_token", return_value="state"),
            patch.object(object_tools, "require_document_write_access") as lease,
        ):
            planned = object_tools.set_object_mode({"mode": "EDIT"})
        self.assertTrue(planned["dry_run"])
        self.assertFalse(planned["changed"])
        lease.assert_not_called()
        with self.assertRaises(InvalidParamsError):
            object_tools.set_object_mode({"mode": "ARBITRARY"})

    def test_mode_set_failure_reports_partial_mode_change(self) -> None:
        active = SimpleNamespace(name="Cube")
        context = SimpleNamespace(
            mode="OBJECT",
            view_layer=SimpleNamespace(objects=SimpleNamespace(active=active)),
            scene=SimpleNamespace(objects={"Cube": active}),
        )

        def fail_mode_set(**_kwargs):
            context.mode = "SCULPT"
            raise RuntimeError("late mode failure")

        bpy = SimpleNamespace(context=context, ops=SimpleNamespace(object=SimpleNamespace(mode_set=fail_mode_set)))
        with (
            patch.object(object_tools, "get_bpy", return_value=bpy),
            patch.object(object_tools, "check_expected_state"),
            patch.object(object_tools, "require_document_write_access"),
            patch.object(object_tools, "current_state_token", return_value="state"),
            patch.object(object_tools, "bump_epoch") as bump,
            self.assertRaises(MutationApplyError) as raised,
        ):
            object_tools.set_object_mode({"mode": "EDIT", "dry_run": False})
        self.assertTrue(raised.exception.data["changed"])
        bump.assert_called_once_with()

    def test_world_to_local_matrix_includes_parent_inverse(self) -> None:
        obj = SimpleNamespace(
            parent=SimpleNamespace(matrix_world=FakeMatrix(10.0)),
            matrix_parent_inverse=FakeMatrix(0.5),
        )
        local = object_tools._world_to_local_matrix(obj, FakeMatrix(20.0))
        self.assertAlmostEqual(local.value, 4.0)
        desired = FakeMatrix(7.0)
        self.assertIs(
            object_tools._world_to_local_matrix(SimpleNamespace(parent=None), desired),
            desired,
        )

    def test_delete_gate_is_non_mutating_until_dry_run_false_and_confirm_true(self) -> None:
        journal = [{"op": "delete", "target": "Cube", "status": "planned"}]
        planned, apply = prepare_destructive_action(
            action="object.delete",
            affected_datablocks=[{"type": "Object", "name": "Cube"}],
            journal=journal,
            require_confirm=True,
            state_token="state-1",
        )
        self.assertFalse(apply)
        self.assertTrue(planned["dry_run"])
        self.assertFalse(planned["changed"])

        with self.assertRaises(ConfirmationRequiredError) as raised:
            prepare_destructive_action(
                action="object.delete",
                affected_datablocks=[{"type": "Object", "name": "Cube"}],
                journal=journal,
                dry_run=False,
                require_confirm=True,
                state_token="state-1",
            )
        self.assertFalse(raised.exception.data["changed"])
        self.assertEqual(raised.exception.data["affected_datablocks"][0]["name"], "Cube")

        ready, apply = prepare_destructive_action(
            action="object.delete",
            affected_datablocks=[{"type": "Object", "name": "Cube"}],
            journal=journal,
            dry_run=False,
            confirm=True,
            require_confirm=True,
            state_token="state-1",
        )
        self.assertTrue(apply)
        self.assertFalse(ready["changed"])

    def test_every_mutator_checks_the_document_lease_before_state_or_lookup(self) -> None:
        calls = [
            (object_tools.create_object, {"type": "EMPTY"}),
            (object_tools.duplicate_object, {"object_name": "A"}),
            (object_tools.rename_object, {"object_name": "A", "name": "B"}),
            (object_tools.set_object_visibility, {"objects": "A", "hide_render": True}),
            (object_tools.set_object_selection, {"objects": "A", "selected": True}),
            (object_tools.parent_object, {"object_name": "A", "parent": None, "dry_run": False}),
            (object_tools.transform_object, {"object_name": "A", "location": [1, 2, 3]}),
        ]
        lock = LockedError("held", {"held_by": "other"})
        for function, params in calls:
            with self.subTest(function=function.__name__):
                with (
                    patch.object(object_tools, "get_bpy", return_value=SimpleNamespace()),
                    patch.object(object_tools, "require_document_write_access", side_effect=lock) as guard,
                    patch.object(object_tools, "check_expected_state") as state_guard,
                    self.assertRaises(LockedError),
                ):
                    function(params)
            guard.assert_called_once()
            state_guard.assert_not_called()

    def test_delete_checks_the_lease_after_confirmation_but_before_removal(self) -> None:
        obj = SimpleNamespace(name="A", data=None, children=[])
        bpy = SimpleNamespace(context=SimpleNamespace(scene=SimpleNamespace(objects={"A": obj})))
        lock = LockedError("held", {"held_by": "other"})
        with (
            patch.object(object_tools, "get_bpy", return_value=bpy),
            patch.object(object_tools, "check_expected_state"),
            patch.object(object_tools, "_update_view_layer"),
            patch.object(object_tools, "_delete_report", return_value=([], [])),
            patch.object(object_tools, "_object_detail", return_value={"name": "A"}),
            patch.object(object_tools, "current_state_token", return_value="state"),
            patch.object(object_tools, "require_document_write_access", side_effect=lock) as guard,
            self.assertRaises(LockedError),
        ):
            object_tools.delete_object({"objects": "A", "dry_run": False, "confirm": True})
        guard.assert_called_once_with(bpy)

    def test_delete_missing_confirmation_never_reaches_the_lease_or_removal(self) -> None:
        obj = SimpleNamespace(name="A", data=None, children=[])
        bpy = SimpleNamespace(context=SimpleNamespace(scene=SimpleNamespace(objects={"A": obj})))
        with (
            patch.object(object_tools, "get_bpy", return_value=bpy),
            patch.object(object_tools, "check_expected_state"),
            patch.object(object_tools, "_update_view_layer"),
            patch.object(object_tools, "_delete_report", return_value=([], [])),
            patch.object(object_tools, "_object_detail", return_value={"name": "A"}),
            patch.object(object_tools, "current_state_token", return_value="state"),
            patch.object(object_tools, "require_document_write_access") as lease,
            self.assertRaises(ConfirmationRequiredError) as raised,
        ):
            object_tools.delete_object({"objects": "A", "dry_run": False})
        lease.assert_not_called()
        self.assertFalse(raised.exception.data["changed"])
        self.assertEqual(raised.exception.data["objects"], ["A"])

    def test_every_mutator_enforces_expected_state_before_target_lookup(self) -> None:
        calls = [
            (object_tools.create_object, {"type": "EMPTY", "expected_state": "stale"}),
            (object_tools.duplicate_object, {"object_name": "A", "expected_state": "stale"}),
            (object_tools.rename_object, {"object_name": "A", "name": "B", "expected_state": "stale"}),
            (object_tools.set_object_visibility, {"objects": "A", "hide_render": True, "expected_state": "stale"}),
            (object_tools.set_object_selection, {"objects": "A", "selected": True, "expected_state": "stale"}),
            (object_tools.parent_object, {"object_name": "A", "parent": None, "expected_state": "stale"}),
            (object_tools.transform_object, {"object_name": "A", "location": [1, 2, 3], "expected_state": "stale"}),
            (object_tools.delete_object, {"objects": "A", "expected_state": "stale"}),
        ]
        for function, params in calls:
            with self.subTest(function=function.__name__):
                with (
                    patch.object(object_tools, "get_bpy", return_value=SimpleNamespace()),
                    patch.object(object_tools, "require_document_write_access"),
                    patch.object(object_tools, "check_expected_state", side_effect=StaleStateError("stale")) as guard,
                    self.assertRaises(StaleStateError),
                ):
                    function(params)
            guard.assert_called_once()

    def test_every_tool_rejects_unknown_parameters(self) -> None:
        functions = (
            object_tools.inspect_object,
            object_tools.create_object,
            object_tools.duplicate_object,
            object_tools.rename_object,
            object_tools.set_object_visibility,
            object_tools.set_object_selection,
            object_tools.parent_object,
            object_tools.set_object_mode,
            object_tools.transform_object,
            object_tools.delete_object,
        )
        for function in functions:
            with self.subTest(function=function.__name__), self.assertRaises(InvalidParamsError):
                function({"unexpected": True})

    def test_visibility_partial_failure_keeps_journal_and_advances_state(self) -> None:
        obj = FailingVisibilityObject()
        bpy = SimpleNamespace(
            context=SimpleNamespace(scene=SimpleNamespace(objects={"Fragile": obj})),
        )
        with (
            patch.object(object_tools, "get_bpy", return_value=bpy),
            patch.object(object_tools, "require_document_write_access"),
            patch.object(object_tools, "check_expected_state"),
            patch.object(object_tools, "bump_epoch") as bump,
            patch.object(object_tools, "current_state_token", return_value="state-new"),
            self.assertRaises(MutationApplyError) as raised,
        ):
            object_tools.set_object_visibility({"objects": "Fragile", "hide_render": True})
        self.assertTrue(raised.exception.data["changed"])
        self.assertEqual(raised.exception.data["journal"][0]["status"], "failed")
        self.assertEqual(raised.exception.data["state_token"], "state-new")
        bump.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
