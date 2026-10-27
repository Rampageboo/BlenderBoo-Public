"""Atomic shared registry for local Blender bridge instances."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from .errors import InvalidStateError

REGISTRY_ENV = "CBT_INSTANCE_REGISTRY"
REGISTRY_FILENAME = "instances.json"
LEGACY_REGISTRY_FILENAME = "codex_blender_instances.json"
MAX_REGISTRY_BYTES = 1_048_576
LOCK_TIMEOUT_SECONDS = 2.0
STALE_LOCK_SECONDS = 30.0
WINDOWS_REPLACE_ATTEMPTS = 5
WINDOWS_REPLACE_RETRY_SECONDS = 0.025


def registry_path() -> Path:
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".blenderboo" / REGISTRY_FILENAME


def _empty_registry() -> dict[str, Any]:
    return {"version": 1, "instances": []}


def _validate_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidStateError("Instance registry entries must be JSON objects")
    required = {"id", "pid", "port", "filepath", "started", "token_location"}
    if set(value) != required:
        raise InvalidStateError("Instance registry entry fields are invalid")
    instance_id = value["id"]
    pid = value["pid"]
    port = value["port"]
    filepath = value["filepath"]
    started = value["started"]
    token_location = value["token_location"]
    if not isinstance(instance_id, str) or not instance_id:
        raise InvalidStateError("Instance registry id must be a non-empty string")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise InvalidStateError("Instance registry pid must be a positive integer")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise InvalidStateError("Instance registry port must be from 1024 to 65535")
    if not isinstance(filepath, str):
        raise InvalidStateError("Instance registry filepath must be a string")
    if not isinstance(started, str) or not started:
        raise InvalidStateError("Instance registry started must be a non-empty string")
    if not isinstance(token_location, str) or not token_location:
        raise InvalidStateError("Instance registry token_location must be a non-empty string")
    return {
        "id": instance_id,
        "pid": pid,
        "port": port,
        "filepath": filepath,
        "started": started,
        "token_location": token_location,
    }


def _read_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_registry()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InvalidStateError(f"Could not inspect instance registry {path}: {exc}") from exc
    if size > MAX_REGISTRY_BYTES:
        raise InvalidStateError(f"Instance registry exceeds {MAX_REGISTRY_BYTES} bytes")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidStateError(f"Could not read instance registry {path}: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise InvalidStateError("Instance registry version is unsupported")
    instances = payload.get("instances")
    if not isinstance(instances, list):
        raise InvalidStateError("Instance registry instances must be an array")
    return {"version": 1, "instances": [_validate_record(item) for item in instances]}


def read_registry() -> dict[str, Any]:
    """Read and validate the registry without changing it."""
    path = registry_path()
    if not os.environ.get(REGISTRY_ENV) and not path.exists():
        path = Path.home() / ".codex" / LEGACY_REGISTRY_FILENAME
    return _read_unlocked(path)


def _acquire_lock(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > STALE_LOCK_SECONDS
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise InvalidStateError(f"Could not inspect registry lock {lock_path}: {exc}") from exc
            if stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise InvalidStateError(f"Could not remove stale registry lock {lock_path}: {exc}") from exc
                continue
            if time.monotonic() >= deadline:
                raise InvalidStateError(f"Timed out waiting for instance registry lock {lock_path}")
            time.sleep(0.025)
            continue
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        finally:
            os.close(descriptor)
        return lock_path


def _write_unlocked(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_REGISTRY_BYTES:
        raise InvalidStateError(f"Instance registry exceeds {MAX_REGISTRY_BYTES} bytes")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    primary_error: Exception | None = None
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        for attempt in range(WINDOWS_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt + 1 == WINDOWS_REPLACE_ATTEMPTS:
                    raise
                # Windows readers and filesystem scanners can briefly deny a
                # replace even though the writer owns the registry lock.
                time.sleep(WINDOWS_REPLACE_RETRY_SECONDS * (attempt + 1))
    except OSError as exc:
        primary_error = InvalidStateError(f"Could not write instance registry {path}: {exc}")
    cleanup_error: OSError | None = None
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        cleanup_error = exc
    if primary_error is not None:
        if cleanup_error is not None:
            raise InvalidStateError(
                f"{primary_error}; could not remove temporary file {temporary}: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error is not None:
        raise InvalidStateError(
            f"Could not remove temporary registry file {temporary}: {cleanup_error}"
        ) from cleanup_error


def _mutate(mutator: Callable[[list[dict[str, Any]]], None]) -> None:
    path = registry_path()
    lock_path = _acquire_lock(path)
    primary_error: Exception | None = None
    try:
        payload = read_registry()
        instances = payload["instances"]
        mutator(instances)
        instances.sort(key=lambda item: item["id"])
        _write_unlocked(path, payload)
    except Exception as exc:
        primary_error = exc
    cleanup_error: OSError | None = None
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        cleanup_error = exc
    if primary_error is not None:
        if cleanup_error is not None:
            raise InvalidStateError(
                f"{primary_error}; could not remove registry lock {lock_path}: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error is not None:
        raise InvalidStateError(
            f"Could not remove instance registry lock {lock_path}: {cleanup_error}"
        ) from cleanup_error


def register_instance(record: Mapping[str, Any]) -> None:
    """Create or replace one instance entry without disturbing other processes."""
    normalized = _validate_record(record)

    def apply(instances: list[dict[str, Any]]) -> None:
        instances[:] = [
            item
            for item in instances
            if item["id"] != normalized["id"] and item["port"] != normalized["port"]
        ]
        instances.append(normalized)

    _mutate(apply)


def unregister_instance(instance_id: str) -> None:
    """Remove only the caller's instance entry. Repeated removal is safe."""
    if not isinstance(instance_id, str) or not instance_id:
        raise InvalidStateError("instance_id must be a non-empty string")

    def apply(instances: list[dict[str, Any]]) -> None:
        instances[:] = [item for item in instances if item["id"] != instance_id]

    _mutate(apply)
