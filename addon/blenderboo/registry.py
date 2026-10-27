"""Transport-independent tool registration and structured dispatch."""

from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import threading
import traceback
from typing import Any, Callable, Mapping

from .errors import InvalidParamsError, NotFoundError

ToolHandler = Callable[[dict[str, Any]], Any]
BeforeDispatch = Callable[[], Any]
DEFERRED_CONTEXT_KEY = "_codex_supports_deferred_results"


_active_context: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "blenderboo_active_context", default=None
)


def active_context() -> Mapping[str, Any] | None:
    """Return the transport context active for the current serialized dispatch."""
    return _active_context.get()


@dataclass(frozen=True)
class ToolResult:
    """Optional handler return type for attaching non-fatal warnings."""

    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _DeferredOutcome:
    result: Any = None
    error: Exception | None = None
    traceback_text: str = ""


class DeferredToolResult:
    """A tool result completed later by Blender's UI/draw lifecycle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False
        self._outcome: _DeferredOutcome | None = None
        self._callbacks: list[Callable[[_DeferredOutcome], None]] = []
        self._cancel_callback: Callable[[], None] | None = None

    def set_cancel_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("Deferred cancel callback must be callable")
        with self._lock:
            if self._done:
                raise RuntimeError("Deferred tool result is already complete")
            if self._cancel_callback is not None:
                raise RuntimeError("Deferred cancel callback is already set")
            self._cancel_callback = callback

    def add_done_callback(self, callback: Callable[[_DeferredOutcome], None]) -> None:
        if not callable(callback):
            raise TypeError("Deferred result callback must be callable")
        with self._lock:
            if not self._done:
                self._callbacks.append(callback)
                return
            outcome = self._outcome
        assert outcome is not None
        callback(outcome)

    def resolve(self, result: Any) -> bool:
        return self._finish(_DeferredOutcome(result=result))

    def reject(self, error: Exception, traceback_text: str = "") -> bool:
        if not isinstance(error, Exception):
            raise TypeError("Deferred tool error must be an Exception")
        return self._finish(
            _DeferredOutcome(
                error=error,
                traceback_text=traceback_text or "Deferred tool failure did not provide a traceback",
            )
        )

    def cancel(self) -> bool:
        outcome = _DeferredOutcome(
            error=RuntimeError("Deferred tool result was cancelled"),
            traceback_text="Deferred tool result was cancelled by bridge shutdown",
        )
        with self._lock:
            if self._done:
                return False
            self._done = True
            self._outcome = outcome
            callbacks = self._callbacks
            self._callbacks = []
            cancel_callback = self._cancel_callback
        if cancel_callback is not None:
            try:
                cancel_callback()
            except Exception as exc:
                outcome = _DeferredOutcome(
                    error=exc,
                    traceback_text=traceback.format_exc(),
                )
                with self._lock:
                    self._outcome = outcome
        for callback in callbacks:
            callback(outcome)
        return True

    def _finish(self, outcome: _DeferredOutcome) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True
            self._outcome = outcome
            callbacks = self._callbacks
            self._callbacks = []
        for callback in callbacks:
            callback(outcome)
        return True


def _base_envelope(tool_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool_name,
        "data": {},
        "error": None,
        "warnings": [],
    }


def _success_envelope(tool_name: str, result: Any) -> dict[str, Any]:
    envelope = _base_envelope(tool_name)
    if isinstance(result, ToolResult):
        data = result.data
        warnings = result.warnings
        meta = result.meta
    else:
        data = result
        warnings = []
        meta = {}
    if not isinstance(data, dict):
        raise TypeError(f"Tool {tool_name} returned {type(data).__name__}; expected dict")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise TypeError(f"Tool {tool_name} returned invalid warnings")
    if not isinstance(meta, dict):
        raise TypeError(f"Tool {tool_name} returned invalid meta")
    envelope.update(ok=True, data=data, warnings=warnings)
    if meta:
        envelope["meta"] = meta
    return envelope


def _failure_envelope(tool_name: str, error: Exception, traceback_text: str) -> dict[str, Any]:
    envelope = _base_envelope(tool_name)
    partial_data = getattr(error, "data", None)
    if isinstance(partial_data, dict):
        envelope["data"] = partial_data
    warnings = getattr(error, "warnings", None)
    if isinstance(warnings, list) and all(isinstance(item, str) for item in warnings):
        envelope["warnings"] = warnings
    meta = getattr(error, "meta", None)
    if isinstance(meta, dict) and meta:
        envelope["meta"] = meta
    envelope["error"] = {
        "type": getattr(error, "original_type", type(error).__name__),
        "message": str(error),
        "traceback": getattr(error, "user_traceback", traceback_text),
    }
    return envelope


class DeferredEnvelope:
    """Bridge-facing deferred value that still resolves to the stable envelope."""

    def __init__(self, tool_name: str, source: DeferredToolResult) -> None:
        self._tool_name = tool_name
        self._source = source

    def add_done_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        def convert(outcome: _DeferredOutcome) -> None:
            if outcome.error is not None:
                envelope = _failure_envelope(
                    self._tool_name, outcome.error, outcome.traceback_text
                )
            else:
                try:
                    envelope = _success_envelope(self._tool_name, outcome.result)
                except Exception as exc:
                    envelope = _failure_envelope(
                        self._tool_name, exc, traceback.format_exc()
                    )
            callback(envelope)

        self._source.add_done_callback(convert)

    def cancel(self) -> bool:
        return self._source.cancel()


@dataclass(frozen=True)
class _RegisteredTool:
    handler: ToolHandler
    description: str
    input_schema: dict[str, Any]
    toolset: str


class ToolRegistry:
    """A small registry that always dispatches into a stable result envelope."""

    def __init__(self, before_dispatch: BeforeDispatch | None = None) -> None:
        self._tools: dict[str, _RegisteredTool] = {}
        self._before_dispatch = before_dispatch

    def register(
        self,
        name: str,
        handler: ToolHandler | None = None,
        *,
        description: str = "",
        input_schema: Mapping[str, Any] | None = None,
        toolset: str | None = None,
    ):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Tool name must be a non-empty string")
        if not isinstance(description, str):
            raise TypeError("Tool description must be a string")
        if input_schema is None:
            normalized_schema = {"type": "object", "additionalProperties": True}
        elif not isinstance(input_schema, Mapping):
            raise TypeError("Tool input_schema must be a mapping")
        else:
            normalized_schema = deepcopy(dict(input_schema))
        if normalized_schema.get("type") != "object":
            raise ValueError("Tool input_schema must describe an object")
        try:
            json.dumps(normalized_schema, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("Tool input_schema must be JSON-serializable") from exc
        normalized_toolset = name.split(".", 1)[0] if toolset is None else toolset
        if not isinstance(normalized_toolset, str) or not normalized_toolset.strip():
            raise ValueError("Tool toolset must be a non-empty string")

        def add(candidate: ToolHandler) -> ToolHandler:
            if name in self._tools:
                raise ValueError(f"Tool already registered: {name}")
            if not callable(candidate):
                raise TypeError("Tool handler must be callable")
            self._tools[name] = _RegisteredTool(
                handler=candidate,
                description=description,
                input_schema=normalized_schema,
                toolset=normalized_toolset,
            )
            return candidate

        return add(handler) if handler is not None else add

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def specs(self) -> tuple[dict[str, Any], ...]:
        """Return transport-ready tool metadata without exposing mutable registry state."""
        return tuple(
            {
                "name": name,
                "toolset": tool.toolset,
                "description": tool.description,
                "input_schema": deepcopy(tool.input_schema),
            }
            for name, tool in sorted(self._tools.items())
        )

    def toolsets(self) -> tuple[dict[str, Any], ...]:
        """Return compact domain summaries derived from the registered tools."""
        grouped: dict[str, list[_RegisteredTool]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.toolset, []).append(tool)
        return tuple(
            {
                "name": name,
                "tool_count": len(tools),
                "description": f"{name} tools for BlenderBoo.",
            }
            for name, tools in sorted(grouped.items())
        )

    def specs_for_toolset(self, toolset: str) -> tuple[dict[str, Any], ...]:
        """Return detached specs for one exact toolset name."""
        return tuple(spec for spec in self.specs() if spec["toolset"] == toolset)

    def dispatch(
        self,
        tool_name: str,
        params: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | DeferredEnvelope:
        """Call a registered tool and always return the stable result envelope."""
        try:
            if not isinstance(tool_name, str) or not tool_name:
                raise InvalidParamsError("tool_name must be a non-empty string")
            if params is None:
                normalized_params: dict[str, Any] = {}
            elif isinstance(params, Mapping):
                normalized_params = dict(params)
            else:
                raise InvalidParamsError("params must be a mapping or null")
            if context is None:
                normalized_context: dict[str, Any] = {}
            elif isinstance(context, Mapping):
                normalized_context = dict(context)
            else:
                raise InvalidParamsError("context must be a mapping or null")

            try:
                handler = self._tools[tool_name].handler
            except KeyError as exc:
                raise NotFoundError(f"Unknown tool: {tool_name}") from exc

            if self._before_dispatch is not None:
                self._before_dispatch()
            context_token = _active_context.set(normalized_context)
            try:
                result = handler(normalized_params)
            finally:
                _active_context.reset(context_token)
            if isinstance(result, DeferredToolResult):
                return DeferredEnvelope(tool_name, result)
            return _success_envelope(tool_name, result)
        except Exception as exc:  # Every ordinary tool failure becomes structured data.
            return _failure_envelope(tool_name, exc, traceback.format_exc())
