"""Pure adapter-side resumable workflow job contract tests; Blender is not required."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from blenderboo_mcp import workflow_jobs
from blenderboo_mcp.workflow_jobs import WorkflowJobManager


def _success(tool, data=None):
    return {
        "ok": True,
        "tool": tool,
        "data": data or {},
        "error": None,
        "warnings": [],
    }


def _start(manager, caller, *, steps, **overrides):
    request = {
        "steps": steps,
        "instance": "instance-a",
        "execute": True,
        "step_budget": 1,
        "rollback_on_failure": False,
        "rollback_on_cancel": False,
        "confirm_rollback": None,
        "idempotency_key": "job-key",
    }
    request.update(overrides)
    return manager.start(caller, **request)


class WorkflowJobTests(unittest.TestCase):
    def test_plan_only_does_not_call_or_create_a_job(self) -> None:
        manager = WorkflowJobManager()
        calls = []
        result = _start(
            manager,
            lambda *args: calls.append(args),
            steps=[{"id": "inspect", "tool": "scene.inspect"}],
            execute=False,
            idempotency_key=None,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["state"], "planned")
        self.assertFalse(result["data"]["created"])
        self.assertEqual(calls, [])

    def test_execute_requires_explicit_instance_and_idempotency_key(self) -> None:
        manager = WorkflowJobManager()
        steps = [{"id": "inspect", "tool": "scene.inspect"}]

        missing_instance = _start(manager, self.fail, steps=steps, instance=None)
        missing_key = _start(manager, self.fail, steps=steps, idempotency_key=None)

        self.assertFalse(missing_instance["ok"])
        self.assertEqual(missing_instance["error"]["type"], "WorkflowJobValidationError")
        self.assertFalse(missing_key["ok"])
        self.assertEqual(missing_key["error"]["type"], "WorkflowJobValidationError")

    def test_job_pauses_and_resumes_with_exact_result_references(self) -> None:
        manager = WorkflowJobManager()
        calls = []

        def caller(tool, arguments, instance):
            calls.append((tool, arguments, instance))
            if tool == "scene.inspect":
                return _success(tool, {"state_token": "state-a"})
            return _success(tool)

        first = _start(
            manager,
            caller,
            steps=[
                {"id": "inspect", "tool": "scene.inspect"},
                {
                    "id": "rename",
                    "tool": "object.rename",
                    "arguments": {"expected_state": {"$ref": "inspect.data.state_token"}},
                },
            ],
        )
        job_id = first["data"]["job_id"]
        status = manager.control(
            caller,
            action="status",
            job_id=job_id,
            expected_revision=None,
            step_budget=1,
            confirm_cancel=None,
        )
        resumed = manager.control(
            caller,
            action="resume",
            job_id=job_id,
            expected_revision=1,
            step_budget=1,
            confirm_cancel=None,
        )

        self.assertEqual(first["data"]["state"], "paused")
        self.assertEqual(first["data"]["revision"], 1)
        self.assertEqual(status["data"]["state"], "paused")
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["data"]["state"], "completed")
        self.assertEqual(resumed["data"]["revision"], 2)
        self.assertEqual(
            calls[1],
            ("object.rename", {"expected_state": "state-a"}, "instance-a"),
        )

    def test_stale_resume_revision_does_not_call_blender(self) -> None:
        manager = WorkflowJobManager()
        calls = []

        def caller(tool, arguments, instance):
            calls.append(tool)
            return _success(tool)

        first = _start(
            manager,
            caller,
            steps=[
                {"id": "one", "tool": "scene.inspect"},
                {"id": "two", "tool": "object.inspect"},
            ],
        )
        stale = manager.control(
            caller,
            action="resume",
            job_id=first["data"]["job_id"],
            expected_revision=0,
            step_budget=1,
            confirm_cancel=None,
        )

        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"]["type"], "WorkflowJobRevisionConflict")
        self.assertEqual(calls, ["scene.inspect"])

    def test_repeated_start_returns_state_without_repeating_a_step(self) -> None:
        manager = WorkflowJobManager()
        calls = []
        steps = [
            {"id": "one", "tool": "scene.inspect"},
            {"id": "two", "tool": "object.inspect"},
        ]

        def caller(tool, arguments, instance):
            calls.append(tool)
            return _success(tool)

        first = _start(manager, caller, steps=steps)
        replay = _start(manager, caller, steps=steps)

        self.assertTrue(replay["ok"])
        self.assertEqual(replay["data"]["job_id"], first["data"]["job_id"])
        self.assertTrue(replay["data"]["idempotency"]["replayed"])
        self.assertEqual(calls, ["scene.inspect"])

    def test_reusing_start_key_for_changed_request_is_rejected(self) -> None:
        manager = WorkflowJobManager()
        calls = []

        def caller(tool, arguments, instance):
            calls.append(tool)
            return _success(tool)

        _start(
            manager,
            caller,
            steps=[
                {"id": "one", "tool": "scene.inspect"},
                {"id": "two", "tool": "object.inspect"},
            ],
        )
        conflict = _start(
            manager,
            caller,
            steps=[
                {"id": "one", "tool": "scene.inspect"},
                {"id": "changed", "tool": "object.inspect"},
            ],
        )

        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["type"], "WorkflowJobIdempotencyConflict")
        self.assertEqual(calls, ["scene.inspect"])

    def test_canceling_a_paused_job_runs_confirmed_reverse_compensation(self) -> None:
        manager = WorkflowJobManager()
        calls = []

        def caller(tool, arguments, instance):
            calls.append((tool, arguments, instance))
            if tool == "object.rename":
                return _success(tool, {"old_name": "Original"})
            return _success(tool)

        first = _start(
            manager,
            caller,
            steps=[
                {
                    "id": "rename",
                    "tool": "object.rename",
                    "rollback": {
                        "tool": "object.rename",
                        "arguments": {"new_name": {"$ref": "rename.data.old_name"}},
                    },
                },
                {"id": "inspect", "tool": "scene.inspect"},
            ],
            rollback_on_cancel=True,
            confirm_rollback=True,
        )
        canceled = manager.control(
            caller,
            action="cancel",
            job_id=first["data"]["job_id"],
            expected_revision=1,
            step_budget=1,
            confirm_cancel=True,
        )

        self.assertTrue(canceled["ok"])
        self.assertEqual(canceled["data"]["state"], "canceled")
        self.assertTrue(canceled["data"]["rollback"]["complete"])
        self.assertFalse(canceled["data"]["partial_change_possible"])
        self.assertEqual(
            calls[-1],
            ("object.rename", {"new_name": "Original"}, "instance-a"),
        )

    def test_running_cancel_waits_for_current_call_and_skips_later_steps(self) -> None:
        manager = WorkflowJobManager()
        entered = threading.Event()
        release = threading.Event()
        calls = []
        outer = []
        steps = [
            {"id": "one", "tool": "object.rename"},
            {"id": "two", "tool": "object.inspect"},
        ]

        def caller(tool, arguments, instance):
            calls.append(tool)
            entered.set()
            self.assertTrue(release.wait(2))
            return _success(tool)

        thread = threading.Thread(
            target=lambda: outer.append(_start(manager, caller, steps=steps, step_budget=2)),
            daemon=True,
        )
        thread.start()
        self.assertTrue(entered.wait(2))
        replay = _start(manager, caller, steps=steps, step_budget=2)
        canceled = manager.control(
            lambda *_args: self.fail("cancel must not call Blender while a step is active"),
            action="cancel",
            job_id=replay["data"]["job_id"],
            expected_revision=0,
            step_budget=1,
            confirm_cancel=True,
        )
        release.set()
        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(canceled["ok"])
        self.assertTrue(canceled["data"]["cancel_pending"])
        self.assertFalse(outer[0]["ok"])
        self.assertEqual(outer[0]["data"]["state"], "canceled")
        self.assertEqual(calls, ["object.rename"])

    def test_result_storage_cap_fails_once_and_clears_terminal_memory(self) -> None:
        manager = WorkflowJobManager()
        calls = []

        def caller(tool, arguments, instance):
            calls.append(tool)
            return _success(tool, {"blob": "x" * 512})

        steps = [{"id": "large", "tool": "scene.inspect"}]
        with patch.object(workflow_jobs, "MAX_JOB_RESULT_BYTES", 256):
            result = _start(manager, caller, steps=steps)
            replay = _start(manager, caller, steps=steps)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "WorkflowJobResultBudgetError")
        self.assertEqual(result["data"]["state"], "failed")
        self.assertTrue(result["data"]["partial_change_possible"])
        self.assertEqual(result["data"]["memory"]["stored_result_bytes"], 0)
        self.assertFalse(replay["ok"])
        self.assertTrue(replay["data"]["idempotency"]["replayed"])
        self.assertEqual(calls, ["scene.inspect"])

    def test_failure_runs_confirmed_reverse_compensation(self) -> None:
        manager = WorkflowJobManager()
        calls = []

        def caller(tool, arguments, instance):
            calls.append((tool, arguments, instance))
            if tool == "object.transform":
                return {
                    "ok": False,
                    "tool": tool,
                    "data": {},
                    "error": {"type": "Failure", "message": "no", "traceback": ""},
                    "warnings": [],
                }
            if tool == "object.rename":
                return _success(tool, {"old_name": "Original"})
            return _success(tool)

        result = _start(
            manager,
            caller,
            steps=[
                {
                    "id": "rename",
                    "tool": "object.rename",
                    "rollback": {
                        "tool": "object.rename",
                        "arguments": {"new_name": {"$ref": "rename.data.old_name"}},
                    },
                },
                {"id": "fail", "tool": "object.transform"},
            ],
            step_budget=2,
            rollback_on_failure=True,
            confirm_rollback=True,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "Failure")
        self.assertEqual(result["data"]["state"], "failed")
        self.assertTrue(result["data"]["rollback"]["attempted"])
        self.assertTrue(result["data"]["rollback"]["complete"])
        self.assertFalse(result["data"]["partial_change_possible"])
        self.assertEqual(
            calls[-1],
            ("object.rename", {"new_name": "Original"}, "instance-a"),
        )

    def test_unexpected_orchestration_failure_compensates_recorded_steps_once(self) -> None:
        manager = WorkflowJobManager()
        calls = []

        def caller(tool, arguments, instance):
            calls.append((tool, arguments, instance))
            if tool == "object.rename" and arguments.get("name") == "Changed":
                return _success(tool, {"old_name": "Original"})
            return _success(tool)

        original_summary = workflow_jobs._result_summary

        def fail_second_summary(step_id, tool, status, result):
            if step_id == "second":
                raise RuntimeError("summary failed")
            return original_summary(step_id, tool, status, result)

        with patch.object(workflow_jobs, "_result_summary", side_effect=fail_second_summary):
            result = _start(
                manager,
                caller,
                steps=[
                    {
                        "id": "first",
                        "tool": "object.rename",
                        "arguments": {"name": "Changed"},
                        "rollback": {
                            "tool": "object.rename",
                            "arguments": {"name": {"$ref": "first.data.old_name"}},
                        },
                    },
                    {"id": "second", "tool": "scene.inspect"},
                ],
                step_budget=2,
                rollback_on_failure=True,
                confirm_rollback=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "WorkflowJobExecutionError")
        self.assertEqual(result["data"]["state"], "failed")
        self.assertTrue(result["data"]["rollback"]["attempted"])
        self.assertTrue(result["data"]["rollback"]["complete"])
        self.assertTrue(result["data"]["partial_change_possible"])
        self.assertEqual(
            calls,
            [
                ("object.rename", {"name": "Changed"}, "instance-a"),
                ("scene.inspect", {}, "instance-a"),
                ("object.rename", {"name": "Original"}, "instance-a"),
            ],
        )

    def test_unexpected_rollback_failure_is_not_retried_or_hidden(self) -> None:
        manager = WorkflowJobManager()
        calls = []
        summary_counts = {}

        def caller(tool, arguments, instance):
            calls.append((tool, arguments, instance))
            if tool == "object.transform":
                return {
                    "ok": False,
                    "tool": tool,
                    "data": {},
                    "error": {"type": "Failure", "message": "no", "traceback": ""},
                    "warnings": [],
                }
            if tool == "object.rename" and arguments.get("name") == "Changed":
                return _success(tool, {"old_name": "Original"})
            return _success(tool)

        original_summary = workflow_jobs._result_summary

        def fail_rollback_summary(step_id, tool, status, result):
            summary_counts[step_id] = summary_counts.get(step_id, 0) + 1
            if step_id == "first" and summary_counts[step_id] == 2:
                raise RuntimeError("rollback summary failed")
            return original_summary(step_id, tool, status, result)

        with patch.object(workflow_jobs, "_result_summary", side_effect=fail_rollback_summary):
            result = _start(
                manager,
                caller,
                steps=[
                    {
                        "id": "first",
                        "tool": "object.rename",
                        "arguments": {"name": "Changed"},
                        "rollback": {
                            "tool": "object.rename",
                            "arguments": {"name": {"$ref": "first.data.old_name"}},
                        },
                    },
                    {"id": "fail", "tool": "object.transform"},
                ],
                step_budget=2,
                rollback_on_failure=True,
                confirm_rollback=True,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "WorkflowJobExecutionError")
        self.assertEqual(result["data"]["failure"]["cause"]["type"], "Failure")
        self.assertTrue(result["data"]["rollback"]["attempted"])
        self.assertFalse(result["data"]["rollback"]["complete"])
        self.assertTrue(result["data"]["partial_change_possible"])
        self.assertEqual(
            calls,
            [
                ("object.rename", {"name": "Changed"}, "instance-a"),
                ("object.transform", {}, "instance-a"),
                ("object.rename", {"name": "Original"}, "instance-a"),
            ],
        )

    def test_active_capacity_recovers_after_a_job_is_canceled(self) -> None:
        manager = WorkflowJobManager()
        calls = []
        steps = [
            {"id": "one", "tool": "scene.inspect"},
            {"id": "two", "tool": "object.inspect"},
        ]

        def caller(tool, arguments, instance):
            calls.append(tool)
            return _success(tool)

        with patch.object(workflow_jobs, "MAX_ACTIVE_JOBS", 1):
            first = _start(manager, caller, steps=steps, idempotency_key="first")
            full = _start(manager, caller, steps=steps, idempotency_key="second")
            canceled = manager.control(
                caller,
                action="cancel",
                job_id=first["data"]["job_id"],
                expected_revision=1,
                step_budget=1,
                confirm_cancel=True,
            )
            admitted = _start(manager, caller, steps=steps, idempotency_key="second")

        self.assertFalse(full["ok"])
        self.assertEqual(full["error"]["type"], "WorkflowJobCapacityError")
        self.assertEqual(canceled["data"]["state"], "canceled")
        self.assertTrue(admitted["ok"])
        self.assertEqual(admitted["data"]["state"], "paused")
        self.assertEqual(calls, ["scene.inspect", "scene.inspect"])

    def test_cancel_requires_confirmation_without_advancing_revision(self) -> None:
        manager = WorkflowJobManager()
        steps = [
            {"id": "one", "tool": "scene.inspect"},
            {"id": "two", "tool": "object.inspect"},
        ]
        first = _start(manager, lambda tool, *_args: _success(tool), steps=steps)
        job_id = first["data"]["job_id"]

        refused = manager.control(
            lambda *_args: self.fail("refused cancel must not call Blender"),
            action="cancel",
            job_id=job_id,
            expected_revision=1,
            step_budget=1,
            confirm_cancel=None,
        )
        status = manager.control(
            lambda *_args: self.fail("status must not call Blender"),
            action="status",
            job_id=job_id,
            expected_revision=None,
            step_budget=1,
            confirm_cancel=None,
        )

        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["type"], "WorkflowJobValidationError")
        self.assertEqual(status["data"]["state"], "paused")
        self.assertEqual(status["data"]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
