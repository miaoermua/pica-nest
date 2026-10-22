#!/usr/bin/env python3
"""Tests for asset planning, preflight staging and the two-phase pipeline."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_assets import (  # noqa: E402
    dedupe_requests,
    http_template_urls,
    http_template_urls_for_arch,
    plan_github_assets,
    plan_http_assets,
    plan_tarball_assets,
)
from container_metadata import declares_package  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_assets.py")


class HttpTemplateUrlTest(unittest.TestCase):
    def test_common_urls_apply_to_every_arch(self):
        source = {"url_templates": ["https://example/{arch}/a", "https://example/{arch}/b"]}
        for arch in ("x86_64", "aarch64_cortex-a53"):
            self.assertEqual(http_template_urls_for_arch(source, arch), source["url_templates"])

    def test_an_arch_entry_replaces_the_common_list(self):
        source = {
            "url_templates": ["https://example/{arch}/old"],
            "arch_url_templates": {
                "aarch64_cortex-a53": ["https://example/{arch}/new"],
            },
        }
        self.assertEqual(
            http_template_urls_for_arch(source, "aarch64_cortex-a53"),
            ["https://example/{arch}/new"],
        )
        self.assertEqual(
            http_template_urls_for_arch(source, "x86_64"),
            ["https://example/{arch}/old"],
        )

    def test_the_singular_template_form_still_works(self):
        source = {"url_template": "https://example/{arch}/one"}
        self.assertEqual(http_template_urls(source), ["https://example/{arch}/one"])
        self.assertEqual(
            http_template_urls_for_arch(source, "x86_64"),
            ["https://example/{arch}/one"],
        )

    def test_an_arch_only_source_has_no_urls_for_other_arches(self):
        source = {"arch_url_templates": {"x86_64": ["https://example/x86"]}}
        self.assertEqual(http_template_urls_for_arch(source, "x86_64"), ["https://example/x86"])
        self.assertEqual(http_template_urls_for_arch(source, "aarch64_generic"), [])


def http_config(urls, matrix=None, overrides=None):
    source = {"provider": "http_template", "url_templates": urls}
    if overrides:
        source["arch_url_templates"] = overrides
    return {
        "mgr": "opkg",
        "source": source,
        "build": {"matrix": matrix or [{"platform": "all", "arch": "all"}]},
    }


class PlanTest(unittest.TestCase):
    def test_http_override_replaces_common_for_one_arch(self):
        matrix = [
            {"platform": "amd64", "arch": "x86_64"},
            {"platform": "arm64", "arch": "aarch64_cortex-a53"},
        ]
        config = http_config(
            ["https://example/{arch}/old.ipk"],
            matrix,
            {"aarch64_cortex-a53": ["https://example/{arch}/new.ipk"]},
        )
        requests, errors, _, _ = plan_http_assets(config, "/stage", "demo")
        self.assertEqual(errors, [])
        urls = {request["arch"]: request["url"] for request in requests}
        self.assertEqual(urls["aarch64_cortex-a53"], "https://example/aarch64_cortex-a53/new.ipk")
        self.assertEqual(urls["x86_64"], "https://example/x86_64/old.ipk")

    def test_http_plan_requires_a_url_for_every_matrix_arch(self):
        matrix = [
            {"platform": "amd64", "arch": "x86_64"},
            {"platform": "arm64", "arch": "aarch64_cortex-a53"},
        ]
        config = http_config([], matrix, {"x86_64": ["https://example/x86.ipk"]})
        requests, errors, _, _ = plan_http_assets(config, "/stage", "demo")
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("aarch64_cortex-a53", errors[0]["reason"])

    def test_http_plan_reports_two_resources_writing_one_path(self):
        config = http_config(["https://a.example/same.ipk", "https://b.example/same.ipk"])
        requests, errors, _, _ = plan_http_assets(config, "/stage", "demo")
        requests, dedupe_errors = dedupe_requests(requests, "demo", "opkg")
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(dedupe_errors), 1)
        self.assertIn("same path", dedupe_errors[0]["reason"])

    def test_github_plan_requires_each_matrix_arch_and_skips_extras(self):
        release = {
            "tag_name": "v1",
            "assets": [
                {"name": "demo-1.0-r1-x86_64.apk", "browser_download_url": "https://x/x86.apk", "size": 10},
                {"name": "demo-1.0-r1-aarch64_cortex-a76.apk", "browser_download_url": "https://x/a76.apk", "size": 10},
            ],
        }
        config = {
            "mgr": "apk",
            "source": {
                "release": {
                    "assets": [
                        {"name": "demo", "regex": r"^demo-[0-9].*-r[0-9]+-(.+?)\.apk$",
                         "capture_to": {"arch": 1}}
                    ]
                }
            },
            "build": {
                "matrix": [
                    {"platform": "amd64", "arch": "x86_64"},
                    {"platform": "arm64", "arch": "aarch64_cortex-a53"},
                ]
            },
        }
        requests, errors, _, skipped = plan_github_assets(release, config, "/stage", "demo")
        self.assertEqual([request["arch"] for request in requests], ["x86_64"])
        self.assertEqual(len(errors), 1)
        self.assertIn("aarch64_cortex-a53", errors[0]["reason"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("aarch64_cortex-a76", skipped[0]["asset"])

    def test_github_noarch_target_is_one_request(self):
        release = {
            "tag_name": "v1",
            "assets": [{"name": "demo_1.0_all.ipk", "browser_download_url": "https://x/all.ipk", "size": 10}],
        }
        config = {
            "mgr": "opkg",
            "source": {"release": {"assets": [
                {"name": "demo", "regex": r"^demo_.*_all\.ipk$",
                 "target": {"platform": "all", "arch": "all"}}
            ]}},
            "build": {"matrix": [
                {"platform": "amd64", "arch": "x86_64"},
                {"platform": "arm64", "arch": "aarch64_generic"},
            ]},
        }
        requests, errors, _, _ = plan_github_assets(release, config, "/stage", "demo")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["arch"], "all")
        self.assertEqual(errors, [])

    def test_github_optional_missing_is_not_an_error(self):
        config = {
            "mgr": "opkg",
            "source": {"release": {"assets": [
                {"name": "extra", "regex": r"^extra_.*\.ipk$", "required": False}
            ]}},
            "build": {"matrix": [{"platform": "all", "arch": "all"}]},
        }
        requests, errors, optional, _ = plan_github_assets({"tag_name": "v1", "assets": []}, config, "/stage", "demo")
        self.assertEqual(requests, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(optional), 1)

    def test_github_arch_map_maps_a_captured_arch_to_the_matrix_arch(self):
        release = {
            "tag_name": "v1",
            "assets": [{"name": "demo-1.0-r1-aarch64.ipk", "browser_download_url": "https://x/a.ipk", "size": 10}],
        }
        config = {
            "mgr": "opkg",
            "source": {
                "arch_map": {"aarch64": "aarch64_generic"},
                "release": {"assets": [
                    {"name": "demo", "regex": r"^demo-.*-r[0-9]+-(.+?)\.ipk$",
                     "capture_to": {"arch": 1}}
                ]},
            },
            "build": {"matrix": [{"platform": "arm64", "arch": "aarch64_generic"}]},
        }
        requests, errors, _, _ = plan_github_assets(release, config, "/stage", "demo")
        self.assertEqual(errors, [])
        self.assertEqual([request["arch"] for request in requests], ["aarch64_generic"])
        self.assertTrue(requests[0]["dest"].endswith("/arm64/aarch64_generic/demo-1.0-r1-aarch64.ipk"))


class DeclaredPackageTest(unittest.TestCase):
    def test_app_i18n_template_matches_a_language_package(self):
        declared = {
            "app": {"zerotier"},
            "base": set(),
            "kmod": set(),
            "app_i18n": {"luci-i18n-zerotier-{lang}"},
        }
        self.assertTrue(declares_package(declared, "luci-i18n-zerotier-zh-cn"))
        self.assertFalse(declares_package(declared, "luci-i18n-other-zh-cn"))

    def test_tarball_plan_rejects_multiple_matrix_arches(self):
        config = {
            "mgr": "pica",
            "source": {"provider": "tarball", "url_template": "https://x/{arch}.bin"},
            "build": {"matrix": [
                {"platform": "amd64", "arch": "x86_64"},
                {"platform": "arm64", "arch": "aarch64_generic"},
            ]},
        }
        requests, errors, _, _ = plan_tarball_assets(config, "/stage", "demo")
        self.assertEqual(requests, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot represent", errors[0]["reason"])


def package_files(root, name, urls):
    (root / "src" / "opkg" / name).mkdir(parents=True, exist_ok=True)
    (root / "ci" / "opkg" / name).mkdir(parents=True, exist_ok=True)
    (root / "src" / "opkg" / name / "manifest").write_text(
        f"pkgname = {name}\nappname = {name}\npkgver = 1.0.0\npkgrel = 1\n"
        "os = openwrt\ntargets = opkg\n",
        encoding="utf-8",
    )
    config = {
        "schema": "pica-ci-job-v1",
        "mgr": "opkg",
        "id": name,
        "manifest": f"src/opkg/{name}/manifest",
        "source": {"provider": "http_template", "url_templates": urls},
        "build": {
            "matrix": [{"platform": "all", "arch": "all"}],
            "include_dirs": ["scripts", "root", "pkgs"],
            "pack": {"tool": "pica-pack", "format": "pkg.tar.gz"},
        },
    }
    (root / "ci" / "opkg" / name / "build.json").write_text(json.dumps(config), encoding="utf-8")


class PreflightIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pica-preflight-test-")
        self.root = Path(self.temp.name)
        (self.root / "assets").mkdir()
        (self.root / "bin").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_preflight(self, packages, stage="stage"):
        return subprocess.run(
            [sys.executable, SCRIPT, "--preflight", "--stage-root", str(self.root / stage), *packages],
            cwd=self.root,
            capture_output=True,
            text=True,
        )

    def fake_pack(self, exit_code=0):
        path = self.root / "bin" / "pica-pack"
        path.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "$CALLS"\n'
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def pipeline(self, packages, stage="stage", pack_exit=0):
        """The workflow's order: preflight, then pack only if it succeeded."""
        preflight = self.run_preflight(packages, stage)
        calls = self.root / "calls.log"
        release = self.root / "release.log"
        if preflight.returncode != 0:
            return preflight, calls, release
        pack = self.fake_pack(pack_exit)
        env = dict(os.environ, CALLS=str(calls))
        for source in packages:
            relative = Path(source).relative_to(self.root)
            stage_dir = self.root / stage / relative.parts[1] / relative.parts[2]
            result = subprocess.run(
                [str(pack), "build", str(stage_dir), "--outdir", str(self.root / "out")],
                cwd=self.root,
                env=env,
            )
            if result.returncode != 0:
                return preflight, calls, release
        release.write_text("release\n", encoding="utf-8")
        return preflight, calls, release

    def test_missing_second_package_blocks_before_any_pack(self):
        (self.root / "assets" / "foo.ipk").write_bytes(b"fake foo")
        package_files(self.root, "foo", [f"file://{self.root}/assets/foo.ipk"])
        package_files(self.root, "bar", [f"file://{self.root}/assets/missing.ipk"])
        packages = [str(self.root / "src/opkg/foo"), str(self.root / "src/opkg/bar")]
        preflight, calls, release = self.pipeline(packages)
        self.assertNotEqual(preflight.returncode, 0)
        self.assertFalse(calls.exists(), "pica-pack must not run when preflight fails")
        self.assertFalse(release.exists(), "release must not run when preflight fails")
        self.assertIn("BUILD PHASE NOT STARTED", preflight.stdout)

    def test_multiple_package_errors_are_aggregated(self):
        package_files(self.root, "foo", [f"file://{self.root}/assets/foo-missing.ipk"])
        package_files(self.root, "bar", [f"file://{self.root}/assets/bar-missing.ipk"])
        result = self.run_preflight([str(self.root / "src/opkg/foo"), str(self.root / "src/opkg/bar")])
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foo/opkg", combined)
        self.assertIn("bar/opkg", combined)
        self.assertIn("Validation failures: 2", combined)

    def test_one_of_two_required_assets_missing_blocks(self):
        (self.root / "assets" / "present.ipk").write_bytes(b"present")
        package_files(
            self.root,
            "foo",
            [f"file://{self.root}/assets/present.ipk", f"file://{self.root}/assets/missing.ipk"],
        )
        result = self.run_preflight([str(self.root / "src/opkg/foo")])
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.ipk", combined)
        self.assertIn("BUILD PHASE NOT STARTED", combined)

    def test_stale_stage_file_cannot_satisfy_a_missing_asset(self):
        package_files(self.root, "foo", [f"file://{self.root}/assets/missing.ipk"])
        stale = self.root / "stage/opkg/foo/pkgs/opkg/all/all/foo.ipk"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")
        result = self.run_preflight([str(self.root / "src/opkg/foo")])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(stale.exists(), "the preflight must clear the stage before checking")

    def test_success_stages_assets_once_and_pack_consumes_them(self):
        (self.root / "assets" / "foo.ipk").write_bytes(b"foo")
        (self.root / "assets" / "bar.ipk").write_bytes(b"bar")
        package_files(self.root, "foo", [f"file://{self.root}/assets/foo.ipk"])
        package_files(self.root, "bar", [f"file://{self.root}/assets/bar.ipk"])
        packages = [str(self.root / "src/opkg/foo"), str(self.root / "src/opkg/bar")]
        preflight, calls, release = self.pipeline(packages)
        self.assertEqual(preflight.returncode, 0, preflight.stdout + preflight.stderr)
        self.assertEqual(preflight.stdout.count("Downloading "), 2, "one download per asset")
        self.assertTrue((self.root / "stage/opkg/foo/pkgs/opkg/all/all/foo.ipk").is_file())
        self.assertTrue((self.root / "stage/opkg/bar/pkgs/opkg/all/all/bar.ipk").is_file())
        self.assertEqual(len(calls.read_text().splitlines()), 2)
        self.assertTrue(release.exists())

    def test_pack_failure_prevents_release(self):
        (self.root / "assets" / "foo.ipk").write_bytes(b"foo")
        package_files(self.root, "foo", [f"file://{self.root}/assets/foo.ipk"])
        preflight, calls, release = self.pipeline(
            [str(self.root / "src/opkg/foo")], pack_exit=1
        )
        self.assertEqual(preflight.returncode, 0)
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        self.assertFalse(release.exists(), "a failed pack must not reach the release phase")

    def test_empty_selection_is_a_no_op(self):
        result = self.run_preflight([])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Packages checked: 0", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
