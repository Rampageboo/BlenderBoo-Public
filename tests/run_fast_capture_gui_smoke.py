"""Run in an isolated Blender 5.2 GUI: screenshot baseline and fast capture latency."""

import json
import os
from pathlib import Path
import secrets
import socket
import statistics
import sys
import threading
import time
import tempfile
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))

from blenderboo.bridge import start_bridge, stop_bridge
from blenderboo_mcp.client import BridgeClient


OUTPUT = Path(os.environ.get("CBT_GUI_CAPTURE_OUTPUT") or tempfile.mkdtemp(prefix="cbt_gui_fast_capture_"))
OUTPUT.mkdir(exist_ok=True)
RESULT = OUTPUT / "result.json"
started = time.monotonic()
result = None
failure = None


def finish(payload):
    RESULT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("FAST_CAPTURE_GUI " + json.dumps(payload), flush=True)
    try:
        stop_bridge()
    finally:
        bpy.ops.wm.quit_blender()
    return None


try:
    assert not bpy.app.background and bpy.context.window is not None
    # The verifier launches an isolated process. Resetting factory settings
    # here detaches the executing window before GPU draw.
    cube = bpy.data.objects.get("Cube")
    if cube is None:
        bpy.ops.mesh.primitive_cube_add()
        cube = bpy.data.objects["Cube"]
    cube.name = "FastCaptureFixture"
    screens = list(bpy.data.screens)
    area = next(area for screen in screens for area in screen.areas if area.type == "VIEW_3D")
    space = area.spaces.active
    original = (space.shading.type, space.overlay.show_overlays,
                space.overlay.show_wireframes, space.region_3d.view_matrix.copy(),
                bpy.context.scene.render.engine, bpy.context.view_layer.material_override)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = secrets.token_hex(32)
    start_bridge(token, port=port, pending_timeout=30.0)

    def measure():
        global result, failure
        try:
            client = BridgeClient(token, port=port, client_id="fast-capture-benchmark")
            timings = {"screenshot_512": [], "screenshot_1024": [],
                       "fast_512": [], "fast_1024": []}
            phases = {key: {"draw": [], "png_write": [], "total": []}
                      for key in timings}
            for label, tool, dimensions, count in (
                ("screenshot_512", "scene.screenshot", 512, 3),
                ("screenshot_1024", "scene.screenshot", 1024, 3),
                ("fast_512", "scene.fast_capture", 512, 5),
                ("fast_1024", "scene.fast_capture", 1024, 3),
            ):
                for index in range(count):
                    path = OUTPUT / f"{label}_{index}.png"
                    params = {"width": dimensions, "height": dimensions,
                              "output_path": str(path), "overwrite": True}
                    if tool == "scene.fast_capture":
                        params.update(object_name=cube.name, views=["three_quarter"],
                                      shading="solid", overlays=False)
                    begin = time.perf_counter()
                    response = client.call(tool, params)
                    timings[label].append(round((time.perf_counter() - begin) * 1000, 3))
                    assert response["ok"], response
                    assert path.is_file() and path.stat().st_size > 0
                    for phase, value in response["data"]["timings_ms"].items():
                        phases[label][phase].append(value)

            sheet_path = OUTPUT / "three_view_sheet.png"
            sheet = client.call("scene.fast_capture", {
                "object_name": cube.name,
                "views": ["front", "top", "three_quarter"],
                "projection": "orthographic", "width": 128, "height": 128,
                "columns": 3, "shading": "solid", "overlays": False,
                "wireframe": True, "output_path": str(sheet_path), "overwrite": True,
            })
            assert sheet["ok"], sheet
            assert sheet["data"]["width"] == 384 and sheet["data"]["height"] == 146
            assert sheet_path.is_file() and sheet_path.stat().st_size > 0

            form_modes = {}
            for mode in ("lit", "normal", "matcap", "curvature", "depth", "wireframe_on_shaded"):
                path = OUTPUT / f"form_{mode}.png"
                response = client.call("scene.fast_capture", {
                    "object_name": cube.name, "views": ["three_quarter"],
                    "width": 192, "height": 128, "view_mode": mode,
                    "output_path": str(path), "overwrite": True,
                })
                assert response["ok"], (mode, response)
                assert response["data"]["view_mode"] == mode
                assert response["data"]["width"] == 192
                assert response["data"]["height"] == 128
                assert path.is_file() and path.stat().st_size > 0
                if mode == "depth":
                    depth = response["data"]["depth_range"]
                    assert depth["far"] > depth["near"] > 0
                    assert depth["units"] == "Blender units"
                form_modes[mode] = str(path)

            barrier = threading.Barrier(3)
            concurrent = {}

            def call_parallel(name):
                other_client = BridgeClient(token, port=port, client_id=f"fast-capture-{name}")
                barrier.wait(timeout=10)
                concurrent[name] = other_client.call("scene.fast_capture", {
                    "object_name": cube.name, "width": 128, "height": 128,
                    "output_path": str(OUTPUT / f"parallel_{name}.png"),
                    "overwrite": True,
                })

            workers = [threading.Thread(target=call_parallel, args=(name,), daemon=True)
                       for name in ("a", "b")]
            for worker in workers:
                worker.start()
            barrier.wait(timeout=10)
            for worker in workers:
                worker.join(timeout=40)
                assert not worker.is_alive(), "Concurrent capture timed out"
            assert len(concurrent) == 2
            assert any(item["ok"] for item in concurrent.values()), concurrent
            assert all(item["ok"] or item["error"]["type"] == "InvalidStateError"
                       for item in concurrent.values()), concurrent

            result = {
                "ok": True,
                "samples_ms": timings,
                "p50_ms": {key: round(statistics.median(values), 3)
                           for key, values in timings.items()},
                "phase_p50_ms": {
                    key: {phase: round(statistics.median(values), 3)
                          for phase, values in values_by_phase.items()}
                    for key, values_by_phase in phases.items()
                },
                "target_500ms_met": statistics.median(timings["fast_512"]) <= 500,
                "sheet": str(sheet_path),
                "form_modes": form_modes,
                "concurrent": {key: {"ok": value["ok"],
                                      "error_type": value.get("error", {}).get("type") if value.get("error") else None}
                               for key, value in concurrent.items()},
            }
        except Exception as exc:
            traceback.print_exc()
            failure = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=measure, daemon=True)
    worker.start()

    def check():
        if time.monotonic() - started > 180:
            return finish({"ok": False, "error": "timeout"})
        if worker.is_alive():
            return 0.1
        same_state = (
            space.shading.type == original[0]
            and space.overlay.show_overlays == original[1]
            and space.overlay.show_wireframes == original[2]
            and space.region_3d.view_matrix == original[3]
            and bpy.context.scene.render.engine == original[4]
            and bpy.context.view_layer.material_override is original[5]
        )
        payload = result if result is not None else {"ok": False, "error": failure}
        payload["viewport_restored"] = same_state
        payload["ok"] = bool(payload["ok"] and same_state)
        return finish(payload)

    bpy.app.timers.register(check, first_interval=0.1)
except Exception as exc:
    traceback.print_exc()
    failure_message = f"{type(exc).__name__}: {exc}"
    bpy.app.timers.register(
        lambda: finish({"ok": False, "error": failure_message}),
        first_interval=0.1,
    )
