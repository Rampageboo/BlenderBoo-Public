"""Pure-Python contract tests for compositor, render-manifest, and interchange guards."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.errors import InvalidParamsError
from blenderboo.tools import compositor, interchange, render_manifest


class CompositorHelperTests(unittest.TestCase):
    def test_scene_tree_prefers_blender_5_group_and_falls_back_to_legacy(self) -> None:
        modern = SimpleNamespace(compositing_node_group=SimpleNamespace(name="Modern"), use_nodes=True)
        legacy = SimpleNamespace(node_tree=SimpleNamespace(name="Legacy"), use_nodes=True)
        self.assertEqual(compositor._scene_tree(modern).name, "Modern")
        self.assertEqual(compositor._scene_tree(legacy).name, "Legacy")
        self.assertIsNone(compositor._scene_tree(SimpleNamespace(node_tree=legacy.node_tree, use_nodes=False)))

    def test_operation_and_page_bounds_are_strict(self) -> None:
        self.assertEqual(compositor._operations([{"op": "disconnect"}]), [{"op": "disconnect"}])
        self.assertEqual(compositor._page({"limit": 25, "cursor": "50"}), (25, 50))
        with self.assertRaises(InvalidParamsError):
            compositor._operations([])
        with self.assertRaises(InvalidParamsError):
            compositor._operations([{"op": "arbitrary_python"}])
        with self.assertRaises(InvalidParamsError):
            compositor._page({"cursor": "-1"})

    def test_json_values_reject_non_finite_or_unbounded_inputs(self) -> None:
        self.assertEqual(compositor._validated_value([1.0, 2.0], "value"), [1.0, 2.0])
        with self.assertRaises(InvalidParamsError):
            compositor._validated_value(float("inf"), "value")
        with self.assertRaises(InvalidParamsError):
            compositor._validated_value(list(range(33)), "value")


class RenderManifestHelperTests(unittest.TestCase):
    def test_unreadable_compositor_returns_structured_warning(self) -> None:
        class BrokenScene:
            @property
            def compositing_node_group(self):
                raise RuntimeError("unreadable")
        result = render_manifest._compositor(BrokenScene(), SimpleNamespace(), False)
        self.assertEqual(result["warning"]["code"], "compositor.unreadable")

    def test_blender_relative_output_patterns_preserve_the_prefix(self) -> None:
        self.assertEqual(
            render_manifest._joined_output_pattern("//renders/", "/beauty_####"),
            "//renders/beauty_####",
        )
        self.assertEqual(render_manifest._joined_output_pattern("", "beauty_####"), "beauty_####")


class InterchangeHelperTests(unittest.TestCase):
    def test_format_inference_and_single_file_gltf_export_boundary(self) -> None:
        self.assertEqual(interchange._format(None, Path("hero.glb"), export=True), "GLTF")
        self.assertEqual(interchange._format(None, Path("hero.gltf"), export=False), "GLTF")
        with self.assertRaises(InvalidParamsError):
            interchange._format(None, Path("hero.gltf"), export=True)
        with self.assertRaises(InvalidParamsError):
            interchange._validate_extension(Path("hero.gltf"), "GLTF", export=True)

    def test_rna_option_types_and_enum_flags_are_bounded(self) -> None:
        boolean = SimpleNamespace(type="BOOLEAN", is_array=False)
        enum = SimpleNamespace(
            type="ENUM",
            is_array=False,
            is_enum_flag=True,
            enum_items=[SimpleNamespace(identifier="MESH"), SimpleNamespace(identifier="CAMERA")],
        )
        self.assertTrue(interchange._validate_option_value(boolean, True, "value"))
        self.assertEqual(
            interchange._validate_option_value(enum, ["MESH"], "value"),
            {"MESH"},
        )
        with self.assertRaises(InvalidParamsError):
            interchange._validate_option_value(boolean, 1, "value")
        with self.assertRaises(InvalidParamsError):
            interchange._validate_option_value(enum, ["SCRIPT"], "value")

    def test_created_summary_is_bounded_and_options_are_json_safe(self) -> None:
        created = {"objects": [SimpleNamespace(name=f"Object_{index:04d}") for index in range(2001)]}
        summary = interchange._created_summary(created)
        self.assertEqual(summary["objects"]["count"], 2001)
        self.assertEqual(len(summary["objects"]["names"]), 2000)
        self.assertTrue(summary["objects"]["truncated"])
        self.assertEqual(interchange._display_options({"object_types": {"MESH", "CAMERA"}}), {"object_types": ["CAMERA", "MESH"]})


if __name__ == "__main__":
    unittest.main()
