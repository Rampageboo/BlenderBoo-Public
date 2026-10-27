"""Blender headless qualification for MCP workflow.run -> bridge -> python.exec.

Run only in a disposable Blender background process. The MCP workflow engine
runs in a worker while this main thread drains the real authenticated bridge.
"""

from pathlib import Path
import sys
import tempfile
import threading
import time

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
from blenderboo_mcp.proxy import ToolkitProxy
from blenderboo_mcp.workflow import run_workflow


def run() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with tempfile.TemporaryDirectory() as directory:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(directory) / "workflow-exec.blend"))
        token = "python-workflow-headless-bridge-token-12345"
        executor = _MainThreadExecutor(dispatch)
        server = _LoopbackServer((BRIDGE_HOST, 0), _BridgeRequestHandler)
        server.controller = _BridgeController(token, executor, 5.0)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        client = BridgeClient(token, server.server_address[1])
        proxy = ToolkitProxy(client)
        outcomes = []
        failures = []

        def invoke_mcp_workflow() -> None:
            try:
                outcomes.append(run_workflow(
                    proxy.call_named,
                    steps=[{
                        "id": "build",
                        "tool": "python.exec",
                        "arguments": {
                            "code": (
                                "mesh = D.meshes.new('WorkflowExecMesh')\n"
                                "obj = D.objects.new('WorkflowExecObject', mesh)\n"
                                "C.scene.collection.objects.link(obj)\n"
                                "result = obj.name"
                            ),
                        },
                    }],
                    execute=True,
                    rollback_on_failure=False,
                    confirm_rollback=None,
                    instance=None,
                ))
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=invoke_mcp_workflow, daemon=True)
        try:
            worker.start()
            deadline = time.monotonic() + 15
            while worker.is_alive() and time.monotonic() < deadline:
                if executor.pending_count:
                    executor.drain()
                else:
                    time.sleep(0.005)
            worker.join(timeout=0.5)
            assert not worker.is_alive(), "MCP workflow did not finish"
            assert not failures, failures
            assert len(outcomes) == 1 and outcomes[0]["ok"], outcomes
            step = outcomes[0]["data"]["steps"][0]
            assert step["tool"] == "python.exec" and step["status"] == "completed", step
            assert step["result"]["data"]["result"] == "WorkflowExecObject", step
            assert bpy.data.objects.get("WorkflowExecObject") is not None
            inspected = dispatch("scene.inspect", {"object_name": "WorkflowExecObject"})
            assert inspected["ok"], inspected
        finally:
            executor.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            worker.join(timeout=2)
    print("PYTHON_WORKFLOW_HEADLESS_OK")


if __name__ == "__main__":
    run()
