"""Shared, deterministic metadata for tools that mutate a Blender document."""

from __future__ import annotations

from itertools import count
from typing import Any

from ..registry import active_context


_operation_counter = count(1)


def new_transaction(affected_datablocks: list[dict[str, str]]) -> dict[str, Any]:
    """Return JSON-safe mutation metadata without depending on clock or randomness."""
    context = active_context()
    client_id = context.get("client_id") if context is not None else None
    return {
        "operation_id": f"op-{next(_operation_counter):08d}",
        "client_id": client_id if isinstance(client_id, str) else None,
        "affected_datablocks": affected_datablocks,
        "changed": False,
        "warnings": [],
    }
