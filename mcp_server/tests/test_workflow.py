"""Pure adapter-side workflow contract tests; Blender is not required."""

from __future__ import annotations

import unittest

from blenderboo_mcp.workflow import (
    run_workflow,
    validate_steps,
    WorkflowReplayCache,
    WorkflowValidationError,
)


class WorkflowTests(unittest.TestCase):
    def test_live_catalog_rejects_unknown_tool_before_execution(self) -> None:
        calls = []
        result = run_workflow(
            lambda *args: calls.append(args),
            steps=[{"id": "bad", "tool": "material.slot.assign"}],
            execute=True,
            rollback_on_failure=False,
            confirm_rollback=None,
            instance=None,
            catalog={"scene.inspect": {"type": "object", "properties": {}}},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        self.assertIn("unknown", result["error"]["message"])

    def test_preflight_checks_static_params_and_defers_references(self) -> None:
        catalog = {
            "scene.inspect": {"type": "object", "properties": {}, "additionalProperties": False},
            "object.rename": {"type": "object", "properties": {"name": {"type": "string"}, "expected_state": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
        }
        result = run_workflow(
            lambda *args: self.fail("plan executed a step"),
            steps=[{"id": "inspect", "tool": "scene.inspect"}, {"id": "rename", "tool": "object.rename", "arguments": {"name": "New", "expected_state": {"$ref": "inspect.data.state_token"}}}],
            execute=False,
            rollback_on_failure=False,
            confirm_rollback=None,
            instance=None,
            catalog=catalog,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["deferred_checks"], ["steps.rename.forward.arguments.expected_state"])
        invalid = run_workflow(
            lambda *args: self.fail("invalid plan executed a step"),
            steps=[{"id": "rename", "tool": "object.rename", "arguments": {"name": 3}}],
            execute=False, rollback_on_failure=False, confirm_rollback=None, instance=None, catalog=catalog,
        )
        self.assertFalse(invalid["ok"])

    def test_python_exec_is_admitted_as_workflow_step(self) -> None:
        steps = validate_steps([{"id": "script", "tool": "python.exec", "arguments": {"code": "result = 1"}}])
        self.assertEqual(steps[0]["tool"], "python.exec")
        self.assertEqual(steps[0]["arguments"]["code"], "result = 1")

    def test_plan_resolves_only_prior_step_ids_without_calling(self) -> None:
        calls = []
        result = run_workflow(
            lambda *args: calls.append(args),
            steps=[
                {"id": "inspect", "tool": "scene.inspect"},
                {"id": "rename", "tool": "object.rename", "arguments": {"expected_state": {"$ref": "inspect.data.state_token"}}},
            ],
            execute=False,
            rollback_on_failure=False,
            confirm_rollback=None,
            instance=None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [])
        self.assertEqual(result["data"]["step_count"], 2)
        self.assertEqual(result["data"]["manifest_version"], 1)
        self.assertEqual(result["data"]["progress"]["state"], "planned")
        self.assertEqual(result["data"]["progress"]["remaining_steps"], 2)
        self.assertFalse(result["data"]["progress"]["cancellation"]["available"])

    def test_forward_reference_is_rejected(self) -> None:
        with self.assertRaises(WorkflowValidationError):
            validate_steps([
                {"id": "first", "tool": "object.rename", "arguments": {"expected_state": {"$ref": "later.data.state_token"}}},
                {"id": "later", "tool": "scene.inspect"},
            ])

    def test_mcp_only_workflow_and_diagnostic_tools_cannot_be_nested(self) -> None:
        for tool in (
            "workflow.template",
            "workflow.batch_plan",
            "workflow.job_start",
            "workflow.job_control",
            "adapter.schema_audit",
        ):
            with self.subTest(tool=tool), self.assertRaises(WorkflowValidationError):
                validate_steps([{"id": "meta", "tool": tool}])

    def test_execute_resolves_envelope_reference(self) -> None:
        calls = []

        def caller(tool, arguments, instance):
            calls.append((tool, arguments, instance))
            if tool == "scene.inspect":
                return {"ok": True, "tool": tool, "data": {"state_token": "state-a"}, "error": None, "warnings": []}
            return {"ok": True, "tool": tool, "data": {}, "error": None, "warnings": []}

        result = run_workflow(
            caller,
            steps=[
                {"id": "inspect", "tool": "scene.inspect"},
                {"id": "rename", "tool": "object.rename", "arguments": {"expected_state": {"$ref": "inspect.data.state_token"}}},
            ],
            execute=True,
            rollback_on_failure=False,
            confirm_rollback=None,
            instance="instance-a",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls[1], ("object.rename", {"expected_state": "state-a"}, "instance-a"))
        self.assertEqual(result["data"]["progress"]["state"], "completed")
        self.assertEqual(result["data"]["progress"]["completed_steps"], 2)
        self.assertEqual(result["data"]["progress"]["fraction"], 1.0)

    def test_confirmed_compensation_runs_in_reverse_after_failure(self) -> None:
        calls = []

        def caller(tool, arguments, instance):
            calls.append(tool)
            ok = tool != "object.transform"
            return {
                "ok": ok,
                "tool": tool,
                "data": {} if ok else {"changed": False},
                "error": None if ok else {"type": "Failure", "message": "no", "traceback": ""},
                "warnings": [],
            }

        result = run_workflow(
            caller,
            steps=[
                {"id": "one", "tool": "object.rename", "rollback": {"tool": "object.rename", "arguments": {}}},
                {"id": "two", "tool": "object.duplicate", "rollback": {"tool": "object.delete", "arguments": {"confirm": True}}},
                {"id": "fail", "tool": "object.transform"},
            ],
            execute=True,
            rollback_on_failure=True,
            confirm_rollback=True,
            instance=None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["object.rename", "object.duplicate", "object.transform", "object.delete", "object.rename"])
        self.assertTrue(result["data"]["rollback"]["complete"])
        self.assertEqual(result["data"]["progress"]["state"], "failed")
        self.assertEqual(result["data"]["progress"]["completed_steps"], 2)
        self.assertEqual(result["data"]["progress"]["failed_steps"], 1)
        self.assertFalse(result["data"]["partial_change_possible"])
        self.assertEqual(
            result["data"]["failed_step_change"],
            {"explicit": True, "possible": False},
        )

    def test_failed_active_step_partial_change_survives_completed_compensation(self) -> None:
        def caller(tool, arguments, instance):
            if tool == "object.transform":
                return {
                    "ok": False,
                    "tool": tool,
                    "data": {"changed": True},
                    "error": {"type": "MutationApplyError", "message": "partial", "traceback": ""},
                    "warnings": [],
                }
            return {"ok": True, "tool": tool, "data": {}, "error": None, "warnings": []}

        result = run_workflow(
            caller,
            steps=[
                {
                    "id": "rename",
                    "tool": "object.rename",
                    "rollback": {"tool": "object.rename"},
                },
                {"id": "fail", "tool": "object.transform"},
            ],
            execute=True,
            rollback_on_failure=True,
            confirm_rollback=True,
            instance=None,
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["data"]["rollback"]["complete"])
        self.assertTrue(result["data"]["partial_change_possible"])
        self.assertEqual(
            result["data"]["failed_step_change"],
            {"explicit": True, "possible": True},
        )

    def test_idempotency_key_replays_success_without_repeating_calls(self) -> None:
        calls = []
        cache = WorkflowReplayCache()

        def caller(tool, arguments, instance):
            calls.append((tool, arguments, instance))
            return {"ok": True, "tool": tool, "data": {"value": 1}, "error": None, "warnings": []}

        request = {
            "steps": [{"id": "once", "tool": "object.rename"}],
            "execute": True,
            "rollback_on_failure": False,
            "confirm_rollback": None,
            "instance": "instance-a",
            "idempotency_key": "rename:hero:1",
            "replay_cache": cache,
        }
        first = run_workflow(caller, **request)
        second = run_workflow(caller, **request)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["data"]["operation_id"], second["data"]["operation_id"])
        self.assertFalse(first["data"]["idempotency"]["replayed"])
        self.assertTrue(second["data"]["idempotency"]["replayed"])

    def test_idempotency_key_conflict_never_calls_changed_workflow(self) -> None:
        calls = []
        cache = WorkflowReplayCache()

        def caller(tool, arguments, instance):
            calls.append(tool)
            return {"ok": True, "tool": tool, "data": {}, "error": None, "warnings": []}

        common = {
            "execute": True,
            "rollback_on_failure": False,
            "confirm_rollback": None,
            "instance": None,
            "idempotency_key": "same-key",
            "replay_cache": cache,
        }
        first = run_workflow(caller, steps=[{"id": "one", "tool": "object.rename"}], **common)
        conflict = run_workflow(caller, steps=[{"id": "one", "tool": "object.transform"}], **common)

        self.assertTrue(first["ok"])
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["type"], "WorkflowIdempotencyConflict")
        self.assertEqual(calls, ["object.rename"])

    def test_failed_idempotent_workflow_is_replayed_without_retry(self) -> None:
        calls = []
        cache = WorkflowReplayCache()

        def caller(tool, arguments, instance):
            calls.append(tool)
            return {
                "ok": False,
                "tool": tool,
                "data": {},
                "error": {"type": "Failure", "message": "no", "traceback": ""},
                "warnings": [],
            }

        request = {
            "steps": [{"id": "fail", "tool": "object.transform"}],
            "execute": True,
            "rollback_on_failure": False,
            "confirm_rollback": None,
            "instance": None,
            "idempotency_key": "failed-once",
            "replay_cache": cache,
        }
        first = run_workflow(caller, **request)
        replay = run_workflow(caller, **request)

        self.assertFalse(first["ok"])
        self.assertFalse(replay["ok"])
        self.assertEqual(calls, ["object.transform"])
        self.assertTrue(first["data"]["partial_change_possible"])
        self.assertEqual(
            first["data"]["failed_step_change"],
            {"explicit": False, "possible": True},
        )
        self.assertTrue(replay["data"]["idempotency"]["replayed"])

    def test_concurrent_duplicate_is_rejected_while_first_request_runs(self) -> None:
        cache = WorkflowReplayCache()
        nested = []
        steps = [{"id": "once", "tool": "object.rename"}]

        def caller(tool, arguments, instance):
            nested.append(
                run_workflow(
                    lambda *_args: self.fail("duplicate request reached its caller"),
                    steps=steps,
                    execute=True,
                    rollback_on_failure=False,
                    confirm_rollback=None,
                    instance=None,
                    idempotency_key="in-flight",
                    replay_cache=cache,
                )
            )
            return {"ok": True, "tool": tool, "data": {}, "error": None, "warnings": []}

        result = run_workflow(
            caller,
            steps=steps,
            execute=True,
            rollback_on_failure=False,
            confirm_rollback=None,
            instance=None,
            idempotency_key="in-flight",
            replay_cache=cache,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(nested[0]["error"]["type"], "WorkflowInProgressError")


if __name__ == "__main__":
    unittest.main()
