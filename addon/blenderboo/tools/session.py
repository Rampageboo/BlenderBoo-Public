"""Read-only Blender session identity and bridge-health tools."""

from __future__ import annotations

import os
from typing import Any

from ..errors import InvalidParamsError
from ..registry import active_context
from ._blender import get_bpy
from .session_lease import lease_status
from .session_state import current_state_token
from .versioning import file_state


def whoami(params: dict[str, Any]) -> dict[str, Any]:
    if params:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(params))}")
    bpy = get_bpy()
    document = file_state(bpy)
    filepath = document["filepath"]
    from ..bridge import bridge_status

    status = bridge_status()
    context = active_context()
    client_id = context.get("client_id") if context is not None else None
    return {
        "pid": os.getpid(),
        "client_id": client_id if isinstance(client_id, str) else None,
        "filepath": filepath,
        "saved": bool(filepath),
        "dirty": bool(bpy.data.is_dirty),
        "document": document,
        "state_token": current_state_token(bpy),
        "bridge": {
            "running": status["running"],
            "host": status["host"],
            "port": status["port"],
            "instance_id": status["instance_id"],
            "started": status["started"],
            "token_location": status["token_location"],
            "registry_path": status["registry_path"],
            "registry_error": status["registry_error"],
            "queue_depth": status["queue_depth"],
            "queue_capacity": status["queue_capacity"],
            "active_connections": status["active_connections"],
            "connection_capacity": status["connection_capacity"],
            "client_inflight": status["client_inflight"],
            "active_clients": status["active_clients"],
            "per_client_inflight_limit": status["per_client_inflight_limit"],
        },
        "pending_calls": status["pending_calls"],
        "lease": lease_status(bpy),
    }
