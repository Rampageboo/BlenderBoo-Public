"""Tests for the config-backed bridge token and fail-safe auto-start hook."""

from pathlib import Path
import os
import sys
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

import blenderboo
from blenderboo.config_token import (
    ConfigTokenError,
    read_bridge_token,
    resolve_or_create_bridge_token,
    write_bridge_token,
)


TOKEN = "a" * 43


class ConfigTokenTests(unittest.TestCase):
    @staticmethod
    def _config(token: str | None = None) -> str:
        token_entry = f', CBT_BRIDGE_TOKEN = "{token}"' if token else ""
        return (
            "[mcp_servers.other]\ncommand = \"other\"\n\n"
            "[mcp_servers.codex-blender]\n"
            "command = \"python\"\n"
            f'env = {{ PYTHONPATH = "C:/adapter", CBT_INSTANCE_REGISTRY = "C:/registry"{token_entry} }}\n\n'
            "[unrelated]\nvalue = \"preserve me\"\n"
        )

    @staticmethod
    def _child_table_config(token: str | None = None) -> str:
        token_line = f'CBT_BRIDGE_TOKEN = "{token}"\n' if token else ""
        return (
            "[mcp_servers.other]\ncommand = \"other\"\n\n"
            "[mcp_servers.codex-blender]\n"
            "command = \"python\"\n\n"
            "[mcp_servers.codex-blender.env]\n"
            "PYTHONPATH = \"C:/adapter\"\n"
            f"{token_line}\n"
            "[unrelated]\nvalue = \"preserve me\"\n"
        )

    def test_reads_existing_token_from_env_overridden_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(self._config(TOKEN), encoding="utf-8")
            with patch.dict(os.environ, {"CBT_CONFIG_PATH": str(path)}, clear=False):
                self.assertEqual(resolve_or_create_bridge_token(), TOKEN)
                self.assertEqual(read_bridge_token(), TOKEN)

    def test_generates_writes_and_preserves_unrelated_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(self._config(), encoding="utf-8")
            with patch.dict(os.environ, {"CBT_CONFIG_PATH": str(path)}, clear=False):
                token = resolve_or_create_bridge_token()
                self.assertGreaterEqual(len(token), 32)
                self.assertEqual(read_bridge_token(), token)
            updated = path.read_text(encoding="utf-8")
            self.assertEqual(
                tomllib.loads(updated)["mcp_servers"]["codex-blender"]["env"]["CBT_BRIDGE_TOKEN"],
                token,
            )
            self.assertIn('[mcp_servers.other]\ncommand = "other"', updated)
            self.assertIn('[unrelated]\nvalue = "preserve me"', updated)
            self.assertNotIn("CBT_BRIDGE_TOKENS", updated)
            self.assertTrue(path.with_name("config.toml.bak-codex-blender").is_file())

    def test_reads_existing_token_from_env_child_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = self._child_table_config(TOKEN)
            path.write_text(original, encoding="utf-8")

            self.assertEqual(resolve_or_create_bridge_token(path), TOKEN)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(path.with_name("config.toml.bak-codex-blender").exists())

    def test_generates_token_in_env_child_table_and_preserves_other_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(self._child_table_config(), encoding="utf-8")

            token = resolve_or_create_bridge_token(path)
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(
                parsed["mcp_servers"]["codex-blender"]["env"]["CBT_BRIDGE_TOKEN"],
                token,
            )
            self.assertEqual(parsed["unrelated"]["value"], "preserve me")
            self.assertTrue(path.with_name("config.toml.bak-codex-blender").is_file())

    def test_write_replaces_child_table_token_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(self._child_table_config(TOKEN), encoding="utf-8")
            replacement = "b" * 43

            write_bridge_token(replacement, path)

            self.assertEqual(read_bridge_token(path), replacement)
            self.assertEqual(path.read_text(encoding="utf-8").count("CBT_BRIDGE_TOKEN"), 1)

    def test_write_does_not_rewrite_feature_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            features = "[features]\nmemories = true\n\n[features.context_management]\nexperimental_mode = true\n\n"
            path.write_text(features + self._config(), encoding="utf-8")

            write_bridge_token(TOKEN, path)

            updated = path.read_text(encoding="utf-8")
            self.assertTrue(updated.startswith(features))
            self.assertEqual(tomllib.loads(updated)["features"]["context_management"], {"experimental_mode": True})

    def test_commented_child_table_token_is_not_treated_as_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            content = self._child_table_config().replace(
                'PYTHONPATH = "C:/adapter"\n',
                f'PYTHONPATH = "C:/adapter"\n# CBT_BRIDGE_TOKEN = "{TOKEN}"\n',
            )
            path.write_text(content, encoding="utf-8")

            token = resolve_or_create_bridge_token(path)

            self.assertNotEqual(token, TOKEN)
            self.assertEqual(read_bridge_token(path), token)

    def test_new_registration_reads_and_rotates_inline_and_child_tokens(self) -> None:
        for fixture in (self._config, self._child_table_config):
            with self.subTest(fixture=fixture.__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                original = fixture(TOKEN).replace("mcp_servers.codex-blender", "mcp_servers.blenderboo")
                path.write_text(original, encoding="utf-8")
                self.assertEqual(resolve_or_create_bridge_token(path), TOKEN)

                replacement = "n" * 43
                write_bridge_token(replacement, path)
                parsed = tomllib.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(parsed["mcp_servers"]["blenderboo"]["env"]["CBT_BRIDGE_TOKEN"], replacement)
                self.assertEqual(parsed["unrelated"]["value"], "preserve me")
                self.assertNotIn("codex-blender", parsed["mcp_servers"])
                self.assertEqual(path.with_name("config.toml.bak-blenderboo").read_text(encoding="utf-8"), original)

    def test_new_registration_wins_and_rotation_preserves_legacy_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            new_token = "n" * 43
            new_table = '[mcp_servers.blenderboo]\n' + f'env = {{ CBT_BRIDGE_TOKEN = "{new_token}" }}\n'
            path.write_text(self._config(TOKEN) + new_table, encoding="utf-8")
            self.assertEqual(read_bridge_token(path), new_token)

            replacement = "r" * 43
            write_bridge_token(replacement, path)
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]
            self.assertEqual(parsed["blenderboo"]["env"]["CBT_BRIDGE_TOKEN"], replacement)
            self.assertEqual(parsed["codex-blender"]["env"]["CBT_BRIDGE_TOKEN"], TOKEN)

    def test_new_registration_without_token_is_active_even_with_legacy_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            new_table = '[mcp_servers.blenderboo]\nenv = { PYTHONPATH = "C:/new-adapter" }\n'
            path.write_text(self._config(TOKEN) + new_table, encoding="utf-8")
            self.assertIsNone(read_bridge_token(path))

            created = resolve_or_create_bridge_token(path)
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]
            self.assertNotEqual(created, TOKEN)
            self.assertEqual(parsed["blenderboo"]["env"]["CBT_BRIDGE_TOKEN"], created)
            self.assertEqual(parsed["codex-blender"]["env"]["CBT_BRIDGE_TOKEN"], TOKEN)

    def test_invalid_new_token_fails_without_exposing_it_or_using_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            invalid = "invalid-secret"
            new_table = '[mcp_servers.blenderboo]\n' + f'env = {{ CBT_BRIDGE_TOKEN = "{invalid}" }}\n'
            original = self._config(TOKEN) + new_table
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(ConfigTokenError) as raised:
                resolve_or_create_bridge_token(path)
            self.assertNotIn(invalid, str(raised.exception))
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_auto_start_failure_is_never_raised_from_enable_hook(self) -> None:
        with (
            patch.object(blenderboo, "bridge_status", return_value={"running": False}),
            patch("blenderboo.config_token.resolve_or_create_bridge_token", side_effect=RuntimeError("config unavailable")),
            patch.dict(
                sys.modules,
                {"blenderboo.bridge_ui": SimpleNamespace(auto_start_enabled=lambda: True)},
            ),
        ):
            blenderboo._auto_start_bridge()

    def test_auto_start_uses_the_config_token(self) -> None:
        with (
            patch.object(blenderboo, "bridge_status", return_value={"running": False}),
            patch.object(blenderboo, "start_bridge") as start,
            patch("blenderboo.config_token.resolve_or_create_bridge_token", return_value=TOKEN),
            patch.dict(
                sys.modules,
                {"blenderboo.bridge_ui": SimpleNamespace(auto_start_enabled=lambda: True)},
            ),
        ):
            blenderboo._auto_start_bridge()
        start.assert_called_once_with(TOKEN, token_location="config")


if __name__ == "__main__":
    unittest.main()
