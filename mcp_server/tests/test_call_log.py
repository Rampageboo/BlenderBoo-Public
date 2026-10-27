"""Local call-log contract tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blenderboo_mcp.call_log import CallLog, summarize


class CallLogTests(unittest.TestCase):
    def test_default_directory_uses_blenderboo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict("os.environ", {}, clear=True), patch(
                "blenderboo_mcp.call_log.Path.home", return_value=home
            ):
                logger = CallLog(enabled=True)
            self.assertEqual(logger.directory, home / ".blenderboo" / "call-logs")
            self.assertTrue(logger.directory.is_dir())

    def test_directory_override_and_explicit_directory_keep_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "override"
            explicit = Path(directory) / "explicit"
            with patch.dict("os.environ", {"CBT_CALL_LOG_DIR": str(override)}):
                self.assertEqual(CallLog(enabled=False).directory, override)
                self.assertEqual(CallLog(explicit, enabled=False).directory, explicit)

    def test_record_redacts_token_even_in_code_and_summarizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "s" * 40
            logger = CallLog(Path(directory), token=secret, enabled=True)
            result = {"ok": True, "tool": "python.exec", "data": {"changed": True,
                      "modules": {"geometry_helpers": "a" * 64}}, "error": None, "warnings": []}
            logger.record(seq=logger.next_seq(), tool="python.exec", params={"code": f"print('{secret}')", "token": secret}, result=result, public={"ok": True}, elapsed_ms=10, parent="workflow-1")
            content = next(Path(directory).glob("*.jsonl")).read_text()
            self.assertNotIn(secret, content)
            entry = json.loads(content)
            self.assertEqual(entry["parent"], "workflow-1")
            self.assertEqual(entry["modules"], {"geometry_helpers": "a" * 64})
            self.assertEqual(entry["response_bytes"]["public"], len('{"ok":true}'))
            self.assertEqual(summarize([Path(directory)])["python_exec"]["call_share"], 1)

    def test_rotation_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.jsonl"
            old.write_text("{}\n")
            before = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
            import os
            os.utime(old, (before, before))
            logger = CallLog(root, enabled=True)
            self.assertFalse(old.exists())
            with patch("blenderboo_mcp.call_log.MAX_FILE_BYTES", 300):
                for index in range(3):
                    logger.record(seq=index + 1, tool="scene.inspect", params={"index": index}, result={"ok": True, "data": {}}, public={"ok": True})
            self.assertGreater(len(list(root.glob("*.jsonl"))), 1)

    def test_pending_and_late_result_keep_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = CallLog(Path(directory), enabled=True)
            pending = {"ok": False, "data": {"operation_id": "op"}, "error": {"type": "BridgeExecutionPendingError"}}
            logger.record(seq=1, tool="python.exec", params={"code": "pass"}, result=pending)
            logger.record(seq=1, tool="python.exec", params={"code": "pass"}, result={"ok": True, "data": {"changed": False}}, late=True)
            entries = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
            self.assertEqual([entry["seq"] for entry in entries], [1, 1])
            self.assertTrue(entries[0]["timeout"])
            self.assertTrue(entries[1]["late"])
            summary = summarize([Path(directory)])
            self.assertEqual(summary["tools"]["python.exec"]["calls"], 1)
            self.assertEqual(summary["tools"]["python.exec"]["success_rate"], 1.0)

    def test_exec_timing_meta_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = CallLog(Path(directory), enabled=True)
            logger.record(
                seq=1,
                tool="python.exec",
                params={"code": "pass"},
                result={
                    "ok": True,
                    "data": {"changed": False, "detection": "no_changes_detected"},
                    "meta": {"timings_ms": {"exec_user": 0.25, "exec_overhead": 1.5}},
                },
            )
            entry = json.loads(next(Path(directory).glob("*.jsonl")).read_text().splitlines()[0])
            self.assertEqual(entry["timings_ms"]["exec_user"], 0.25)
            self.assertEqual(entry["timings_ms"]["exec_overhead"], 1.5)
            summary = summarize([Path(directory)])["tools"]["python.exec"]
            self.assertEqual(summary["exec_user_ms"]["p50"], 0.25)
            self.assertEqual(summary["exec_overhead_ms"]["p50"], 1.5)

    def test_compact_delta_keeps_versions_and_acceptance_distinct_from_api_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = CallLog(Path(directory), enabled=True, adapter_version="0.7.0")
            logger.record(seq=1, tool="scene.inspect", params={}, result={"ok": True, "data": {},
                          "meta": {"addon_version": "0.7.0"}})
            logger.record(seq=2, tool="assembly.analyze", params={"expectations": []}, result={
                "ok": True, "data": {"pairs": [{"expectation": {"status": "fail"}}]},
                "meta": {"addon_version": "0.7.0"}})
            rows = [json.loads(line) for line in next(Path(directory).glob("*.jsonl")).read_text().splitlines()]
            self.assertEqual(rows[1]["addon_version"], "0.7.0")
            self.assertEqual(rows[1]["adapter_version"], "0.7.0")
            self.assertEqual(rows[1]["acceptance"], "fail")
            summary = summarize([Path(directory)], since_seq=1, session=logger.session, detail="compact")
            self.assertEqual(summary["calls"], 1)
            self.assertEqual(summary["last_seq"], 2)
            self.assertEqual(summary["tools"]["assembly.analyze"]["success_rate"], 1.0)
            self.assertEqual(summary["tools"]["assembly.analyze"]["acceptance"]["fail"], 1)
            self.assertNotIn("exec_overhead_ms", summary["tools"]["assembly.analyze"])


if __name__ == "__main__":
    unittest.main()
