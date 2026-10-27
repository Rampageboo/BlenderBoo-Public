"""Isolated Blender GUI qualification for two concurrent bridge render clients."""

import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))

from blenderboo.bridge import start_bridge, stop_bridge
from blenderboo_mcp.client import BridgeClient


OUTPUT = ROOT / "tests" / ".tmp_gui_render_smoke"
OUTPUT.mkdir(exist_ok=True)
RESULT = OUTPUT / "result.json"
START = time.monotonic()
results = {}
errors = {}
threads = []


def finish(payload):
    RESULT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        stop_bridge()
    except Exception as exc:
        print(f"Bridge cleanup: {type(exc).__name__}: {exc}", flush=True)
    bpy.ops.wm.quit_blender()
    return None


try:
    assert not bpy.app.background, "Expected Blender GUI process"
    assert bpy.context.window is not None, "Expected active Blender window"
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.data.objects["Cube"]
    cube.name = "IsolatedRenderCube"
    initial_camera = scene.camera
    initial_visibility = {obj.name: obj.hide_render for obj in scene.objects}
    initial_resolution = (scene.render.resolution_x, scene.render.resolution_y,
                          scene.render.resolution_percentage)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = secrets.token_hex(32)
    start_bridge(token, port=port, pending_timeout=30.0)
    barrier = threading.Barrier(3)

    def request(label, tool, params):
        try:
            client = BridgeClient(token, port=port, client_id=f"gui-smoke-{label}")
            barrier.wait(timeout=10)
            results[label] = client.call(tool, params)
        except Exception as exc:
            errors[label] = f"{type(exc).__name__}: {exc}"

    detail = {"object_name": cube.name, "direction": "front", "projection": "orthographic",
              "isolate": True, "width": 96, "height": 64,
              "output_path": str(OUTPUT / "detail.png")}
    ortho = {"object_names": [cube.name], "shared_scale": True,
             "width": 64, "height": 64, "output_prefix": str(OUTPUT / "ortho")}
    for label, tool, params in (("detail", "render.detail", detail),
                                ("ortho", "render.ortho_set", ortho)):
        thread = threading.Thread(target=request, args=(label, tool, params), daemon=True)
        thread.start()
        threads.append(thread)
    barrier.wait(timeout=10)

    def check():
        if time.monotonic() - START > 120:
            return finish({"ok": False, "error": "timeout", "results": results,
                           "client_errors": errors})
        if any(thread.is_alive() for thread in threads):
            return 0.2
        try:
            assert not errors, errors
            assert set(results) == {"detail", "ortho"}, results
            assert all(result["ok"] for result in results.values()), results
            detail_path = Path(results["detail"]["data"]["path"])
            manifest = results["ortho"]["data"]["manifest"]
            assert detail_path.is_file() and detail_path.stat().st_size > 0
            assert set(manifest) == {"front", "back", "left", "right", "top", "bottom"}
            assert all(Path(entry["path"]).is_file() and Path(entry["path"]).stat().st_size > 0
                       for entry in manifest.values())
            assert scene.camera is initial_camera
            assert {obj.name: obj.hide_render for obj in scene.objects} == initial_visibility
            assert (scene.render.resolution_x, scene.render.resolution_y,
                    scene.render.resolution_percentage) == initial_resolution
            assert all(not obj.name.startswith("CodexDetailCamera") for obj in bpy.data.objects)
            print("PASS GUI deferred two-client detail + ortho_set, seven PNGs, state restored", flush=True)
            return finish({"ok": True, "tools": list(results), "png_count": 7,
                           "elapsed_seconds": round(time.monotonic() - START, 3),
                           "paths": [str(detail_path)] + [entry["path"] for entry in manifest.values()]})
        except Exception as exc:
            traceback.print_exc()
            return finish({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                           "results": results, "client_errors": errors})

    bpy.app.timers.register(check, first_interval=0.2)
except Exception as exc:
    traceback.print_exc()
    failure = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    bpy.app.timers.register(lambda: finish(failure), first_interval=0.1)
