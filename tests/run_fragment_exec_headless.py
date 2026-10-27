"""Exercise bounded new-island evidence in python.exec on one named mesh."""

from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo import dispatch


def run() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh = bpy.data.meshes.new("FragmentFixture")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new("Part", mesh)
    bpy.context.scene.collection.objects.link(obj)

    clean = dispatch("python.exec", {
        "code": "D.objects['Part'].data.vertices[0].co.x = 0.1",
        "fragment_small_ratio": 0.5,
    })
    assert clean["ok"], clean
    assert not any(item.get("reason") == "new_islands" for item in clean["data"]["suspicious_change"]), clean

    sliver = dispatch("python.exec", {
        "code": (
            "mesh = D.objects['Part'].data\n"
            "mesh.clear_geometry()\n"
            "mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0),"
            "(4,0,0),(4.1,0,0),(4,0.1,0)], [], [(0,1,2),(3,4,5)])\n"
            "mesh.update()"
        ),
        "fragment_small_ratio": 0.5,
    })
    assert sliver["ok"], sliver
    findings = [item for item in sliver["data"]["suspicious_change"] if item.get("reason") == "new_islands"]
    assert len(findings) == 1 and findings[0]["increase"] == 1, sliver
    assert findings[0]["small_islands"] and findings[0]["small_islands"][0]["focus"], sliver
    assert sliver["data"]["fragment_check"]["checked_objects"] == ["Part"], sliver
    print("FRAGMENT_EXEC_OK")


if __name__ == "__main__":
    run()
