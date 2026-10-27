"""Regression coverage for Blender's static add-on metadata discovery."""

import ast
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
ADDON_INIT = ROOT / "addon" / "blenderboo" / "__init__.py"
sys.path.insert(0, str(ROOT / "addon"))

import blenderboo


class AddonMetadataTests(unittest.TestCase):
    def test_bl_info_is_statically_parseable_and_matches_runtime_version(self) -> None:
        tree = ast.parse(ADDON_INIT.read_text(encoding="utf-8"))
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "bl_info"
                for target in node.targets
            )
        )
        metadata = ast.literal_eval(assignment.value)
        self.assertEqual(metadata["version"], blenderboo.ADDON_VERSION)
        self.assertEqual(metadata["version"], (0, 10, 0))
        self.assertEqual(metadata["name"], "BlenderBoo")


if __name__ == "__main__":
    unittest.main()
