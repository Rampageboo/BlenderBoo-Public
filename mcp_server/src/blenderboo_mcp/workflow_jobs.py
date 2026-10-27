"""Bounded process-local chunked workflow jobs with between-step cancellation."""

from __future__ import annotations

from .call_log import workflow_parent

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from itertools import count
import json
import re
import threading
from typing import Any, Callable

from .workflow import _reference_ids, _resolve, validate_steps, WorkflowValidationError


MAX_JOB_ENTRIES = 32
MAX_ACTIVE_JOBS = 8
MAX_JOB_RESULT_BYTES = 16_777_216
MAX_JOB_STEP_BUDGET = 4
_JOB_ID = re.compile(r"^workflow-job-[0-9]{8}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_job_counter = count(1)


class WorkflowJobError(ValueError):
    def __init__(self, error_type: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.data = data or {}


@dataclass
class _Job:
    job_id: str
    operation_id: str
    workflow_digest: str
    start_digest: str
    idempotency_key: str
    instance: str | int
    steps: list[dict[str, Any]]
    rollback_on_failure: bool
    rollback_on_cancel: bool
    state: str = "running"
    revision: int = 0
    next_step_index: int = 0
    current_step: str | None = None
    cancel_requested: bool = False
    results: dict[str, Any] = field(default_factory=dict)
    result_bytes: int = 0
    completed_indices: list[int] = field(default_factory=list)
    step_summaries: list[dict[str, Any]] = field(default_factory=list)
    rollback_summaries: list[dict[str, Any]] = field(default_factory=list)
    rollback_attempted: bool = False
    rollback_complete: bool | None = None
    failed_step: str | None = None
    failure: dict[str, Any] | None = None
    partial_change_possible: bool = False
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    last_chunk_summary: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))


def _normalize_instance(value: Any) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WorkflowJobError(
            "WorkflowJobValidationError",
            "instance must be an explicit registry id or bridge port",
        )
    if isinstance(value, int):
        if not 1024 <= value <= 65535:
            raise WorkflowJobError(
                "WorkflowJobValidationError",
                "instance port must be from 1024 to 65535",
            )
        return value
    if not value or value != value.strip() or len(value) > 128:
        raise WorkflowJobError(
            "WorkflowJobValidationError",
            "instance must be a non-empty registry id of at most 128 characters",
        )
    if value.isdecimal():
        port = int(value)
        if not 1024 <= port <= 65535:
            raise WorkflowJobError(
                "WorkflowJobValidationError",
                "instance port must be from 1024 to 65535",
            )
        return port
    return value


def _validate_step_budget(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_JOB_STEP_BUDGET:
        raise WorkflowJobError(
            "WorkflowJobValidationError",
            f"step_budget must be an integer from 1 to {MAX_JOB_STEP_BUDGET}",
        )
    return value


def _result_summary(step_id: str, tool: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
    try:
        result_digest = _digest(result)
        result_bytes = _json_size(result)
    except (TypeError, ValueError):
        result_digest = None
        result_bytes = None
    data = result.get("data")
    summary: dict[str, Any] = {
        "id": step_id,
        "tool": tool,
        "status": status,
        "result_ok": result.get("ok") is True,
        "result_digest": result_digest,
        "result_bytes": result_bytes,
        "data_keys": sorted(str(key) for key in data) if isinstance(data, dict) else [],
    }
    error = result.get("error")
    if isinstance(error, dict):
        summary["error"] = {
            "type": str(error.get("type", "WorkflowStepError")),
            "message": str(error.get("message", ""))[:2000],
        }
    return summary


def _result_may_have_changed(result: dict[str, Any]) -> bool:
    data = result.get("data")
    if isinstance(data, dict) and (
        data.get("partial_change_possible") is True or data.get("changed") is True
    ):
        return True
    error = result.get("error")
    error_data = error.get("data") if isinstance(error, dict) else None
    return isinstance(error_data, dict) and (
        error_data.get("partial_change_possible") is True
        or error_data.get("changed") is True
    )


def _invalid_result(tool: str, exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "data": {},
        "error": {"type": type(exc).__name__, "message": str(exc), "traceback": ""},
        "warnings": [],
    }


def _call_step(
    caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
    tool: str,
    arguments: dict[str, Any],
    instance: str | int,
) -> dict[str, Any]:
    try:
        result = caller(tool, arguments, instance)
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise RuntimeError("tool returned an invalid envelope")
        _json_size(result)
        return result
    except Exception as exc:
        return _invalid_result(tool, exc)


def _envelope(
    tool: str,
    ok: bool,
    data: dict[str, Any],
    error_type: str | None = None,
    message: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": tool,
        "data": data,
        "error": None
        if ok
        else {
            "type": error_type or "WorkflowJobError",
            "message": message or "workflow job failed",
            "traceback": "",
        },
        "warnings": list(warnings or []),
    }


class WorkflowJobManager:
    """Own chunk state and prevent concurrent execution of the same process-local job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._start_keys: dict[str, str] = {}
        self._lru: list[str] = []

    def _touch_locked(self, job: _Job) -> None:
        job.updated_at = _now()
        if job.job_id in self._lru:
            self._lru.remove(job.job_id)
        self._lru.append(job.job_id)

    def _evict_terminal_locked(self) -> None:
        while len(self._jobs) >= MAX_JOB_ENTRIES:
            candidate = next(
                (
                    job_id
                    for job_id in self._lru
                    if self._jobs[job_id].state
                    in {"completed", "failed", "canceled"}
                ),
                None,
            )
            if candidate is None:
                break
            job = self._jobs.pop(candidate)
            self._lru.remove(candidate)
            if self._start_keys.get(job.idempotency_key) == candidate:
                del self._start_keys[job.idempotency_key]

    def _active_count_locked(self) -> int:
        return sum(
            job.state not in {"completed", "failed", "canceled"}
            for job in self._jobs.values()
        )

    def _snapshot_locked(self, job: _Job) -> dict[str, Any]:
        total = len(job.steps)
        completed = len(job.completed_indices)
        failed = 1 if job.failed_step is not None else 0
        plan = [
            {
                "id": step["id"],
                "tool": step["tool"],
                "references": sorted(_reference_ids(step["arguments"])),
                "has_rollback": step["rollback"] is not None,
            }
            for step in job.steps
        ]
        return {
            "manifest_version": 1,
            "job_id": job.job_id,
            "operation_id": job.operation_id,
            "workflow_digest": job.workflow_digest,
            "scope": "mcp_process",
            "instance": job.instance,
            "state": job.state,
            "revision": job.revision,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "step_count": total,
            "next_step_index": job.next_step_index,
            "current_step": job.current_step,
            "plan": plan,
            "steps": deepcopy(job.step_summaries),
            "progress": {
                "state": job.state,
                "total_steps": total,
                "completed_steps": completed,
                "failed_steps": failed,
                "remaining_steps": max(0, total - job.next_step_index),
                "fraction": completed / total if total else 0.0,
            },
            "rollback": {
                "on_failure": job.rollback_on_failure,
                "on_cancel": job.rollback_on_cancel,
                "attempted": job.rollback_attempted,
                "complete": job.rollback_complete,
                "steps": deepcopy(job.rollback_summaries),
            },
            "cancellation": {
                "requested": job.cancel_requested,
                "safe_boundary": "between_tool_calls",
                "in_flight_tool_interruptible": False,
            },
            "failed_step": job.failed_step,
            "failure": deepcopy(job.failure),
            "partial_change_possible": job.partial_change_possible,
            "memory": {
                "stored_result_bytes": job.result_bytes,
                "stored_result_limit": MAX_JOB_RESULT_BYTES,
            },
            "last_chunk": deepcopy(job.last_chunk_summary),
            "idempotency": {
                "key": job.idempotency_key,
                "replayed": False,
                "scope": "mcp_process",
            },
        }

    def _job_or_error_locked(self, job_id: Any) -> _Job:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise WorkflowJobError(
                "WorkflowJobValidationError",
                f"job_id must match {_JOB_ID.pattern}",
            )
        job = self._jobs.get(job_id)
        if job is None:
            raise WorkflowJobError("WorkflowJobNotFound", f"workflow job not found: {job_id}")
        self._touch_locked(job)
        return job

    def _revision_locked(self, job: _Job, expected_revision: Any) -> None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise WorkflowJobError(
                "WorkflowJobValidationError",
                "expected_revision must be a non-negative integer",
                {"job_id": job.job_id, "current_revision": job.revision},
            )
        if expected_revision != job.revision:
            raise WorkflowJobError(
                "WorkflowJobRevisionConflict",
                f"expected revision {expected_revision}, current revision is {job.revision}",
                self._snapshot_locked(job),
            )

    def _terminalize_locked(self, job: _Job) -> None:
        job.current_step = None
        job.results.clear()
        job.result_bytes = 0
        self._touch_locked(job)

    def _rollback(
        self,
        caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
        job: _Job,
        *,
        terminal_state: str,
        chunk_rollback: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            job.rollback_attempted = True
            job.state = "canceling" if terminal_state == "canceled" else "rolling_back"
            self._touch_locked(job)
        rollback_ok = True
        for index in reversed(job.completed_indices):
            step = job.steps[index]
            rollback = step["rollback"]
            if rollback is None:
                summary = {"id": step["id"], "status": "not_defined"}
                with self._lock:
                    job.rollback_summaries.append(summary)
                chunk_rollback.append(deepcopy(summary))
                rollback_ok = False
                continue
            try:
                with self._lock:
                    arguments = _resolve(rollback["arguments"], job.results)
                marker = workflow_parent.set(job.operation_id)
                try:
                    result = _call_step(caller, rollback["tool"], arguments, job.instance)
                finally:
                    workflow_parent.reset(marker)
            except Exception as exc:
                result = _invalid_result(rollback["tool"], exc)
            ok = result.get("ok") is True
            summary = _result_summary(
                step["id"],
                rollback["tool"],
                "completed" if ok else "failed",
                result,
            )
            with self._lock:
                job.rollback_summaries.append(summary)
            chunk_rollback.append({**summary, "result": result})
            rollback_ok = rollback_ok and ok
        with self._lock:
            job.rollback_complete = rollback_ok
            job.state = terminal_state
            job.partial_change_possible = job.partial_change_possible or (
                bool(job.completed_indices) and not rollback_ok
            )
            job.revision += 1
            self._terminalize_locked(job)

    def _finish_failure(
        self,
        caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
        job: _Job,
        *,
        chunk_rollback: list[dict[str, Any]],
    ) -> None:
        if job.rollback_on_failure:
            self._rollback(
                caller,
                job,
                terminal_state="failed",
                chunk_rollback=chunk_rollback,
            )
            return
        with self._lock:
            job.state = "failed"
            job.partial_change_possible = bool(job.completed_indices) or job.partial_change_possible
            job.revision += 1
            self._terminalize_locked(job)

    def _finish_cancel(
        self,
        caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
        job: _Job,
        *,
        chunk_rollback: list[dict[str, Any]],
    ) -> None:
        if job.rollback_on_cancel:
            self._rollback(
                caller,
                job,
                terminal_state="canceled",
                chunk_rollback=chunk_rollback,
            )
            return
        with self._lock:
            job.state = "canceled"
            job.partial_change_possible = bool(job.completed_indices)
            job.revision += 1
            self._terminalize_locked(job)

    def _run_chunk(
        self,
        caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
        job: _Job,
        step_budget: int,
    ) -> dict[str, Any]:
        chunk_steps: list[dict[str, Any]] = []
        chunk_rollback: list[dict[str, Any]] = []
        attempted = 0
        while attempted < step_budget:
            with self._lock:
                if job.cancel_requested:
                    cancel_now = True
                else:
                    cancel_now = False
                if not cancel_now and job.next_step_index >= len(job.steps):
                    job.state = "completed"
                    job.revision += 1
                    self._terminalize_locked(job)
                    break
                if cancel_now:
                    step = None
                else:
                    step = job.steps[job.next_step_index]
                    job.current_step = step["id"]
                    self._touch_locked(job)
                    results = deepcopy(job.results)
            if cancel_now:
                self._finish_cancel(caller, job, chunk_rollback=chunk_rollback)
                break
            assert step is not None
            try:
                arguments = _resolve(step["arguments"], results)
                marker = workflow_parent.set(job.operation_id)
                try:
                    result = _call_step(caller, step["tool"], arguments, job.instance)
                finally:
                    workflow_parent.reset(marker)
            except Exception as exc:
                result = _invalid_result(step["tool"], exc)
            ok = result.get("ok") is True
            status = "completed" if ok else "failed"
            summary = _result_summary(step["id"], step["tool"], status, result)
            chunk_steps.append({**summary, "result": result})
            attempted += 1

            result_size = summary.get("result_bytes")
            with self._lock:
                job.current_step = None
                job.step_summaries.append(summary)
                if ok:
                    index = job.next_step_index
                    job.results[step["id"]] = result
                    job.result_bytes += result_size if isinstance(result_size, int) else 0
                    job.completed_indices.append(index)
                    job.next_step_index += 1
                    if job.result_bytes > MAX_JOB_RESULT_BYTES:
                        job.failed_step = step["id"]
                        job.failure = {
                            "type": "WorkflowJobResultBudgetError",
                            "message": f"stored referenced results exceed {MAX_JOB_RESULT_BYTES} bytes",
                        }
                        memory_failure = True
                    else:
                        memory_failure = False
                else:
                    job.failed_step = step["id"]
                    error = result.get("error")
                    job.failure = {
                        "type": str(error.get("type", "WorkflowStepError"))
                        if isinstance(error, dict)
                        else "WorkflowStepError",
                        "message": str(error.get("message", "step failed"))
                        if isinstance(error, dict)
                        else "step failed",
                    }
                    job.partial_change_possible = _result_may_have_changed(result)
                    memory_failure = False
                self._touch_locked(job)
            if not ok or memory_failure:
                self._finish_failure(caller, job, chunk_rollback=chunk_rollback)
                break

        with self._lock:
            if job.cancel_requested and job.state in {"running", "cancel_requested"}:
                finalize_cancel = True
            elif job.state == "running":
                if job.next_step_index >= len(job.steps):
                    job.state = "completed"
                    job.revision += 1
                    self._terminalize_locked(job)
                    finalize_cancel = False
                else:
                    job.state = "paused"
                    job.revision += 1
                    self._touch_locked(job)
                    finalize_cancel = False
            else:
                finalize_cancel = False
        if finalize_cancel:
            self._finish_cancel(caller, job, chunk_rollback=chunk_rollback)

        with self._lock:
            job.last_chunk_summary = {
                "attempted_steps": len(chunk_steps),
                "step_ids": [item["id"] for item in chunk_steps],
                "rollback_steps": [item["id"] for item in chunk_rollback],
            }
            self._touch_locked(job)
            data = self._snapshot_locked(job)
        data["chunk"] = {
            "steps": chunk_steps,
            "rollback": chunk_rollback,
        }
        if data["state"] == "failed":
            failure = data.get("failure") or {}
            return _envelope(
                "workflow.job_start",
                False,
                data,
                str(failure.get("type", "WorkflowJobFailed")),
                str(failure.get("message", "workflow job failed")),
            )
        if data["state"] == "canceled":
            return _envelope(
                "workflow.job_start",
                False,
                data,
                "WorkflowJobCanceled",
                "workflow job was canceled at a between-step boundary",
            )
        return _envelope("workflow.job_start", True, data)

    def _unexpected_failure(
        self,
        caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
        tool: str,
        job: _Job,
        exc: Exception,
    ) -> dict[str, Any]:
        chunk_rollback: list[dict[str, Any]] = []
        with self._lock:
            previous_failure = deepcopy(job.failure)
            job.failure = {
                "type": "WorkflowJobExecutionError",
                "message": f"workflow job orchestration failed unexpectedly: {exc}",
            }
            if previous_failure is not None:
                job.failure["cause"] = previous_failure
            job.partial_change_possible = bool(job.completed_indices) or job.current_step is not None
            should_rollback = job.rollback_on_failure and not job.rollback_attempted
        if should_rollback:
            try:
                self._rollback(
                    caller,
                    job,
                    terminal_state="failed",
                    chunk_rollback=chunk_rollback,
                )
            except Exception as rollback_exc:
                with self._lock:
                    job.rollback_attempted = True
                    job.rollback_complete = False
                    job.state = "failed"
                    job.partial_change_possible = True
                    job.failure["rollback_error"] = {
                        "type": type(rollback_exc).__name__,
                        "message": str(rollback_exc),
                    }
                    job.revision += 1
                    self._terminalize_locked(job)
        else:
            with self._lock:
                job.state = "failed"
                if job.rollback_attempted and job.rollback_complete is None:
                    job.rollback_complete = False
                    job.partial_change_possible = True
                job.revision += 1
                self._terminalize_locked(job)
        with self._lock:
            job.last_chunk_summary = {
                "attempted_steps": 0,
                "step_ids": [],
                "rollback_steps": [item["id"] for item in chunk_rollback],
            }
            self._touch_locked(job)
            data = self._snapshot_locked(job)
        data["chunk"] = {"steps": [], "rollback": chunk_rollback}
        return _envelope(
            tool,
            False,
            data,
            "WorkflowJobExecutionError",
            str(job.failure["message"]),
        )

    def start(
        self,
        caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
        *,
        steps: Any,
        instance: Any,
        execute: bool,
        step_budget: Any,
        rollback_on_failure: bool,
        rollback_on_cancel: bool,
        confirm_rollback: bool | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        try:
            normalized = validate_steps(steps)
            resolved_instance = _normalize_instance(instance)
            budget = _validate_step_budget(step_budget)
            if not isinstance(execute, bool):
                raise WorkflowJobError("WorkflowJobValidationError", "execute must be a boolean")
            if not isinstance(rollback_on_failure, bool) or not isinstance(rollback_on_cancel, bool):
                raise WorkflowJobError(
                    "WorkflowJobValidationError",
                    "rollback_on_failure and rollback_on_cancel must be booleans",
                )
            if confirm_rollback is not None and not isinstance(confirm_rollback, bool):
                raise WorkflowJobError(
                    "WorkflowJobValidationError",
                    "confirm_rollback must be a boolean",
                )
            if execute and (rollback_on_failure or rollback_on_cancel) and confirm_rollback is not True:
                raise WorkflowJobError(
                    "WorkflowJobValidationError",
                    "rollback_on_failure or rollback_on_cancel requires confirm_rollback: true",
                )
            if idempotency_key is not None and (
                not isinstance(idempotency_key, str)
                or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
            ):
                raise WorkflowJobError(
                    "WorkflowJobValidationError",
                    "idempotency_key must start with an alphanumeric character and contain at most 128 alphanumeric, '.', '_', ':', or '-' characters",
                )
            if execute and idempotency_key is None:
                raise WorkflowJobError(
                    "WorkflowJobValidationError",
                    "execute: true requires idempotency_key for resumable job creation",
                )
        except WorkflowValidationError as exc:
            return _envelope(
                "workflow.job_start",
                False,
                {"created": False, "executed": False},
                "WorkflowJobValidationError",
                str(exc),
            )
        except WorkflowJobError as exc:
            return _envelope(
                "workflow.job_start",
                False,
                {"created": False, "executed": False, **exc.data},
                exc.error_type,
                str(exc),
            )

        workflow_payload = {
            "contract": 1,
            "steps": normalized,
            "instance": resolved_instance,
            "rollback_on_failure": rollback_on_failure,
            "rollback_on_cancel": rollback_on_cancel,
        }
        workflow_digest = _digest(workflow_payload)
        start_digest = _digest({**workflow_payload, "step_budget": budget})
        plan = [
            {
                "id": step["id"],
                "tool": step["tool"],
                "references": sorted(_reference_ids(step["arguments"])),
                "has_rollback": step["rollback"] is not None,
            }
            for step in normalized
        ]
        if not execute:
            return _envelope(
                "workflow.job_start",
                True,
                {
                    "manifest_version": 1,
                    "created": False,
                    "executed": False,
                    "state": "planned",
                    "workflow_digest": workflow_digest,
                    "instance": resolved_instance,
                    "step_budget": budget,
                    "step_count": len(normalized),
                    "plan": plan,
                    "cancellation": {
                        "safe_boundary": "between_tool_calls",
                        "in_flight_tool_interruptible": False,
                    },
                },
            )

        assert idempotency_key is not None
        with self._lock:
            existing_id = self._start_keys.get(idempotency_key)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and existing.start_digest != start_digest:
                    return _envelope(
                        "workflow.job_start",
                        False,
                        {
                            "created": False,
                            "existing_job_id": existing.job_id,
                            "existing_revision": existing.revision,
                        },
                        "WorkflowJobIdempotencyConflict",
                        "idempotency_key is already bound to a different job-start request",
                    )
                if existing is not None:
                    data = self._snapshot_locked(existing)
                    data["idempotency"]["replayed"] = True
                    return _envelope(
                        "workflow.job_start",
                        existing.state not in {"failed", "canceled"},
                        data,
                        None if existing.state not in {"failed", "canceled"} else "WorkflowJobTerminalReplay",
                        None if existing.state not in {"failed", "canceled"} else f"workflow job is {existing.state}",
                        ["Returned current job state without executing another Blender step."],
                    )
            self._evict_terminal_locked()
            if len(self._jobs) >= MAX_JOB_ENTRIES or self._active_count_locked() >= MAX_ACTIVE_JOBS:
                return _envelope(
                    "workflow.job_start",
                    False,
                    {"created": False, "active_jobs": self._active_count_locked()},
                    "WorkflowJobCapacityError",
                    "process-local workflow job capacity is exhausted",
                )
            job_id = f"workflow-job-{next(_job_counter):08d}"
            job = _Job(
                job_id=job_id,
                operation_id=job_id,
                workflow_digest=workflow_digest,
                start_digest=start_digest,
                idempotency_key=idempotency_key,
                instance=resolved_instance,
                steps=normalized,
                rollback_on_failure=rollback_on_failure,
                rollback_on_cancel=rollback_on_cancel,
            )
            self._jobs[job_id] = job
            self._start_keys[idempotency_key] = job_id
            self._touch_locked(job)
        try:
            return self._run_chunk(caller, job, budget)
        except Exception as exc:
            return self._unexpected_failure(caller, "workflow.job_start", job, exc)

    def control(
        self,
        caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
        *,
        action: str,
        job_id: Any,
        expected_revision: int | None,
        step_budget: Any,
        confirm_cancel: bool | None,
    ) -> dict[str, Any]:
        try:
            if action not in {"status", "resume", "cancel"}:
                raise WorkflowJobError(
                    "WorkflowJobValidationError",
                    "action must be 'status', 'resume', or 'cancel'",
                )
            with self._lock:
                job = self._job_or_error_locked(job_id)
                if action == "status":
                    return _envelope("workflow.job_control", True, self._snapshot_locked(job))
                self._revision_locked(job, expected_revision)
                if action == "resume":
                    budget = _validate_step_budget(step_budget)
                    if job.state != "paused":
                        raise WorkflowJobError(
                            "WorkflowJobStateConflict",
                            f"workflow job must be paused to resume; current state is {job.state}",
                            self._snapshot_locked(job),
                        )
                    job.state = "running"
                    job.current_step = None
                    self._touch_locked(job)
                else:
                    if confirm_cancel is not True:
                        raise WorkflowJobError(
                            "WorkflowJobValidationError",
                            "cancel requires confirm_cancel: true",
                            self._snapshot_locked(job),
                        )
                    if job.state in {"completed", "failed", "canceled"}:
                        data = self._snapshot_locked(job)
                        data["cancel_accepted"] = job.state == "canceled"
                        return _envelope("workflow.job_control", True, data)
                    if job.state in {"running", "cancel_requested"}:
                        if not job.cancel_requested:
                            job.cancel_requested = True
                            job.state = "cancel_requested"
                            job.revision += 1
                            self._touch_locked(job)
                        data = self._snapshot_locked(job)
                        data["cancel_accepted"] = True
                        data["cancel_pending"] = True
                        return _envelope("workflow.job_control", True, data)
                    if job.state != "paused":
                        raise WorkflowJobError(
                            "WorkflowJobStateConflict",
                            f"workflow job cannot be canceled from state {job.state}",
                            self._snapshot_locked(job),
                        )
                    job.cancel_requested = True
                    job.state = "canceling"
                    self._touch_locked(job)
                    budget = None
            if action == "resume":
                try:
                    result = self._run_chunk(caller, job, budget)
                except Exception as exc:
                    return self._unexpected_failure(caller, "workflow.job_control", job, exc)
                result["tool"] = "workflow.job_control"
                return result
            chunk_rollback: list[dict[str, Any]] = []
            self._finish_cancel(caller, job, chunk_rollback=chunk_rollback)
            with self._lock:
                data = self._snapshot_locked(job)
            data["cancel_accepted"] = True
            data["chunk"] = {"steps": [], "rollback": chunk_rollback}
            return _envelope("workflow.job_control", True, data)
        except WorkflowJobError as exc:
            return _envelope(
                "workflow.job_control",
                False,
                {"job_id": job_id, **exc.data},
                exc.error_type,
                str(exc),
            )
