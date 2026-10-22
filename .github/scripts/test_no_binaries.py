#!/usr/bin/env python3
"""Tests for the guard that keeps built packages out of Git."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


class NoBinariesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        )

    def write(self, name, content=b"data"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def run_check(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "check_no_binaries.py"), "--root", str(self.root), *extra],
            cwd=self.root, capture_output=True, text=True,
        )

    def test_a_tracked_package_archive_fails(self):
        self.write("r/packages/demo-1.0.0-1-all-opkg.pkg.tar.gz")
        self.git("add", "-A")
        self.git("commit", "-qm", "add binary")
        result = self.run_check("--tracked")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("r/packages/demo-1.0.0-1-all-opkg.pkg.tar.gz", result.stderr)

    def test_an_untracked_package_archive_fails_before_it_is_committed(self):
        """The auto-commit step runs `git add`; the guard has to see it first."""
        self.write("r/packages/demo-1.0.0-1-all-opkg.pkg.tar.gz")
        result = self.run_check("--pending")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("demo-1.0.0-1-all-opkg.pkg.tar.gz", result.stderr)

    def test_a_staged_backend_container_fails(self):
        for name in ("pkgs/demo.ipk", "pkgs/demo.apk", "pkgs/packages.adb"):
            self.write(name)
        self.git("add", "-A")
        result = self.run_check("--pending")
        self.assertNotEqual(result.returncode, 0)
        for name in ("demo.ipk", "demo.apk", "packages.adb"):
            self.assertIn(name, result.stderr)

    def test_a_plain_source_archive_is_left_alone(self):
        """Only the project's own artefact set is forbidden, not every tarball."""
        self.write("src/opkg/demo/upstream/demo-1.0.0.tar.gz")
        self.write("src/opkg/demo/manifest", b"pkgname = demo\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "sources")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_oversized_added_file_fails(self):
        self.write("r/big.bin", b"x" * 4096)
        result = self.run_check("--pending", "--max-bytes", "1024")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("r/big.bin", result.stderr)

    def test_a_clean_repository_passes(self):
        self.write("r/repo.json", b'{"schema": 2, "packages": []}\n')
        self.git("add", "-A")
        self.git("commit", "-qm", "index")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok:", result.stdout)


if __name__ == "__main__":
    unittest.main()
