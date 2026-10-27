"""Live Blender smoke for unrestricted python.exec and its mutation receipt."""

from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))
from blenderboo import dispatch
from blenderboo.bridge import (
    BRIDGE_HOST, _BridgeController, _BridgeRequestHandler, _LoopbackServer,
    _MainThreadExecutor,
)
from blenderboo_mcp.client import BridgeClient


def check_live_bridge_timeout():
    """Send a real authenticated socket request while Blender runs a long script."""
    token = "python-exec-headless-bridge-token-12345"
    executor = _MainThreadExecutor(dispatch)
    server = _LoopbackServer((BRIDGE_HOST, 0), _BridgeRequestHandler)
    server.controller = _BridgeController(token, executor, 0.1)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    replies = []
    worker = threading.Thread(target=lambda: replies.append(
        BridgeClient(token, server.server_address[1]).call(
            "python.exec", {"code": "import time; time.sleep(0.3); result = 'completed'"}
        )
    ))
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while executor.pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert executor.pending_count == 1, "Bridge did not queue python.exec"
        # drain dispatches on this main Blender thread; the socket handler
        # returns its pending receipt while the Python code is still sleeping.
        assert executor.drain() == 1
        worker.join(timeout=5)
        assert not worker.is_alive(), "Bridge client did not receive timeout receipt"
        reply = replies[0]
        assert reply["error"]["type"] == "BridgeExecutionPendingError", reply
        assert reply["data"]["possibly_still_running"] is True, reply
        assert reply["data"]["automatic_retry_safe"] is False, reply
    finally:
        executor.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        worker.join(timeout=2)


def run():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.TemporaryDirectory() as directory:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(directory) / "python-exec.blend"))
        created = dispatch("python.exec", {
            "code": "m = D.meshes.new('ExecMesh'); o = D.objects.new('ExecObject', m); C.scene.collection.objects.link(o); result = o.name",
            "checkpoint": "before-create", "diff": "detailed",
        })
        assert created["ok"], created
        assert created["data"]["result"] == "ExecObject", created
        assert "ExecObject" in created["data"]["changes"]["objects"]["created"], created
        assert created["meta"]["timings_ms"]["exec_user"] >= 0, created
        assert created["meta"]["timings_ms"]["exec_overhead"] >= 0, created
        assert Path(created["data"]["checkpoint"]).is_file(), created
        token = created["data"]["state_token"]
        stale = dispatch("python.exec", {"code": "result = 'no'", "expected_state": "stale"})
        assert not stale["ok"] and stale["error"]["type"] == "StaleStateError", stale
        modified = dispatch("python.exec", {"code": "D.objects['ExecObject'].location.x = 4; result = 4", "expected_state": token})
        assert modified["ok"] and modified["data"]["changes"]["objects"]["counts"]["modified"] == 1, modified
        assert modified["data"]["changes"]["transform_hierarchy"]["ExecObject"] == "matrix_world", modified
        unchanged = dispatch("python.exec", {"code": "result = len(D.objects)"})
        assert unchanged["ok"] and unchanged["data"]["changed"] is False, unchanged
        assert unchanged["data"]["detection"] == "depsgraph+inventory", unchanged
        assert unchanged["data"]["change_status"] == "no_changes_detected", unchanged
        with patch("blenderboo.tools.python_exec.snapshot",
                   side_effect=AssertionError("diff:none must not snapshot")), patch(
                       "blenderboo.tools.python_exec._fragment_summaries",
                       side_effect=AssertionError("diff:none must not sample fragments")):
            disabled = dispatch("python.exec", {"code": "result = len(D.objects)", "diff": "none"})
        assert disabled["ok"] and disabled["data"]["detection"] == "disabled", disabled
        assert disabled["data"]["changes"] == {} and not disabled["data"]["changed"], disabled
        assert disabled["meta"]["timings_ms"]["snapshot_before"] >= 0, disabled
        cases = (
            ("geometry", "D.meshes['ExecMesh'].vertices[0].co.x = 1.0"),
            ("modifiers", "m = D.objects['ExecObject'].modifiers.new('Subd', 'SUBSURF'); m.levels = 2"),
            ("shading", "m = D.materials.new('ExecMaterial'); m.use_nodes = True; D.objects['ExecObject'].data.materials.append(m); m.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.2, 0.3, 0.4, 1.0)"),
            ("uv_attributes", "m = D.meshes['ExecMesh']; uv = m.uv_layers.new(name='UVMap'); uv.data[0].uv = (0.2, 0.3)"),
            ("other", "D.objects['ExecObject']['custom'] = 3"),
        )
        # The initial mesh has no vertices, so populate it before same-count edits.
        populated = dispatch("python.exec", {"code": "D.meshes['ExecMesh'].from_pydata([(0,0,0),(1,0,0),(0,1,0)], [], [(0,1,2)])"})
        assert populated["ok"] and "geometry" in populated["data"]["changes"], populated
        for category, code in cases:
            outcome = dispatch("python.exec", {"code": code})
            assert outcome["ok"] and outcome["data"]["changed"], (category, outcome)
            assert category in outcome["data"]["changes"], (category, outcome)
        isolated = (
            ("modifiers", "D.objects['ExecObject'].modifiers['Subd'].levels = 3"),
            ("shading", "D.materials['ExecMaterial'].node_tree.nodes['Principled BSDF'].inputs['Roughness'].default_value = 0.23"),
            ("uv_attributes", "D.meshes['ExecMesh'].uv_layers['UVMap'].data[1].uv = (0.7, 0.8)"),
            ("shading", "D.meshes['ExecMesh'].materials.append(D.materials.new('Second')); D.meshes['ExecMesh'].polygons[0].material_index = 1"),
        )
        for category, code in isolated:
            outcome = dispatch("python.exec", {"code": code})
            assert outcome["ok"] and category in outcome["data"]["changes"], (category, outcome)
        bulk_geometry = dispatch("python.exec", {"code": "from array import array; m = D.meshes['ExecMesh']; values = array('f', [0.0]) * (3 * len(m.vertices)); m.vertices.foreach_get('co', values); values[1] = 0.33; m.vertices.foreach_set('co', values); m.update()"})
        assert bulk_geometry["ok"] and "geometry" in bulk_geometry["data"]["changes"], bulk_geometry
        curve_setup = dispatch("python.exec", {"code": "c = D.curves.new('ExecCurve', 'CURVE'); c.dimensions = '3D'; s = c.splines.new('BEZIER'); o = D.objects.new('CurveObject', c); C.scene.collection.objects.link(o)"})
        assert curve_setup["ok"], curve_setup
        curve_edit = dispatch("python.exec", {"code": "D.curves['ExecCurve'].splines[0].bezier_points[0].handle_left.x = -2"})
        assert curve_edit["ok"] and "geometry" in curve_edit["data"]["changes"], curve_edit
        attribute_setup = dispatch("python.exec", {"code": "D.meshes['ExecMesh'].attributes.new('detail_weight', 'FLOAT', 'POINT')"})
        assert attribute_setup["ok"], attribute_setup
        attribute_edit = dispatch("python.exec", {"code": "D.meshes['ExecMesh'].attributes['detail_weight'].data[0].value = 0.7"})
        assert attribute_edit["ok"] and "uv_attributes" in attribute_edit["data"]["changes"], attribute_edit
        float2_setup = dispatch("python.exec", {"code": "D.meshes['ExecMesh'].attributes.new('flow', 'FLOAT2', 'CORNER')"})
        assert float2_setup["ok"], float2_setup
        float2_edit = dispatch("python.exec", {"code": "D.meshes['ExecMesh'].attributes['flow'].data[0].vector = (0.3, 0.4)", "diff": "detailed"})
        assert float2_edit["ok"] and "uv_attributes" in float2_edit["data"]["changes"], float2_edit
        edge_marks = dispatch("python.exec", {"code": "m = D.meshes['ExecMesh']; m.edges[0].use_seam = True; m.edges[1].use_edge_sharp = True; a = m.attributes.new('crease_edge', 'FLOAT', 'EDGE'); a.data[0].value = 0.4"})
        assert edge_marks["ok"] and "uv_attributes" in edge_marks["data"]["changes"], edge_marks
        world_setup = dispatch("python.exec", {"code": "w = D.worlds.new('ExecWorld'); C.scene.world = w; w.use_nodes = True"})
        assert world_setup["ok"], world_setup
        world_edit = dispatch("python.exec", {"code": "D.worlds['ExecWorld'].node_tree.nodes['Background'].inputs['Strength'].default_value = 0.5"})
        assert world_edit["ok"] and "shading" in world_edit["data"]["changes"], world_edit
        topology_setup = dispatch("python.exec", {"code": "m = D.meshes.new('TopologyMesh'); m.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)], [(0,1),(1,2),(2,3),(3,0),(0,2)], [(0,1,2),(0,2,3)]); o = D.objects.new('TopologyObject', m); C.scene.collection.objects.link(o)"})
        assert topology_setup["ok"], topology_setup
        topology_edit = dispatch("python.exec", {"code": "m = D.meshes['TopologyMesh']; m.clear_geometry(); m.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)], [(0,1),(1,2),(2,3),(3,0),(1,3)], [(0,1,3),(1,2,3)])"})
        assert topology_edit["ok"] and "geometry" in topology_edit["data"]["changes"], topology_edit
        nodes_setup = dispatch("python.exec", {"code": "g = D.node_groups.new('ExecGN', 'GeometryNodeTree'); g.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry'); g.interface.new_socket(name='Value', in_out='INPUT', socket_type='NodeSocketFloat'); g.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry'); gi = g.nodes.new('NodeGroupInput'); go = g.nodes.new('NodeGroupOutput'); g.links.new(gi.outputs['Geometry'], go.inputs['Geometry']); m = D.objects['ExecObject'].modifiers.new('Nodes', 'NODES'); m.node_group = g"})
        assert nodes_setup["ok"], nodes_setup
        nodes_edit = dispatch("python.exec", {"code": "D.node_groups['ExecGN'].nodes.new('GeometryNodeSetPosition')"})
        assert nodes_edit["ok"] and "geometry_nodes" in nodes_edit["data"]["changes"], nodes_edit
        nodes_input = dispatch("python.exec", {"code": "next(n for n in D.node_groups['ExecGN'].nodes if n.bl_idname == 'GeometryNodeSetPosition').inputs['Offset'].default_value = (0.1, 0.2, 0.3)"})
        assert nodes_input["ok"] and "geometry_nodes" in nodes_input["data"]["changes"], nodes_input
        modifier_input = dispatch("python.exec", {"code": "g = D.node_groups['ExecGN']; key = next(i.identifier for i in g.interface.items_tree if i.name == 'Value'); m = D.objects['ExecObject'].modifiers['Nodes']; getattr(m.properties.inputs, key).value = 0.5"})
        assert modifier_input["ok"] and "geometry_nodes" in modifier_input["data"]["changes"], modifier_input
        bpy.context.view_layer.objects.active = bpy.data.objects["ExecObject"]
        bpy.data.objects["ExecObject"].select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        edit = dispatch("python.exec", {"code": "import bmesh; bm = bmesh.from_edit_mesh(D.meshes['ExecMesh']); bm.verts.ensure_lookup_table(); bm.verts[0].co.x = 3; bmesh.update_edit_mesh(D.meshes['ExecMesh'])"})
        assert edit["ok"] and "geometry" in edit["data"]["changes"], edit
        bpy.ops.object.mode_set(mode="OBJECT")
        failed = dispatch("python.exec", {"code": "D.objects.remove(D.objects['ExecObject'], do_unlink=True); print('removed'); raise ValueError('expected')"})
        assert not failed["ok"] and failed["error"]["type"] == "ValueError", failed
        assert failed["data"]["partial_change_possible"], failed
        assert failed["data"]["stdout"] == "removed\n", failed
        assert "ExecObject" in failed["data"]["changes"]["objects"]["removed"], failed
        fixture = dispatch("python.exec", {"code": "bpy.ops.mesh.primitive_cube_add(); o = C.object; o.name = 'CollapseFixture'; bm = bmesh.new(); bm.from_mesh(o.data); bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=3, use_grid_fill=True); bm.to_mesh(o.data); bm.free()"})
        assert fixture["ok"], fixture
        collapsed = dispatch("python.exec", {"code": "o = D.objects['CollapseFixture']; bm = bmesh.new(); bm.from_mesh(o.data); bmesh.ops.delete(bm, geom=list(bm.faces)[7:], context='FACES'); bm.to_mesh(o.data); bm.free()", "diff": "detailed"})
        assert collapsed["ok"], collapsed
        assert collapsed["data"]["changes"]["object_metrics"]["CollapseFixture"]["after"]["faces"] == 7, collapsed
        assert any("face_count_drop" in item["reasons"] for item in collapsed["data"]["suspicious_change"]), collapsed
        assert any("Suspicious geometry change" in warning for warning in collapsed["warnings"]), collapsed
        check_live_bridge_timeout()

        # Keep file reload last: it can invalidate add-on/UI state, and this
        # disposable Blender process owns no user document.
        original_path = str(Path(directory) / "python-exec.blend")
        alternate_path = str(Path(directory) / "alternate.blend")
        bpy.ops.wm.save_as_mainfile(filepath=alternate_path)
        before_reload = dispatch("python.exec", {"code": "result = D.filepath"})["data"]["state_token"]
        reopened = dispatch("python.exec", {
            "code": f"bpy.ops.wm.open_mainfile(filepath={original_path!r}, load_ui=False); result = D.filepath",
            "expected_state": before_reload,
        })
        assert reopened["ok"], reopened
        assert reopened["data"]["result"] == original_path, reopened
        assert reopened["data"]["state_token"] != before_reload, reopened
        assert any("active file or add-on state" in warning for warning in reopened["warnings"]), reopened
        assert not dispatch("python.exec", {"code": "result = 'stale'", "expected_state": before_reload})["ok"]
    print("python.exec live smoke passed")


if __name__ == "__main__":
    run()
