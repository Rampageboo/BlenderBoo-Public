"""Registry-backed routing across local Blender bridge instances."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .client import BridgeClient, BridgeClientError, DEFAULT_BRIDGE_PORT

REGISTRY_ENV = "CBT_INSTANCE_REGISTRY"
TOKENS_ENV = "CBT_BRIDGE_TOKENS"
MAX_REGISTRY_BYTES = 1_048_576
MAX_TOKEN_MAP_BYTES = 65_536


class InstanceRoutingError(BridgeClientError):
    """Raised when an instance cannot be selected or authenticated safely."""


@dataclass(frozen=True)
class InstanceRecord:
    id: str
    pid: int
    port: int
    filepath: str
    started: str
    token_location: str


def registry_path() -> Path:
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".blenderboo" / "instances.json"


def _validate_record(value: Any) -> InstanceRecord:
    if not isinstance(value, Mapping):
        raise InstanceRoutingError("Instance registry entries must be JSON objects")
    required = {"id", "pid", "port", "filepath", "started", "token_location"}
    if set(value) != required:
        raise InstanceRoutingError("Instance registry entry fields are invalid")
    instance_id = value["id"]
    pid = value["pid"]
    port = value["port"]
    filepath = value["filepath"]
    started = value["started"]
    token_location = value["token_location"]
    if not isinstance(instance_id, str) or not instance_id:
        raise InstanceRoutingError("Instance registry id must be a non-empty string")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise InstanceRoutingError("Instance registry pid must be a positive integer")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise InstanceRoutingError("Instance registry port must be from 1024 to 65535")
    if not isinstance(filepath, str):
        raise InstanceRoutingError("Instance registry filepath must be a string")
    if not isinstance(started, str) or not started:
        raise InstanceRoutingError("Instance registry started must be a non-empty string")
    if not isinstance(token_location, str) or not token_location:
        raise InstanceRoutingError("Instance registry token_location must be a non-empty string")
    return InstanceRecord(
        instance_id,
        pid,
        port,
        filepath,
        started,
        token_location,
    )


def load_instances() -> list[InstanceRecord]:
    path = registry_path()
    if not os.environ.get(REGISTRY_ENV) and not path.exists():
        path = Path.home() / ".codex" / "codex_blender_instances.json"
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InstanceRoutingError(f"Could not inspect instance registry {path}: {exc}") from exc
    if size > MAX_REGISTRY_BYTES:
        raise InstanceRoutingError(f"Instance registry exceeds {MAX_REGISTRY_BYTES} bytes")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstanceRoutingError(f"Could not read instance registry {path}: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise InstanceRoutingError("Instance registry version is unsupported")
    raw_instances = payload.get("instances")
    if not isinstance(raw_instances, list):
        raise InstanceRoutingError("Instance registry instances must be an array")
    instances = [
        record
        for record in (_validate_record(value) for value in raw_instances)
        if _pid_is_alive(record.pid)
    ]
    ids = [item.id for item in instances]
    ports = [item.port for item in instances]
    if len(ids) != len(set(ids)):
        raise InstanceRoutingError("Instance registry contains duplicate ids")
    if len(ports) != len(set(ports)):
        raise InstanceRoutingError("Instance registry contains duplicate ports")
    return sorted(instances, key=lambda item: (item.port, item.id))


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return getattr(exc, "winerror", None) == 5
    return True


def _windows_pid_is_alive(pid: int) -> bool:
    """Check a Windows PID without os.kill(pid, 0)'s cross-process bug."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied still proves that the PID exists.
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _legacy_port() -> int:
    raw_port = os.environ.get("CBT_BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise InstanceRoutingError("CBT_BRIDGE_PORT must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise InstanceRoutingError("CBT_BRIDGE_PORT must be from 1024 to 65535")
    return port


def _legacy_record(port: int | None = None) -> InstanceRecord:
    resolved_port = _legacy_port() if port is None else port
    return InstanceRecord(
        id=f"port-{resolved_port}",
        pid=0,
        port=resolved_port,
        filepath="",
        started="",
        token_location="env:CBT_BRIDGE_TOKEN",
    )


def _normalize_selector(instance: str | int | None) -> str | int | None:
    if instance is None:
        return None
    if isinstance(instance, bool) or not isinstance(instance, (str, int)):
        raise InstanceRoutingError("instance must be a registry id or port")
    if isinstance(instance, int):
        if not 1024 <= instance <= 65535:
            raise InstanceRoutingError("instance port must be from 1024 to 65535")
        return instance
    if not instance:
        raise InstanceRoutingError("instance must be a non-empty registry id or port")
    if instance.isdecimal():
        port = int(instance)
        if not 1024 <= port <= 65535:
            raise InstanceRoutingError("instance port must be from 1024 to 65535")
        return port
    return instance


def select_instance(
    instances: list[InstanceRecord], instance: str | int | None
) -> InstanceRecord:
    selector = _normalize_selector(instance)
    if not instances:
        legacy_token = os.environ.get("CBT_BRIDGE_TOKEN")
        if legacy_token is None:
            raise InstanceRoutingError(
                "No Blender instances are registered and CBT_BRIDGE_TOKEN is not configured"
            )
        if selector is None:
            return _legacy_record()
        if isinstance(selector, int):
            return _legacy_record(selector)
        raise InstanceRoutingError(f"Blender instance {selector!r} is not registered")
    if selector is None:
        if len(instances) != 1:
            raise InstanceRoutingError(
                "Multiple Blender instances are registered; pass instance as an id or port"
            )
        return instances[0]
    matches = [
        item
        for item in instances
        if item.id == selector or (isinstance(selector, int) and item.port == selector)
    ]
    if not matches:
        raise InstanceRoutingError(f"Blender instance {selector!r} is not registered")
    return matches[0]


def _token_map() -> dict[str, str]:
    raw = os.environ.get(TOKENS_ENV)
    if raw is None:
        return {}
    if len(raw.encode("utf-8")) > MAX_TOKEN_MAP_BYTES:
        raise InstanceRoutingError(f"{TOKENS_ENV} exceeds {MAX_TOKEN_MAP_BYTES} bytes")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstanceRoutingError(f"{TOKENS_ENV} must be a JSON object") from exc
    if not isinstance(payload, Mapping):
        raise InstanceRoutingError(f"{TOKENS_ENV} must be a JSON object")
    result: dict[str, str] = {}
    for key, token in payload.items():
        if not isinstance(key, str) or not key:
            raise InstanceRoutingError(f"{TOKENS_ENV} keys must be non-empty strings")
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise InstanceRoutingError(
                f"{TOKENS_ENV} token values must contain 32 to 4096 characters"
            )
        result[key] = token
    return result


def resolve_token(record: InstanceRecord, instance_count: int) -> str:
    token = os.environ.get("CBT_BRIDGE_TOKEN")
    if token is None:
        tokens = _token_map()
        token = tokens.get(record.id, tokens.get(str(record.port)))
    if token is None and record.token_location.startswith("env:"):
        variable = record.token_location[4:]
        token = os.environ.get(variable)
    if token is None:
        raise InstanceRoutingError(
            f"No token is configured for Blender instance {record.id!r}; set CBT_BRIDGE_TOKEN"
        )
    if not 32 <= len(token) <= 4096:
        raise InstanceRoutingError(
            f"Configured token for Blender instance {record.id!r} must contain 32 to 4096 characters"
        )
    return token


class InstanceRouter:
    def __init__(self, client_id: str | None = None, connect_timeout: float = 5.0) -> None:
        if client_id is not None and (not isinstance(client_id, str) or not client_id):
            raise ValueError("client_id must be a non-empty string")
        self.client_id = client_id
        self.connect_timeout = connect_timeout

    @classmethod
    def from_env(cls) -> "InstanceRouter":
        return cls(client_id=os.environ.get("CBT_CLIENT_ID"))

    def list_instances(self) -> dict[str, Any]:
        instances = load_instances()
        if not instances and os.environ.get("CBT_BRIDGE_TOKEN") is not None:
            instances = [_legacy_record()]
        return {
            "count": len(instances),
            "default_instance": instances[0].id if len(instances) == 1 else None,
            "registry_path": str(registry_path()),
            "instances": [asdict(item) for item in instances],
        }

    def call(
        self,
        tool: str,
        params: Mapping[str, Any] | None = None,
        instance: str | int | None = None,
    ) -> dict[str, Any]:
        instances = load_instances()
        selected = select_instance(instances, instance)
        token = resolve_token(selected, len(instances))
        client = BridgeClient(
            token,
            selected.port,
            connect_timeout=self.connect_timeout,
            client_id=self.client_id,
        )
        return client.call(tool, params)
