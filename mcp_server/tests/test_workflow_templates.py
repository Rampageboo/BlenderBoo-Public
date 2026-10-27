"""Pure adapter-side workflow-template contract tests; Blender is not required."""

from __future__ import annotations

import unittest

from blenderboo_mcp.workflow_templates import (
    describe_template,
    expand_template,
    list_templates,
    WorkflowTemplateError,
)


class WorkflowTemplateTests(unittest.TestCase):
    def test_catalog_is_versioned_and_digest_stable_within_process(self) -> None:
        first = list_templates()
        second = list_templates()
        self.assertEqual(first["count"], 7)
        self.assertEqual(first["catalog_digest"], second["catalog_digest"])
        self.assertEqual(
            {item["name"] for item in first["templates"]},
            {
                "scene_review_v1",
                "object_move_review_v1",
                "animation_health_v1",
                "boolean_opening_v1",
                "mirror_array_v1",
                "panel_inset_v1",
                "support_loop_pair_v1",
            },
        )

    def test_describe_returns_parameter_contract_without_expanding(self) -> None:
        result = describe_template("object_move_review_v1")
        self.assertEqual(result["version"], 1)
        self.assertEqual(
            set(result["parameter_schema"]["required"]),
            {"object_name", "location"},
        )
        self.assertEqual(result["step_outline"][2]["tool"], "object.transform")
        self.assertTrue(result["step_outline"][2]["has_rollback"])

    def test_scene_review_expansion_applies_bounded_defaults_and_omits_none(self) -> None:
        result = expand_template("scene_review_v1")
        review = result["steps"][1]
        self.assertEqual(review["tool"], "render.review_set")
        self.assertNotIn("output_dir", review["arguments"])
        self.assertEqual(review["arguments"]["width"], 960)
        self.assertEqual(review["arguments"]["views"], ["camera", "top", "side", "three_quarter"])

    def test_object_move_expands_state_precondition_and_exact_compensation(self) -> None:
        result = expand_template(
            "object_move_review_v1",
            {"object_name": "Hero", "location": [1.0, 2.0, 3.0]},
        )
        move = result["steps"][2]
        self.assertEqual(move["arguments"]["expected_state"], {"$ref": "inspect.data.state_token"})
        self.assertEqual(move["rollback"]["arguments"]["space"], "local")
        self.assertEqual(
            move["rollback"]["arguments"]["location"],
            {"$ref": "inspect.data.object_detail.transform.location"},
        )
        self.assertEqual(
            move["rollback"]["arguments"]["expected_state"],
            {"$ref": "move.data.state_token"},
        )

    def test_animation_optional_parameters_are_omitted(self) -> None:
        result = expand_template("animation_health_v1", {"object_name": "Rig"})
        inspect = result["steps"][0]["arguments"]
        validate = result["steps"][1]["arguments"]
        self.assertNotIn("action_name", inspect)
        self.assertNotIn("frame_start", validate)
        self.assertNotIn("frame_end", validate)

    def test_boolean_opening_is_exact_target_and_removable(self) -> None:
        result = expand_template(
            "boolean_opening_v1",
            {
                "object_name": "Panel",
                "cutter_object": "OpeningCutter",
                "expected_state": "state-a",
            },
        )
        step = result["steps"][0]
        self.assertEqual(step["tool"], "modifier.add")
        self.assertEqual(step["arguments"]["type"], "BOOLEAN")
        self.assertEqual(step["arguments"]["settings"]["operation"], "DIFFERENCE")
        self.assertEqual(step["arguments"]["settings"]["object"], "OpeningCutter")
        self.assertEqual(
            step["rollback"]["arguments"]["modifier_name"],
            {"$ref": "add_boolean.data.modifier"},
        )
        self.assertEqual(
            result["steps"][1]["arguments"]["modifier_name"],
            {"$ref": "add_boolean.data.modifier"},
        )
        self.assertEqual(
            result["run_options"],
            {"rollback_on_failure": True, "confirm_rollback": True},
        )

    def test_mirror_array_chains_state_and_enables_compensation(self) -> None:
        result = expand_template(
            "mirror_array_v1",
            {"object_name": "Panel", "expected_state": "state-a", "count": 4},
        )
        mirror, array, verify = result["steps"]
        self.assertNotIn("mirror_object", mirror["arguments"]["settings"])
        self.assertEqual(array["arguments"]["settings"]["count"], 4)
        self.assertEqual(
            array["arguments"]["expected_state"],
            {"$ref": "add_mirror.data.state_token"},
        )
        self.assertEqual(
            result["run_options"],
            {"rollback_on_failure": True, "confirm_rollback": True},
        )
        self.assertEqual(
            verify["arguments"]["modifier_name"],
            {"$ref": "add_array.data.modifier"},
        )

    def test_panel_and_support_templates_expand_to_atomic_mesh_edits(self) -> None:
        panel = expand_template(
            "panel_inset_v1",
            {
                "object_name": "Panel",
                "face_indices": [2, 4],
                "expected_state": "state-a",
                "thickness": 0.05,
            },
        )
        panel_args = panel["steps"][0]["arguments"]
        self.assertTrue(panel_args["dry_run"])
        self.assertNotIn("confirm", panel_args)
        self.assertEqual(panel_args["operations"][0]["op"], "inset")
        self.assertEqual(
            panel_args["operations"][0]["selection"]["predicate"]["indices"],
            [2, 4],
        )

        support = expand_template(
            "support_loop_pair_v1",
            {
                "object_name": "Panel",
                "edge_indices": [1, 3],
                "expected_state": "state-b",
                "offset": 0.01,
                "dry_run": False,
                "confirm": True,
            },
        )
        support_args = support["steps"][0]["arguments"]
        self.assertFalse(support_args["dry_run"])
        self.assertTrue(support_args["confirm"])
        self.assertEqual(support_args["operations"][0]["segments"], 1)

    def test_modeling_template_cross_parameter_guards(self) -> None:
        with self.assertRaises(WorkflowTemplateError):
            expand_template(
                "boolean_opening_v1",
                {
                    "object_name": "Panel",
                    "cutter_object": "Panel",
                    "expected_state": "state-a",
                },
            )
        with self.assertRaises(WorkflowTemplateError):
            expand_template(
                "mirror_array_v1",
                {
                    "object_name": "Panel",
                    "expected_state": "state-a",
                    "use_axis": [False, False, False],
                },
            )
        with self.assertRaises(WorkflowTemplateError):
            expand_template(
                "panel_inset_v1",
                {
                    "object_name": "Panel",
                    "face_indices": [0],
                    "expected_state": "state-a",
                    "thickness": 0.05,
                    "dry_run": False,
                },
            )

    def test_unknown_or_invalid_parameters_are_rejected(self) -> None:
        with self.assertRaises(WorkflowTemplateError):
            expand_template("scene_review_v1", {"arbitrary_code": "pass"})
        with self.assertRaises(WorkflowTemplateError):
            expand_template(
                "object_move_review_v1",
                {"object_name": "Hero", "location": [1.0, 2.0]},
            )
        with self.assertRaises(WorkflowTemplateError):
            expand_template(
                "animation_health_v1",
                {"object_name": "Rig", "frame_start": 20, "frame_end": 10},
            )


if __name__ == "__main__":
    unittest.main()
