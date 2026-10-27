"""Safe access to the bridge token stored in Codex's MCP configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import threading


TARGET_TABLE = "mcp_servers.blenderboo"
LEGACY_TABLE = "mcp_servers.codex-blender"
TOKEN_ENV = "CBT_BRIDGE_TOKEN"
CONFIG_PATH_ENV = "CBT_CONFIG_PATH"
_TOKEN_PAIR_RE = re.compile(r"(?P<name>CBT_BRIDGE_TOKENS?|CBT_BRIDGE_TOKEN)\s*=\s*(?P<value>\"(?:[^\"\\\\]|\\\\.)*\")")
_ENV_LINE_RE = re.compile(r"^(?P<prefix>\s*env\s*=\s*\{)(?P<body>.*)(?P<suffix>\}\s*(?:#.*)?(?:\r?\n)?)$")


class ConfigTokenError(RuntimeError):
    """A token-config error whose message never includes a token value."""


def config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex" / "config.toml"


def _table_span(text: str, table: str = TARGET_TABLE) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip().split("#", 1)[0].strip()
        if stripped == f"[{table}]":
            if start is not None:
                raise ConfigTokenError(f"Codex config contains more than one [{table}] table")
            start = index
    if start is None:
        raise ConfigTokenError(f"Codex config has no [{table}] table")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip().split("#", 1)[0].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    return start, end


def _optional_table_span(text: str, table: str) -> tuple[int, int] | None:
    try:
        return _table_span(text, table)
    except ConfigTokenError as exc:
        if str(exc) == f"Codex config has no [{table}] table":
            return None
        raise


def _active_table(text: str) -> str:
    """Prefer the new registration; use legacy only when it is absent."""
    if _optional_table_span(text, TARGET_TABLE) is not None:
        return TARGET_TABLE
    _table_span(text, LEGACY_TABLE)
    return LEGACY_TABLE


def _validate_token(token: object) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 4096:
        raise ConfigTokenError("Codex bridge token must contain 32 to 4096 characters")
    return token


def _token_from_table(lines: list[str]) -> str | None:
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        match = _TOKEN_PAIR_RE.search(line)
        if match is None or match.group("name") != TOKEN_ENV:
            continue
        try:
            return _validate_token(json.loads(match.group("value")))
        except (json.JSONDecodeError, ConfigTokenError) as exc:
            raise ConfigTokenError("Codex bridge token is invalid") from exc
    return None


def read_bridge_token(path: Path | None = None) -> str | None:
    """Return the existing config token, or ``None`` when the table has none."""
    target = path or config_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigTokenError("Codex config.toml does not exist") from None
    except OSError as exc:
        raise ConfigTokenError("Could not read Codex config.toml") from exc
    lines = text.splitlines(keepends=True)
    table = _active_table(text)
    start, end = _table_span(text, table)
    token = _token_from_table(lines[start:end])
    if token is not None:
        return token
    env_span = _optional_table_span(text, f"{table}.env")
    if env_span is None:
        return None
    env_start, env_end = env_span
    return _token_from_table(lines[env_start:env_end])


def _write_exact_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_bridge_token(token: str, path: Path | None = None) -> Path:
    """Back up and update only the active BlenderBoo config table."""
    token = _validate_token(token)
    target = path or config_path()
    try:
        with target.open("r", encoding="utf-8", newline="") as handle:
            original = handle.read()
    except FileNotFoundError:
        raise ConfigTokenError("Codex config.toml does not exist") from None
    except OSError as exc:
        raise ConfigTokenError("Could not read Codex config.toml") from exc

    lines = original.splitlines(keepends=True)
    table = _active_table(original)
    start, end = _table_span(original, table)
    replacement = json.dumps(token, ensure_ascii=True)
    changed = False
    rewritten: list[str] = []
    for line in lines[start:end]:
        env_line = _ENV_LINE_RE.match(line)
        if env_line is None:
            rewritten.append(line)
            continue
        body = _TOKEN_PAIR_RE.sub("", env_line.group("body"))
        body = re.sub(r",\s*,", ",", body).strip().strip(",").strip()
        separator = ", " if body else ""
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        comment = ""
        suffix = env_line.group("suffix")
        if "#" in suffix:
            comment = " " + suffix[suffix.index("#") :].rstrip("\r\n")
        rewritten.append(f"{env_line.group('prefix')} {body}{separator}{TOKEN_ENV} = {replacement} }}{comment}{newline}")
        changed = True
    if changed:
        updated_lines = lines[:start] + rewritten + lines[end:]
    else:
        env_span = _optional_table_span(original, f"{table}.env")
        if env_span is None:
            raise ConfigTokenError(
                "Codex bridge table has neither an inline env mapping nor an env child table to update"
            )
        env_start, env_end = env_span
        env_lines = list(lines[env_start:env_end])
        token_replaced = False
        for index, line in enumerate(env_lines):
            if line.lstrip().startswith("#"):
                continue
            match = _TOKEN_PAIR_RE.search(line)
            if match is None or match.group("name") != TOKEN_ENV:
                continue
            env_lines[index] = (
                line[: match.start()]
                + f"{TOKEN_ENV} = {replacement}"
                + line[match.end() :]
            )
            token_replaced = True
            break
        if not token_replaced:
            newline = "\r\n" if "\r\n" in original else "\n"
            if env_lines and not env_lines[-1].endswith(("\n", "\r")):
                env_lines[-1] += newline
            env_lines.append(f"{TOKEN_ENV} = {replacement}{newline}")
        updated_lines = lines[:env_start] + env_lines + lines[env_end:]

    updated = "".join(updated_lines)
    backup = target.with_name(target.name + ".bak-" + table.rsplit(".", 1)[-1])
    try:
        shutil.copy2(target, backup)
        _write_exact_text(target, updated)
        if read_bridge_token(target) != token:
            raise ConfigTokenError("Written Codex bridge token failed verification; restore its backup")
    except ConfigTokenError:
        raise
    except OSError as exc:
        raise ConfigTokenError("Could not update Codex config.toml") from exc
    return target


def resolve_or_create_bridge_token(path: Path | None = None) -> str:
    """Read the authoritative token or create and persist a new one."""
    token = read_bridge_token(path)
    if token is not None:
        return token
    import secrets

    token = secrets.token_urlsafe(32)
    write_bridge_token(token, path)
    return token
