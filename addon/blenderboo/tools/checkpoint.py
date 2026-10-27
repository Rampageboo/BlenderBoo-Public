"""Non-overwriting Blender checkpoint management."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from string import Formatter
from typing import Any

from ..errors import ConfirmationRequiredError, InvalidParamsError, InvalidStateError, NotFoundError
from ._blender import get_bpy
from .session_lease import follow_active_document, require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .transaction import new_transaction

_CHECKPOINT_RE = re.compile(r"^(?P<base>.+)_ckpt_(?P<index>\d{3,})_(?P<tag>[A-Za-z0-9_-]+)\.blend$")
_DEFAULT_TEMPLATE = "{base}_ckpt_{index:03d}_{tag}.blend"


def _template(value: Any) -> str:
    if value is None:
        return _DEFAULT_TEMPLATE
    if not isinstance(value, str) or not value.endswith(".blend") or Path(value).name != value or len(value) > 240:
        raise InvalidParamsError("name_template must be a .blend filename template")
    fields = []
    for _, field, spec, conversion in Formatter().parse(value):
        if field is None:
            continue
        if field not in {"base", "index", "tag"} or conversion is not None:
            raise InvalidParamsError("name_template supports only base, index, and tag fields")
        if spec not in ({"", "03d"} if field == "index" else {""}):
            raise InvalidParamsError("name_template supports only {index} or {index:03d} formatting")
        fields.append(field)
    if len(fields) != len(set(fields)):
        raise InvalidParamsError("name_template fields must not repeat")
    return value


def _template_pattern(template: str) -> re.Pattern[str]:
    pieces = []
    for literal, field, _, _ in Formatter().parse(template):
        pieces.append(re.escape(literal))
        if field == "base":
            pieces.append(r"(?P<base>.+?)")
        elif field == "index":
            pieces.append(r"(?P<index>\d+)")
        elif field == "tag":
            pieces.append(r"(?P<tag>[A-Za-z0-9_-]+)")
    return re.compile("^" + "".join(pieces) + "$")


def _asset(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise InvalidParamsError("asset must be a 1-128 character identifier using letters, digits, _, ., or -")
    return value


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".cbt.json")


def _directory(value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise InvalidParamsError("directory must be an absolute path")
    return Path(value).resolve()


def _context(filepath: str, directory: Path | None = None, template: str = _DEFAULT_TEMPLATE) -> tuple[Path, str]:
    if not filepath:
        raise InvalidStateError("Save the current .blend file before using checkpoints")
    current = Path(filepath).resolve()
    match = _template_pattern(template).match(current.name)
    if match and (directory is not None and current.parent == directory or current.parent.name == "checkpoints"):
        return directory or current.parent, match.groupdict().get("base") or current.stem
    return directory or current.parent / "checkpoints", current.stem


def _sanitize_tag(value: Any) -> str:
    if value is None:
        return "checkpoint"
    if not isinstance(value, str):
        raise InvalidParamsError("tag must be a string")
    tag = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_")
    if not tag:
        raise InvalidParamsError("tag must contain at least one letter or number")
    return tag[:64]


def _entries(directory: Path, base: str, template: str = _DEFAULT_TEMPLATE,
             asset: str | None = None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not directory.is_dir():
        return entries
    fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
    pattern = _template_pattern(template)
    paths = directory.glob("*.blend") if asset is not None else directory.iterdir()
    for path in paths:
        if not path.is_file() or path.suffix.lower() != ".blend":
            continue
        match = pattern.match(path.name)
        if asset is not None:
            try:
                metadata = json.loads(_sidecar(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(metadata, dict) or metadata.get("asset") != asset:
                continue
        elif not match or ("base" in fields and match.group("base") != base):
            continue
        stat = path.stat()
        entries.append({
            "name": path.name,
            "path": str(path.resolve()),
            "index": int(match.group("index")) if match and "index" in fields else None,
            "tag": match.group("tag") if match and "tag" in fields else None,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    return sorted(entries, key=(lambda item: (item["index"], item["name"])) if "index" in fields and asset is None
                  else (lambda item: (item["modified_at"], item["name"])))


def save_checkpoint(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"tag", "directory", "name_template", "asset", "keep_latest", "preview_only", "prune", "confirm", "expected_state"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    template = _template(params.get("name_template"))
    fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
    asset = _asset(params.get("asset"))
    directory, base = _context(bpy.data.filepath, _directory(params.get("directory")), template)
    if "index" not in fields and params.get("keep_latest") is not None and asset is None:
        raise InvalidParamsError("asset is required for retention when name_template has no index")
    existing = _entries(directory, base, template, asset)
    index = max((entry["index"] or 0 for entry in existing), default=0) + 1
    tag = _sanitize_tag(params.get("tag"))
    path = directory / template.format(base=base, index=index, tag=tag)
    while "index" in fields and path.exists():
        index += 1
        path = directory / template.format(base=base, index=index, tag=tag)
    if path.resolve() == Path(bpy.data.filepath).resolve():
        raise InvalidParamsError("checkpoint destination cannot overwrite the active document")
    keep_latest = params.get("keep_latest")
    if keep_latest is not None and (isinstance(keep_latest, bool) or not isinstance(keep_latest, int) or not 1 <= keep_latest <= 10_000):
        raise InvalidParamsError("keep_latest must be an integer from 1 to 10000")
    preview_only = params.get("preview_only", False)
    prune = params.get("prune", False)
    if not isinstance(preview_only, bool) or not isinstance(prune, bool):
        raise InvalidParamsError("preview_only and prune must be booleans")
    if prune and keep_latest is None:
        raise InvalidParamsError("prune requires keep_latest")
    if prune and not preview_only and params.get("confirm") is not True:
        raise ConfirmationRequiredError("checkpoint.save pruning requires confirm: true")
    if "confirm" in params and not isinstance(params["confirm"], bool):
        raise InvalidParamsError("confirm must be a boolean")
    overwrite = path.exists()
    if overwrite and not preview_only and params.get("confirm") is not True:
        raise ConfirmationRequiredError("checkpoint.save destination exists; confirm: true is required to overwrite it")
    projected = [entry for entry in existing if Path(entry["path"]) != path.resolve()]
    projected.append({"name": path.name, "path": str(path),
                      "index": index if "index" in fields else None,
                      "tag": tag if "tag" in fields else None})
    prune_preview = projected[:-keep_latest] if keep_latest is not None else []
    if preview_only:
        return {"path": str(path), "name": path.name, "index": index if "index" in fields else None,
                "tag": tag if "tag" in fields else None,
                "directory": str(directory), "name_template": template, "asset": asset,
                "overwrite": overwrite,
                "prune_preview": prune_preview, "saved": False, "pruned": []}
    require_document_write_access(bpy)
    directory.mkdir(parents=True, exist_ok=True)
    result = bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, check_existing=False)
    if "FINISHED" not in result or not path.is_file():
        raise InvalidStateError(f"Blender did not create checkpoint: {path}")
    if asset is not None:
        metadata = _sidecar(path)
        temporary = metadata.with_suffix(metadata.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps({"asset": asset}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(metadata)
        except OSError as exc:
            raise InvalidStateError(f"Checkpoint saved at {path}, but asset metadata failed: {exc}") from exc
    pruned = []
    warnings = []
    if prune:
        for entry in prune_preview:
            try:
                Path(entry["path"]).unlink()
                _sidecar(Path(entry["path"])).unlink(missing_ok=True)
                pruned.append(entry["name"])
            except OSError as exc:
                warnings.append(f"Could not prune {entry['name']}: {exc}")
    return {"path": str(path.resolve()), "name": path.name,
            "index": index if "index" in fields else None,
            "tag": tag if "tag" in fields else None,
            "directory": str(directory), "name_template": template, "asset": asset,
            "overwrite": overwrite,
            "prune_preview": prune_preview, "saved": True, "pruned": pruned, "warnings": warnings}


def list_checkpoints(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"directory", "name_template", "asset"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    bpy = get_bpy()
    template = _template(params.get("name_template"))
    asset = _asset(params.get("asset"))
    directory, base = _context(bpy.data.filepath, _directory(params.get("directory")), template)
    entries = _entries(directory, base, template, asset)
    return {"directory": str(directory.resolve()), "base": base, "name_template": template,
            "asset": asset, "count": len(entries), "checkpoints": entries}


def restore_checkpoint(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"name", "confirm", "expected_state", "directory", "name_template", "asset"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")
    if params.get("confirm") is not True:
        raise ConfirmationRequiredError("checkpoint.restore requires confirm: true; restoring discards unsaved state")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidParamsError("name must be a checkpoint filename")
    if Path(name).name != name:
        raise InvalidParamsError("name must be a filename, not a path")

    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    old_path = str(bpy.data.filepath or "")
    template = _template(params.get("name_template"))
    asset = _asset(params.get("asset"))
    directory, base = _context(bpy.data.filepath, _directory(params.get("directory")), template)
    available = {entry["name"]: entry for entry in _entries(directory, base, template, asset)}
    entry = available.get(name)
    if entry is None:
        raise NotFoundError(f"Checkpoint not found for {base}: {name}")
    transaction = new_transaction(
        [{"type": "BlendFile", "name": Path(old_path).name}]
    )
    result = bpy.ops.wm.open_mainfile(filepath=entry["path"], load_ui=False)
    if "FINISHED" not in result:
        raise InvalidStateError(f"Blender did not restore checkpoint: {entry['path']}")
    transaction.update(
        path=entry["path"],
        name=entry["name"],
        restored=True,
        changed=True,
        before={"active_path": old_path},
        after={"active_path": str(bpy.data.filepath or entry["path"])},
    )
    follow_active_document(bpy)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
