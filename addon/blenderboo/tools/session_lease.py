"""Process-global, expiring document lease for coordinated Blender writes."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Any
from uuid import uuid4

from ..errors import InvalidParamsError, InvalidStateError, LockedError, NotFoundError
from ..registry import active_context
from ._blender import get_bpy


LEASE_SCOPE = "document"
DEFAULT_LEASE_TTL = 300
MIN_LEASE_TTL = 5
MAX_LEASE_TTL = 3600


@dataclass(frozen=True)
class _DocumentLease:
    lease_id: str
    client_id: str
    document_path: str
    ttl: int
    expires_at: float


_lease: _DocumentLease | None = None
_lock = threading.RLock()


def _client_id(*, required: bool) -> str | None:
    context = active_context()
    value = context.get("client_id") if context is not None else None
    if isinstance(value, str) and value:
        return value
    if required:
        raise InvalidStateError(
            "Session lease tools require a non-empty transport client_id"
        )
    return None


def _document_path(bpy_module: Any | None = None) -> str:
    bpy = get_bpy() if bpy_module is None else bpy_module
    return str(getattr(bpy.data, "filepath", "") or "")


def _ttl(value: Any, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidParamsError("ttl must be an integer number of seconds")
    if not MIN_LEASE_TTL <= value <= MAX_LEASE_TTL:
        raise InvalidParamsError(
            f"ttl must be from {MIN_LEASE_TTL} to {MAX_LEASE_TTL} seconds"
        )
    return value


def _lease_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidParamsError("lease_id must be a non-empty string")
    return value


def _remaining(lease: _DocumentLease, now: float) -> int:
    return max(0, math.ceil(lease.expires_at - now))


def _reconcile_locked(now: float, document_path: str | None = None) -> _DocumentLease | None:
    global _lease
    if _lease is not None and _lease.expires_at <= now:
        _lease = None
    if (
        _lease is not None
        and document_path is not None
        and _lease.document_path != document_path
    ):
        _lease = None
    return _lease


def _public_data(
    lease: _DocumentLease,
    now: float,
    client_id: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "active": True,
        "scope": LEASE_SCOPE,
        "held_by": lease.client_id,
        "ttl": lease.ttl,
        "expires_in": _remaining(lease, now),
    }
    if client_id == lease.client_id:
        data["lease_id"] = lease.lease_id
    return data


def _locked(lease: _DocumentLease, now: float) -> LockedError:
    data = _public_data(lease, now, client_id=None)
    return LockedError(
        f"Document lease is held by {lease.client_id!r} for another {data['expires_in']} seconds",
        data,
    )


def expire_lease(now: float | None = None) -> bool:
    """Clear an expired lease without touching Blender state."""
    global _lease
    current = time.monotonic() if now is None else now
    with _lock:
        if _lease is None or _lease.expires_at > current:
            return False
        _lease = None
        return True


def clear_lease() -> bool:
    """Clear process-local lease state during bridge shutdown or test cleanup."""
    global _lease
    with _lock:
        existed = _lease is not None
        _lease = None
        return existed


def follow_active_document(bpy_module: Any | None = None) -> bool:
    """Move the holder's lease after a successful toolkit-controlled file switch."""
    client_id = _client_id(required=False)
    if client_id is None:
        return False
    document_path = _document_path(bpy_module)
    now = time.monotonic()
    global _lease
    with _lock:
        lease = _reconcile_locked(now)
        if lease is None or lease.client_id != client_id:
            return False
        _lease = _DocumentLease(
            lease_id=lease.lease_id,
            client_id=lease.client_id,
            document_path=document_path,
            ttl=lease.ttl,
            expires_at=lease.expires_at,
        )
        return True


def lease_status(bpy_module: Any | None = None) -> dict[str, Any]:
    """Return a bounded lease record, revealing the id only to its holder."""
    client_id = _client_id(required=False)
    document_path = _document_path(bpy_module)
    now = time.monotonic()
    with _lock:
        lease = _reconcile_locked(now, document_path)
        if lease is None:
            return {"active": False, "scope": LEASE_SCOPE}
        return _public_data(lease, now, client_id)


def acquire_lease(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"scope", "ttl"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    scope = params.get("scope", LEASE_SCOPE)
    if scope != LEASE_SCOPE:
        raise InvalidParamsError("scope must be 'document'")
    ttl = _ttl(params.get("ttl"), default=DEFAULT_LEASE_TTL)
    client_id = _client_id(required=True)
    document_path = _document_path()
    now = time.monotonic()
    global _lease
    with _lock:
        lease = _reconcile_locked(now, document_path)
        if lease is not None:
            if lease.client_id != client_id:
                raise _locked(lease, now)
            data = _public_data(lease, now, client_id)
            data["acquired"] = False
            return data
        _lease = _DocumentLease(
            lease_id=uuid4().hex,
            client_id=client_id,
            document_path=document_path,
            ttl=ttl,
            expires_at=now + ttl,
        )
        data = _public_data(_lease, now, client_id)
        data["acquired"] = True
        return data


def renew_lease(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"lease_id", "ttl"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    requested_id = _lease_id(params.get("lease_id"))
    requested_ttl = params.get("ttl")
    if "ttl" in params:
        requested_ttl = _ttl(requested_ttl)
    client_id = _client_id(required=True)
    document_path = _document_path()
    now = time.monotonic()
    global _lease
    with _lock:
        lease = _reconcile_locked(now, document_path)
        if lease is None:
            raise NotFoundError("No active document lease to renew")
        if lease.client_id != client_id:
            raise _locked(lease, now)
        if lease.lease_id != requested_id:
            raise NotFoundError("Document lease_id does not match the active lease")
        ttl = lease.ttl if "ttl" not in params else requested_ttl
        assert isinstance(ttl, int)
        _lease = _DocumentLease(
            lease_id=lease.lease_id,
            client_id=lease.client_id,
            document_path=lease.document_path,
            ttl=ttl,
            expires_at=now + ttl,
        )
        data = _public_data(_lease, now, client_id)
        data["renewed"] = True
        return data


def release_lease(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"lease_id"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    requested_id = _lease_id(params.get("lease_id"))
    client_id = _client_id(required=True)
    document_path = _document_path()
    now = time.monotonic()
    global _lease
    with _lock:
        lease = _reconcile_locked(now, document_path)
        if lease is None:
            raise NotFoundError("No active document lease to release")
        if lease.client_id != client_id:
            raise _locked(lease, now)
        if lease.lease_id != requested_id:
            raise NotFoundError("Document lease_id does not match the active lease")
        _lease = None
        return {
            "released": True,
            "scope": LEASE_SCOPE,
            "lease_id": requested_id,
        }


def require_document_write_access(bpy_module: Any | None = None) -> None:
    """Reject a document write when a different client holds the active lease."""
    client_id = _client_id(required=False)
    document_path = _document_path(bpy_module)
    now = time.monotonic()
    with _lock:
        lease = _reconcile_locked(now, document_path)
        if lease is not None and lease.client_id != client_id:
            raise _locked(lease, now)
