"""Capability discovery and non-secret runtime diagnostics."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from typing import Any, Callable

from ..errors import InvalidParamsError, NotFoundError
from ..registry import ToolResult
from ..version import version_string
from ._blender import get_bpy
from .session_state import current_state_token


MAX_CAPABILITY_PAGE = 200


def _unknown(params: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(params) - allowed)
    if extra:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(extra)}")


def _limit_offset(params: dict[str, Any]) -> tuple[int, int]:
    limit = params.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CAPABILITY_PAGE:
        raise InvalidParamsError(f"limit must be an integer from 1 to {MAX_CAPABILITY_PAGE}")
    cursor = params.get("cursor")
    if cursor is None:
        return limit, 0
    if isinstance(cursor, str) and 1 <= len(cursor) <= 18 and cursor.isascii() and cursor.isdecimal():
        return limit, int(cursor)
    raise InvalidParamsError("cursor must be a non-negative decimal string returned by system.capabilities")


def capability_digest(specs: list[dict[str, Any]]) -> str:
    payload = json.dumps(specs, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def tool_schema_digests(specs: list[dict[str, Any]]) -> dict[str, str]:
    """Stable hashes of the current add-on input schemas, keyed by tool name."""
    return {
        item["name"]: sha256(json.dumps(item["input_schema"], sort_keys=True,
                                        separators=(",", ":"), ensure_ascii=True,
                                        allow_nan=False).encode("utf-8")).hexdigest()
        for item in specs
    }


def capabilities_handler(
    toolsets_provider: Callable[[], tuple[dict[str, Any], ...]],
    specs_provider: Callable[[], tuple[dict[str, Any], ...]],
    specs_for_toolset: Callable[[str], tuple[dict[str, Any], ...]],
):
    """Create a handler bound to the finalized transport-independent registry."""

    def capabilities(params: dict[str, Any]) -> ToolResult:
        _unknown(params, {"toolset", "include_schemas", "limit", "cursor"})
        toolset = params.get("toolset")
        if toolset is not None and (not isinstance(toolset, str) or not toolset):
            raise InvalidParamsError("toolset must be a non-empty string")
        include_schemas = params.get("include_schemas", False)
        if not isinstance(include_schemas, bool):
            raise InvalidParamsError("include_schemas must be a boolean")
        limit, offset = _limit_offset(params)
        all_specs = list(specs_provider())
        if toolset is None:
            records = list(toolsets_provider())
            kind = "toolsets"
        else:
            records = list(specs_for_toolset(toolset))
            if not records:
                raise NotFoundError(f"Toolset not found: {toolset}")
            kind = "tools"
            if not include_schemas:
                records = [
                    {"name": item["name"], "toolset": item["toolset"], "description": item["description"]}
                    for item in records
                ]
        page = records[offset : offset + limit]
        next_offset = offset + len(page)
        return ToolResult(data={
            "kind": kind,
            "toolset": toolset,
            "count": len(records),
            kind: page,
            "truncated": next_offset < len(records),
            "next_cursor": str(next_offset) if next_offset < len(records) else None,
            "capability_count": len(all_specs),
            "capability_digest": capability_digest(all_specs),
        })

    return capabilities


def doctor_handler(specs_provider: Callable[[], tuple[dict[str, Any], ...]]):
    """Create a read-only diagnostic handler without exposing credentials."""

    def doctor(params: dict[str, Any]) -> ToolResult:
        _unknown(params, set())
        bpy = get_bpy()
        from ..bridge import bridge_status

        bridge = bridge_status()
        specs = list(specs_provider())
        problems: list[str] = []
        if bridge.get("registry_error"):
            problems.append("instance registry reports an error")
        if not bridge.get("running"):
            problems.append("bridge is not running")
        filepath = str(bpy.data.filepath or "")
        return ToolResult(data={
            "healthy": not problems,
            "problems": problems,
            "addon_version": version_string(),
            "blender_version": str(bpy.app.version_string),
            "python_pid": os.getpid(),
            "document": {
                "filepath": filepath,
                "saved": bool(filepath),
                "dirty": bool(bpy.data.is_dirty),
                "mode": str(bpy.context.mode),
                "state_token": current_state_token(bpy),
            },
            "bridge": {
                "running": bridge.get("running"),
                "host": bridge.get("host"),
                "port": bridge.get("port"),
                "instance_id": bridge.get("instance_id"),
                "registry_error": bridge.get("registry_error"),
                "queue_depth": bridge.get("queue_depth"),
                "queue_capacity": bridge.get("queue_capacity"),
            },
            "capabilities": {
                "count": len(specs),
                "digest": capability_digest(specs),
                "schema_digests": tool_schema_digests(specs),
            },
            "client_schema_notice": "Restart the MCP client after an adapter upgrade to refresh cached parameters of existing tools.",
        })

    return doctor
