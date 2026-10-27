"""Non-overwriting active-document version saves."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ..errors import InvalidParamsError, InvalidStateError, MutationApplyError
from ._blender import get_bpy
from .checkpoint import _sanitize_tag
from .session_lease import follow_active_document, require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token
from .destructive import prepare_destructive_action
from .transaction import new_transaction


_TRAILING_VERSION_RE = re.compile(r"(?:[_-](?:v|version)[_-]?\d+)$", re.IGNORECASE)
_observed_disk: tuple[str, int, int] | None = None


def observe_loaded_file(*_args: Any) -> None:
    """Anchor the active file's disk identity immediately after Blender loads it.

    Register this for load_post and call it once during add-on registration.
    A later first call to save_current must not invent its own baseline.
    """
    global _observed_disk
    state = file_state(get_bpy())
    _observed_disk = _disk_identity(state)


def _disk_identity(state: dict[str, Any]) -> tuple[str, int, int] | None:
    if not state["disk_exists"]:
        return None
    return (state["filepath"], state["disk_mtime_ns"], state["disk_size"])


def _remember_save(state: dict[str, Any]) -> None:
    global _observed_disk
    _observed_disk = _disk_identity(state)


def file_state(bpy: Any) -> dict[str, Any]:
    """Return Blender state and independently observed on-disk metadata."""
    filepath = str(bpy.data.filepath or "")
    path = Path(filepath).resolve() if filepath else None
    try:
        stat = path.stat() if path is not None else None
    except OSError:
        stat = None
    return {
        "filepath": str(path) if path is not None else "",
        "has_filepath": path is not None,
        "blender_dirty": bool(bpy.data.is_dirty),
        "disk_exists": stat is not None,
        "disk_mtime_ns": int(stat.st_mtime_ns) if stat is not None else None,
        "disk_size": int(stat.st_size) if stat is not None else None,
    }


def save_current(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"dry_run", "confirm", "expected_state"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    before = file_state(bpy)
    safe_in_place = _disk_identity(before) is not None and _disk_identity(before) == _observed_disk
    expected_state = params.get("expected_state")
    if expected_state is not None and (not isinstance(expected_state, str) or not expected_state):
        raise InvalidParamsError("expected_state must be a non-empty string")
    explicit_confirm = params.get("confirm") is True and expected_state is not None
    dry_run = params.get("dry_run", not safe_in_place and not explicit_confirm)
    if not isinstance(dry_run, bool):
        raise InvalidParamsError("dry_run must be a boolean")
    if not before["has_filepath"]:
        raise InvalidStateError("Current document has no filepath; use scene.save_version or Blender Save As first")
    journal = [{"op": "save_current", "target": before["filepath"], "status": "planned"}]
    transaction, apply = prepare_destructive_action(
        action="scene.save_current",
        affected_datablocks=[{"type": "BlendFile", "name": Path(before["filepath"]).name}],
        journal=journal,
        dry_run=dry_run,
        confirm=params.get("confirm"),
        require_confirm=not safe_in_place,
        state_token=current_state_token(bpy),
        extra={"before": before, "disk_unchanged_since_load_or_save": safe_in_place},
    )
    if not apply:
        return transaction
    require_document_write_access(bpy)
    if safe_in_place and _disk_identity(file_state(bpy)) != _disk_identity(before):
        raise InvalidStateError("The active .blend changed on disk before saving; inspect it and use confirm: true")
    operator_error = None
    try:
        result = bpy.ops.wm.save_mainfile(filepath=before["filepath"], check_existing=False)
    except Exception as exc:
        result = None
        operator_error = exc
    after = file_state(bpy)
    disk_metadata_changed = (
        not before["disk_exists"] and after["disk_exists"]
        or before["disk_mtime_ns"] != after["disk_mtime_ns"]
        or before["disk_size"] != after["disk_size"]
    )
    write_confirmed = (
        after["disk_exists"] and
        (result is not None and "FINISHED" in result or disk_metadata_changed)
    )
    if not write_confirmed:
        transaction.update(
            changed=before["blender_dirty"] != after["blender_dirty"],
            after=after,
            operator_result=sorted(result) if result is not None else None,
            write_confirmed=False,
            dirty_confirmed=not after["blender_dirty"],
            partial_change_possible=operator_error is not None,
        )
        if transaction["changed"]:
            bump_epoch()
        transaction["state_token"] = current_state_token(bpy)
        message = f"scene.save_current operator failed: {operator_error}" if operator_error else "Blender did not confirm an in-place disk save"
        raise MutationApplyError(message, transaction) from operator_error
    journal[0]["status"] = "applied"
    transaction.update(
        changed=True,
        after=after,
        operator_result=sorted(result) if result is not None else None,
        operator_error=str(operator_error) if operator_error is not None else None,
        write_confirmed=True,
        dirty_confirmed=not after["blender_dirty"],
        disk_metadata_changed=disk_metadata_changed,
    )
    _remember_save(after)
    follow_active_document(bpy)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction


def save_as(params: dict[str, Any]) -> dict[str, Any]:
    """Save the active document to an explicit .blend path, including its first save."""
    unknown = sorted(set(params) - {"path", "dry_run", "overwrite", "confirm", "expected_state"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    supplied = params.get("path")
    if not isinstance(supplied, str) or not supplied.strip():
        raise InvalidParamsError("path must be a non-empty .blend filepath")
    target = Path(supplied).expanduser().resolve()
    if target.suffix.lower() != ".blend" or target.exists() and target.is_dir():
        raise InvalidParamsError("path must name a .blend file")
    dry_run = params.get("dry_run", True)
    overwrite = params.get("overwrite", False)
    if not isinstance(dry_run, bool) or not isinstance(overwrite, bool):
        raise InvalidParamsError("dry_run and overwrite must be booleans")
    bpy = get_bpy()
    check_expected_state(params.get("expected_state"), bpy)
    before = file_state(bpy)
    existed = target.exists()
    target_before = target.stat() if existed and target.is_file() else None
    if existed and not overwrite:
        raise InvalidStateError(f"Refusing to overwrite existing file: {target}")
    plan = {
        "path": str(target), "old_path": before["filepath"],
        "would_overwrite": existed, "dry_run": dry_run, "changed": False,
        "state_token": current_state_token(bpy),
    }
    if dry_run:
        return plan
    if existed and params.get("confirm") is not True:
        raise InvalidParamsError("confirm: true is required to overwrite an existing .blend")
    require_document_write_access(bpy)
    target.parent.mkdir(parents=True, exist_ok=True)
    operator_error = None
    try:
        operator_result = bpy.ops.wm.save_as_mainfile(
            filepath=str(target), copy=False, check_existing=False
        )
    except Exception as exc:
        operator_result = None
        operator_error = exc
    after = file_state(bpy)
    target_after = target.stat() if target.is_file() else None
    disk_changed = target_after is not None and (target_before is None or
        target_after.st_mtime_ns != target_before.st_mtime_ns or
        target_after.st_size != target_before.st_size)
    write_confirmed = target_after is not None and (
        operator_result is not None and "FINISHED" in operator_result or disk_changed
    )
    if not write_confirmed:
        data = {**plan, "after": after, "operator_result": sorted(operator_result) if operator_result else None,
                "write_confirmed": False, "partial_change_possible": operator_error is not None}
        message = f"scene.save_as operator failed: {operator_error}" if operator_error else "Blender did not confirm scene.save_as"
        raise MutationApplyError(message, data) from operator_error
    _remember_save(after)
    follow_active_document(bpy)
    bump_epoch()
    return {
        **plan, "dry_run": False, "changed": True,
        "after": after, "write_confirmed": True,
        "dirty_confirmed": not after["blender_dirty"],
        "operator_result": sorted(operator_result) if operator_result else None,
        "operator_error": str(operator_error) if operator_error else None,
        "state_token": current_state_token(bpy),
    }


def _base_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidParamsError("base must be a non-empty filename stem")
    candidate = value.strip()
    if Path(candidate).name != candidate or candidate in {".", ".."}:
        raise InvalidParamsError("base must be a filename stem, not a path")
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", candidate).strip("-_")
    if not sanitized:
        raise InvalidParamsError("base must contain at least one letter or number")
    return _TRAILING_VERSION_RE.sub("", sanitized) or sanitized


def _version_context(filepath: str, base: Any = None) -> tuple[Path, str]:
    if not filepath:
        raise InvalidStateError("Save the current .blend file before saving a version")
    current = Path(filepath).resolve()
    return current, _base_name(current.stem if base is None else base)


def _allocate_version_path(directory: Path, base: str, tag: str | None) -> tuple[Path, int]:
    expression = re.compile(rf"^{re.escape(base)}_v(?P<index>\d{{3,}})(?:_[A-Za-z0-9_-]+)?\.blend$", re.IGNORECASE)
    indices = []
    if directory.is_dir():
        for path in directory.iterdir():
            match = expression.match(path.name)
            if path.is_file() and match:
                indices.append(int(match.group("index")))
    index = max(indices, default=0) + 1
    suffix = f"_{tag}" if tag else ""
    path = directory / f"{base}_v{index:03d}{suffix}.blend"
    while path.exists():
        index += 1
        path = directory / f"{base}_v{index:03d}{suffix}.blend"
    return path, index


def save_version(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"base", "tag", "expected_state"})
    if unknown:
        raise InvalidParamsError(f"Unknown parameters: {', '.join(unknown)}")
    if "base" in params and params["base"] is None:
        raise InvalidParamsError("base must be a non-empty filename stem")
    if "expected_state" in params and (
        not isinstance(params["expected_state"], str) or not params["expected_state"]
    ):
        raise InvalidParamsError("expected_state must be a non-empty string")
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    tag = None if params.get("tag") is None else _sanitize_tag(params["tag"])
    old_path, base = _version_context(bpy.data.filepath, params.get("base"))
    new_path, index = _allocate_version_path(old_path.parent, base, tag)
    transaction = new_transaction([{"type": "BlendFile", "name": old_path.name}])
    transaction.update(old_path=str(old_path), new_path=str(new_path.resolve()), version=index)
    result = bpy.ops.wm.save_as_mainfile(filepath=str(new_path), copy=False, check_existing=False)
    if "FINISHED" not in result or not new_path.is_file():
        raise InvalidStateError(f"Blender did not create version file: {new_path}")
    transaction.update(changed=True, before={"active_path": str(old_path)}, after={"active_path": str(new_path.resolve())})
    _remember_save(file_state(bpy))
    follow_active_document(bpy)
    bump_epoch()
    transaction["state_token"] = current_state_token(bpy)
    return transaction
