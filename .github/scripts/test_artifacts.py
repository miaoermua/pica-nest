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

    def test_a_script_installed_native_package_passes(self):
        """A native package may install itself through scripts instead of a container.

        cattools does this: `targets = opkg, pica`, its runtime dependencies
        handed to opkg as `base@opkg = bash`, and its payload delivered by
        `scripts/install`. There is deliberately no `.ipk` for it to carry, and
        demanding one would report an artifact the packer built as empty.
        """
        filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = opkg\n",
            {"scripts/install": b"#!/bin/sh\n", "root/usr/bin/demo": b"binary"},
        )
        self.write_index(filename, pkgmgr="opkg")
        result = self.run_verify("--strict-all")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_artifact_with_nothing_installable_fails(self):
        """A manifest alone installs nothing, whatever the backend."""
        filename = "demo-1.0.0-1-x86_64-opkg.pkg.tar.gz"
        make_artifact(
            self.packages / filename,
            "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\nplatform = all\narch = x86_64\npkgmgr = opkg\n",
            {},
        )
        self.write_index(filename, pkgmgr="opkg")
        result = self.run_verify("--strict-all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no payload", result.stderr)

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


class MultiArchIndexTest(unittest.TestCase):
    """One job building several architectures must produce one entry each.

    Keying the merge on `(pkgmgr, pkgname)` alone collapses them onto a single
    entry: four artifacts reach `r/packages/` with nothing in the index naming
    them, and `verify_artifacts` reports each as "built artifact has no index
    entry". Which entry survives is then decided by matrix order rather than by
    anything the repository chose.
    """

    ARCHES = [
        ("amd64", "x86_64"),
        ("arm64", "aarch64_generic"),
        ("arm64", "aarch64_cortex-a53"),
        ("arm64", "aarch64_cortex-a72"),
        ("mipsel", "mipsel_24kc"),
    ]

    def test_every_architecture_keeps_its_own_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            (root / "r/repo.json").write_text(json.dumps({"schema": 2, "packages": []}))
            output = root / "build/opkg"
            output.mkdir(parents=True)

            entries = []
            for platform, arch in self.ARCHES:
                filename = f"demo-1.0.0-1-{platform}-{arch}-opkg.pkg.tar.gz"
                make_artifact(
                    output / filename,
                    f"pkgname = demo\npkgver = 1.0.0\npkgrel = 1\n"
                    f"platform = {platform}\narch = {arch}\npkgmgr = opkg\n",
                    {"pkgs/demo.ipk": b"ipk"},
                )
                entries.append({
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": platform, "arch": arch,
                    "filename": filename, "sha256": sha256(output / filename),
                })
            (output / "repo.json").write_text(json.dumps({"schema": 2, "packages": entries}))

            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"),
                 str(output / "repo.json"),
                 "--repo", str(root / "r/repo.json"),
                 "--release-list", str(root / "r/release-files.txt")],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            merged = json.loads((root / "r/repo.json").read_text())["packages"]
            self.assertEqual(len(merged), len(self.ARCHES), "one entry per architecture")
            self.assertEqual(
                {(pkg["platform"], pkg["arch"]) for pkg in merged},
                set(self.ARCHES),
            )
            released = (root / "r/release-files.txt").read_text().splitlines()
            self.assertEqual(len(released), len(self.ARCHES))

    def test_an_older_entry_for_one_architecture_is_replaced_not_kept(self):
        """A rebuild of one arch updates that entry and leaves the others alone."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            stale = {
                "pkgname": "demo", "pkgver": "0.9", "pkgrel": "1", "pkgmgr": "opkg",
                "platform": "amd64", "arch": "x86_64",
                "filename": "demo-0.9-1-amd64-x86_64-opkg.pkg.tar.gz", "sha256": "b" * 64,
            }
            keep = {
                "pkgname": "demo", "pkgver": "0.9", "pkgrel": "1", "pkgmgr": "opkg",
                "platform": "mipsel", "arch": "mipsel_24kc",
                "filename": "demo-0.9-1-mipsel-mipsel_24kc-opkg.pkg.tar.gz", "sha256": "c" * 64,
            }
            (root / "r/repo.json").write_text(
                json.dumps({"schema": 2, "packages": [stale, keep]})
            )

            output = root / "build/opkg"
            output.mkdir(parents=True)
            filename = "demo-1.0.0-1-amd64-x86_64-opkg.pkg.tar.gz"
            make_artifact(
                output / filename,
                "pkgname = demo\npkgver = 1.0.0\npkgrel = 1\n"
                "platform = amd64\narch = x86_64\npkgmgr = opkg\n",
                {"pkgs/demo.ipk": b"ipk"},
            )
            (output / "repo.json").write_text(json.dumps({
                "schema": 2,
                "packages": [{
                    "pkgname": "demo", "pkgver": "1.0.0", "pkgrel": "1",
                    "pkgmgr": "opkg", "platform": "amd64", "arch": "x86_64",
                    "filename": filename, "sha256": sha256(output / filename),
                }],
            }))

            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "gen_repo_index.py"),
                 str(output / "repo.json"), "--repo", str(root / "r/repo.json")],
                cwd=root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            merged = json.loads((root / "r/repo.json").read_text())["packages"]
            by_arch = {pkg["arch"]: pkg["pkgver"] for pkg in merged}
            self.assertEqual(by_arch["x86_64"], "1.0.0", "the rebuilt arch is updated")
            self.assertEqual(by_arch["mipsel_24kc"], "0.9", "an untouched arch is left alone")


if __name__ == "__main__":
    unittest.main()
