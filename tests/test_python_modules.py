"""Pure Python contract tests for document-local exec modules."""

from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))

from blenderboo.tools import python_modules as modules


class FakeText:
    def __init__(self, name, uid):
        self.name = name
        self.session_uid = uid
        self.source = ""

    def as_string(self):
        return self.source

    def clear(self):
        self.source = ""

    def write(self, code):
        self.source += code


class FakeTexts:
    def __init__(self):
        self.items = {}
        self.next_uid = 1

    def __iter__(self):
        return iter(self.items.values())

    def get(self, name):
        return self.items.get(name)

    def new(self, name):
        item = FakeText(name, self.next_uid)
        self.next_uid += 1
        self.items[name] = item
        return item

    def remove(self, item):
        del self.items[item.name]


class PythonModulesTests(unittest.TestCase):
    def setUp(self):
        self.bpy = SimpleNamespace(data=SimpleNamespace(texts=FakeTexts(), filepath="/a.blend"))
        self.patches = [
            patch.object(modules, "get_bpy", return_value=self.bpy),
            patch.object(modules, "require_document_write_access"),
            patch.object(modules, "check_expected_state"),
            patch.object(modules, "reconcile"),
            patch.object(modules, "bump_epoch"),
            patch.object(modules, "current_state_token", return_value="state.1"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        for name in tuple(sys.modules):
            if name == modules.PACKAGE or name.startswith(modules.PACKAGE + "."):
                sys.modules.pop(name, None)
        if modules._IMPORTER in sys.meta_path:
            sys.meta_path.remove(modules._IMPORTER)

    def _exec(self, code):
        used = {}
        with modules.exec_module_imports(self.bpy, used) as module_builtins:
            namespace = {"__builtins__": module_builtins}
            exec(code, namespace, namespace)
        return namespace, used

    def test_put_import_update_remove_and_receipt_hash(self):
        first = modules.module_put({"name": "helper", "code": "VALUE = 1"})
        self.assertTrue(first["created"])
        self.assertEqual(self.bpy.data.texts.get(first["version_text_name"]).as_string(), "VALUE = 1")
        self.assertEqual(modules.module_list({})["modules"][0]["sha256"], first["sha256"])
        namespace, used = self._exec("import cbt_modules.helper; result = cbt_modules.helper.VALUE")
        self.assertEqual(namespace["result"], 1)
        self.assertEqual(used, {"helper": first["sha256"]})
        # Cached imports still appear in the next exec receipt.
        self.assertEqual(self._exec("from cbt_modules import helper; result = helper.VALUE")[1], used)
        second = modules.module_put({"name": "helper", "code": "VALUE = 2"})
        self.assertFalse(second["created"])
        self.assertNotEqual(second["sha256"], first["sha256"])
        self.assertEqual(self.bpy.data.texts.get(first["version_text_name"]).as_string(), "VALUE = 1")
        self.assertEqual(self.bpy.data.texts.get(second["version_text_name"]).as_string(), "VALUE = 2")
        namespace, used = self._exec("import cbt_modules.helper; result = cbt_modules.helper.VALUE")
        self.assertEqual(namespace["result"], 2)
        self.assertEqual(used, {"helper": second["sha256"]})
        removed = modules.module_remove({"name": "helper"})
        self.assertEqual(removed["sha256"], second["sha256"])
        self.assertIsNone(self.bpy.data.texts.get("cbt_module.helper"))
        self.assertIsNotNone(self.bpy.data.texts.get(first["version_text_name"]))
        self.assertIsNotNone(self.bpy.data.texts.get(second["version_text_name"]))
        with self.assertRaises(ModuleNotFoundError):
            self._exec("import cbt_modules.helper")

    def test_out_of_band_edit_and_document_reopen_invalidate_cache(self):
        modules.module_put({"name": "helper", "code": "VALUE = 3"})
        self._exec("import cbt_modules.helper")
        self.bpy.data.texts.get("cbt_module.helper").source = "VALUE = 4"
        namespace, used = self._exec("import cbt_modules.helper; result = cbt_modules.helper.VALUE")
        self.assertEqual(namespace["result"], 4)
        self.assertEqual(used["helper"], modules._source_hash("VALUE = 4"))
        self.bpy.data.filepath = "/reopened.blend"
        namespace, _ = self._exec("import cbt_modules.helper; result = cbt_modules.helper.VALUE")
        self.assertEqual(namespace["result"], 4)

    def test_nested_imports_report_both_module_hashes(self):
        dependency = modules.module_put({"name": "dependency", "code": "VALUE = 7"})
        helper = modules.module_put({"name": "helper", "code": "from cbt_modules import dependency\nVALUE = dependency.VALUE + 1"})
        namespace, used = self._exec("import cbt_modules.helper; result = cbt_modules.helper.VALUE")
        self.assertEqual(namespace["result"], 8)
        self.assertEqual(used, {"dependency": dependency["sha256"], "helper": helper["sha256"]})

    def test_name_and_syntax_validation(self):
        for bad in ("", "a.b", "1bad", "with space", "class"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                modules.module_put({"name": bad, "code": "pass"})
        with self.assertRaises(Exception):
            modules.module_put({"name": "broken", "code": "def bad(:"})
        self.assertEqual(modules.module_list({})["count"], 0)

    def test_put_from_path_reads_once_and_stores_in_document(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "helper.py"
            path.write_text("VALUE = 13\n", encoding="utf-8")
            receipt = modules.module_put({"name": "helper", "path": str(path)})
            self.assertEqual(receipt["source_path"], str(path.resolve()))
            self.assertEqual(self.bpy.data.texts.get("cbt_module.helper").as_string(), "VALUE = 13\n")
            path.write_text("VALUE = 99\n", encoding="utf-8")
            namespace, hashes = self._exec("from cbt_modules import helper; result = helper.VALUE")
            self.assertEqual(namespace["result"], 13)
            self.assertEqual(hashes["helper"], receipt["sha256"])
            with self.assertRaises(Exception):
                modules.module_put({"name": "bad", "code": "pass", "path": str(path)})


if __name__ == "__main__":
    unittest.main()
