"""Pure Python coverage for the installed-API lookup's RNA shape and limits."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))

from blenderboo.tools.api_lookup import lookup_api


def prop(identifier: str, kind: str = "STRING", **fields):
    base = dict(identifier=identifier, type=kind, subtype="NONE", array_length=0,
                is_readonly=False, default=None, unit="NONE", description="A property",
                enum_items=(), fixed_type=None)
    base.update(fields)
    return SimpleNamespace(**base)


class ApiLookupTests(unittest.TestCase):
    def setUp(self):
        engine = prop("engine", "ENUM", default="BLENDER_EEVEE_NEXT",
                      enum_items=[SimpleNamespace(identifier="BLENDER_EEVEE_NEXT", name="EEVEE",
                                                  description="Real-time renderer", value=1)])
        render_rna = SimpleNamespace(identifier="RenderSettings", base=None,
                                     properties=[engine], functions=[])
        render = prop("render", "POINTER", fixed_type=render_rna)
        scene_rna = SimpleNamespace(identifier="Scene", base=SimpleNamespace(identifier="ID"),
                                    properties=[render, prop("use_nodes", "BOOLEAN", default=False),
                                                prop("frame_current", "INT", default=1)],
                                    functions=[SimpleNamespace(identifier="frame_set", description="Set frame",
                                                               parameters=[prop("frame", "INT")], return_values=[])])
        bevel_rna = SimpleNamespace(description="Bevel selected edges",
                                    properties=[prop("offset", "FLOAT", default=0.1)])
        self.bpy = SimpleNamespace(
            app=SimpleNamespace(version_string="5.2.0"),
            types=SimpleNamespace(Scene=SimpleNamespace(bl_rna=scene_rna)),
            ops=SimpleNamespace(mesh=SimpleNamespace(bevel=SimpleNamespace(get_rna_type=lambda: bevel_rna))),
        )
        module = patch.dict(sys.modules, {"bpy": self.bpy})
        module.start()
        self.addCleanup(module.stop)

    def test_struct_and_property_details(self):
        struct = lookup_api({"path": "bpy.types.Scene", "detail": "full"})
        self.assertEqual(struct["kind"], "rna_struct")
        self.assertEqual(struct["base"], "ID")
        self.assertEqual(struct["blender_version"], "5.2.0")
        engine = lookup_api({"path": "Scene.render.engine", "detail": "full"})
        self.assertEqual(engine["property"]["default"], "BLENDER_EEVEE_NEXT")
        self.assertEqual(engine["property"]["enum_items"][0]["identifier"], "BLENDER_EEVEE_NEXT")
        self.assertEqual(engine["owner"], "bpy.types.Scene.render")

    def test_function_and_operator(self):
        function = lookup_api({"path": "Scene.frame_set", "detail": "full"})
        self.assertEqual(function["function"]["parameters"][0]["identifier"], "frame")
        operator = lookup_api({"path": "bpy.ops.mesh.bevel", "detail": "full"})
        self.assertEqual(operator["kind"], "operator")
        self.assertEqual(operator["items"][0]["identifier"], "offset")
        self.assertIn("not introspectable", operator["poll_note"])

    def test_missing_path_suggests_same_parent(self):
        missing = lookup_api({"path": "Scene.node_tree"})
        self.assertFalse(missing["exists"])
        self.assertIn("use_nodes", missing["suggestions"])
        self.assertEqual(missing, lookup_api({"path": "bpy.types.Scene.node_tree"}))

    def test_documented_replacement_is_suggested_only_when_live(self):
        scene = self.bpy.types.Scene.bl_rna
        scene.properties.append(prop("compositing_node_group", "POINTER"))
        missing = lookup_api({"path": "Scene.node_tree"})
        self.assertEqual(missing["suggestions"][0], "compositing_node_group")
        self.assertEqual(missing["replacement"]["replacement"],
                         "bpy.types.Scene.compositing_node_group")
        self.assertIn("docs.blender.org/api/5.0/change_log.html", missing["replacement"]["source_url"])

    def test_paging_and_search(self):
        first = lookup_api({"path": "Scene", "limit": 2})
        self.assertEqual(len(first["items"]), 2)
        second = lookup_api({"path": "Scene", "limit": 2, "cursor": first["next_cursor"]})
        self.assertEqual(len(second["items"]), 2)
        with patch("blenderboo.tools.api_lookup._search_index",
                   return_value=(("bpy.types.Scene", "bpy.types.Scene.frame_current", "bpy.types.Mesh"), False)):
            page = lookup_api({"search": "Scene", "limit": 1})
            self.assertEqual(page["total"], 2)
            self.assertEqual(page["next_cursor"], "1")

    def test_rejects_bad_or_ambiguous_paths(self):
        for arguments in ({}, {"path": "Scene", "search": "Mesh"},
                          {"path": "bpy.context.scene"}, {"path": "Scene.__dict__"},
                          {"path": "Scene", "limit": 1000},
                          {"path": "Scene", "typo": True}):
            with self.subTest(arguments=arguments), self.assertRaises(Exception):
                lookup_api(arguments)


if __name__ == "__main__":
    unittest.main()
