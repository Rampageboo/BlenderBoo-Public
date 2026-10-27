"""Pure adapter-side batch-planning contract tests; Blender is not required."""

from __future__ import annotations

import unittest

from blenderboo_mcp.workflow_batch import plan_batch, WorkflowBatchError


class WorkflowBatchTests(unittest.TestCase):
    def test_serial_plan_keeps_explicit_attribution_and_never_executes(self) -> None:
        result = plan_batch(
            [
                {
                    "id": "inspect_a",
                    "instance": "instance-a",
                    "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                },
                {
                    "id": "inspect_b",
                    "instance": "instance-a",
                    "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                },
            ]
        )

        self.assertEqual(result["manifest_version"], 1)
        self.assertEqual(result["job_count"], 2)
        self.assertEqual(result["instance_count"], 1)
        self.assertEqual(result["execution_waves"], [["inspect_a"], ["inspect_b"]])
        self.assertFalse(result["execution"]["performed"])
        self.assertFalse(result["execution"]["background_started"])
        self.assertFalse(result["jobs"][0]["run_request"]["execute"])
        self.assertEqual(result["jobs"][0]["run_request"]["instance"], "instance-a")

    def test_parallel_plan_requires_distinct_normalized_instances(self) -> None:
        with self.assertRaises(WorkflowBatchError):
            plan_batch(
                [
                    {
                        "id": "one",
                        "instance": 9877,
                        "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                    },
                    {
                        "id": "two",
                        "instance": "9877",
                        "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                    },
                ],
                scheduling="parallel",
            )

    def test_parallel_plan_groups_distinct_instances_in_one_wave(self) -> None:
        result = plan_batch(
            [
                {
                    "id": "one",
                    "instance": 9877,
                    "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                },
                {
                    "id": "two",
                    "instance": "instance-b",
                    "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                },
            ],
            scheduling="parallel",
            failure_policy="continue",
        )
        self.assertEqual(result["execution_waves"], [["one", "two"]])
        self.assertEqual(result["failure_policy"], "continue")

    def test_template_job_expands_to_plan_only_workflow(self) -> None:
        result = plan_batch(
            [
                {
                    "id": "animation_check",
                    "instance": "instance-a",
                    "template": "animation_health_v1",
                    "parameters": {"object_name": "Rig"},
                }
            ]
        )
        job = result["jobs"][0]
        self.assertEqual(job["source"]["kind"], "template")
        self.assertEqual(job["source"]["name"], "animation_health_v1")
        self.assertEqual(job["step_count"], 2)
        self.assertFalse(job["run_request"]["execute"])
        self.assertFalse(job["run_request"]["rollback_on_failure"])

        modeling = plan_batch(
            [
                {
                    "id": "repeat_panel",
                    "instance": "instance-a",
                    "template": "mirror_array_v1",
                    "parameters": {"object_name": "Panel", "expected_state": "state-a"},
                }
            ]
        )["jobs"][0]
        self.assertTrue(modeling["run_request"]["rollback_on_failure"])
        self.assertTrue(modeling["run_request"]["confirm_rollback"])

    def test_implicit_instance_duplicate_ids_and_mixed_sources_are_rejected(self) -> None:
        with self.assertRaises(WorkflowBatchError):
            plan_batch(
                [{"id": "implicit", "steps": [{"id": "inspect", "tool": "scene.inspect"}]}]
            )
        with self.assertRaises(WorkflowBatchError):
            plan_batch(
                [
                    {
                        "id": "same",
                        "instance": "instance-a",
                        "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                    },
                    {
                        "id": "same",
                        "instance": "instance-b",
                        "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                    },
                ]
            )
        with self.assertRaises(WorkflowBatchError):
            plan_batch(
                [
                    {
                        "id": "mixed",
                        "instance": "instance-a",
                        "steps": [{"id": "inspect", "tool": "scene.inspect"}],
                        "template": "scene_review_v1",
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
