"""Build an installable Blender add-on ZIP with the package at archive root."""

from __future__ import annotations

from pathlib import Path
import zipfile

from importlib.util import module_from_spec, spec_from_file_location

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "addon" / "blenderboo"
DIST = ROOT / "dist"
_version_spec = spec_from_file_location("cbt_version", PACKAGE / "version.py")
assert _version_spec and _version_spec.loader
_version_module = module_from_spec(_version_spec)
_version_spec.loader.exec_module(_version_module)
OUTPUT = DIST / f"blenderboo-{_version_module.version_string()}.zip"


def build() -> Path:
    if not (PACKAGE / "__init__.py").is_file():
        raise FileNotFoundError(f"Add-on package is missing: {PACKAGE}")
    DIST.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        OUTPUT.unlink()
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(path for path in PACKAGE.rglob("*") if path.is_file() and path.suffix in {".py", ".json"}):
            archive.write(path, Path(PACKAGE.name) / path.relative_to(PACKAGE))
    return OUTPUT


if __name__ == "__main__":
    output = build()
    print(output.resolve())
