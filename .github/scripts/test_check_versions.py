#!/usr/bin/env python3
"""Tests for the manifest/source version audit."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_versions import check_job  # noqa: E402


class VersionAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.old_cwd = os.getcwd()
        os.chdir(self.root)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def write_job(self, source, pkgver="1.0.0", versioning=None, matrix=None):
        os.makedirs("src/opkg/demo", exist_ok=True)
        with open("src/opkg/demo/manifest", "w", encoding="utf-8") as handle:
            handle.write(f"pkgname = demo\npkgver = {pkgver}\npkgrel = 1\n")
        job = {
            "mgr": "opkg",
            "manifest": "src/opkg/demo/manifest",
            "source": source,
            "build": {"matrix": matrix or [{"platform": "all", "arch": "x86_64"}]},
        }
        if versioning:
            job["versioning"] = versioning
        os.makedirs("ci/opkg/demo", exist_ok=True)
        with open("ci/opkg/demo/build.json", "w", encoding="utf-8") as handle:
            json.dump(job, handle)
        return "ci/opkg/demo/build.json"

    def check(self, path):
        errors = {path: []}
        rows = {path: []}
        check_job(path, errors, rows)
        return errors[path], rows[path]

    def test_url_with_manifest_version_passes(self):
        path = self.write_job(
            {
                "provider": "http_template",
                "url_templates": ["https://example/{arch}/demo_1.0.0-r1_{arch}.ipk"],
            }
        )
        errors, rows = self.check(path)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0][0], "ok")

    def test_url_with_another_version_fails(self):
        path = self.write_job(
            {
                "provider": "http_template",
                "url_templates": ["https://example/{arch}/demo_2.0.0-r1_{arch}.ipk"],
            }
        )
        errors, _ = self.check(path)
        self.assertTrue(any("no URL carries version" in error for error in errors))

    def test_declared_arch_version_is_accepted(self):
        path = self.write_job(
            {
                "provider": "http_template",
                "url_templates": ["https://example/{arch}/demo_1.0.0-r1_{arch}.ipk"],
                "arch_url_templates": {
                    "aarch64_cortex-a53": [
                        "https://example/aarch64_cortex-a53/demo_1.0.1-r1_aarch64_cortex-a53.ipk"
                    ]
                },
            },
            matrix=[
                {"platform": "amd64", "arch": "x86_64"},
                {"platform": "arm64", "arch": "aarch64_cortex-a53"},
            ],
            versioning={"arch_versions": {"aarch64_cortex-a53": "1.0.1"}},
        )
        errors, _ = self.check(path)
        self.assertEqual(errors, [])

    def test_an_architecture_path_for_another_arch_fails(self):
        path = self.write_job(
            {
                "provider": "http_template",
                "url_templates": ["https://example/x86_64/demo_1.0.0-r1_{arch}.ipk"],
            },
            matrix=[{"platform": "arm64", "arch": "aarch64_generic"}],
        )
        errors, _ = self.check(path)
        self.assertTrue(any("URL path names 'x86_64'" in error for error in errors))

    def test_pinned_tag_must_name_the_manifest_version(self):
        path = self.write_job(
            {
                "provider": "github",
                "repo": "owner/demo",
                "release": {"tag": {"regex": r"^v1\.0\.0$"}},
            }
        )
        errors, rows = self.check(path)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0][0], "pinned tag")

        path = self.write_job(
            {
                "provider": "github",
                "repo": "owner/demo",
                "release": {"tag": {"regex": r"^v2\.0\.0$"}},
            }
        )
        errors, _ = self.check(path)
        self.assertTrue(any("does not carry manifest version" in error for error in errors))

    def test_tarball_url_must_name_the_manifest_version(self):
        path = self.write_job(
            {
                "provider": "tarball",
                "url_template": "https://example/demo-{arch}-1.0.0.tar.gz",
            }
        )
        errors, _ = self.check(path)
        self.assertEqual(errors, [])

        path = self.write_job(
            {
                "provider": "tarball",
                "url_template": "https://example/demo-{arch}-2.0.0.tar.gz",
            }
        )
        errors, _ = self.check(path)
        self.assertTrue(any("no URL carries version" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
