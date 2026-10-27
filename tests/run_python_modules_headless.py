"""Blender 5.2 persistence and import smoke for document-local exec modules."""

from pathlib import Path
import sys
import tempfile

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo import dispatch


def call(tool, params):
    outcome = dispatch(tool, params)
    assert outcome["ok"], outcome
    return outcome["data"]


def run():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "modules.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        first = call("python.module_put", {"name": "helper", "code": "VALUE = 11\n"})
        assert bpy.data.texts.get("cbt_module.helper") is not None
        assert call("python.module_list", {})["modules"][0]["sha256"] == first["sha256"]
        imported = call("python.exec", {"code": "import cbt_modules.helper; result = cbt_modules.helper.VALUE"})
        assert imported["result"] == 11 and imported["modules"] == {"helper": first["sha256"]}, imported
        cached = call("python.exec", {"code": "from cbt_modules import helper; result = helper.VALUE"})
        assert cached["result"] == 11 and cached["modules"] == {"helper": first["sha256"]}, cached
        failed_import_user_code = dispatch("python.exec", {"code": "import cbt_modules.helper; raise ValueError('expected')"})
        assert not failed_import_user_code["ok"] and failed_import_user_code["data"]["modules"] == {"helper": first["sha256"]}, failed_import_user_code
        second = call("python.module_put", {"name": "helper", "code": "VALUE = 22\n"})
        assert second["sha256"] != first["sha256"]
        assert bpy.data.texts[first["version_text_name"]].as_string() == "VALUE = 11\n"
        reloaded = call("python.exec", {"code": "import cbt_modules.helper; result = cbt_modules.helper.VALUE"})
        assert reloaded["result"] == 22 and reloaded["modules"] == {"helper": second["sha256"]}, reloaded
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
        reopened = call("python.exec", {"code": "import cbt_modules.helper; result = cbt_modules.helper.VALUE"})
        assert reopened["result"] == 22 and reopened["modules"] == {"helper": second["sha256"]}, reopened
        assert bpy.data.texts[first["version_text_name"]].as_string() == "VALUE = 11\n"
        assert bpy.data.texts[second["version_text_name"]].as_string() == "VALUE = 22\n"
        removed = call("python.module_remove", {"name": "helper"})
        assert removed["sha256"] == second["sha256"]
        assert bpy.data.texts.get(first["version_text_name"]) is not None
        failed = dispatch("python.exec", {"code": "import cbt_modules.helper"})
        assert not failed["ok"] and failed["error"]["type"] == "ModuleNotFoundError", failed
        assert "cbt_modules.helper" in failed["error"]["message"], failed
        helper_path = Path(directory) / "path_helper.py"
        helper_path.write_text("VALUE = 41\n", encoding="utf-8")
        imported_path = call("python.module_put", {"name": "path_helper", "path": str(helper_path)})
        helper_path.write_text("VALUE = 99\n", encoding="utf-8")
        retained = call("python.exec", {"code": "from cbt_modules import path_helper; result = path_helper.VALUE"})
        assert retained["result"] == 41 and retained["modules"] == {"path_helper": imported_path["sha256"]}, retained
    print("python modules headless smoke passed")


if __name__ == "__main__":
    run()
