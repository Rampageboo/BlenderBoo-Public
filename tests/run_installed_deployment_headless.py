"""Verify installed add-on and configured MCP adapter in fresh isolated processes."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "tests" / ".tmp_deployment_verify"
EVIDENCE.mkdir(exist_ok=True)


def adapter_client():
    from blenderboo_mcp.client import BridgeClient
    from blenderboo_mcp.proxy import ToolkitProxy
    from blenderboo_mcp.server import create_server
    import blenderboo_mcp

    package_path = Path(blenderboo_mcp.__file__).resolve()
    configured_source = Path(os.environ["PYTHONPATH"]).resolve()
    assert package_path.is_relative_to(configured_source), package_path
    proxy = ToolkitProxy(BridgeClient(os.environ["CBT_TEST_TOKEN"], int(os.environ["CBT_TEST_PORT"]),
                                     client_id="installed-deployment-smoke"))
    server = create_server(proxy, tool_mode="eager", response_profile="full")

    def call(name, params):
        response = asyncio.run(server.call_tool(name, params))
        envelope = response[1] if isinstance(response, tuple) else response.structuredContent
        assert isinstance(envelope, dict) and envelope.get("ok"), envelope
        return envelope["data"]

    doctor = call("system.doctor", {})
    assert doctor["healthy"] and doctor["addon_version"] == "0.10.0", doctor
    assert doctor["adapter"]["version"] == doctor["addon_version"], doctor
    assert len(doctor["capabilities"]["schema_digests"]) == doctor["capabilities"]["count"], doctor
    assert len(doctor["capabilities"]["schema_digests"]["mesh.join"]) == 64, doctor
    inspected = call("scene.inspect", {})
    assert "state_token" in inspected, inspected
    executed = call("python.exec", {"code": "result = 6 * 7"})
    assert executed["result"] == 42, executed
    audit = call("adapter.schema_audit", {})
    assert audit["aligned"] and audit["error_count"] == 0 and audit["warning_count"] == 0, audit
    default_server = create_server(proxy, tool_mode="eager", response_profile="default")
    rejected = asyncio.run(default_server.call_tool("mesh.build_lathe", {
        "name": "RejectedCap", "profile": [
            {"radius": 1, "z": 0, "round_radius": .1}, {"radius": 1, "z": 1}],
    }))
    rejected_content, rejected_envelope = rejected if isinstance(rejected, tuple) else (rejected.content, rejected.structuredContent)
    assert rejected_envelope["data"]["status"] == "unsupported", rejected_envelope
    assert "Endpoint fillets" in rejected_envelope["data"]["reason"], rejected_envelope
    assert "Endpoint fillets" in rejected_content[0].text, rejected_content
    analysis = asyncio.run(default_server.call_tool("mesh.fair_fit", {
        "source": "MCP_Fair_Source", "center": [0, 0, 0],
        "theta_samples": 16, "phi_samples": 9, "phi_range": [-45, 45],
        "order_m": 2, "order_k": 2, "apply": False,
    }))
    analysis_content, analysis_envelope = analysis if isinstance(analysis, tuple) else (analysis.content, analysis.structuredContent)
    assert analysis_envelope["data"]["status"] in {"analysis", "partial"}, analysis_envelope
    assert analysis_envelope["data"]["coverage"] > .9, analysis_envelope
    assert "coverage" in analysis_content[0].text, analysis_content
    lathe = call("mesh.build_lathe", {"name": "MCPGridCap", "segments": 32,
        "profile": [{"radius": .4, "z": 0}, {"radius": .45, "z": .8}]})
    assert lathe["status"] == "built" and lathe["counts"]["all_quads"], lathe
    lathe_gate = call("asset.quality_gate", {"object_name": "MCPGridCap"})
    assert lathe_gate["pass"] and lathe_gate["self_intersections"]["count"] == 0, lathe_gate
    plate = call("mesh.build_plate", {"name": "MCPProjectedPlate",
        "outline": [[-.1,-.1,1.2],[.1,-.1,1.2],[.1,.1,1.2],[-.1,.1,1.2]],
        "subdivisions": [4,4], "target": "MCP_Fair_Source",
        "projection_direction": [0,0,-1], "thickness": .003})
    assert plate["status"] == "built" and plate["projection_outward_offset_scene_units"] > 0, plate
    slab = call("mesh.build_slab", {"name": "MCPMagwell", "outline": [[-2,-1],[2,-1],[2,1],[-2,1]],
        "opening": [[-.5,-.4],[.5,-.4],[.5,.4],[-.5,.4]],
        "thickness": .5, "pocket_depth": .2})
    assert slab["status"] == "built" and slab["opening_mode"] == "pocket", slab
    region_seams = call("uv.seams_from_regions", {"object_name": "MCPMagwell",
        "attribute_name": "fregion", "dry_run": True})
    assert region_seams["new_seam_count"] > 0, region_seams
    atlas = call("uv.audit", {"object_names": ["MCPAtlasA", "MCPAtlasB"],
        "texture_size": 128, "raster_size": 64})
    assert atlas["cross_object_overlap_raster_pixels"] > 0, atlas
    joined = call("mesh.join", {"object_names": ["MCPAtlasA", "MCPAtlasB"],
        "name": "MCPAtlasJoined", "keep_sources": False, "max_elements": 500000,
        "fragment_small_ratio": 0.01, "confirm": True, "dry_run": False})
    assert joined["changed"] and joined["sources_preserved"], joined
    clipped = call("render.detail", {"bounds": {"min": [-1,-1,-1], "max": [1,1,1]},
        "direction": "top", "projection": "orthographic", "clip_to_bounds": True,
        "width": 64, "height": 64, "output_path": str(EVIDENCE / "mcp_clipped.png"),
        "overwrite": True})
    assert clipped["camera"]["clip_scope"] == "camera_depth", clipped
    summary = call("session.call_log_summary", {})
    assert summary["calls"] >= 4, summary
    logs = list(Path(os.environ["CBT_CALL_LOG_DIR"]).glob("*.jsonl"))
    assert logs, "No adapter call log was written"
    token = os.environ["CBT_TEST_TOKEN"]
    assert all(token not in path.read_text(encoding="utf-8") for path in logs), "Token leaked into call log"
    (EVIDENCE / "adapter_result.json").write_text(json.dumps({
        "adapter_path": str(package_path), "doctor_version": doctor["addon_version"],
        "adapter_version": doctor["adapter"]["version"],
        "python_exec_result": executed["result"], "addon_tools": audit["addon_tool_count"],
        "schema_errors": audit["error_count"], "schema_warnings": audit["warning_count"],
        "logged_calls": summary["calls"], "token_redacted": True,
        "default_refusal_reason_visible": True,
        "default_fair_fit_analysis_visible": True,
        "lathe_quality_gate_pass": lathe_gate["pass"],
        "projected_plate_offset_scene_units": plate["projection_outward_offset_scene_units"],
        "slab_opening_mode": slab["opening_mode"],
        "region_seams_planned": region_seams["new_seam_count"],
        "atlas_cross_object_overlap_pixels": atlas["cross_object_overlap_raster_pixels"],
        "named_join_sources_preserved": joined["sources_preserved"],
        "doctor_per_tool_schema_digests": len(doctor["capabilities"]["schema_digests"]),
        "cached_join_defaults_accepted": True,
        "detail_clip_scope": clipped["camera"]["clip_scope"],
    }, indent=2), encoding="utf-8")
    print("INSTALLED_ADAPTER_OK", flush=True)


def blender_host():
    import bpy
    isolated_root = os.environ.get("CBT_TEST_ADDON_ROOT")
    if isolated_root:
        sys.path.insert(0, str(Path(isolated_root).resolve()))
    import blenderboo as addon
    from blenderboo import dispatch, start_bridge, stop_bridge
    import blenderboo.bridge as bridge

    addon_path = Path(addon.__file__).resolve()
    if isolated_root:
        assert addon_path.is_relative_to(Path(isolated_root).resolve()), addon_path
    else:
        assert "AppData" in addon_path.parts and "addons" in addon_path.parts, addon_path
    assert not addon_path.is_relative_to(ROOT / "addon"), addon_path
    assert addon.bl_info["version"] == (0, 10, 0), addon.bl_info
    assert bpy.app.version[:2] == (5, 2), bpy.app.version_string
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=1)
    bpy.context.object.name = "MCP_Fair_Source"
    bpy.ops.mesh.primitive_plane_add(size=1)
    bpy.context.object.name = "MCPAtlasA"
    bpy.ops.mesh.primitive_plane_add(size=1)
    bpy.context.object.name = "MCPAtlasB"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = secrets.token_hex(32)
    start_bridge(token, port=port)
    try:
        direct = dispatch("system.doctor", {})
        assert direct["ok"] and direct["data"]["healthy"], direct
        assert direct["data"]["addon_version"] == "0.10.0", direct
        adapter_override = os.environ.get("CBT_TEST_ADAPTER_SOURCE")
        if adapter_override:
            configured_source = adapter_override
        else:
            config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8"))
            configured_source = config["mcp_servers"]["blenderboo"]["env"]["PYTHONPATH"]
        assert Path(configured_source).resolve() == (ROOT / "mcp_server" / "src").resolve()
        python = shutil.which("python")
        assert python, "Host Python unavailable"
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = configured_source
        child_env["CBT_TEST_TOKEN"] = token
        child_env["CBT_TEST_PORT"] = str(port)
        child_env["CBT_CALL_LOG_DIR"] = str(EVIDENCE / "call_logs")
        child = subprocess.Popen([python, str(Path(__file__).resolve()), "--adapter-client"],
                                 cwd=str(ROOT), env=child_env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 60
        while child.poll() is None and time.monotonic() < deadline:
            running = bridge._running_bridge
            if running is not None and running.executor.pending_count:
                running.executor.drain()
            else:
                time.sleep(0.005)
        if child.poll() is None:
            child.kill()
            raise AssertionError("Configured adapter client timed out")
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, f"Configured adapter failed ({child.returncode}): {stderr[-2500:]}"
        assert "INSTALLED_ADAPTER_OK" in stdout, stdout
        (EVIDENCE / "addon_result.json").write_text(json.dumps({
            "addon_path": str(addon_path), "addon_version": list(addon.bl_info["version"]),
            "blender_version": bpy.app.version_string, "doctor_healthy": True,
            "configured_adapter_source": configured_source,
            "adapter_source_mode": "isolated_override" if adapter_override else "global_config",
        }, indent=2), encoding="utf-8")
        print("INSTALLED_DEPLOYMENT_OK", flush=True)
    finally:
        stop_bridge()


if __name__ == "__main__":
    if "--adapter-client" in sys.argv:
        adapter_client()
    else:
        blender_host()
