"""Isolated Blender GUI bridge smoke for deferred ROM fast capture."""

import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))
from blenderboo.bridge import start_bridge, stop_bridge
from blenderboo_mcp.client import BridgeClient


output = Path(tempfile.mkdtemp(prefix="cbt_rom_gui_"))
result_path = output / "result.json"
started = time.monotonic()
result = None
failure = None


def finish(payload):
    result_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("ROM_GUI " + json.dumps(payload), flush=True)
    try:
        stop_bridge()
    finally:
        bpy.ops.wm.quit_blender()
    return None


try:
    assert not bpy.app.background and bpy.context.window is not None
    cube = bpy.data.objects.get("Cube")
    if cube is None:
        bpy.ops.mesh.primitive_cube_add()
        cube = bpy.context.object
    cube.name = "ROMGuiMesh"
    group = cube.vertex_groups.new(name="Tip")
    for vertex in cube.data.vertices:
        if vertex.co.z > 0:
            group.add([vertex.index], 1.0, "REPLACE")
    rig_data = bpy.data.armatures.new("ROMGuiRigData")
    rig = bpy.data.objects.new("ROMGuiRig", rig_data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.ops.object.select_all(action="DESELECT")
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    root_bone = rig_data.edit_bones.new("Root")
    root_bone.head, root_bone.tail = (0, 0, -1), (0, 0, 0)
    tip_bone = rig_data.edit_bones.new("Tip")
    tip_bone.head, tip_bone.tail = (0, 0, 0), (0, 0, 1)
    tip_bone.parent = root_bone
    bpy.ops.object.mode_set(mode="OBJECT")
    modifier = cube.modifiers.new("ROMGuiArmature", "ARMATURE")
    modifier.object = rig
    bpy.context.view_layer.update()
    baseline_pose = {bone.name: bone.matrix_basis.copy() for bone in rig.pose.bones}
    area = next(area for screen in bpy.data.screens for area in screen.areas if area.type == "VIEW_3D")
    space = area.spaces.active
    baseline_view = (space.shading.type, space.overlay.show_overlays,
                     space.overlay.show_wireframes, space.region_3d.view_matrix.copy())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = secrets.token_hex(32)
    start_bridge(token, port=port, pending_timeout=90.0)

    def call():
        global result, failure
        try:
            client = BridgeClient(token, port=port, client_id="rom-gui-smoke")
            result = client.call("rig.rom_test", {
                "object_name": cube.name, "armature_name": rig.name,
                "poses": [{"name": "bend", "bones": [
                    {"bone": "Tip", "axis": "X", "degrees": 35}]}],
                "output_prefix": str(output / "rom"), "width": 192, "height": 192,
                "view_mode": "matcap",
            })
        except Exception as exc:
            traceback.print_exc()
            failure = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=call, daemon=True)
    worker.start()

    def check():
        if time.monotonic() - started > 180:
            return finish({"ok": False, "error": "timeout", "evidence_dir": str(output)})
        if worker.is_alive():
            return .1
        restored = (
            all(rig.pose.bones[name].matrix_basis == matrix for name, matrix in baseline_pose.items())
            and space.shading.type == baseline_view[0]
            and space.overlay.show_overlays == baseline_view[1]
            and space.overlay.show_wireframes == baseline_view[2]
            and space.region_3d.view_matrix == baseline_view[3]
        )
        response = result if result is not None else {"ok": False, "error": failure}
        payload = {"ok": bool(response.get("ok") and restored),
                   "response_ok": bool(response.get("ok")), "restored": restored,
                   "error": response.get("error"), "evidence_dir": str(output)}
        if response.get("ok"):
            data = response["data"]
            payload.update(capture_backend=data.get("capture_backend"),
                           contact_sheet=data.get("contact_sheet"),
                           contact_sheet_exists=Path(data["contact_sheet"]).is_file(),
                           pose_image_exists=Path(data["pose_images"]["bend"]).is_file())
            payload["ok"] = bool(payload["ok"] and payload["capture_backend"] == "scene.fast_capture"
                                 and payload["contact_sheet_exists"] and payload["pose_image_exists"])
        return finish(payload)

    bpy.app.timers.register(check, first_interval=.1)
except Exception as exc:
    traceback.print_exc()
    finish({"ok": False, "error": f"{type(exc).__name__}: {exc}", "evidence_dir": str(output)})
