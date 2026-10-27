"""Run detailed exec on a disposable copy of the preserved orphanage checkpoint."""

from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo import dispatch


def run():
    copy_path = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
    assert copy_path.is_file() and copy_path.stat().st_size == 401_860_914, copy_path
    assert copy_path.name != "orphanage_pre-game-realism_2026-09-26_013200.blend", copy_path
    bpy.ops.wm.open_mainfile(filepath=str(copy_path), load_ui=False)
    result = dispatch("python.exec", {"code": "result = len(D.objects)", "diff": "detailed"})
    assert result["ok"], result
    print("ORPHANAGE_REPRO_RESULT=" + str({
        "object_count": result["data"]["result"],
        "changed": result["data"]["changed"],
        "detection": result["data"]["detection"],
        "timings_ms": result["meta"]["timings_ms"],
    }))


if __name__ == "__main__":
    run()
