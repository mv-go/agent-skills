import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills/mac-storage-cleanup/scripts/storage_inventory.py"
spec = importlib.util.spec_from_file_location("storage_inventory", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "root with spaces"
        self.root.mkdir()
        (self.root / "folder").mkdir()
        (self.root / "folder" / "nested").write_bytes(b"a" * 10000)
        (self.root / "large file").write_bytes(b"b" * 20000)
        outside = self.base / "external"
        outside.mkdir()
        (outside / "secret").write_bytes(b"c" * 30000)
        (self.root / "external link").symlink_to(outside, target_is_directory=True)

    def test_real_du_and_symlinks(self):
        data = module.inventory(str(self.root), children=True)
        self.assertEqual(data["status"], "complete", data["errors"])
        self.assertGreater(data["allocated_bytes"], 0)
        names = {Path(row["path"]).name for row in data["children"]}
        self.assertEqual(names, {"folder", "large file"})
        self.assertEqual(module.inventory(str(self.root / "external link"))["status"], "partial")
        self.assertGreater(data["filesystem"]["free_bytes"], 0)

    def test_failure_and_timeout_are_partial(self):
        with mock.patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "denied")):
            result = module.inventory(str(self.root))
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["allocated_bytes"])
        self.assertIn("denied", result["errors"][0])
        with mock.patch.object(module.subprocess, "run", side_effect=subprocess.TimeoutExpired("du", 1)):
            result = module.inventory(str(self.root), timeout=1)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("timed out" in e for e in result["errors"]))

    def test_failed_du_retains_partial_root_and_child_allocations(self):
        output = "12\t%s\n24\t%s\n" % (self.root / "folder", self.root)
        failed = subprocess.CompletedProcess([], 1, output, "access denied")
        with mock.patch.object(module.subprocess, "run", return_value=failed):
            result = module.inventory(str(self.root), children=True)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["allocated_bytes"], 24 * 1024)
        child = next(row for row in result["children"] if row["path"] == str(self.root / "folder"))
        self.assertEqual(child["allocated_bytes"], 12 * 1024)
        self.assertTrue(any("access denied" in error for error in result["errors"]))

    def test_timeout_retains_partial_stdout_bytes(self):
        output = ("24\t%s\n" % self.root).encode()
        timeout = subprocess.TimeoutExpired("du", 1, output=output)
        with mock.patch.object(module.subprocess, "run", side_effect=timeout):
            result = module.inventory(str(self.root), timeout=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["allocated_bytes"], 24 * 1024)
        self.assertTrue(any("timed out" in error for error in result["errors"]))

    def test_sparse_file_uses_allocated_blocks(self):
        sparse = self.root / "sparse file"
        with sparse.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024)
        info = sparse.stat()
        if info.st_blocks * 512 >= info.st_size // 10:
            self.skipTest("filesystem did not create a sparse file")
        result = module.inventory(str(self.root), children=True)
        self.assertEqual(result["status"], "complete", result["errors"])
        child = next(row for row in result["children"] if row["path"] == str(sparse))
        self.assertEqual(child["allocated_bytes"], info.st_blocks * 512)
        self.assertLess(child["allocated_bytes"], info.st_size // 10)

    def test_output_collision_and_symlink_are_safe(self):
        destination = self.base / "existing"
        destination.write_text("keep")
        link = self.base / "link"
        link.symlink_to(destination)
        for output in (destination, link):
            proc = subprocess.run([sys.executable, str(SCRIPT), "--path", str(self.root), "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(destination.read_text(), "keep")

    def test_json_independent_overlapping_observations(self):
        output = self.base / "report.json"
        proc = subprocess.run([sys.executable, str(SCRIPT), "--path", str(self.root), "--path", str(self.root / "folder"), "--children", "--output", str(output)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(output.read_text())
        self.assertEqual(len(data["observations"]), 2)
        self.assertNotIn("total_allocated_bytes", data)
        self.assertIn("do not sum", data["note"])
        self.assertEqual(data["units"], "bytes")

    def test_relative_path_rejected(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--path", "relative"], capture_output=True)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
