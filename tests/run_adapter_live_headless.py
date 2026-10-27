"""Qualify MCP schema audit and a typed call against an isolated Blender bridge."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def run_client():
    sys.path.insert(0, str(ROOT / "mcp_server" / "src"))
    from blenderboo_mcp.client import BridgeClient
    from blenderboo_mcp.proxy import ToolkitProxy
    from blenderboo_mcp.server import create_server

    proxy = ToolkitProxy(BridgeClient(os.environ["CBT_TEST_BRIDGE_TOKEN"], int(os.environ["CBT_TEST_BRIDGE_PORT"])))
    server = create_server(proxy, tool_mode="eager", response_profile="full")
    response_sizes = []

    def call(name, params):
        print(f"ADAPTER_LIVE_CALL_START {name}", flush=True)
        response = asyncio.run(server.call_tool(name, params))
        envelope = response[1] if isinstance(response, tuple) else response.structuredContent
        assert isinstance(envelope, dict) and envelope.get("ok"), envelope
        response_sizes.append((name, len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))))
        print(f"ADAPTER_LIVE_CALL_DONE {name}", flush=True)
        return envelope["data"]

    inspected = call("scene.inspect", {})
    assert "state_token" in inspected, inspected
    workflow = call("workflow.run", {
        "steps": [{"id": "inspect", "tool": "scene.inspect", "arguments": {}}],
        "execute": True,
    })
    assert workflow["steps"][0]["status"] == "completed", workflow
    token = os.environ["CBT_TEST_BRIDGE_TOKEN"]
    echoed = call("python.exec", {"code": f"result = '{token}'"})
    assert echoed["result"] == token, echoed
    audit = call("adapter.schema_audit", {})
    assert audit["aligned"] and audit["error_count"] == 0 and audit["warning_count"] == 0, audit
    summary = call("session.call_log_summary", {})
    log_text = "".join(path.read_text() for path in Path(os.environ["CBT_CALL_LOG_DIR"]).glob("*.jsonl"))
    records = [json.loads(line) for line in log_text.splitlines()]
    assert token not in log_text, "Bridge token leaked into the call log"
    assert sum(item["tool"] == "scene.inspect" for item in records) >= 2, records
    assert any(item["tool"] == "scene.inspect" and item.get("parent") for item in records), records
    assert summary["calls"] >= 4, summary
    call("python.exec", {"code": "bpy.context.scene['edit_cycle_probe'] = 1"})
    old_start = len(response_sizes)
    old_plan = call("scene.save_current", {"dry_run": True})
    assert old_plan["dry_run"], old_plan
    old_saved = call("scene.save_current", {"dry_run": False, "confirm": True})
    assert old_saved["write_confirmed"], old_saved
    old_bytes = sum(size for _, size in response_sizes[old_start:])
    call("python.exec", {"code": "bpy.context.scene['edit_cycle_probe'] = 2"})
    new_start = len(response_sizes)
    new_saved = call("scene.save_current", {})
    assert new_saved["write_confirmed"], new_saved
    new_bytes = sum(size for _, size in response_sizes[new_start:])
    full_start = len(response_sizes)
    full_summary = call("session.call_log_summary", {"detail": "full"})
    full_bytes = response_sizes[full_start][1]
    compact_start = len(response_sizes)
    compact_summary = call("session.call_log_summary", {"detail": "compact", "since_seq": summary["last_seq"]})
    compact_bytes = response_sizes[compact_start][1]
    assert full_summary["calls"] >= compact_summary["calls"]
    (ROOT / "tests" / ".tmp_edit_cycle_measurement.json").write_text(
        json.dumps({
            "runtime_version": "0.7.1",
            "historical_pattern": "two-step dry-run plus confirm emulated on v0.7.1; no v0.6.4 runtime measurement",
            "serialized_envelope_encoding": "UTF-8 compact JSON",
            "old_pattern": {"calls": 2, "response_bytes": old_bytes},
            "new_pattern": {"calls": 1, "response_bytes": new_bytes},
            "full_summary_response_bytes": full_bytes,
            "compact_since_seq_delta_response_bytes": compact_bytes,
        }, indent=2), encoding="utf-8",
    )
    print(f"EDIT_CYCLE_MEASURED old_calls=2 old_bytes={old_bytes} new_calls=1 new_bytes={new_bytes} "
          f"full_summary_bytes={full_bytes} compact_delta_bytes={compact_bytes}", flush=True)
    print(f"ADAPTER_LIVE_OK addon_tools={audit['addon_tool_count']} errors=0 warnings=0 "
          f"log_records={len(records)} workflow_child=1 token_redacted=1", flush=True)


def run_blender():
    import bpy
    sys.path.insert(0, str(ROOT / "addon"))
    for name in list(sys.modules):
        if name == "blenderboo" or name.startswith("blenderboo."):
            del sys.modules[name]
    from blenderboo import dispatch
    from blenderboo.bridge import BRIDGE_HOST, _BridgeController, _BridgeRequestHandler, _LoopbackServer, _MainThreadExecutor

    bpy.ops.wm.read_factory_settings(use_empty=True)
    with TemporaryDirectory() as directory:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(directory) / "adapter-live.blend"))
        token = secrets.token_hex(32)
        executor = _MainThreadExecutor(dispatch)
        bridge = _LoopbackServer((BRIDGE_HOST, 0), _BridgeRequestHandler)
        bridge.controller = _BridgeController(token, executor, 5.0)
        server_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        server_thread.start()
        python = shutil.which("python")
        assert python, "Host Python with MCP SDK is unavailable"
        child_env = os.environ.copy()
        child_env["CBT_TEST_BRIDGE_TOKEN"] = token
        child_env["CBT_TEST_BRIDGE_PORT"] = str(bridge.server_address[1])
        child_env["CBT_CALL_LOG_DIR"] = str(Path(directory) / "call-logs")
        child_env["PYTHONPATH"] = str(ROOT / "mcp_server" / "src")
        child = subprocess.Popen([python, str(Path(__file__).resolve()), "--client"],
                                 cwd=str(ROOT), env=child_env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 60
            while child.poll() is None and time.monotonic() < deadline:
                if executor.pending_count:
                    executor.drain()
                else:
                    time.sleep(0.005)
            if child.poll() is None:
                child.kill()
                stdout, stderr = child.communicate(timeout=5)
                raise AssertionError(f"MCP client timed out; progress={stdout[-2000:]} stderr={stderr[-1000:]}")
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0, f"MCP client failed ({child.returncode}): {stderr[-3000:]}"
            print(stdout.strip())
        finally:
            executor.close()
            bridge.shutdown()
            bridge.server_close()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    if "--client" in sys.argv:
        run_client()
    else:
        run_blender()
