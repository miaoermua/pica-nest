#!/usr/bin/env python3
"""Tests for artifact/index verification and the release-file list."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def make_artifact(path, manifest, payload):
    with tarfile.open(path, "w:gz") as archive:
        data = manifest.encode()
        info = tarfile.TarInfo("manifest")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        for name, content in payload.items():
            data = content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ArtifactVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packages = self.root / "r/packages"
        self.packages.mkdir(parents=True)
        self.index = self.root / "r/repo.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_index(self, filename, pkgmgr="pica", platform="all", arch="x86_64"):
        artifact = self.packages / filename
        entry = {
            "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1", "pkgmgr": pkgmgr,
            "platform": platform, "arch": arch, "filename": filename,
            "sha256": sha256(artifact), "csize": artifact.stat().st_size,
        }
        self.index.write_text(json.dumps({"schema": 2, "packages": [entry]}), encoding="utf-8")
        return artifact

    def run_verify(self, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "verify_artifacts.py"),
             "--index", str(self.index), "--packages-dir", str(self.packages), *extra],
            cwd=self.root, capture_output=True, text=True,
        )

    def test_a_matching_artifact_passes(self):
        filename = "demo-1.0.0-1-x86_64-pica.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename)
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_hash_mismatch_fails(self):
        filename = "demo-1.0.0-1-x86_64-pica.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename)
        index = json.loads(self.index.read_text())
        index["packages"][0]["sha256"] = "0" * 64
        self.index.write_text(json.dumps(index))
        result = self.run_verify("--strict-all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256", result.stderr)

    def test_a_required_artifact_must_have_an_index_entry(self):
        filename = "demo-1.0.0-1-x86_64-pica.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = pica\n",
            {"root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename)
        required = self.root / "required.txt"
        required.write_text("r/packages/other.pkg.tar.gz\n", encoding="utf-8")
        result = self.run_verify("--require-file", str(required))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no index entry", result.stderr)


class ReleaseListTest(unittest.TestCase):
    def test_new_artifacts_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            (root / "r/repo.json").write_text(json.dumps({"schema": 2, "packages": []}))
            output = root / "build/opkg"
            output.mkdir(parents=True)
            filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
            make_artifact(
                output / filename,
                "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = opkg\n",
                {"pkgs/demo.ipk": b"ipk"},
            )
            (output / "repo.json").write_text(json.dumps({
                "schema": 2,
                "packages": [{
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": "all", "arch": "x86_64",
                    "filename": filename, "sha256": sha256(output / filename),
                }],
            }))
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"), str(output / "repo.json"),
                 "--repo", str(root / "r/repo.json"), "--release-list", str(root / "r/release-files.txt")],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = (root / "r/release-files.txt").read_text().splitlines()
            self.assertEqual(lines, [f"r/packages/{filename}"])


if __name__ == "__main__":
    unittest.main()
