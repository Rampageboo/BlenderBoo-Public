"""Blender 5.2 smoke for PLY import and checkpoint destination/retention."""

from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
from blenderboo import dispatch


def require_ok(tool, params):
    result = dispatch(tool, params)
    assert result["ok"], result
    return result["data"]


def run():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        project = directory / "scene.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(project))

        header = (
            b"ply\nformat ascii 1.0\nelement vertex 3\n"
            b"property float x\nproperty float y\nproperty float z\n"
            b"property float nx\nproperty float ny\nproperty float nz\n"
            b"property uchar red\nproperty uchar green\nproperty uchar blue\n"
            b"element face 1\nproperty list uchar int vertex_indices\nend_header\n"
        )
        ascii_path = directory / "ascii.ply"
        ascii_path.write_bytes(header + b"0 0 0 0 0 1 255 0 0\n1 0 0 0 0 1 0 255 0\n0 1 0 0 0 1 0 0 255\n3 0 1 2\n")
        binary_path = directory / "binary.ply"
        binary_path.write_bytes(
            header.replace(b"format ascii 1.0", b"format binary_little_endian 1.0")
            + b"".join(struct.pack("<ffffffBBB", *xyz, 0, 0, 1, *rgb) for xyz, rgb in (
                ((0, 0, 0), (255, 0, 0)), ((1, 0, 0), (0, 255, 0)), ((0, 1, 0), (0, 0, 255))
            ))
            + struct.pack("<Biii", 3, 0, 1, 2)
        )
        for path in (ascii_path, binary_path):
            dry = require_ok("interchange.import", {"format": "PLY", "filepath": str(path)})
            assert dry["dry_run"] and not dry["changed"], dry
            imported = require_ok("interchange.import", {
                "format": "PLY", "filepath": str(path), "dry_run": False, "confirm": True,
            })
            assert imported["changed"] and imported["ply_meshes"][0]["vertices"] == 3, imported
            obj = bpy.data.objects[path.stem]
            assert len(obj.data.polygons) == 1
            assert {"ply_normal", "ply_color"} <= {item.name for item in obj.data.attributes}
            assert tuple(obj.data.attributes["ply_normal"].data[0].vector) == (0.0, 0.0, 1.0)
            assert obj.data.attributes["ply_color"].data[0].color[0] == 1.0

        checkpoint_dir = directory / "custom-checkpoints"
        template = "{base}--{index:03d}--{tag}.blend"
        options = {"directory": str(checkpoint_dir), "name_template": template}
        preview = require_ok("checkpoint.save", {**options, "tag": "first", "preview_only": True})
        assert not preview["saved"] and not checkpoint_dir.exists(), preview
        first = require_ok("checkpoint.save", {**options, "tag": "first"})
        second = require_ok("checkpoint.save", {**options, "tag": "second"})
        assert Path(first["path"]).is_file() and Path(second["path"]).is_file()
        listed = require_ok("checkpoint.list", options)
        assert [item["name"] for item in listed["checkpoints"]] == [first["name"], second["name"]], listed
        prune_preview = require_ok("checkpoint.save", {**options, "tag": "third", "keep_latest": 2, "prune": True, "preview_only": True})
        assert [item["name"] for item in prune_preview["prune_preview"]] == [first["name"]]
        denied = dispatch("checkpoint.save", {**options, "tag": "third", "keep_latest": 2, "prune": True})
        assert not denied["ok"] and denied["error"]["type"] == "ConfirmationRequiredError", denied
        third = require_ok("checkpoint.save", {**options, "tag": "third", "keep_latest": 2, "prune": True, "confirm": True})
        assert first["name"] in third["pruned"] and not Path(first["path"]).exists()
        assert require_ok("checkpoint.list", options)["count"] == 2

        fixed = {"directory": str(checkpoint_dir), "name_template": "manual-20260925.blend"}
        fixed_saved = require_ok("checkpoint.save", fixed)
        assert Path(fixed_saved["path"]).is_file() and fixed_saved["index"] is None
        denied_overwrite = dispatch("checkpoint.save", fixed)
        assert not denied_overwrite["ok"] and denied_overwrite["error"]["type"] == "ConfirmationRequiredError"
        overwritten = require_ok("checkpoint.save", {**fixed, "confirm": True})
        assert overwritten["overwrite"] is True and overwritten["path"] == fixed_saved["path"]

        asset_options = {"directory": str(checkpoint_dir), "asset": "trigger-guard"}
        first_asset = require_ok("checkpoint.save", {**asset_options, "name_template": "trigger-20260925T120000.blend"})
        second_asset = require_ok("checkpoint.save", {**asset_options, "name_template": "trigger-20260925T120100.blend",
                                                       "keep_latest": 1, "prune": True, "confirm": True})
        assert first_asset["name"] in second_asset["pruned"] and not Path(first_asset["path"]).exists()
        assert [item["name"] for item in require_ok("checkpoint.list", asset_options)["checkpoints"]] == [second_asset["name"]]
        restored = require_ok("checkpoint.restore", {**options, "name": second["name"], "confirm": True})
        assert restored["restored"] and Path(bpy.data.filepath) == Path(second["path"]), restored
    print("PLY_CHECKPOINT_HEADLESS_OK")


if __name__ == "__main__":
    run()
