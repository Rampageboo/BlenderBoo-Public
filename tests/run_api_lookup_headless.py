"""Qualify api.lookup against the Blender process actually executing this script."""

from pathlib import Path
import sys
import time

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo import dispatch
from blenderboo.tools.session_state import current_state_token


def lookup(**params):
    response = dispatch("api.lookup", params)
    assert response["ok"], response
    return response["data"]


def run():
    assert bpy.app.version[:2] == (5, 2), bpy.app.version_string
    for index in range(10_000):
        item = bpy.data.objects.new(f"ApiLookupLoad{index:05d}", None)
        bpy.context.scene.collection.objects.link(item)
    before = current_state_token()
    before_dirty = bpy.data.is_dirty
    for path, kind in (
        ("bpy.types.Scene", "rna_struct"),
        ("bpy.types.Mesh", "rna_struct"),
        ("bpy.ops.mesh.bevel", "operator"),
        ("bmesh.ops.bevel", "python_callable"),
    ):
        found = lookup(path=path, detail="full", limit=5)
        assert found["exists"] and found["kind"] == kind, found
        assert found["blender_version"] == bpy.app.version_string, found
        assert len(found.get("items", [])) <= 5, found
    search = lookup(search="bmesh.ops.bevel", limit=5)
    assert "bmesh.ops.bevel" in search["items"], search
    missing = lookup(path="bpy.types.Scene.node_tree")
    assert missing["exists"] is False, missing
    assert missing["suggestions"][0] == "compositing_node_group", missing
    assert missing["replacement"]["replacement"] == "bpy.types.Scene.compositing_node_group", missing
    scene_names = {prop.identifier for prop in bpy.types.Scene.bl_rna.properties if prop.identifier != "rna_type"}
    assert missing["suggestions"] and set(missing["suggestions"]) <= scene_names, missing
    compositor_candidates = sorted(name for name in scene_names if "node" in name or "composit" in name)
    start = time.perf_counter()
    for _ in range(5):
        lookup(path="bpy.types.Scene", limit=10)
    elapsed_ms = (time.perf_counter() - start) * 1000 / 5
    assert elapsed_ms < 1000, elapsed_ms
    assert current_state_token() == before, "read-only lookup advanced the document state"
    assert bpy.data.is_dirty == before_dirty, "read-only lookup dirtied the document"
    failure = dispatch("python.exec", {"code": "result = C.scene.node_tree", "diff": "none"})
    assert not failure["ok"] and failure["error"]["type"] == "AttributeError", failure
    hint = failure["data"].get("api_hint")
    assert hint and hint["path"] == "bpy.types.Scene.node_tree" and hint["exists"] is False, failure
    print({"api_lookup": "passed", "blender_version": bpy.app.version_string,
           "scene_objects": len(bpy.data.objects),
           "compositor_properties": compositor_candidates,
           "missing_suggestions": missing["suggestions"], "summary_mean_ms": round(elapsed_ms, 3)})


if __name__ == "__main__":
    run()
