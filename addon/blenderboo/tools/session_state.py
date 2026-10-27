"""Process-global optimistic concurrency state for the active Blender document."""

from __future__ import annotations

from hashlib import sha1
from typing import Any

from ..errors import InvalidParamsError, StaleStateError
from ._blender import get_bpy


_epoch = 0
_last_filepath: str | None = None


def _active_filepath(bpy_module: Any | None = None) -> str:
    bpy = get_bpy() if bpy_module is None else bpy_module
    return str(getattr(bpy.data, "filepath", "") or "")


def reconcile(bpy_module: Any | None = None) -> int:
    """Observe an out-of-band active-file change and advance the global epoch."""
    global _epoch, _last_filepath
    filepath = _active_filepath(bpy_module)
    if _last_filepath is None:
        _last_filepath = filepath
    elif filepath != _last_filepath:
        _epoch += 1
        _last_filepath = filepath
    return _epoch


def bump_epoch() -> int:
    """Advance and return the process-global document mutation epoch."""
    global _epoch
    _epoch += 1
    return _epoch


def current_state_token(bpy_module: Any | None = None) -> str:
    """Return an opaque deterministic token for the active filepath and epoch."""
    epoch = reconcile(bpy_module)
    filepath = _active_filepath(bpy_module)
    digest = sha1(filepath.encode("utf-8")).hexdigest()[:8]
    return f"{digest}.{epoch}"


def check_expected_state(expected_state: Any, bpy_module: Any | None = None) -> str:
    """Validate an optional expected token and return the reconciled current token."""
    if expected_state is not None and (
        not isinstance(expected_state, str) or not expected_state
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")
    current = current_state_token(bpy_module)
    if expected_state is not None and expected_state != current:
        raise StaleStateError(
            f"Expected Blender state {expected_state!r}, but current state is {current!r}"
        )
    return current
