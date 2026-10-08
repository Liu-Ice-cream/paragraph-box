import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

from scripts.package_plugin import PACKAGE_FILES, build_package


class PackagePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "project"
        source = Path(__file__).resolve().parents[1]
        for name in PACKAGE_FILES:
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, path)

    def test_private_files_in_any_folder_stay_out_of_zip(self):
        canary = "PRIVATE-PACKAGE-CANARY"
        for name in ("config.json", ".env", ".cache/index.sqlite3", ".runtime/token.txt",
                     "docs/private-notes.md", "samples/work/personal.txt",
                     "ui/config.json", "Images/private-key.pem", "backup.bundle"):
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canary, encoding="utf-8")
        package = build_package(self.base)
        with ZipFile(package) as archive:
            self.assertEqual(set(archive.namelist()), set(PACKAGE_FILES))
            self.assertIn("MIT License", archive.read("LICENSE").decode())
            self.assertFalse(any(canary.encode() in archive.read(name) for name in archive.namelist()))
            self.assertEqual(json.loads(archive.read("plugin.json"))["Version"], "0.2.5")
        self.assertEqual((self.base / "config.json").read_text(), canary)

    def test_missing_file_does_not_create_partial_release(self):
        (self.base / "LICENSE").unlink()
        with self.assertRaises(FileNotFoundError):
            build_package(self.base)
        self.assertFalse((self.base / "dist").exists())

    @unittest.skipUnless(__import__('os').name == 'nt', "Windows junction check")
    def test_whitelisted_directory_cannot_point_to_private_folder(self):
        private = Path(self.temp.name) / "private"
        (self.base / "samples").rename(private)
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.base / "samples"), str(private)],
                                capture_output=True, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.addCleanup(lambda: (self.base / "samples").rmdir())
        with self.assertRaises(ValueError):
            build_package(self.base)
        self.assertFalse((self.base / "dist").exists())


if __name__ == "__main__":
    unittest.main()
