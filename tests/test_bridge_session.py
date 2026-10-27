"""Pure-Python tests for the multi-instance BlenderBoo configuration bootstrap."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bridge_session

TOKEN_A = "unit-test-token-a-that-is-never-printed"
TOKEN_B = "unit-test-token-b-that-is-never-printed"
TOKENS = {"instance-a": TOKEN_A, "instance-b": TOKEN_B}
REGISTRY = Path("C:/Users/test/.blenderboo/instances.json")


class ConfigRewriteTests(unittest.TestCase):
    def replacement(self, newline: str = "\n") -> str:
        return bridge_session.build_config_table(
            "C:\\Python311\\python.exe",
            Path("C:/repo/mcp_server/src"),
            TOKENS,
            REGISTRY,
            newline,
        )

    def assert_managed_entry(self, content: str) -> None:
        entry = tomllib.loads(content)["mcp_servers"]["blenderboo"]
        self.assertEqual(entry["command"], "C:\\Python311\\python.exe")
        self.assertEqual(entry["args"], ["-m", "blenderboo_mcp.server"])
        self.assertEqual(entry["env"]["PYTHONPATH"], "C:\\repo\\mcp_server\\src")
        self.assertEqual(entry["env"]["CBT_INSTANCE_REGISTRY"], str(REGISTRY))
        self.assertEqual(json.loads(entry["env"]["CBT_BRIDGE_TOKENS"]), TOKENS)

    def test_fresh_file(self) -> None:
        updated = bridge_session.rewrite_codex_config("", self.replacement())
        self.assert_managed_entry(updated)
        self.assertTrue(updated.startswith("[mcp_servers.blenderboo]\n"))

    def test_legacy_registration_remains_until_verified_cutover(self) -> None:
        legacy = '[mcp_servers.codex-blender]\ncommand = "old-python"\n'
        updated = bridge_session.rewrite_codex_config(legacy, self.replacement())
        self.assertIn(legacy, updated)
        self.assert_managed_entry(updated)

    def test_existing_entry_and_child_table_are_replaced(self) -> None:
        original = (
            "[mcp_servers.blenderboo]\n"
            'command = "old-python"\n'
            "[mcp_servers.blenderboo.env]\n"
            'OLD_TOKEN = "old"\n'
        )
        updated = bridge_session.rewrite_codex_config(original, self.replacement())
        self.assertNotIn("old-python", updated)
        self.assertNotIn("OLD_TOKEN", updated)
        self.assertEqual(updated.count("[mcp_servers.blenderboo]"), 1)
        self.assert_managed_entry(updated)

    def test_unrelated_content_is_preserved_byte_for_byte(self) -> None:
        prefix = '# owner comment\r\n[features]\r\nalpha = true\r\n\r\n'
        managed = (
            "[mcp_servers.blenderboo]\r\n"
            'command = "old"\r\n'
            'args = []\r\n'
        )
        suffix = (
            "[mcp_servers.other]\r\n"
            'command = "leave-me"\r\n'
            "# trailing owner text\r\n"
        )
        updated = bridge_session.rewrite_codex_config(
            prefix + managed + suffix,
            self.replacement("\r\n"),
        )
        self.assertTrue(updated.startswith(prefix))
        self.assertTrue(updated.endswith(suffix))
        self.assert_managed_entry(updated)

    def test_interleaved_child_table_is_removed_without_changing_other_table(self) -> None:
        unrelated = '[mcp_servers.other]\ncommand = "leave-exactly"\n'
        original = (
            '[mcp_servers.blenderboo]\ncommand = "old"\n'
            + unrelated
            + '[mcp_servers.blenderboo.env]\nOLD = "secret"\n'
            + '[features]\nalpha = true\n'
        )
        updated = bridge_session.rewrite_codex_config(original, self.replacement())
        self.assertIn(unrelated, updated)
        self.assertNotIn("OLD", updated)
        self.assertTrue(tomllib.loads(updated)["features"]["alpha"])
        self.assert_managed_entry(updated)

    def test_update_backs_up_original_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            original = '[features]\nalpha = true\n'
            config_path.write_text(original, encoding="utf-8")
            bridge_session.update_codex_config(config_path, TOKENS, REGISTRY)
            backup = config_path.with_name("config.toml.bak-blenderboo")
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads(
                    parsed["mcp_servers"]["blenderboo"]["env"]["CBT_BRIDGE_TOKENS"]
                ),
                TOKENS,
            )
            self.assertTrue(parsed["features"]["alpha"])

    def test_empty_or_invalid_token_map_is_rejected(self) -> None:
        for tokens in ({}, {"instance-a": "short"}, {"": TOKEN_A}):
            with self.subTest(tokens=tokens), self.assertRaises(bridge_session.BootstrapError):
                bridge_session.build_config_table(
                    sys.executable,
                    Path("C:/repo/mcp_server/src"),
                    tokens,
                    REGISTRY,
                )


class RuntimeBootstrapTests(unittest.TestCase):
    def instances(self):
        return [
            SimpleNamespace(
                id="instance-a",
                port=9881,
                token_location="clipboard",
            ),
            SimpleNamespace(
                id="instance-b",
                port=9882,
                token_location="env:CBT_TOKEN_B",
            ),
        ]

    def test_collect_tokens_uses_mapping_environment_and_secure_prompt(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CBT_BRIDGE_TOKENS": json.dumps({"instance-a": TOKEN_A}),
                "CBT_TOKEN_B": TOKEN_B,
            },
            clear=True,
        ), patch("bridge_session.getpass.getpass") as prompt:
            self.assertEqual(bridge_session.collect_tokens(self.instances()), TOKENS)
        prompt.assert_not_called()

        with patch.dict(os.environ, {}, clear=True), patch(
            "bridge_session.getpass.getpass",
            side_effect=[TOKEN_A, TOKEN_B],
        ) as prompt:
            self.assertEqual(bridge_session.collect_tokens(self.instances()), TOKENS)
        self.assertEqual(prompt.call_count, 2)

    def test_verify_instances_checks_returned_identity(self) -> None:
        instances = self.instances()

        class Client:
            def __init__(self, token, port):
                self.port = port

            def call(self, tool, params):
                instance_id = "instance-a" if self.port == 9881 else "instance-b"
                return {
                    "ok": True,
                    "tool": tool,
                    "data": {"bridge": {"instance_id": instance_id, "port": self.port}},
                }

        bridge_session.verify_instances(Client, instances, TOKENS)

        class WrongClient(Client):
            def call(self, tool, params):
                result = super().call(tool, params)
                result["data"]["bridge"]["instance_id"] = "wrong"
                return result

        with self.assertRaisesRegex(bridge_session.BootstrapError, "mismatched identity"):
            bridge_session.verify_instances(WrongClient, instances, TOKENS)

    def test_community_execute_code_path_is_removed(self) -> None:
        self.assertFalse(hasattr(bridge_session, "restart_bridge"))
        self.assertFalse(hasattr(bridge_session, "build_execute_code_payload"))
        self.assertFalse(hasattr(bridge_session, "COMMUNITY_PORT"))


if __name__ == "__main__":
    unittest.main()
