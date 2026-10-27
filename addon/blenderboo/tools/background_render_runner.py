"""Blender child-process entry point for one background render or bake job."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
import time
from typing import Any


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _absolute_external_paths(bpy: Any, source_dir: Path) -> list[str]:
    """Resolve paths originally relative to the live file, not the temp copy."""
    warnings: list[str] = []
    for collection_name in ("images", "movieclips", "sounds", "fonts", "libraries", "cache_files", "volumes"):
        collection = getattr(bpy.data, collection_name, ())
        for item in collection:
            raw = getattr(item, "filepath", None)
            if not isinstance(raw, str) or not raw.startswith("//"):
                continue
            absolute = str((source_dir / raw[2:]).resolve())
            try:
                item.filepath = absolute
                if collection_name == "images" and hasattr(item, "reload"):
                    item.reload()
            except (AttributeError, TypeError, RuntimeError) as exc:
                warnings.append(f"Could not resolve {collection_name} path {getattr(item, 'name', '')}: {exc}")
    return warnings


def _job_path(argv: list[str]) -> Path:
    args = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
    if len(args) != 2 or args[0] != "--job":
        raise ValueError("Expected -- --job <job.json>")
    path = Path(args[1]).resolve()
    if not path.is_file():
        raise ValueError("Background render job file does not exist")
    return path


def run(job_path: Path, bpy: Any) -> dict[str, Any]:
    from blenderboo.tools import render

    job = json.loads(job_path.read_text(encoding="utf-8"))
    if not isinstance(job, dict) or job.get("mode") not in {
        "preview", "detail", "ortho_set", "review_set", "reference_match", "ai_source_bake"
    } or not isinstance(job.get("options"), dict):
        raise ValueError("Invalid background render job")
    progress_path = Path(job["progress_path"])
    started = time.monotonic()
    started_cpu = time.process_time()
    progress_lock = threading.Lock()
    heartbeat_stop = threading.Event()
    latest_progress: dict[str, Any] = {}
    sequence = 0

    def progress(value: dict[str, Any]) -> None:
        nonlocal latest_progress, sequence
        with progress_lock:
            latest_progress = dict(value)
            sequence += 1
            payload = {**latest_progress,
                       "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "sequence": sequence,
                       "child_wall_s": round(time.monotonic() - started, 3),
                       "child_cpu_s": round(time.process_time() - started_cpu, 3)}
            try:
                _write(progress_path, payload)
            except OSError:
                pass

    def heartbeat() -> None:
        while not heartbeat_stop.wait(5):
            with progress_lock:
                current = dict(latest_progress)
            progress(current)

    progress({"phase": "preparing", "frame": int(bpy.context.scene.frame_current)})
    heartbeat_thread = threading.Thread(target=heartbeat, name="blenderboo-progress", daemon=True)
    warnings = _absolute_external_paths(bpy, Path(job["source_dir"]))
    engine = job.get("engine")
    if engine:
        bpy.context.scene.render.engine = engine
    samples = job.get("samples")
    if samples is not None:
        if bpy.context.scene.render.engine != "CYCLES":
            warnings.append("samples override applies only to Cycles; saved scene setting was used")
        else:
            bpy.context.scene.cycles.samples = int(samples)
    callbacks: list[tuple[Any, Any]] = []

    def on_render_init(scene: Any, *_: Any) -> None:
        progress({"phase": "rendering", "frame": int(getattr(scene, "frame_current", 0))})

    def on_render_stats(scene: Any, stats: Any = None, *_: Any) -> None:
        detail = str(stats or "")[:200]
        progress({"phase": "rendering", "frame": int(getattr(scene, "frame_current", 0)), "stats": detail})

    if job["mode"] != "ai_source_bake":
        for name, callback in (("render_init", on_render_init), ("render_stats", on_render_stats)):
            handlers = getattr(bpy.app.handlers, name, None)
            if handlers is not None:
                handlers.append(callback)
                callbacks.append((handlers, callback))
    heartbeat_thread.start()
    try:
        from blenderboo.tools.ai_source_bake import bake_from_ai_source

        handler = {
            "preview": render.render_preview,
            "detail": render.render_detail,
            "ortho_set": render.render_ortho_set,
            "review_set": render.render_review_set,
            "reference_match": render.render_reference_match,
            "ai_source_bake": bake_from_ai_source,
        }[job["mode"]]
        result = handler(job["options"], progress=progress) if job["mode"] == "ai_source_bake" else handler(job["options"])
        if not hasattr(result, "data"):
            raise RuntimeError("Background render handler did not return a completed result")
        warnings.extend(result.warnings)
        envelope = {"ok": True, "operation_id": job["operation_id"], "mode": job["mode"],
                    "data": result.data, "warnings": warnings,
                    "timing": {"child_wall_s": round(time.monotonic() - started, 3),
                               "child_cpu_s": round(time.process_time() - started_cpu, 3)}}
        progress({"phase": "completed", "frame": int(bpy.context.scene.frame_current)})
        return envelope
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
        for handlers, callback in callbacks:
            if callback in handlers:
                handlers.remove(callback)


def main(argv: list[str] | None = None) -> None:
    package_parent = Path(__file__).resolve().parents[2]
    if str(package_parent) not in sys.path:
        sys.path.insert(0, str(package_parent))
    import bpy

    job_path = _job_path(sys.argv if argv is None else argv)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    result_path = Path(job["result_path"])
    try:
        result = run(job_path, bpy)
    except Exception as exc:
        result = {"ok": False, "operation_id": job.get("operation_id"),
                  "error": {"type": type(exc).__name__, "message": str(exc)}}
        _write(Path(job["progress_path"]), {"phase": "failed", "error": str(exc)[:500]})
        _write(result_path, result)
        raise
    _write(result_path, result)


if __name__ == "__main__":
    main()
