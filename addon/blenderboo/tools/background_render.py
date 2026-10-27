"""Snapshot a live document and render its copy in a separate Blender process."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4

from ..errors import InvalidParamsError, NotFoundError
from ..registry import ToolResult
from ._blender import get_bpy
from .session_state import current_state_token


MODES = frozenset({"preview", "detail", "ortho_set", "review_set", "reference_match"})
BAKE_MODE = "ai_source_bake"
MAX_RECORDS = 64
MAX_FAILURE_SNAPSHOTS = 3
MAX_ACTIVE_JOBS = 1
_RUNNER = Path(__file__).with_name("background_render_runner.py")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _safe_remove(directory: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved = directory.resolve()
    if resolved.parent != resolved_root:
        raise RuntimeError("Background job cleanup path escaped its temp root")
    if resolved.exists():
        shutil.rmtree(resolved)


def _output_options(mode: str, options: dict[str, Any], job_id: str, source_dir: Path) -> dict[str, Any]:
    resolved = dict(options)
    if mode in {"preview", "detail"}:
        resolved.setdefault("output_path", str(source_dir / "previews" / f"background_{job_id}.png"))
    elif mode in {"ortho_set", "review_set", "reference_match"}:
        resolved.setdefault("output_prefix", str(source_dir / "previews" / f"background_{job_id}"))
    for key in ("output_path", "output_dir", "output_prefix", "reference_path"):
        value = resolved.get(key)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise InvalidParamsError(f"options.{key} must be a non-empty path")
            path = Path(value).expanduser()
            resolved[key] = str((source_dir / path).resolve() if not path.is_absolute() else path.resolve())
    return resolved


@dataclass
class _Job:
    operation_id: str
    mode: str
    options: dict[str, Any]
    engine: str | None
    samples: int | None
    created_at: str = field(default_factory=_now)
    last_updated_at: str = field(default_factory=_now)
    started_clock: float = field(default_factory=time.monotonic)
    finished_clock: float | None = None
    status: str = "queued"
    phase: str = "queued"
    directory: Path | None = None
    process: subprocess.Popen | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    dirty_at_snapshot: bool | None = None
    snapshot_state_token: str | None = None
    snapshot_depsgraph_epoch: int | None = None
    snapshot_stale: bool = False
    cancel_requested: bool = False
    cancelled_at: float | None = None
    warnings: list[str] = field(default_factory=list)
    done_event: threading.Event = field(default_factory=threading.Event, repr=False)


class BackgroundRenderManager:
    """Main-thread snapshot scheduler with thread-safe status and cancellation."""

    def __init__(self, *, temp_root: Path | None = None, max_active: int = MAX_ACTIVE_JOBS,
                 popen: Any = subprocess.Popen) -> None:
        self.root = temp_root or Path(tempfile.gettempdir()) / "codex_blender_background"
        self.max_active = max_active
        self._popen = popen
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._queue: deque[str] = deque()
        self._failed_dirs: deque[Path] = deque()
        self._lock = threading.RLock()
        self._timer_scheduled = False
        self._depsgraph_epoch = 0
        self._depsgraph_handler: Any = None

    def start(self, params: dict[str, Any]) -> ToolResult:
        unknown = set(params) - {"mode", "options", "engine", "samples"}
        if unknown:
            raise InvalidParamsError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        mode = params.get("mode", "preview")
        options = params.get("options", {})
        engine = params.get("engine")
        samples = params.get("samples")
        if not isinstance(mode, str) or mode not in MODES | {BAKE_MODE} or not isinstance(options, dict):
            raise InvalidParamsError("mode must be a supported render mode and options must be an object")
        if mode == BAKE_MODE and (engine is not None or samples is not None):
            raise InvalidParamsError("AI-source bake uses its own CPU Cycles settings")
        if engine is not None and (not isinstance(engine, str) or engine not in {"CYCLES", "BLENDER_EEVEE_NEXT"}):
            raise InvalidParamsError("engine must be CYCLES or BLENDER_EEVEE_NEXT")
        if samples is not None and (isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 100_000):
            raise InvalidParamsError("samples must be an integer from 1 to 100000")
        from .render import preflight_render_options

        bpy = get_bpy()
        original_path = str(bpy.data.filepath or "")
        source_dir = Path(original_path).resolve().parent if original_path else Path.cwd().resolve()
        operation_id = str(uuid4())
        resolved_options = _output_options(mode, options, operation_id, source_dir)
        if mode == BAKE_MODE:
            from .ai_source_bake import _options as bake_options

            bake_options(resolved_options)
            normalized_options = resolved_options
        else:
            normalized_options = preflight_render_options(mode, resolved_options, bpy)
        job = _Job(operation_id, mode, normalized_options, engine, samples)
        with self._lock:
            self._prune_records_locked()
            self._jobs[operation_id] = job
            self._queue.append(operation_id)
            position = len(self._queue)
        try:
            self._arm_timer()
        except Exception:
            with self._lock:
                self._queue.remove(operation_id)
                self._jobs.pop(operation_id, None)
            raise
        return ToolResult(data={"operation_id": operation_id, "status": "queued",
                                "queue_position": position, "mode": mode})

    def _arm_timer(self) -> None:
        with self._lock:
            if self._timer_scheduled:
                return
            self._timer_scheduled = True
        try:
            get_bpy().app.timers.register(self._timer, first_interval=0.01)
        except Exception:
            with self._lock:
                self._timer_scheduled = False
            raise

    def _timer(self) -> float | None:
        self.pump()
        with self._lock:
            active = bool(self._queue or any(job.status == "running" for job in self._jobs.values()))
            self._timer_scheduled = active
        if not active:
            self._remove_change_handler()
        return 0.2 if active else None

    def _ensure_change_handler(self, bpy: Any) -> None:
        handlers = getattr(getattr(bpy.app, "handlers", None), "depsgraph_update_post", None)
        if handlers is None or self._depsgraph_handler is not None:
            return

        def observed_change(*_: Any) -> None:
            with self._lock:
                self._depsgraph_epoch += 1

        handlers.append(observed_change)
        self._depsgraph_handler = observed_change

    def _remove_change_handler(self) -> None:
        callback = self._depsgraph_handler
        if callback is None:
            return
        handlers = getattr(getattr(get_bpy().app, "handlers", None), "depsgraph_update_post", None)
        if handlers is not None and callback in handlers:
            handlers.remove(callback)
        self._depsgraph_handler = None

    def pump(self) -> None:
        """Advance jobs from Blender's main thread; headless tests may call it explicitly."""
        with self._lock:
            running = [job for job in self._jobs.values() if job.status == "running"]
        for job in running:
            if job.snapshot_state_token:
                try:
                    job.snapshot_stale = (
                        current_state_token(get_bpy()) != job.snapshot_state_token
                        or (job.snapshot_depsgraph_epoch is not None and
                            self._depsgraph_epoch != job.snapshot_depsgraph_epoch)
                    )
                except Exception:
                    pass
            if job.process is not None:
                code = job.process.poll()
                if code is None and job.cancel_requested and job.cancelled_at is not None and time.monotonic() - job.cancelled_at > 2:
                    job.process.kill()
                    code = job.process.poll()
                if code is not None:
                    self._finish(job, code)
        with self._lock:
            active_count = sum(job.status == "running" for job in self._jobs.values())
            if active_count >= self.max_active or not self._queue:
                return
            operation_id = self._queue.popleft()
            job = self._jobs[operation_id]
            if job.status != "queued":
                return
            job.status = "running"
            job.phase = "snapshot"
            job.last_updated_at = _now()
        try:
            self._snapshot_and_launch(job)
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            self._finish(job, None)

    def _snapshot_and_launch(self, job: _Job) -> None:
        bpy = get_bpy()
        original_path = str(bpy.data.filepath or "")
        source_dir = Path(original_path).resolve().parent if original_path else Path.cwd().resolve()
        job.dirty_at_snapshot = bool(bpy.data.is_dirty)
        job.snapshot_state_token = current_state_token(bpy)
        job.directory = Path(tempfile.mkdtemp(prefix="job-", dir=self._ensure_root()))
        snapshot = job.directory / "snapshot.blend"
        outcome = bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True, check_existing=False)
        if "FINISHED" not in outcome or not snapshot.is_file():
            raise RuntimeError("Blender did not confirm the background snapshot")
        if str(bpy.data.filepath or "") != original_path:
            raise RuntimeError("Snapshot changed the active Blender document")
        self._ensure_change_handler(bpy)
        job.snapshot_depsgraph_epoch = self._depsgraph_epoch
        if job.cancel_requested:
            self._finish(job, None)
            return
        options = _output_options(job.mode, job.options, job.operation_id, source_dir)
        payload = {"operation_id": job.operation_id, "mode": job.mode, "options": options,
                   "engine": job.engine, "samples": job.samples, "source_dir": str(source_dir),
                   "unsaved_source": not bool(original_path), "snapshot_dirty": job.dirty_at_snapshot,
                   "progress_path": str(job.directory / "progress.json"),
                   "result_path": str(job.directory / "result.json")}
        job_file = job.directory / "job.json"
        _write_json(job_file, payload)
        binary = str(bpy.app.binary_path or "")
        if not binary or not Path(binary).is_file():
            raise RuntimeError("Blender binary path is unavailable for background render")
        command = [binary]
        if job.mode == BAKE_MODE:
            # The user's enabled add-on may be an older installation of this
            # package. Load the runner from this checkout without its startup
            # modules shadowing the bake implementation.
            command.append("--factory-startup")
        command.extend(["-b", str(snapshot), "--python-exit-code", "1",
                        "--python", str(_RUNNER), "--", "--job", str(job_file)])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with (job.directory / "stdout.log").open("wb") as stdout, (job.directory / "stderr.log").open("wb") as stderr:
            job.process = self._popen(command, stdout=stdout, stderr=stderr, creationflags=flags)
        job.phase = "baking" if job.mode == BAKE_MODE else "rendering"
        job.last_updated_at = _now()
        if job.dirty_at_snapshot:
            job.warnings.append("Processed a copy that included unsaved document changes")
        if not original_path:
            job.warnings.append("Original document was never saved; relative external paths use Blender's working directory")

    def _ensure_root(self) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        return str(self.root)

    def _finish(self, job: _Job, return_code: int | None) -> None:
        if job.status in {"succeeded", "failed", "cancelled"}:
            return
        result_path = job.directory / "result.json" if job.directory else None
        child_result = _read_json(result_path) if result_path else None
        job.result = child_result
        if job.cancel_requested:
            job.status = "cancelled"
        elif return_code == 0 and child_result and child_result.get("ok") is True:
            job.status = "succeeded"
        else:
            job.status = "failed"
            if job.error is None:
                error = child_result.get("error") if isinstance(child_result, dict) else None
                job.error = str(error or f"Background Blender exited with code {return_code}; inspect retained logs")[:500]
        job.phase = "done"
        job.last_updated_at = _now()
        job.finished_clock = time.monotonic()
        if job.directory:
            if job.status == "failed":
                self._failed_dirs.append(job.directory)
                while len(self._failed_dirs) > MAX_FAILURE_SNAPSHOTS:
                    try:
                        _safe_remove(self._failed_dirs.popleft(), self.root)
                    except OSError as exc:
                        job.warnings.append(f"Could not prune an old diagnostic snapshot: {exc}")
            else:
                try:
                    _safe_remove(job.directory, self.root)
                    job.directory = None
                except OSError as exc:
                    job.warnings.append(f"Could not remove temporary snapshot: {exc}")
        job.done_event.set()

    def status(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(operation_id)
            if job is None:
                raise NotFoundError("Background operation ID is unknown or expired")
            queue_position = list(self._queue).index(operation_id) + 1 if operation_id in self._queue else None
            data: dict[str, Any] = {"operation_id": operation_id, "status": job.status,
                "phase": job.phase, "elapsed_ms": round(((job.finished_clock or time.monotonic()) - job.started_clock) * 1000),
                "last_updated_at": job.last_updated_at,
                "queue_position": queue_position, "snapshot_dirty": job.dirty_at_snapshot,
                "snapshot_stale": job.snapshot_stale, "cancel_requested": job.cancel_requested,
                "warnings": list(job.warnings)}
            if job.directory and job.status == "failed":
                data["diagnostics_dir"] = str(job.directory)
            if job.error:
                data["error"] = job.error
            if job.process is not None and job.status == "running":
                data["child_pid"] = job.process.pid
            if job.result is not None:
                data["result"] = job.result
                output = job.result.get("data") if isinstance(job.result.get("data"), dict) else {}
                paths = [output[key] for key in ("path", "render_path", "side_by_side_path", "overlay_path")
                         if isinstance(output.get(key), str)]
                if isinstance(output.get("paths"), dict):
                    paths.extend(path for path in output["paths"].values() if isinstance(path, str))
                manifest = output.get("manifest")
                if isinstance(manifest, dict):
                    paths.extend(entry["path"] for entry in manifest.values()
                                 if isinstance(entry, dict) and isinstance(entry.get("path"), str))
                if paths:
                    data["output_paths"] = paths
            progress_path = job.directory / "progress.json" if job.directory and job.status == "running" else None
        progress = _read_json(progress_path) if progress_path else None
        if progress:
            data["progress"] = progress
            if isinstance(progress.get("phase"), str) and data["status"] == "running":
                data["phase"] = progress["phase"]
            if isinstance(progress.get("updated_at"), str):
                data["last_updated_at"] = progress["updated_at"]
        if data["snapshot_stale"]:
            data["warnings"].append("Live document changed after the background snapshot; this render reflects the snapshot")
        return data

    def wait_status(self, operation_id: str, wait_ms: int = 0) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(operation_id)
            if job is None:
                raise NotFoundError("Background operation ID is unknown or expired")
            done = job.done_event
        if wait_ms:
            done.wait(wait_ms / 1000)
        return self.status(operation_id)

    def cancel(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(operation_id)
            if job is None:
                raise NotFoundError("Background operation ID is unknown or expired")
            if job.status == "queued":
                self._queue.remove(operation_id)
                job.cancel_requested = True
                job.status = "cancelled"
                job.phase = "done"
                job.last_updated_at = _now()
                job.finished_clock = time.monotonic()
                job.done_event.set()
            elif job.status == "running":
                job.cancel_requested = True
                job.cancelled_at = time.monotonic()
                if job.process is not None and job.process.poll() is None:
                    try:
                        job.process.terminate()
                    except ProcessLookupError:
                        pass
        return self.status(operation_id)

    def _prune_records_locked(self) -> None:
        while len(self._jobs) >= MAX_RECORDS:
            finished = next((key for key, item in self._jobs.items()
                             if item.status in {"succeeded", "failed", "cancelled"}), None)
            if finished is None:
                raise RuntimeError("Background operation registry is full")
            self._jobs.pop(finished)

    def shutdown(self) -> None:
        try:
            with self._lock:
                ids = [key for key, job in self._jobs.items() if job.status in {"queued", "running"}]
            for operation_id in ids:
                try:
                    self.cancel(operation_id)
                except Exception as exc:
                    self._jobs[operation_id].warnings.append(f"Could not request child cancellation: {exc}")
            with self._lock:
                running = [job for job in self._jobs.values() if job.status == "running" and job.process is not None]
            for job in running:
                process = job.process
                assert process is not None
                code = None
                try:
                    try:
                        code = process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        code = process.wait(timeout=1)
                except Exception as exc:
                    job.warnings.append(f"Could not confirm child exit during shutdown: {exc}")
                if code is None:
                    try:
                        code = process.poll()
                    except Exception as exc:
                        job.warnings.append(f"Could not poll child after shutdown: {exc}")
                if code is None:
                    job.warnings.append("Background child exit is unconfirmed; snapshot retained")
                    continue
                try:
                    self._finish(job, code)
                except Exception as exc:
                    job.warnings.append(f"Could not finalize background job after child exit: {exc}")
        finally:
            self._remove_change_handler()


_manager = BackgroundRenderManager()


def start_background_render(params: dict[str, Any]) -> ToolResult:
    if params.get("mode") == BAKE_MODE:
        raise InvalidParamsError("Use bake.from_ai_source for a background AI-source bake")
    return _manager.start(params)


def background_render_status(params: dict[str, Any]) -> ToolResult:
    if set(params) != {"operation_id"} or not isinstance(params["operation_id"], str) or not params["operation_id"]:
        raise InvalidParamsError("operation_id must be a non-empty string")
    return ToolResult(data=_manager.status(params["operation_id"]))


def lookup_background_status(operation_id: str, wait_ms: int = 0) -> dict[str, Any] | None:
    """Bridge-thread-safe lookup; never touches bpy or schedules Blender work."""
    try:
        return _manager.wait_status(operation_id, wait_ms)
    except NotFoundError:
        return None


def cancel_background_render(params: dict[str, Any]) -> ToolResult:
    if set(params) != {"operation_id"} or not isinstance(params["operation_id"], str) or not params["operation_id"]:
        raise InvalidParamsError("operation_id must be a non-empty string")
    return ToolResult(data=_manager.cancel(params["operation_id"]))


def pump_background_jobs() -> None:
    _manager.pump()


def shutdown_background_jobs() -> None:
    _manager.shutdown()
