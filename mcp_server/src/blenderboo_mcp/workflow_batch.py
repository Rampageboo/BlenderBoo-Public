"""Data-only planning for explicitly attributed multi-instance workflow batches."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from typing import Any

from .workflow import validate_steps
from .workflow_templates import expand_template, WorkflowTemplateError


MAX_BATCH_JOBS = 32
MAX_BATCH_BYTES = 524_288
_JOB_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class WorkflowBatchError(ValueError):
    pass


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _normalize_instance(value: Any, label: str) -> str | int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WorkflowBatchError(f"{label} must be an explicit registry id or bridge port")
    if isinstance(value, int):
        if not 1024 <= value <= 65535:
            raise WorkflowBatchError(f"{label} port must be from 1024 to 65535")
        return value
    if not value or value != value.strip() or len(value) > 128:
        raise WorkflowBatchError(f"{label} must be a non-empty instance id of at most 128 characters")
    if value.isdecimal():
        port = int(value)
        if not 1024 <= port <= 65535:
            raise WorkflowBatchError(f"{label} port must be from 1024 to 65535")
        return port
    return value


def _normalize_job(raw: Any, index: int) -> dict[str, Any]:
    label = f"jobs[{index}]"
    if not isinstance(raw, dict):
        raise WorkflowBatchError(f"{label} must be an object")
    allowed = {"id", "instance", "steps", "template", "parameters"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise WorkflowBatchError(f"Unknown {label} fields: {', '.join(unknown)}")
    job_id = raw.get("id")
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise WorkflowBatchError(f"{label}.id must match {_JOB_ID.pattern}")
    if "instance" not in raw:
        raise WorkflowBatchError(f"{label}.instance is required; batch jobs cannot use implicit routing")
    instance = _normalize_instance(raw["instance"], f"{label}.instance")
    has_steps = "steps" in raw
    has_template = "template" in raw
    if has_steps == has_template:
        raise WorkflowBatchError(f"{label} must contain exactly one of steps or template")

    if has_template:
        template = raw["template"]
        if not isinstance(template, str) or not template or len(template) > 128:
            raise WorkflowBatchError(f"{label}.template must be a non-empty name of at most 128 characters")
        try:
            expansion = expand_template(template, raw.get("parameters"))
        except WorkflowTemplateError as exc:
            raise WorkflowBatchError(f"{label} template is invalid: {exc}") from exc
        steps = expansion["steps"]
        source = {
            "kind": "template",
            "name": expansion["name"],
            "version": expansion["version"],
            "template_digest": expansion["template_digest"],
            "parameters": expansion["parameters"],
        }
        run_options = expansion["run_options"]
    else:
        if "parameters" in raw:
            raise WorkflowBatchError(f"{label}.parameters requires template")
        try:
            steps = validate_steps(raw["steps"])
        except ValueError as exc:
            raise WorkflowBatchError(f"{label}.steps are invalid: {exc}") from exc
        source = {"kind": "steps"}
        run_options = {}

    request_payload = {
        "contract": 1,
        "instance": instance,
        "steps": steps,
        "run_options": run_options,
    }
    return {
        "index": index,
        "id": job_id,
        "instance": instance,
        "source": source,
        "step_count": len(steps),
        "tools": [step["tool"] for step in steps],
        "workflow_digest": _digest(request_payload),
        "state": "planned",
        "run_request": {
            "steps": deepcopy(steps),
            "execute": False,
            "instance": instance,
            **deepcopy(run_options),
        },
    }


def plan_batch(
    jobs: Any,
    *,
    scheduling: str = "serial",
    failure_policy: str = "stop",
) -> dict[str, Any]:
    if scheduling not in {"serial", "parallel"}:
        raise WorkflowBatchError("scheduling must be 'serial' or 'parallel'")
    if failure_policy not in {"stop", "continue"}:
        raise WorkflowBatchError("failure_policy must be 'stop' or 'continue'")
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_BATCH_JOBS:
        raise WorkflowBatchError(f"jobs must contain 1 to {MAX_BATCH_JOBS} items")
    try:
        size = len(json.dumps(jobs, ensure_ascii=True, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise WorkflowBatchError("jobs must contain only finite JSON values") from exc
    if size > MAX_BATCH_BYTES:
        raise WorkflowBatchError(f"batch definition exceeds {MAX_BATCH_BYTES} bytes")

    normalized = [_normalize_job(raw, index) for index, raw in enumerate(jobs)]
    ids = [job["id"] for job in normalized]
    if len(ids) != len(set(ids)):
        raise WorkflowBatchError("batch job ids must be unique")
    instances = [job["instance"] for job in normalized]
    if scheduling == "parallel" and len(instances) != len(set(instances)):
        raise WorkflowBatchError(
            "parallel batch jobs must target distinct instances; use serial scheduling for repeated targets"
        )

    manifest_payload = {
        "contract": 1,
        "scheduling": scheduling,
        "failure_policy": failure_policy,
        "jobs": [
            {
                "id": job["id"],
                "instance": job["instance"],
                "workflow_digest": job["workflow_digest"],
            }
            for job in normalized
        ],
    }
    waves = (
        [[job["id"]] for job in normalized]
        if scheduling == "serial"
        else [[job["id"] for job in normalized]]
    )
    return {
        "manifest_version": 1,
        "batch_digest": _digest(manifest_payload),
        "scheduling": scheduling,
        "failure_policy": failure_policy,
        "job_count": len(normalized),
        "instance_count": len(set(instances)),
        "execution_waves": waves,
        "jobs": normalized,
        "progress": {
            "state": "planned",
            "total_jobs": len(normalized),
            "completed_jobs": 0,
            "failed_jobs": 0,
            "remaining_jobs": len(normalized),
        },
        "execution": {
            "performed": False,
            "background_started": False,
            "instance_existence_verified": False,
            "note": "This planner performs no bridge calls. Verify session.instances, review every run_request, then invoke workflow.run explicitly for each attributed job.",
        },
    }
