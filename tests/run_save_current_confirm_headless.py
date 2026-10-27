"""Disposable Blender save-current confirmation regression."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo import dispatch
from blenderboo.tools.versioning import observe_loaded_file

bpy.ops.wm.read_factory_settings(use_empty=True)
with tempfile.TemporaryDirectory(prefix="blenderboo_save_confirm_") as temp:
    path = Path(temp) / "scene.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    observe_loaded_file()
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    inspected = dispatch("scene.inspect", {})
    assert inspected["ok"], inspected
    expected_state = inspected["data"]["state_token"]
    plan = dispatch("scene.save_current", {})
    assert plan["ok"] and plan["data"]["dry_run"], plan
    saved = dispatch("scene.save_current", {"confirm": True, "expected_state": expected_state})
    assert saved["ok"] and saved["data"]["write_confirmed"], saved
    assert saved["data"]["dry_run"] is False, saved
    assert saved["data"]["after"]["filepath"] == str(path.resolve()), saved

print("SAVE_CURRENT_CONFIRM_HEADLESS_OK")
