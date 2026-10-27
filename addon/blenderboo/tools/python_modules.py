"""Document-local Python helpers stored in Blender Text datablocks.

Save/reopen retains the helper sources in that .blend. Loading another file
exposes its own Text datablocks; helpers are not carried between documents.
A path supplied to module_put is copied once, not linked to future file loads.
"""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import importlib.abc
import importlib.util
import keyword
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Any, Iterator

from ..errors import InvalidParamsError, InvalidStateError, NotFoundError
from ._blender import get_bpy
from .session_lease import require_document_write_access
from .session_state import bump_epoch, check_expected_state, current_state_token, reconcile


PACKAGE = "cbt_modules"
TEXT_PREFIX = "cbt_module."
VERSION_PREFIX = "cbt_module_version."
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_usage: ContextVar[dict[str, str] | None] = ContextVar("cbt_module_usage", default=None)
_import_override: ContextVar[Any] = ContextVar("cbt_module_import_override", default=None)


def _name(value: Any) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or keyword.iskeyword(value):
        raise InvalidParamsError("name must be one Python identifier without dots")
    return value


def _text_name(name: str) -> str:
    return TEXT_PREFIX + name


def _source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _version_text_name(source: str) -> str:
    # 40 hex digits keep the Blender ID name below the historical 63-byte
    # limit while still using a 160-bit content address.
    return VERSION_PREFIX + _source_hash(source)[:40]


def _ensure_version(bpy: Any, source: str) -> str:
    name = _version_text_name(source)
    version = bpy.data.texts.get(name)
    if version is not None:
        if version.as_string() != source:
            raise InvalidStateError(f"Stored module version collision or edit at {name!r}")
        return name
    version = bpy.data.texts.new(name)
    try:
        version.write(source)
        if version.as_string() != source:
            raise InvalidStateError(f"Blender changed source while storing module version {name!r}")
    except Exception:
        bpy.data.texts.remove(version)
        raise
    return name


def _text(bpy: Any, name: str) -> Any:
    return bpy.data.texts.get(_text_name(name))


def _signature(bpy: Any, name: str) -> tuple[str, int, str] | None:
    text = _text(bpy, name)
    if text is None:
        return None
    uid = getattr(text, "session_uid", None)
    identity = int(uid) if uid is not None else int(text.as_pointer())
    return (_source_hash(text.as_string()), identity, str(getattr(bpy.data, "filepath", "") or ""))


def _drop(name: str) -> None:
    sys.modules.pop(f"{PACKAGE}.{name}", None)
    package = sys.modules.get(PACKAGE)
    if package is not None and hasattr(package, name):
        delattr(package, name)


class _StoredTextImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == PACKAGE:
            return importlib.util.spec_from_loader(fullname, self, is_package=True)
        if not fullname.startswith(PACKAGE + "."):
            return None
        name = fullname[len(PACKAGE) + 1:]
        if not _NAME.fullmatch(name) or _text(get_bpy(), name) is None:
            return None
        return importlib.util.spec_from_loader(fullname, self, origin=f"blend://{_text_name(name)}")

    def create_module(self, spec: Any) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        if module.__name__ == PACKAGE:
            module.__path__ = []
            module.__cbt_package__ = True
            return
        name = module.__name__[len(PACKAGE) + 1:]
        bpy = get_bpy()
        text = _text(bpy, name)
        if text is None:
            raise ModuleNotFoundError(f"Stored Blender module {module.__name__!r} was removed")
        source = text.as_string()
        _ensure_version(bpy, source)
        signature = _signature(bpy, name)
        module.__cbt_signature__ = signature
        module.__file__ = f"blend://{_text_name(name)}"
        wrapper = _import_override.get()
        if wrapper is not None:
            module.__dict__["__builtins__"] = {**vars(builtins), "__import__": wrapper}
        usage = _usage.get()
        if usage is not None:
            usage[name] = signature[0]
        exec(compile(source, module.__file__, "exec"), module.__dict__)


_IMPORTER = _StoredTextImporter()


def _install_importer() -> None:
    if _IMPORTER not in sys.meta_path:
        sys.meta_path.insert(0, _IMPORTER)
    package = sys.modules.get(PACKAGE)
    if package is not None and not getattr(package, "__cbt_package__", False):
        sys.modules.pop(PACKAGE, None)


def _sync_cache(bpy: Any) -> None:
    for fullname, module in tuple(sys.modules.items()):
        if not fullname.startswith(PACKAGE + "."):
            continue
        name = fullname[len(PACKAGE) + 1:]
        if not _NAME.fullmatch(name) or getattr(module, "__cbt_signature__", None) != _signature(bpy, name):
            _drop(name)


@contextmanager
def exec_module_imports(bpy: Any, used: dict[str, str]) -> Iterator[dict[str, Any]]:
    """Provide a per-exec import hook without changing process-wide builtins."""
    _install_importer()
    _sync_cache(bpy)
    original_import = builtins.__import__

    def tracked_import(name: str, globals: Any = None, locals: Any = None,
                       fromlist: Any = (), level: int = 0) -> Any:
        relative_from_package = bool(level and isinstance(globals, dict) and
                                     globals.get("__package__") == PACKAGE)
        if name == PACKAGE or name.startswith(PACKAGE + ".") or relative_from_package:
            _sync_cache(bpy)
        result = original_import(name, globals, locals, fromlist, level)
        names: list[str] = []
        if name.startswith(PACKAGE + "."):
            names.append(name[len(PACKAGE) + 1:].split(".", 1)[0])
        elif name == PACKAGE or relative_from_package:
            names.extend(item for item in (fromlist or ()) if isinstance(item, str) and _NAME.fullmatch(item))
        for item in names:
            signature = _signature(bpy, item)
            if signature is not None:
                used[item] = signature[0]
        return result

    usage_token = _usage.set(used)
    import_token = _import_override.set(tracked_import)
    try:
        yield {**vars(builtins), "__import__": tracked_import}
    finally:
        _import_override.reset(import_token)
        _usage.reset(usage_token)


def module_put(params: dict[str, Any]) -> dict[str, Any]:
    if set(params) - {"name", "code", "path", "expected_state"}:
        raise InvalidParamsError("Unknown parameters for python.module_put")
    name = _name(params.get("name"))
    if ("code" in params) == ("path" in params):
        raise InvalidParamsError("Provide exactly one of code or path")
    source_path = None
    if "path" in params:
        supplied = params["path"]
        if not isinstance(supplied, str) or not supplied.strip():
            raise InvalidParamsError("path must be a non-empty Python file path")
        path = Path(supplied).expanduser().resolve()
        if not path.is_file():
            raise InvalidParamsError(f"Python module path is not a file: {path}")
        try:
            code = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise InvalidParamsError(f"Cannot read Python module path: {path}: {exc}") from exc
        source_path = str(path)
    else:
        code = params["code"]
    if not isinstance(code, str):
        raise InvalidParamsError("code must be a string")
    code = code.replace("\r\n", "\n").replace("\r", "\n")
    try:
        compile(code, f"blend://{_text_name(name)}", "exec")
    except SyntaxError as exc:
        raise InvalidParamsError(f"Module {name!r} has invalid Python syntax: {exc.msg} at line {exc.lineno}") from exc
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    text = _text(bpy, name)
    created = text is None
    previous = text.as_string() if text is not None else None
    if text is not None:
        _ensure_version(bpy, previous)
    version_name = _ensure_version(bpy, code)
    if created:
        text = bpy.data.texts.new(_text_name(name))
    else:
        text.clear()
    try:
        text.write(code)
    except Exception:
        if created:
            bpy.data.texts.remove(text)
        else:
            text.clear()
            text.write(previous)
        raise
    stored = text.as_string()
    if stored != code:
        if created:
            bpy.data.texts.remove(text)
        else:
            text.clear()
            text.write(previous)
        raise InvalidStateError("Blender changed source while storing the active Python module")
    _drop(name)
    reconcile(bpy)
    bump_epoch()
    return {"name": name, "text_name": text.name, "sha256": _source_hash(stored),
            "source_path": source_path,
            "version_text_name": version_name, "version_retention": "unbounded_in_document",
            "created": created, "size_bytes": len(stored.encode("utf-8")),
            "state_token": current_state_token(bpy)}


def module_list(params: dict[str, Any]) -> dict[str, Any]:
    if params:
        raise InvalidParamsError("python.module_list takes no parameters")
    bpy = get_bpy()
    modules = []
    for text in bpy.data.texts:
        if not text.name.startswith(TEXT_PREFIX):
            continue
        name = text.name[len(TEXT_PREFIX):]
        if not _NAME.fullmatch(name):
            continue
        source = text.as_string()
        modules.append({"name": name, "text_name": text.name,
                        "sha256": _source_hash(source), "version_text_name": _version_text_name(source),
                        "size_bytes": len(source.encode("utf-8"))})
    modules.sort(key=lambda item: item["name"])
    return {"count": len(modules), "modules": modules}


def module_remove(params: dict[str, Any]) -> dict[str, Any]:
    if set(params) - {"name", "expected_state"}:
        raise InvalidParamsError("Unknown parameters for python.module_remove")
    name = _name(params.get("name"))
    bpy = get_bpy()
    require_document_write_access(bpy)
    check_expected_state(params.get("expected_state"), bpy)
    text = _text(bpy, name)
    if text is None:
        raise NotFoundError(f"Stored Blender module {name!r} does not exist")
    old_hash = _source_hash(text.as_string())
    _ensure_version(bpy, text.as_string())
    bpy.data.texts.remove(text)
    _drop(name)
    reconcile(bpy)
    bump_epoch()
    return {"name": name, "removed": True, "sha256": old_hash,
            "version_text_name": VERSION_PREFIX + old_hash[:40],
            "state_token": current_state_token(bpy)}
