"""Bounded MCP response profiles and process-local full-result artifacts."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
from itertools import count
import json
import os
import threading
from typing import Any, Literal, cast


ResponseMode = Literal["compact", "default", "full"]
RESPONSE_MODES = frozenset({"compact", "default", "full"})
DEFAULT_INLINE_BYTES = 2 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_ENTRIES = 64
MAX_RETRIEVAL_CHARS = 16_000
DEFAULT_RETRIEVAL_CHARS = 8_000

_PRIORITY_DATA_KEYS = (
    "api_hint",
    "blender_version",
    "exists",
    "kind",
    "owner",
    "base",
    "search",
    "suggestions",
    "signature",
    "poll_note",
    "total",
    "suspicious_change",
    "fragment_check",
    "status",
    "dry_run",
    "action",
    "reason",
    "source",
    "target",
    "coverage",
    "source_objects",
    "sources_preserved",
    "seam_edge_indices",
    "new_seam_count",
    "rays",
    "hits",
    "retained_samples",
    "rejected_samples",
    "coefficients",
    "residual_p95_scene_units",
    "translation_world",
    "clearance",
    "gap_scene_units",
    "method",
    "limitation",
    "ok",
    "error",
    "changed",
    "state_token",
    "dirty",
    "saved",
    "write_confirmed",
    "confirmation_required",
    "partial_change_possible",
    "failed_step",
    "failed_step_change",
    "progress",
    "operation_id",
    "workflow_digest",
    "executed",
    "step_count",
    "idempotency",
    "instance",
    "instance_id",
    "selected_instance",
    "name",
    "object",
    "object_name",
    "detail",
    "collection",
    "material",
    "path",
    "paths",
    "diff_path",
    "output_path",
    "created",
    "deleted",
    "renamed",
    "affected",
    "changes",
    "detection",
    "result",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
    "stdout_capture_limit_reached",
    "stderr_capture_limit_reached",
    "count",
    "counts",
    "truncated",
    "next_cursor",
)
_OMITTED_DATA_KEYS = frozenset(
    {
        "journal",
        "steps",
        "rollback",
        "objects",
        "items",
        "vertices",
        "edges",
        "faces",
        "nodes",
        "links",
        "manifest",
        "schemas",
        "tools",
        "stdout_overflow",
        "stderr_overflow",
    }
)


def response_mode(value: str | None) -> ResponseMode:
    resolved = os.environ.get("CBT_MCP_RESPONSE_MODE", "default") if value is None else value
    if not isinstance(resolved, str) or resolved.lower() not in RESPONSE_MODES:
        raise ValueError("response mode must be 'compact', 'default', or 'full'")
    return cast(ResponseMode, resolved.lower())


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


class ResponseArtifactStore:
    """Keep authoritative full responses in a bounded process-local LRU."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_ARTIFACT_ENTRIES,
        max_item_bytes: int = MAX_ARTIFACT_BYTES,
        max_total_bytes: int = MAX_ARTIFACT_TOTAL_BYTES,
    ) -> None:
        if max_entries < 1 or max_item_bytes < 1024 or max_total_bytes < max_item_bytes:
            raise ValueError("response artifact bounds are invalid")
        self._max_entries = max_entries
        self._max_item_bytes = max_item_bytes
        self._max_total_bytes = max_total_bytes
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._total_bytes = 0
        self._counter = count(1)
        self._lock = threading.Lock()

    def put(self, result: dict[str, Any]) -> dict[str, Any] | None:
        text = _json_text(result)
        encoded = text.encode("utf-8")
        size = len(encoded)
        if size > self._max_item_bytes:
            return None
        digest = sha256(encoded).hexdigest()
        with self._lock:
            while self._entries and (
                len(self._entries) >= self._max_entries
                or self._total_bytes + size > self._max_total_bytes
            ):
                _, removed = self._entries.popitem(last=False)
                self._total_bytes -= removed["bytes"]
            if self._total_bytes + size > self._max_total_bytes:
                return None
            artifact_id = f"response-{next(self._counter):08d}-{digest[:12]}"
            self._entries[artifact_id] = {
                "text": text,
                "bytes": size,
                "chars": len(text),
                "sha256": digest,
                "tool": result.get("tool"),
            }
            self._total_bytes += size
        return {
            "artifact_id": artifact_id,
            "sha256": digest,
            "bytes": size,
            "chars": len(text),
            "tool": result.get("tool"),
            "scope": "mcp_process",
            "retrieval_tool": "response.artifact_get",
        }

    def get(
        self,
        artifact_id: str,
        *,
        cursor: int = 0,
        max_chars: int = DEFAULT_RETRIEVAL_CHARS,
        path: str | None = None,
    ) -> dict[str, Any]:
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if not 1 <= max_chars <= MAX_RETRIEVAL_CHARS:
            raise ValueError(f"max_chars must be between 1 and {MAX_RETRIEVAL_CHARS}")
        with self._lock:
            entry = self._entries.get(artifact_id)
            if entry is None:
                raise KeyError(artifact_id)
            self._entries.move_to_end(artifact_id)
            text = entry["text"]
            if path is not None:
                if cursor != 0:
                    raise ValueError("cursor cannot be used with path")
                value: Any = json.loads(text)
                expression = path[2:] if path.startswith("$.") else path[1:] if path == "$" else path
                if expression:
                    for token in expression.replace("[", ".").replace("]", "").split("."):
                        try:
                            if isinstance(value, dict):
                                value = value[token]
                            elif isinstance(value, list) and token.isdecimal():
                                value = value[int(token)]
                            else:
                                raise ValueError(f"JSON path is unavailable: {path}")
                        except (KeyError, IndexError) as exc:
                            raise ValueError(f"JSON path is unavailable: {path}") from exc
                return {"artifact_id": artifact_id, "path": path, "value": value, "complete": True, "scope": "mcp_process"}
            if cursor > len(text):
                raise ValueError("cursor is beyond the artifact")
            end = min(len(text), cursor + max_chars)
            chunk = text[cursor:end]
            next_cursor = str(end) if end < len(text) else None
            metadata = {
                key: entry[key]
                for key in ("bytes", "chars", "sha256", "tool")
            }
        return {
            "artifact_id": artifact_id,
            **metadata,
            "cursor": str(cursor),
            "next_cursor": next_cursor,
            "complete": next_cursor is None,
            "chunk": chunk,
            "scope": "mcp_process",
        }


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 1000 else value[:1000] + "…"
    if depth >= 3:
        if isinstance(value, list):
            return {"type": "array", "count": len(value)}
        if isinstance(value, dict):
            return {"type": "object", "keys": sorted(str(key) for key in value)[:16]}
        return str(value)[:1000]
    if isinstance(value, list):
        items = [_bounded_value(item, depth=depth + 1) for item in value[:16]]
        if len(value) > 16:
            items.append({"items_omitted": len(value) - 16})
        return items
    if isinstance(value, dict):
        keys = list(value)[:24]
        result = {
            str(key): _bounded_value(value[key], depth=depth + 1)
            for key in keys
        }
        if len(value) > 24:
            result["keys_omitted"] = len(value) - 24
        return result
    return str(value)[:1000]


def _receipt_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"result_type": type(data).__name__}
    receipt: dict[str, Any] = {}
    for key in _PRIORITY_DATA_KEYS:
        if key in data:
            receipt[key] = _bounded_value(data[key])
    if "write_confirmed" in data and isinstance(data.get("after"), dict):
        receipt["after"] = _bounded_value(data["after"])
    for key, value in data.items():
        if key in receipt or key in _OMITTED_DATA_KEYS:
            continue
        if (
            key.endswith(("_id", "_name", "_count", "_path", "_cursor"))
            or key.startswith(("created_", "deleted_", "affected_"))
        ):
            receipt[key] = _bounded_value(value)
        if len(receipt) >= 32:
            break
    return receipt


def _compact_error(error: Any) -> Any:
    if not isinstance(error, dict):
        return error
    compact = {
        key: _bounded_value(error[key])
        for key in ("type", "message")
        if key in error
    }
    traceback = error.get("traceback")
    if isinstance(traceback, str) and traceback:
        compact["traceback_omitted"] = True
    return compact


def _compact_warnings(warnings: Any) -> list[Any]:
    if not isinstance(warnings, list):
        return []
    compact = [_bounded_value(item) for item in warnings[:20]]
    if len(warnings) > 20:
        compact.append({"warnings_omitted": len(warnings) - 20})
    return compact


def _workflow_receipt(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    items = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        nested = step.get("result") if isinstance(step.get("result"), dict) else {}
        nested_data = nested.get("data") if isinstance(nested.get("data"), dict) else {}
        item = {"id": step.get("id"), "status": step.get("status")}
        for key in ("changed", "renamed_from", "clamped", "warnings"):
            if key in nested_data and (key == "changed" or nested_data[key]):
                item[key] = nested_data[key]
        items.append(item)
    receipt: dict[str, Any] = {"ok": result.get("ok") is True, "tool": result.get("tool"), "completed": [item["id"] for item in items if item["status"] == "completed"], "failed": [item["id"] for item in items if item["status"] == "failed"], "steps": items}
    if data.get("executed") is False or data.get("state") == "planned":
        receipt["plan"] = data.get("plan", [])
        receipt["executed"] = False
    for key in ("state_token", "operation_id", "job_id", "state", "revision", "progress", "failed_step", "partial_change_possible", "deferred_checks"):
        if key in data and data[key] is not None:
            receipt[key] = data[key]
    if result.get("ok") is not True:
        receipt["error"] = _compact_error(result.get("error"))
        if "rollback" in data:
            receipt["rollback"] = _bounded_value(data["rollback"])
    return receipt


def apply_response_profile(
    result: dict[str, Any],
    *,
    mode: ResponseMode,
    store: ResponseArtifactStore,
    default_inline_bytes: int = DEFAULT_INLINE_BYTES,
    requested: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the public envelope for one configured response profile."""
    if mode == "full":
        return result
    if mode == "default" and result.get("tool") == "bridge.operation_status":
        return result
    if mode == "default" and result.get("tool") in {"workflow.job_start", "workflow.job_control"}:
        job_data = result.get("data")
        if isinstance(job_data, dict) and job_data.get("state") in {"planned", "paused"}:
            return result
    if mode == "default" and result.get("tool") in {"workflow.run", "workflow.job_start", "workflow.job_control"}:
        return _workflow_receipt(result)
    encoded_bytes = len(_json_text(result).encode("utf-8"))
    data = result.get("data")
    if (mode == "default" and result.get("tool") == "python.exec"
            and result.get("ok") is True and isinstance(data, dict)
            and isinstance(data.get("changes"), dict)):
        changes = data["changes"]
        counts = {kind: len(entries) for kind, entries in changes.items()
                  if isinstance(entries, (dict, list))}
        if sum(counts.values()) > 32:
            artifact = store.put(result)
            if artifact is not None:
                compact_data = dict(data)
                compact_data.pop("changes")
                compact_data["changes_summary"] = {
                    "total_entries": sum(counts.values()), "by_category": counts,
                    "examples": {kind: sorted(entries)[:5] for kind, entries in changes.items()
                                 if isinstance(entries, dict) and entries},
                }
                compact_data["artifact"] = artifact["artifact_id"]
                return {**result, "data": compact_data}
    mutation_receipt = (
        mode == "default"
        and result.get("tool") != "python.exec"
        and isinstance(data, dict)
        and isinstance(data.get("changed"), bool)
    )
    if mode == "default" and not mutation_receipt:
        return result
    if (mutation_receipt and result.get("ok") is True and data["changed"] is False
            and (isinstance(data.get("status"), str) or data.get("dry_run") is True)):
        # A planned, analytical, or unsupported result is the tool's answer,
        # not an empty mutation receipt. Keep small results intact; put large
        # fields such as fair_fit's fitted/residual grids in an artifact.
        if encoded_bytes <= default_inline_bytes:
            return result
        artifact = store.put(result)
        if artifact is None:
            return result
        receipt_data = _receipt_data(data)
        receipt_data["artifact"] = artifact["artifact_id"]
        return {"ok": True, "tool": result.get("tool"), "data": receipt_data,
                "error": None, "warnings": _compact_warnings(result.get("warnings"))}
    if mutation_receipt and result.get("ok") is True:
        source = result["data"]
        receipt: dict[str, Any] = {"ok": True, "tool": result.get("tool"), "changed": source["changed"]}
        if source["changed"] is False and isinstance(source.get("reason"), str):
            receipt["reason"] = source["reason"][:1000]
        if "state_token" in source:
            receipt["state_token"] = source["state_token"]
        if "write_confirmed" in source:
            receipt["write_confirmed"] = source["write_confirmed"]
            if isinstance(source.get("after"), dict):
                receipt["after"] = source["after"]
        requested = requested or {}
        for key in ("renamed_from", "clamped", "created", "created_names", "warnings", "suspicious_change"):
            if source.get(key):
                receipt[key] = source[key]
        resolved = source.get("resolved_name")
        original = source.get("requested_name", requested.get("name"))
        if isinstance(resolved, str) and resolved != original:
            receipt["object"] = resolved
            if isinstance(original, str):
                receipt["renamed_from"] = original
        for key in ("object", "name", "object_name", "material", "collection"):
            if key in source and source[key] != requested.get(key, source.get("requested_name")) and key not in receipt:
                receipt[key] = source[key]
        warnings = result.get("warnings")
        if warnings:
            receipt["warnings"] = _compact_warnings(warnings)
        omitted = len(receipt) > (4 if "state_token" in receipt else 3) and any(key in source for key in ("journal", "before", "after"))
        if omitted:
            artifact = store.put(result)
            if artifact is not None:
                receipt["artifact"] = artifact["artifact_id"]
        return receipt
    artifact = store.put(result)
    if artifact is None:
        return result
    data = _receipt_data(result.get("data"))
    if mutation_receipt:
        data.pop("detail", None)
        data.pop("operation_id", None)
        data.pop("client_id", None)
        data.pop("affected_datablocks", None)
        source = result["data"]
        requested = source.get("requested_name")
        resolved = source.get("resolved_name")
        if isinstance(resolved, str) and resolved:
            data.setdefault("object", resolved)
            if isinstance(requested, str) and requested and requested != resolved:
                data["renamed_from"] = requested
        data.pop("requested_name", None)
        data.pop("resolved_name", None)
        affected = source.get("affected_datablocks")
        if isinstance(affected, list) and "affected" not in data:
            names = [item.get("name") for item in affected if isinstance(item, dict) and isinstance(item.get("name"), str)]
            if names:
                data["affected"] = sorted(set(names))[:16]
        if data.get("warnings") == result.get("warnings"):
            data.pop("warnings", None)
    data["artifact"] = artifact["artifact_id"]
    if os.environ.get("CBT_MCP_RESPONSE_DEBUG") == "1":
        data["response_profile"] = {"mode": mode, "compacted": True, "original_bytes": encoded_bytes}
    return {
        "ok": result.get("ok") is True,
        "tool": result.get("tool"),
        "data": data,
        "error": _compact_error(result.get("error")),
        "warnings": _compact_warnings(result.get("warnings")),
    }
