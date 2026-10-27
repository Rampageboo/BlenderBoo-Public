"""Best-effort, local JSONL evidence for adapter and Blender calls."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from itertools import count
import json
import os
from pathlib import Path
import re
import sys
from threading import Lock
import time
from typing import Any


workflow_parent: ContextVar[str | None] = ContextVar("workflow_parent", default=None)
MAX_FILE_BYTES = 50 * 1024 * 1024
_SECRET_KEYS = re.compile(r"(?:token|secret|password|api[_-]?key|credential)", re.I)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"), default=str)


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _SECRET_KEYS.search(str(key)) else _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    return value


def _size(value: Any) -> int:
    try:
        return len(_json(value).encode("utf-8"))
    except Exception:
        return 0


class CallLog:
    def __init__(self, directory: Path | None = None, *, token: str | None = None, enabled: bool | None = None, now: datetime | None = None, adapter_version: str | None = None):
        self.enabled = os.environ.get("CBT_CALL_LOG", "1") != "0" if enabled is None else enabled
        self.directory = directory or Path(os.environ.get("CBT_CALL_LOG_DIR", Path.home() / ".blenderboo" / "call-logs"))
        self.started = now or datetime.now(timezone.utc)
        self.adapter_version = adapter_version
        self.session = f"{os.getpid()}-{self.started.strftime('%Y%m%dT%H%M%S%fZ')}"
        mapped: list[str] = []
        try:
            token_map = json.loads(os.environ.get("CBT_BRIDGE_TOKENS", "{}"))
            if isinstance(token_map, dict):
                mapped = [value for value in token_map.values() if isinstance(value, str)]
        except ValueError:
            pass
        self._secrets = tuple(secret for secret in (token, os.environ.get("CBT_BRIDGE_TOKEN"), *mapped) if secret)
        self._sequence = count(1)
        self._lock = Lock()
        self._part = 0
        self._warned = False
        if self.enabled:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                cutoff = self.started - timedelta(days=30)
                for path in self.directory.glob("*.jsonl"):
                    if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                        path.unlink()
            except OSError as exc:
                self._warn(exc)

    def _warn(self, exc: Exception) -> None:
        if not self._warned:
            print(f"BlenderBoo call log unavailable: {type(exc).__name__}", file=sys.stderr)
            self._warned = True

    def next_seq(self) -> int:
        return next(self._sequence)

    def target_context(self, selector: str | int | None, client: Any) -> tuple[str | None, str | None]:
        """Resolve non-secret target identity from the same registry as the router."""
        port = getattr(client, "port", None)
        if isinstance(port, int):
            return f"port-{port}", None
        try:
            from .instances import load_instances, select_instance
            entries = load_instances()
            selected = select_instance(entries, selector)
            label = f"port-{selected.port}/pid-{selected.pid}" if selected.pid else f"port-{selected.port}"
            return label, Path(selected.filepath).name or None
        except Exception:
            return str(selector) if selector is not None else None, None

    def record(self, *, seq: int, tool: str, params: Any, result: Any = None, elapsed_ms: float | None = None, serialize_ms: float | None = None, public: Any = None, instance: Any = None, blend_file: str | None = None, parent: str | None = None, late: bool = False, error: Exception | None = None) -> None:
        if not self.enabled:
            return
        try:
            safe_params = _redact(params, self._secrets)
            canonical = _json(safe_params)
            safe_result = _redact(result, self._secrets)
            envelope = safe_result if isinstance(safe_result, dict) else {}
            data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
            failure = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
            meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
            acceptance = data.get("acceptance")
            if tool == "assembly.analyze" and not (isinstance(acceptance, str) and acceptance in {"pass", "fail", "borderline"}):
                checks = [pair for pair in data.get("pairs", []) if isinstance(pair, dict) and isinstance(pair.get("expectation"), dict)] if isinstance(data.get("pairs"), list) else []
                if checks:
                    acceptance = "borderline" if any(pair.get("borderline") for pair in checks) else (
                        "fail" if any(pair["expectation"].get("status") == "fail" for pair in checks) else "pass")
            if not isinstance(acceptance, str) or acceptance not in {"pass", "fail", "borderline"}:
                acceptance = None
            timings = meta.get("timings") if isinstance(meta.get("timings"), dict) else {}
            durations = {name: timings[source] for name, source in (("queue", "queue_ms"), ("blender", "blender_ms")) if isinstance(timings.get(source), (int, float))}
            exec_timings = meta.get("timings_ms") if isinstance(meta.get("timings_ms"), dict) else {}
            for name in ("exec_user", "exec_overhead"):
                value = exec_timings.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    durations[name] = value
            if elapsed_ms is not None:
                durations["total"] = round(elapsed_ms, 3)
            if serialize_ms is not None:
                durations["serialize"] = round(serialize_ms, 3)
            record = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "session": self.session,
                "seq": seq,
                "parent": parent,
                "tool": tool,
                "instance": instance,
                "blend_file": Path(str(data.get("filepath", data.get("blend_file", "")))).name or blend_file,
                "params_digest": sha256(canonical.encode("utf-8")).hexdigest(),
                "params_preview": canonical[:2048],
                "code": _redact(params.get("code", ""), self._secrets)[:65536] if tool == "python.exec" and isinstance(params, dict) else None,
                "modules": data.get("modules") if tool == "python.exec" and isinstance(data.get("modules"), dict) else None,
                "addon_version": meta.get("addon_version", data.get("addon_version")),
                "adapter_version": self.adapter_version,
                "acceptance": acceptance,
                "ok": envelope.get("ok") is True if error is None else False,
                "error_type": type(error).__name__ if error else failure.get("type"),
                "error_message": (_redact(str(error), self._secrets) if error else str(failure.get("message") or ""))[:500] or None,
                "changed": data.get("changed"),
                "partial_change_possible": data.get("partial_change_possible", False),
                "timings_ms": durations,
                "response_bytes": {"raw": _size(safe_result), "public": _size(_redact(public, self._secrets)) if public is not None else 0},
                "timeout": bool(data.get("timed_out") or failure.get("type") in {"TimeoutError", "BridgeTimeoutError", "BridgeExecutionPendingError"} or isinstance(error, TimeoutError)),
            }
            if late:
                record["late"] = True
            line = (_json(record) + "\n").encode("utf-8")
            with self._lock:
                path = self.directory / f"{self.started.date()}_{self.session}{'_' + str(self._part) if self._part else ''}.jsonl"
                if path.exists() and path.stat().st_size + len(line) > MAX_FILE_BYTES:
                    self._part += 1
                    path = self.directory / f"{self.started.date()}_{self.session}_{self._part}.jsonl"
                with path.open("ab") as handle:
                    handle.write(line)
        except (OSError, ValueError, TypeError) as exc:
            self._warn(exc)


def summarize(paths: list[Path], *, since: datetime | None = None, instance: str | None = None,
              since_seq: int | None = None, session: str | None = None,
              detail: str = "full") -> dict[str, Any]:
    if detail not in {"compact", "full"}:
        raise ValueError("detail must be compact or full")
    if since_seq is not None and (isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0 or session is None):
        raise ValueError("since_seq requires a non-negative integer and session")
    records = []
    for path in paths:
        files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
        for file in files:
            with file.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                        when = datetime.fromisoformat(item["ts"].replace("Z", "+00:00"))
                    except (ValueError, KeyError, TypeError):
                        continue
                    if (since is None or when >= since) and (instance is None or item.get("instance") == instance) and (session is None or item.get("session") == session) and (since_seq is None or item.get("seq", -1) > since_seq):
                        records.append(item)
    # A pending synchronous call and its later authoritative result share a
    # sequence. Count the final outcome once in per-tool success rates.
    resolved = {(item.get("session"), item.get("seq"), item.get("tool"))
                for item in records if item.get("late")}
    effective_records = [item for item in records if item.get("late") or
                         (item.get("session"), item.get("seq"), item.get("tool")) not in resolved]
    groups = defaultdict(list)
    for item in effective_records:
        groups[item.get("tool", "unknown")].append(item)

    def distribution(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        values.sort()
        return {"p50": values[(len(values) - 1) // 2], "p95": values[min(len(values) - 1, int(len(values) * .95))], "max": values[-1]}

    per_tool = {}
    for name, items in sorted(groups.items()):
        acceptance_items = [item["acceptance"] for item in items if item.get("acceptance") in {"pass", "fail", "borderline"}]
        per_tool[name] = {
            "calls": len(items),
            "success_rate": sum(item.get("ok") is True for item in items) / len(items),
            "acceptance": {"evaluated": len(acceptance_items), "pass": acceptance_items.count("pass"),
                           "fail": acceptance_items.count("fail"), "borderline": acceptance_items.count("borderline")},
            "error_types": dict(Counter(item.get("error_type") for item in items if item.get("error_type"))),
            "total_ms": distribution([item["timings_ms"]["total"] for item in items if isinstance(item.get("timings_ms", {}).get("total"), (int, float))]),
            "blender_ms": distribution([item["timings_ms"]["blender"] for item in items if isinstance(item.get("timings_ms", {}).get("blender"), (int, float))]),
            "exec_user_ms": distribution([item["timings_ms"]["exec_user"] for item in items if isinstance(item.get("timings_ms", {}).get("exec_user"), (int, float))]),
            "exec_overhead_ms": distribution([item["timings_ms"]["exec_overhead"] for item in items if isinstance(item.get("timings_ms", {}).get("exec_overhead"), (int, float))]),
            "response_bytes": {key: sum(item.get("response_bytes", {}).get(key, 0) for item in items) for key in ("raw", "public")},
        }
    exec_items = groups.get("python.exec", [])
    mutation_items = [item for item in effective_records if item.get("changed") is not None]
    shapes = Counter(re.sub(r"(?<!\w)(?:\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*')", "?", "\n".join(str(item.get("code", "")).splitlines()[:3])) for item in exec_items)
    if detail == "compact":
        return {
            "session": session, "last_seq": max((item.get("seq", 0) for item in records), default=since_seq or 0),
            "calls": len(records),
            "tools": {name: {"calls": info["calls"], "success_rate": info["success_rate"],
                              "acceptance": info["acceptance"], "response_bytes": info["response_bytes"]}
                      for name, info in per_tool.items()},
            "total_public_bytes": sum(item.get("response_bytes", {}).get("public", 0) for item in records),
            "timeouts": sum(bool(item.get("timeout")) for item in records),
        }
    return {
        "session": session, "last_seq": max((item.get("seq", 0) for item in records), default=since_seq or 0),
        "calls": len(records), "tools": per_tool,
        "python_exec": {"call_share": len(exec_items) / len(records) if records else 0, "mutation_share": sum(item.get("changed") is not None for item in exec_items) / len(mutation_items) if mutation_items else 0, "top_shapes": shapes.most_common(10)},
        "timeouts": sum(bool(item.get("timeout")) for item in records),
        "late_results": sum(bool(item.get("late")) for item in records),
        "partial_changes": sum(bool(item.get("partial_change_possible")) for item in records),
    }
