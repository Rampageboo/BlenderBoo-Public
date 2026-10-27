"""Exact-file export publication and rollback using real temporary files."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
from blenderboo.errors import InvalidStateError
from blenderboo.tools import game_asset_export as export


class ExportPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.staging = self.root / "stage"
        self.staging.mkdir()
        self.output = self.root / "assets"
        self.output.mkdir()
        self.names = ["SM_Hinge.fbx", "T_Hinge_BaseColor.png", "SM_Hinge.ue.json"]
        for name in self.names:
            (self.staging / name).write_bytes(("new " + name).encode())

    def contents(self):
        return {p.relative_to(self.output).as_posix(): p.read_bytes()
                for p in self.output.rglob("*") if p.is_file()}

    def existing(self):
        (self.output / self.names[0]).write_bytes(b"previous mesh")
        (self.output / self.names[2]).write_bytes(b"previous manifest")
        (self.output / "T_Hinge_NormalDX.png").write_bytes(b"old texture not written this time")
        (self.output / "other_asset.fbx").write_bytes(b"another asset")
        (self.output / "notes").mkdir()
        (self.output / "notes" / "keep.txt").write_bytes(b"keep nested contents")

    def test_default_still_refuses_nonempty_directory(self):
        self.existing()
        before = self.contents()
        with self.assertRaisesRegex(InvalidStateError, "overwrite"):
            export._publish_files(self.staging, self.output, self.names, False)
        self.assertEqual(self.contents(), before)

    def test_overwrite_publishes_only_exact_current_outputs(self):
        self.existing()
        before = self.contents()
        export._publish_files(self.staging, self.output, self.names, True)
        after = self.contents()
        for name in self.names:
            self.assertEqual(after[name], ("new " + name).encode())
        for name in before.keys() - set(self.names):
            self.assertEqual(after[name], before[name])

    def test_failure_restores_previous_files_and_removes_only_new_outputs(self):
        self.existing()
        before = self.contents()
        replace = os.replace
        def fail_sidecar(source, target):
            if Path(source) == self.staging / self.names[-1]:
                raise OSError("injected publication failure")
            return replace(source, target)
        with patch.object(export.os, "replace", side_effect=fail_sidecar):
            with self.assertRaisesRegex(OSError, "injected"):
                export._publish_files(self.staging, self.output, self.names, True)
        self.assertEqual(self.contents(), before)

    def test_backup_failure_never_replaces_any_output(self):
        self.existing()
        before = self.contents()
        with patch.object(export.shutil, "copy2", side_effect=OSError("backup failure")):
            with self.assertRaisesRegex(OSError, "backup failure"):
                export._publish_files(self.staging, self.output, self.names, True)
        self.assertEqual(self.contents(), before)

    def test_new_output_directory_removed_after_publication_failure(self):
        self.output.rmdir()
        replace = os.replace
        def fail_sidecar(source, target):
            if Path(source) == self.staging / self.names[-1]:
                raise OSError("publication failure")
            return replace(source, target)
        with patch.object(export.os, "replace", side_effect=fail_sidecar):
            with self.assertRaises(OSError):
                export._publish_files(self.staging, self.output, self.names, False)
        self.assertFalse(self.output.exists())

    def test_colliding_directory_is_rejected_without_writes(self):
        (self.output / self.names[-1]).mkdir()
        with self.assertRaisesRegex(InvalidStateError, "regular file"):
            export._publish_files(self.staging, self.output, self.names, True)
        self.assertEqual(self.contents(), {})
        self.assertTrue((self.output / self.names[-1]).is_dir())

    def test_symlink_target_is_never_followed(self):
        external = self.root / "external"
        external.write_bytes(b"protected")
        try:
            (self.output / self.names[0]).symlink_to(external)
        except OSError as exc:
            self.skipTest(f"Host does not allow symlinks: {exc}")
        with self.assertRaisesRegex(InvalidStateError, "regular file"):
            export._publish_files(self.staging, self.output, self.names, True)
        self.assertEqual(external.read_bytes(), b"protected")

    def test_rollback_failure_retains_backup_and_reports_recovery_path(self):
        self.existing()
        replace = os.replace
        def fail_publish_and_restore(source, target):
            source = Path(source)
            if source == self.staging / self.names[-1] or source.parent.name == "previous":
                raise OSError("persistent filesystem failure")
            return replace(source, target)
        with patch.object(export.os, "replace", side_effect=fail_publish_and_restore):
            with self.assertRaisesRegex(export._RollbackError, "Recovery files retained") as raised:
                export._publish_files(self.staging, self.output, self.names, True)
        self.assertIn(str(self.staging), str(raised.exception))
        self.assertEqual((self.staging / "previous" / self.names[0]).read_bytes(), b"previous mesh")


if __name__ == "__main__":
    unittest.main()
