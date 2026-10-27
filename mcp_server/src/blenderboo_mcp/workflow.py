"""Bounded MCP-side workflow orchestration with explicit compensating steps."""

from __future__ import annotations

from .call_log import workflow_parent

from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
from itertools import count
import json
import math
import re
import threading
from typing import Any, Callable


MAX_WORKFLOW_STEPS = 32
MAX_WORKFLOW_BYTES = 262_144
_STEP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_TOOL_NAME = re.compile(r"^[a-z_]+(?:\.[a-z_]+)+$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_workflow_counter = count(1)
_KNOWN_NONMUTATING_FAILURES = frozenset(
    {
        "ConfirmationRequiredError",
        "InvalidParamsError",
        "InvalidStateError",
        "LockedError",
        "NameConflictError",
        "NotFoundError",
        "StaleStateError",
    }
)


class WorkflowValidationError(ValueError):
    pass


class WorkflowReplayCache:
    """Bounded process-local duplicate suppression for executed workflows."""

    def __init__(self, max_entries: int = 64, max_result_bytes: int = 524_288) -> None:
        if max_entries < 1 or max_result_bytes < 1024:
            raise ValueError("workflow replay cache bounds are invalid")
        self._max_entries = max_entries
        self._max_result_bytes = max_result_bytes
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def begin(
        self,
        key: str,
        digest: str,
        operation_id: str,
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                if entry["digest"] != digest:
                    return "conflict", None, entry["operation_id"]
                if entry["state"] == "running":
                    return "running", None, entry["operation_id"]
                return "replay", deepcopy(entry["result"]), entry["operation_id"]

            while len(self._entries) >= self._max_entries:
                completed_key = next(
                    (
                        candidate
                        for candidate, candidate_entry in self._entries.items()
                        if candidate_entry["state"] == "complete"
                    ),
                    None,
                )
                if completed_key is None:
                    return "capacity", None, None
                del self._entries[completed_key]

            self._entries[key] = {
                "digest": digest,
                "operation_id": operation_id,
                "state": "running",
                "result": None,
            }
            return "reserved", None, operation_id

    def complete(self, key: str, digest: str, result: dict[str, Any]) -> None:
        cached = _bounded_replay_result(result, self._max_result_bytes)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry["digest"] != digest:
                return
            entry["state"] = "complete"
            entry["result"] = cached
            self._entries.move_to_end(key)


_workflow_replay_cache = WorkflowReplayCache()


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowValidationError("workflow values must be finite")
        return value
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise WorkflowValidationError("workflow object keys must be strings")
        return {key: _safe_json(item) for key, item in value.items()}
    raise WorkflowValidationError(f"workflow values cannot contain {type(value).__name__}")


def _action(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowValidationError(f"{label} must be an object")
    extra = sorted(set(value) - {"tool", "arguments"})
    if extra:
        raise WorkflowValidationError(f"Unknown {label} fields: {', '.join(extra)}")
    tool = value.get("tool")
    if not isinstance(tool, str) or not _TOOL_NAME.fullmatch(tool):
        raise WorkflowValidationError(f"{label}.tool must be a dotted lowercase tool name")
    if tool in {"workflow.run", "workflow.template", "workflow.batch_plan", "workflow.job_start", "workflow.job_control", "adapter.schema_audit"} or tool.startswith("toolsets."):
        raise WorkflowValidationError(f"{label}.tool cannot call MCP-only workflow, schema-audit, or toolset meta-tools")
    arguments = value.get("arguments", {})
    if not isinstance(arguments, dict):
        raise WorkflowValidationError(f"{label}.arguments must be an object")
    return {"tool": tool, "arguments": _safe_json(arguments)}


def _reference_ids(value: Any) -> set[str]:
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_reference_ids(item))
        return result
    if not isinstance(value, dict):
        return set()
    if "$ref" in value and set(value) != {"$ref"}:
        raise WorkflowValidationError("$ref objects cannot contain sibling fields")
    if set(value) == {"$ref"}:
        reference = value["$ref"]
        if not isinstance(reference, str) or "." not in reference:
            raise WorkflowValidationError("$ref must be 'step_id.path' with a non-empty result path")
        step_id, _, path = reference.partition(".")
        if not _STEP_ID.fullmatch(step_id) or not path:
            raise WorkflowValidationError("$ref contains an invalid step id or empty path")
        return {step_id}
    result = set()
    for item in value.values():
        result.update(_reference_ids(item))
    return result


def _static_schema_check(value: Any, schema: dict[str, Any], path: str, deferred: list[str]) -> None:
    if isinstance(value, dict) and set(value) == {"$ref"}:
        deferred.append(path)
        return
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        for variant in variants:
            try:
                _static_schema_check(value, variant, path, [])
                return
            except WorkflowValidationError:
                continue
        raise WorkflowValidationError(f"{path} does not match any accepted schema")
    kind = schema.get("type")
    valid = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list), "string": lambda x: isinstance(x, str), "integer": lambda x: isinstance(x, int) and not isinstance(x, bool), "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool), "boolean": lambda x: isinstance(x, bool), "null": lambda x: x is None}
    if isinstance(kind, str) and kind in valid and not valid[kind](value):
        raise WorkflowValidationError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise WorkflowValidationError(f"{path} must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        raise WorkflowValidationError(f"{path} must equal {schema['const']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, comparison in (("minimum", lambda x: value < x), ("maximum", lambda x: value > x), ("exclusiveMinimum", lambda x: value <= x), ("exclusiveMaximum", lambda x: value >= x)):
            if key in schema and comparison(schema[key]):
                raise WorkflowValidationError(f"{path} violates {key}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise WorkflowValidationError(f"{path} violates length bound")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise WorkflowValidationError(f"{path} does not match pattern")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise WorkflowValidationError(f"{path} violates item bound")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _static_schema_check(item, item_schema, f"{path}[{index}]", deferred)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            missing = set(schema.get("required", [])) - set(value)
            if missing:
                raise WorkflowValidationError(f"{path} is missing {', '.join(sorted(missing))}")
            for key, item in value.items():
                item_schema = properties.get(key, schema.get("additionalProperties", True))
                if item_schema is False:
                    raise WorkflowValidationError(f"{path}.{key} is unknown")
                if isinstance(item_schema, dict):
                    _static_schema_check(item, item_schema, f"{path}.{key}", deferred)


def preflight_steps(steps: list[dict[str, Any]], catalog: dict[str, dict[str, Any]]) -> list[str]:
    deferred: list[str] = []
    for step in steps:
        for action_name, action in (("forward", step), ("rollback", step.get("rollback"))):
            if action is None:
                continue
            tool = action["tool"]
            if tool not in catalog:
                raise WorkflowValidationError(f"Step {step['id']} uses unknown {action_name} tool: {tool}")
            _static_schema_check(action["arguments"], catalog[tool], f"steps.{step['id']}.{action_name}.arguments", deferred)
    return deferred


def validate_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_WORKFLOW_STEPS:
        raise WorkflowValidationError(f"steps must contain 1 to {MAX_WORKFLOW_STEPS} items")
    try:
        size = len(json.dumps(steps, allow_nan=False, ensure_ascii=True).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise WorkflowValidationError("steps must be finite JSON-safe data") from exc
    if size > MAX_WORKFLOW_BYTES:
        raise WorkflowValidationError(f"workflow definition exceeds {MAX_WORKFLOW_BYTES} bytes")
    normalized: list[dict[str, Any]] = []
    prior_ids: set[str] = set()
    rollback_refs: list[tuple[str, set[str]]] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise WorkflowValidationError(f"steps[{index}] must be an object")
        extra = sorted(set(raw) - {"id", "tool", "arguments", "rollback"})
        if extra:
            raise WorkflowValidationError(f"Unknown steps[{index}] fields: {', '.join(extra)}")
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
            raise WorkflowValidationError(f"steps[{index}].id must match {_STEP_ID.pattern}")
        if step_id in prior_ids:
            raise WorkflowValidationError(f"Duplicate workflow step id: {step_id}")
        action = _action(
            {"tool": raw.get("tool"), "arguments": raw.get("arguments", {})},
            f"steps[{index}]",
        )
        forward_refs = _reference_ids(action["arguments"])
        unknown = sorted(forward_refs - prior_ids)
        if unknown:
            raise WorkflowValidationError(f"Step {step_id} references unavailable prior steps: {', '.join(unknown)}")
        item = {"id": step_id, **action, "rollback": None}
        if raw.get("rollback") is not None:
            rollback = _action(raw["rollback"], f"steps[{index}].rollback")
            item["rollback"] = rollback
            rollback_refs.append((step_id, _reference_ids(rollback["arguments"])))
        normalized.append(item)
        prior_ids.add(step_id)
    all_ids = {step["id"] for step in normalized}
    for step_id, references in rollback_refs:
        unknown = sorted(references - all_ids)
        if unknown:
            raise WorkflowValidationError(f"Rollback for {step_id} references unknown steps: {', '.join(unknown)}")
    return normalized


def _lookup(reference: str, results: dict[str, Any]) -> Any:
    step_id, _, path = reference.partition(".")
    if step_id not in results:
        raise WorkflowValidationError(f"Reference step has no completed result: {step_id}")
    current: Any = results[step_id]
    for token in path.split("."):
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdecimal() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise WorkflowValidationError(f"Reference path not found: {reference}")
    return deepcopy(current)


def _resolve(value: Any, results: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_resolve(item, results) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value and set(value) != {"$ref"}:
        raise WorkflowValidationError("$ref objects cannot contain sibling fields")
    if set(value) == {"$ref"}:
        return _lookup(value["$ref"], results)
    return {key: _resolve(item, results) for key, item in value.items()}


def _failed_step_change_possible(result: dict[str, Any]) -> bool:
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("changed"), bool):
        return data["changed"]
    error = result.get("error")
    error_type = error.get("type") if isinstance(error, dict) else None
    return error_type not in _KNOWN_NONMUTATING_FAILURES


def _workflow_digest(
    normalized: list[dict[str, Any]],
    *,
    rollback_on_failure: bool,
    confirm_rollback: bool | None,
    instance: str | int | None,
) -> str:
    payload = {
        "contract": 1,
        "steps": normalized,
        "rollback_on_failure": rollback_on_failure,
        "confirm_rollback": confirm_rollback,
        "instance": instance,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _step_result_summary(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        return {"status": "invalid_cached_step"}
    summary = {
        key: deepcopy(step[key])
        for key in ("id", "tool", "status")
        if key in step
    }
    result = step.get("result")
    if isinstance(result, dict):
        summary["result_ok"] = result.get("ok") is True
        error = result.get("error")
        if isinstance(error, dict):
            message = str(error.get("message", ""))
            summary["error"] = {
                "type": str(error.get("type", "WorkflowStepError")),
                "message": message[:2000],
            }
    return summary


def _compact_replay_result(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    compact_data: dict[str, Any] = {}
    if isinstance(data, dict):
        for key in (
            "manifest_version",
            "operation_id",
            "workflow_digest",
            "executed",
            "step_count",
            "plan",
            "progress",
            "rollback_confirmation_required",
            "failed_step",
            "partial_change_possible",
            "idempotency",
        ):
            if key in data:
                compact_data[key] = deepcopy(data[key])
        steps = data.get("steps")
        compact_data["steps"] = (
            [_step_result_summary(step) for step in steps]
            if isinstance(steps, list)
            else []
        )
        rollback = data.get("rollback")
        if isinstance(rollback, dict):
            compact_data["rollback"] = {
                key: deepcopy(rollback[key])
                for key in ("requested", "attempted", "complete")
                if key in rollback
            }
            rollback_steps = rollback.get("steps")
            compact_data["rollback"]["steps"] = (
                [_step_result_summary(step) for step in rollback_steps]
                if isinstance(rollback_steps, list)
                else []
            )
    compact_data["replay_result_compacted"] = True
    warnings = result.get("warnings")
    compact_warnings = list(warnings) if isinstance(warnings, list) else []
    compact_warnings.append(
        "The process-local idempotency cache retained an outcome summary because the original workflow result exceeded its replay size limit."
    )
    return {
        "ok": result.get("ok") is True,
        "tool": "workflow.run",
        "data": compact_data,
        "error": deepcopy(result.get("error")),
        "warnings": compact_warnings,
    }


def _bounded_replay_result(result: dict[str, Any], max_result_bytes: int) -> dict[str, Any]:
    try:
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return _compact_replay_result(result)
    return deepcopy(result) if len(encoded) <= max_result_bytes else _compact_replay_result(result)


def _mark_idempotency(
    result: dict[str, Any],
    *,
    key: str,
    digest: str,
    replayed: bool,
    state: str = "complete",
) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
        result["data"] = data
    data["idempotency"] = {
        "enabled": True,
        "key": key,
        "workflow_digest": digest,
        "scope": "mcp_process",
        "state": state,
        "replayed": replayed,
    }
    return result


def _envelope(ok: bool, data: dict[str, Any], error_type: str | None = None, message: str | None = None) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": "workflow.run",
        "data": data,
        "error": None if ok else {"type": error_type or "WorkflowError", "message": message or "Workflow failed", "traceback": ""},
        "warnings": [],
    }


def _update_progress(
    data: dict[str, Any],
    state: str,
    *,
    current_step: str | None = None,
) -> None:
    steps = data.get("steps")
    completed_steps = 0
    failed_steps = 0
    last_completed_step = None
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("status") == "completed":
                completed_steps += 1
                last_completed_step = step.get("id")
            elif step.get("status") == "failed":
                failed_steps += 1
    total_steps = data.get("step_count", 0)
    attempted_steps = completed_steps + failed_steps
    data["progress"] = {
        "state": state,
        "total_steps": total_steps,
        "completed_steps": completed_steps,
        "failed_steps": failed_steps,
        "remaining_steps": max(0, total_steps - attempted_steps),
        "current_step": current_step,
        "last_completed_step": last_completed_step,
        "fraction": completed_steps / total_steps if total_steps else 0.0,
        "cancellation": {
            "available": False,
            "safe_boundary": "between_tool_calls",
            "in_flight_tool_interruptible": False,
            "note": "workflow.run is synchronous; the current adapter cannot safely interrupt a Blender tool after dispatch.",
        },
    }


def _execute_normalized(
    caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
    *,
    normalized: list[dict[str, Any]],
    data: dict[str, Any],
    rollback_on_failure: bool,
    instance: str | int | None,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    completed: list[dict[str, Any]] = []
    failed_step: str | None = None
    failure_message = ""
    for step in normalized:
        _update_progress(data, "running", current_step=step["id"])
        try:
            arguments = _resolve(step["arguments"], results)
            marker = workflow_parent.set(data["operation_id"])
            try:
                result = caller(step["tool"], arguments, instance)
            finally:
                workflow_parent.reset(marker)
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise RuntimeError("tool returned an invalid envelope")
        except Exception as exc:
            result = {
                "ok": False,
                "tool": step["tool"],
                "data": {},
                "error": {"type": type(exc).__name__, "message": str(exc), "traceback": ""},
                "warnings": [],
            }
        status = "completed" if result["ok"] else "failed"
        data["steps"].append(
            {"id": step["id"], "tool": step["tool"], "status": status, "result": result}
        )
        _update_progress(data, "running" if result["ok"] else "failed")
        results[step["id"]] = result
        if result["ok"]:
            completed.append(step)
            continue
        failed_step = step["id"]
        error = result.get("error")
        failure_message = (
            error.get("message", "step failed")
            if isinstance(error, dict)
            else "step failed"
        )
        break

    if failed_step is None:
        _update_progress(data, "completed")
        return _envelope(True, data)

    if rollback_on_failure:
        rollback_data = data["rollback"]
        rollback_data["attempted"] = True
        _update_progress(data, "rolling_back")
        rollback_ok = True
        for step in reversed(completed):
            rollback = step["rollback"]
            if rollback is None:
                rollback_data["steps"].append({"id": step["id"], "status": "not_defined"})
                rollback_ok = False
                continue
            try:
                arguments = _resolve(rollback["arguments"], results)
                marker = workflow_parent.set(data["operation_id"])
                try:
                    result = caller(rollback["tool"], arguments, instance)
                finally:
                    workflow_parent.reset(marker)
                ok = isinstance(result, dict) and result.get("ok") is True
            except Exception as exc:
                result = {
                    "ok": False,
                    "tool": rollback["tool"],
                    "data": {},
                    "error": {"type": type(exc).__name__, "message": str(exc), "traceback": ""},
                    "warnings": [],
                }
                ok = False
            rollback_data["steps"].append(
                {
                    "id": step["id"],
                    "tool": rollback["tool"],
                    "status": "completed" if ok else "failed",
                    "result": result,
                }
            )
            rollback_ok = rollback_ok and ok
        rollback_data["complete"] = rollback_ok
    data["failed_step"] = failed_step
    failed_step_partial = _failed_step_change_possible(results[failed_step])
    completed_step_partial = bool(completed) and not (
        data["rollback"]["complete"] is True
    )
    data["partial_change_possible"] = failed_step_partial or completed_step_partial
    data["failed_step_change"] = {
        "explicit": isinstance(results[failed_step].get("data"), dict)
        and isinstance(results[failed_step]["data"].get("changed"), bool),
        "possible": failed_step_partial,
    }
    _update_progress(data, "failed")
    return _envelope(
        False,
        data,
        "WorkflowStepError",
        f"Step {failed_step} failed: {failure_message}",
    )


def run_workflow(
    caller: Callable[[str, dict[str, Any], str | int | None], dict[str, Any]],
    *,
    steps: Any,
    execute: bool,
    rollback_on_failure: bool,
    confirm_rollback: bool | None,
    instance: str | int | None,
    idempotency_key: str | None = None,
    replay_cache: WorkflowReplayCache | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    operation_id = f"workflow-{next(_workflow_counter):08d}"
    try:
        normalized = validate_steps(steps)
        deferred_checks = preflight_steps(normalized, catalog) if catalog is not None else []
        if not isinstance(execute, bool):
            raise WorkflowValidationError("execute must be a boolean")
        if not isinstance(rollback_on_failure, bool):
            raise WorkflowValidationError("rollback_on_failure must be a boolean")
        if confirm_rollback is not None and not isinstance(confirm_rollback, bool):
            raise WorkflowValidationError("confirm_rollback must be a boolean")
        if execute and rollback_on_failure and confirm_rollback is not True:
            raise WorkflowValidationError("rollback_on_failure requires confirm_rollback: true")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key)
        ):
            raise WorkflowValidationError(
                "idempotency_key must start with an alphanumeric character and contain at most 128 alphanumeric, '.', '_', ':', or '-' characters"
            )
    except WorkflowValidationError as exc:
        return _envelope(
            False,
            {"operation_id": operation_id, "executed": False, "steps": []},
            "WorkflowValidationError",
            str(exc),
        )

    digest = _workflow_digest(
        normalized,
        rollback_on_failure=rollback_on_failure,
        confirm_rollback=confirm_rollback,
        instance=instance,
    )

    plan = [
        {
            "id": step["id"],
            "tool": step["tool"],
            "references": sorted(_reference_ids(step["arguments"])),
            "has_rollback": step["rollback"] is not None,
        }
        for step in normalized
    ]
    data: dict[str, Any] = {
        "manifest_version": 1,
        "operation_id": operation_id,
        "workflow_digest": digest,
        "executed": execute,
        "step_count": len(normalized),
        "plan": plan,
        "deferred_checks": deferred_checks,
        "steps": [],
        "rollback": {"requested": rollback_on_failure, "attempted": False, "complete": None, "steps": []},
        "rollback_confirmation_required": rollback_on_failure,
        "idempotency": {
            "enabled": idempotency_key is not None,
            "key": idempotency_key,
            "workflow_digest": digest if idempotency_key is not None else None,
            "scope": "mcp_process" if idempotency_key is not None else None,
            "state": "plan_only" if not execute and idempotency_key is not None else "disabled",
            "replayed": False,
        },
    }
    _update_progress(data, "planned" if not execute else "running")
    if not execute:
        return _envelope(True, data)

    cache = replay_cache or _workflow_replay_cache
    if idempotency_key is None:
        return _execute_normalized(
            caller,
            normalized=normalized,
            data=data,
            rollback_on_failure=rollback_on_failure,
            instance=instance,
        )

    status, cached, existing_operation_id = cache.begin(
        idempotency_key,
        digest,
        operation_id,
    )
    if status == "replay":
        assert cached is not None
        return _mark_idempotency(
            cached,
            key=idempotency_key,
            digest=digest,
            replayed=True,
        )
    if status != "reserved":
        error_types = {
            "conflict": "WorkflowIdempotencyConflict",
            "running": "WorkflowInProgressError",
            "capacity": "WorkflowReplayCapacityError",
        }
        messages = {
            "conflict": "idempotency_key is already bound to a different workflow request",
            "running": "an identical workflow request with this idempotency_key is still running",
            "capacity": "the process-local workflow replay cache is fully occupied by running requests",
        }
        data["executed"] = False
        data["idempotency"].update(
            state=status,
            existing_operation_id=existing_operation_id,
        )
        _update_progress(data, "rejected")
        return _envelope(False, data, error_types[status], messages[status])

    try:
        result = _execute_normalized(
            caller,
            normalized=normalized,
            data=data,
            rollback_on_failure=rollback_on_failure,
            instance=instance,
        )
    except Exception as exc:
        data["partial_change_possible"] = True
        _update_progress(data, "failed")
        result = _envelope(
            False,
            data,
            "WorkflowExecutionError",
            f"workflow orchestration failed unexpectedly: {exc}",
        )
    _mark_idempotency(
        result,
        key=idempotency_key,
        digest=digest,
        replayed=False,
    )
    cache.complete(idempotency_key, digest, result)
    return result
