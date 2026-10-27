"""Configure Codex for bridges started from the toolkit's Blender panel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import getpass
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib
import threading
from typing import Any

TARGET_TABLE = "mcp_servers.blenderboo"

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SRC = (REPO_ROOT / "mcp_server" / "src").resolve()


class BootstrapError(RuntimeError):
    """A safe, one-line error that never contains bridge tokens."""


def _header_name(line: str) -> str | None:
    stripped = line.strip()
    if "#" in stripped:
        stripped = stripped.split("#", 1)[0].rstrip()
    if stripped.startswith("[[") and stripped.endswith("]]" ):
        return stripped[2:-2].strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1].strip()
    return None


def rewrite_codex_config(original: str, replacement_table: str) -> str:
    """Replace only the target table span, preserving every unrelated byte."""
    lines = original.splitlines(keepends=True)
    headers = [
        (index, header)
        for index, line in enumerate(lines)
        if (header := _header_name(line)) is not None
    ]
    target_starts = [index for index, header in headers if header == TARGET_TABLE]
    if len(target_starts) > 1:
        raise BootstrapError(f"Config contains more than one [{TARGET_TABLE}] table")

    if not replacement_table.endswith(("\n", "\r")):
        replacement_table += "\n"
    related_ranges: list[tuple[int, int]] = []
    for header_index, (start, header) in enumerate(headers):
        if header == TARGET_TABLE or header.startswith(TARGET_TABLE + "."):
            end = headers[header_index + 1][0] if header_index + 1 < len(headers) else len(lines)
            related_ranges.append((start, end))
    if related_ranges:
        insertion_index = related_ranges[0][0]
        range_by_start = dict(related_ranges)
        output: list[str] = []
        index = 0
        while index < len(lines):
            end = range_by_start.get(index)
            if end is not None:
                if index == insertion_index:
                    output.append(replacement_table)
                index = end
                continue
            output.append(lines[index])
            index += 1
        return "".join(output)

    if not original:
        return replacement_table
    newline = "\r\n" if "\r\n" in original else "\n"
    separator = newline if original.endswith(("\n", "\r")) else newline + newline
    return original + separator + replacement_table


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _validate_tokens(tokens: Mapping[str, str]) -> dict[str, str]:
    if not tokens:
        raise BootstrapError("No Blender instance tokens were supplied")
    normalized: dict[str, str] = {}
    for instance_id, token in tokens.items():
        if not isinstance(instance_id, str) or not instance_id:
            raise BootstrapError("Instance token ids must be non-empty strings")
        if not isinstance(token, str) or not 32 <= len(token) <= 4096:
            raise BootstrapError(
                f"Token for Blender instance {instance_id!r} must contain 32 to 4096 characters"
            )
        normalized[instance_id] = token
    return normalized


def build_config_table(
    python_executable: str,
    mcp_src: Path,
    tokens: Mapping[str, str],
    registry_path: Path,
    newline: str = "\n",
) -> str:
    """Build the managed multi-instance TOML table. Callers must not log it."""
    token_json = json.dumps(_validate_tokens(tokens), ensure_ascii=True, sort_keys=True)
    lines = [
        f"[{TARGET_TABLE}]",
        f"command = {_toml_string(python_executable)}",
        'args = ["-m", "blenderboo_mcp.server"]',
        "env = { "
        f"PYTHONPATH = {_toml_string(str(mcp_src))}, "
        f"CBT_INSTANCE_REGISTRY = {_toml_string(str(registry_path))}, "
        f"CBT_BRIDGE_TOKENS = {_toml_string(token_json)} "
        "}",
        "",
    ]
    return newline.join(lines)


def _read_exact_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_exact_text(path: Path, content: str) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    primary_error: OSError | None = None
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except OSError as exc:
        primary_error = exc
    cleanup_error: OSError | None = None
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        cleanup_error = exc
    if primary_error is not None:
        if cleanup_error is not None:
            raise OSError(f"{primary_error}; temporary cleanup failed: {cleanup_error}") from primary_error
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error


def _verified_config_text(
    original: str,
    tokens: Mapping[str, str],
    registry_path: Path,
) -> str:
    normalized_tokens = _validate_tokens(tokens)
    if original:
        try:
            tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            raise BootstrapError("Codex config.toml is invalid TOML; repair it before bootstrapping") from exc
    newline = "\r\n" if "\r\n" in original else "\n"
    replacement = build_config_table(
        sys.executable,
        MCP_SRC,
        normalized_tokens,
        registry_path,
        newline,
    )
    updated = rewrite_codex_config(original, replacement)
    try:
        parsed = tomllib.loads(updated)
        entry = parsed["mcp_servers"]["blenderboo"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise BootstrapError("Could not produce a valid BlenderBoo MCP configuration") from exc
    expected_env = {
        "PYTHONPATH": str(MCP_SRC),
        "CBT_INSTANCE_REGISTRY": str(registry_path),
        "CBT_BRIDGE_TOKENS": json.dumps(
            normalized_tokens,
            ensure_ascii=True,
            sort_keys=True,
        ),
    }
    if (
        entry.get("command") != sys.executable
        or entry.get("args") != ["-m", "blenderboo_mcp.server"]
        or entry.get("env") != expected_env
    ):
        raise BootstrapError("Generated BlenderBoo MCP configuration failed verification")
    return updated


def update_codex_config(
    config_path: Path,
    tokens: Mapping[str, str],
    registry_path: Path,
) -> Path:
    """Back up and update only the managed table in Codex config.toml."""
    try:
        original = _read_exact_text(config_path) if config_path.exists() else ""
        updated = _verified_config_text(original, tokens, registry_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = config_path.with_name(config_path.name + ".bak-blenderboo")
        if config_path.exists():
            shutil.copy2(config_path, backup_path)
        _write_exact_text(config_path, updated)
        durable = _read_exact_text(config_path)
        if durable != updated:
            raise BootstrapError("Written Codex config did not match the verified content; restore its backup")
        tomllib.loads(durable)
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(f"Could not update Codex config at {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BootstrapError("Written Codex config failed TOML verification; restore its backup") from exc
    return config_path


def _load_runtime():
    sys.path.insert(0, str(MCP_SRC))
    try:
        from blenderboo_mcp.client import BridgeClient
        from blenderboo_mcp.instances import load_instances, registry_path
    except ImportError as exc:
        raise BootstrapError("Could not import the repository MCP instance router") from exc
    return BridgeClient, load_instances, registry_path


def _configured_token_map() -> dict[str, str]:
    raw = os.environ.get("CBT_BRIDGE_TOKENS")
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError("CBT_BRIDGE_TOKENS must be a JSON object") from exc
    if not isinstance(payload, Mapping):
        raise BootstrapError("CBT_BRIDGE_TOKENS must be a JSON object")
    return _validate_tokens(payload)


def collect_tokens(instances: Sequence[Any]) -> dict[str, str]:
    """Resolve configured tokens or securely prompt for panel-copied values."""
    configured = _configured_token_map()
    tokens: dict[str, str] = {}
    for instance in instances:
        token = configured.get(instance.id, configured.get(str(instance.port)))
        if token is None and instance.token_location.startswith("env:"):
            token = os.environ.get(instance.token_location[4:])
        if token is None and len(instances) == 1:
            token = os.environ.get("CBT_BRIDGE_TOKEN")
        if token is None:
            token = getpass.getpass(
                f"Paste the panel-copied token for {instance.id} on port {instance.port}: "
            )
        tokens[instance.id] = token
    return _validate_tokens(tokens)


def verify_instances(
    bridge_client: Any,
    instances: Sequence[Any],
    tokens: Mapping[str, str],
) -> None:
    for instance in instances:
        try:
            result = bridge_client(tokens[instance.id], instance.port).call(
                "session.whoami", {}
            )
        except Exception as exc:
            raise BootstrapError(
                f"Could not verify Blender instance {instance.id!r} on port {instance.port}"
            ) from exc
        data = result.get("data") if isinstance(result, Mapping) else None
        bridge = data.get("bridge") if isinstance(data, Mapping) else None
        if (
            not result.get("ok")
            or result.get("tool") != "session.whoami"
            or not isinstance(bridge, Mapping)
            or bridge.get("instance_id") != instance.id
            or bridge.get("port") != instance.port
        ):
            raise BootstrapError(
                f"Blender instance {instance.id!r} returned mismatched identity"
            )


def run_session() -> None:
    BridgeClient, load_instances, get_registry_path = _load_runtime()
    instances = load_instances()
    if not instances:
        raise BootstrapError(
            "No Blender instances are registered; use BlenderBoo > BlenderBoo Bridge > Start Bridge first"
        )
    tokens = collect_tokens(instances)
    verify_instances(BridgeClient, instances, tokens)
    registry = get_registry_path()
    config_path = Path.home() / ".codex" / "config.toml"
    update_codex_config(config_path, tokens, registry)
    print("BlenderBoo multi-instance session ready")
    print(f"Verified instances: {len(instances)}")
    for instance in instances:
        print(f"- {instance.id} on 127.0.0.1:{instance.port}")
    print(f"Codex config updated: {config_path}")
    print("Restart Codex Desktop or reload MCP servers to pick up the instance routes.")


def main() -> int:
    try:
        run_session()
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected session bootstrap failure ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
