"""Real .blend switch qualification for document-local helper sources/cache.

Run: blender -b --factory-startup --python-exit-code 1 --python tests/run_python_modules_file_switch_headless.py
"""
from pathlib import Path
import sys
import tempfile

import bpy

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
from blenderboo import dispatch


def call(tool, params):
    result = dispatch(tool, params)
    assert result["ok"], result
    return result["data"]


def value(expected):
    for code in ("import cbt_modules.helper; result = cbt_modules.helper.VALUE",
                 "from cbt_modules import helper; result = helper.VALUE"):
        receipt = call("python.exec", {"code": code})
        assert receipt["result"] == expected, receipt
        assert set(receipt["modules"]) == {"helper"}, receipt


bpy.ops.wm.read_factory_settings(use_empty=True)
with tempfile.TemporaryDirectory(prefix="cbt_modules_switch_") as temporary:
    root = Path(temporary)
    file_a, file_b = root / "a.blend", root / "b.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(file_b))
    source = root / "helper.py"
    source.write_text("VALUE = 11\n", encoding="utf-8")
    first = call("python.module_put", {"name": "helper", "path": str(source)})
    value(11)
    bpy.ops.wm.save_as_mainfile(filepath=str(file_a))
    source.write_text("VALUE = 99\n", encoding="utf-8")
    bpy.ops.wm.open_mainfile(filepath=str(file_b), load_ui=False)
    assert call("python.module_list", {})["count"] == 0
    for code in ("import cbt_modules.helper", "from cbt_modules import helper"):
        missing = dispatch("python.exec", {"code": code})
        assert not missing["ok"], missing
        assert "cbt_modules.helper" not in sys.modules
    second = call("python.module_put", {"name": "helper", "code": "VALUE = 22\n"})
    assert second["sha256"] != first["sha256"]
    value(22)
    bpy.ops.wm.save_as_mainfile(filepath=str(file_b))
    for blend, expected, digest in ((file_a, 11, first["sha256"]), (file_b, 22, second["sha256"]),
                                    (file_a, 11, first["sha256"])):
        bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
        listed = call("python.module_list", {})
        assert listed["count"] == 1 and listed["modules"][0]["sha256"] == digest, listed
        value(expected)
        print("PASS document-local helper", blend.name, expected)

print("PYTHON_MODULES_FILE_SWITCH_HEADLESS_OK")
