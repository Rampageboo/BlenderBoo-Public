"""Runtime compatibility and operation-result regressions for 0.7.2."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from blenderboo_mcp.proxy import ToolkitProxy
from blenderboo_mcp.call_log import summarize
from blenderboo_mcp.server import create_server


class OlderClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.busy_doctor = False

    def call(self, tool: str, params: dict, **kwargs) -> dict:
        self.calls.append((tool, params))
        if tool == "system.doctor" and self.busy_doctor:
            return {"ok": False, "tool": tool,
                    "data": {"running_operation_id": "op-running"},
                    "error": {"type": "BridgeBusyError", "message": "Blender occupied"},
                    "warnings": [], "meta": {"addon_version": "0.6.4"}}
        if tool == "system.doctor":
            data = {"addon_version": "0.6.4", "healthy": True, "problems": []}
        elif tool == "system.capabilities":
            data = {"tools": [{"name": "python.exec", "input_schema": {
                "properties": {"code": {}, "diff": {}, "checkpoint": {}}
            }}]}
        elif tool == "bridge.operation_start":
            data = {"operation_id": "op-1", "status": "queued"}
        elif tool == "bridge.operation_status":
            data = {"operation_id": "op-1", "status": "failed", "ok": False,
                    "error": {"type": "AssertionError", "message": "bad geometry"},
                    "changed": True, "partial_change_possible": True,
                    "result": {"ok": False, "tool": "python.exec", "data": {
                        "changed": True, "partial_change_possible": True},
                        "error": {"type": "AssertionError", "message": "bad geometry"},
                        "warnings": []}}
        else:
            data = {"changed": False}
        return {"ok": True, "tool": tool, "data": data, "error": None,
                "warnings": [], "meta": {"addon_version": "0.6.4"}}


class RuntimeGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log_directory = Path(directory.name)
        env = patch.dict("os.environ", {"CBT_CALL_LOG_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.client = OlderClient()
        self.server = create_server(ToolkitProxy(self.client))

    def test_unsupplied_defaults_are_not_sent_to_older_addon(self) -> None:
        _, result = asyncio.run(self.server.call_tool("python.exec", {"code": "result = 1"}))
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.calls[-1], ("python.exec", {"code": "result = 1"}))
        self.assertIn("restart Blender", result["warnings"][0])

    def test_explicit_unknown_field_is_rejected_before_execution(self) -> None:
        with self.assertRaises((ToolError, ValueError)):
            asyncio.run(self.server.call_tool("python.exec", {
                "code": "result = 1", "collapse_face_ratio": 0.3,
            }))
        self.assertFalse(any(tool == "python.exec" for tool, _ in self.client.calls))

    def test_toolsets_and_completed_operation_log_inner_tools(self) -> None:
        asyncio.run(self.server.call_tool("toolsets.call", {"tool": "scene.inspect", "arguments": {}}))
        asyncio.run(self.server.call_tool("operation.start", {
            "tool": "python.exec", "arguments": {"code": "raise AssertionError()"},
        }))
        _, status = asyncio.run(self.server.call_tool("operation.status", {"operation_id": "op-1"}))
        self.assertFalse(status["data"]["ok"])
        summary = summarize([self.log_directory])
        self.assertEqual(summary["tools"]["scene.inspect"]["success_rate"], 1.0)
        self.assertEqual(summary["tools"]["python.exec"]["success_rate"], 0.0)
        self.assertNotIn("toolsets.call", summary["tools"])

    def test_status_recovery_does_not_need_main_thread_version_probe(self) -> None:
        _, status = asyncio.run(self.server.call_tool("operation.status", {"operation_id": "op-1"}))
        self.assertFalse(status["data"]["ok"])
        self.assertEqual([tool for tool, _ in self.client.calls], ["bridge.operation_status"])
        self.assertIn("restart Blender", status["warnings"][0])

    def test_busy_first_contact_keeps_running_operation_id(self) -> None:
        self.client.busy_doctor = True
        _, result = asyncio.run(self.server.call_tool("python.exec", {"code": "result = 1"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "BridgeBusyError")
        self.assertEqual(result["data"]["running_operation_id"], "op-running")
        self.assertFalse(any(tool == "python.exec" for tool, _ in self.client.calls))


if __name__ == "__main__":
    unittest.main()
