import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

from ci.nest import feeds, net, package, sources, stage

from .helpers import LUCI_MANIFEST, TempRepo, make_ipk

PACKAGES_TEXT = """\
Package: demo
Version: 1.2.3-r1
Architecture: x86_64
Filename: demo_1.2.3-r1_x86_64.ipk
Size: 10
SHA256sum: {sha}

Package: luci-app-demo
Version: 26.1.2~abc-r1
Architecture: all
Filename: luci-app-demo_26.1.2~abc-r1_all.ipk
Size: 20
"""


class FeedParsing(unittest.TestCase):
    def test_packages(self):
        index = feeds.parse_packages(PACKAGES_TEXT.format(sha="A" * 64), "https://f/x86_64/luci", "luci")
        self.assertEqual(index["demo"].url, "https://f/x86_64/luci/demo_1.2.3-r1_x86_64.ipk")
        self.assertEqual(index["demo"].sha256, "a" * 64)
        self.assertEqual(index["luci-app-demo"].arch, "all")
        self.assertEqual(index["demo"].size, 10)

    def test_apk_index_with_abi_named_file(self):
        document = {"architecture": "x86_64", "packages": {"node": "22.1-r1", "npm": "10-r1"}}
        listing = ["node127-22.1-r1.apk", "npm-10-r1.apk"]
        index = feeds.parse_apk_index(document, "https://f/p", "packages", listing)
        self.assertEqual(index["node"].filename, "node127-22.1-r1.apk")
        self.assertEqual(index["node127"].filename, "node127-22.1-r1.apk")
        self.assertEqual(index["npm"].filename, "npm-10-r1.apk")

    def test_apk_index_without_listing(self):
        index = feeds.parse_apk_index({"packages": {"a": "1-r1"}}, "https://f/p")
        self.assertEqual(index["a"].filename, "a-1-r1.apk")

    def test_listing(self):
        html = '<a href="../">..</a><a href="a-1.apk">a</a><a href="b_1_all.ipk">b</a><a href="sub/">s</a>'
        self.assertEqual(feeds.parse_listing(html), ["a-1.apk", "b_1_all.ipk"])


class FeedPlanning(TempRepo):
    def setUp(self):
        super().setUp()
        feeds._INDEXES.clear()

    def fake_get(self, url, attempts=3):
        if url.endswith("/luci/Packages.gz") or url.endswith("/packages/Packages.gz") or url.endswith("/base/Packages.gz"):
            raise net.FetchError(url, "missing", "HTTP 404")
        if url.endswith("/luci/Packages"):
            return PACKAGES_TEXT.format(sha="b" * 64)
        raise net.FetchError(url, "missing", "HTTP 404")

    def test_feed_plan_follows_version_per_arch(self):
        directory = self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), {
            "matrix": ["x86_64"],
            "source": {"type": "feed", "feed": "immortalwrt", "packages": ["demo", "luci-app-demo"]},
        })
        pkg = package.Package(directory)
        with mock.patch.object(net, "get_bytes", side_effect=net.FetchError("u", "missing", "404")), \
                mock.patch.object(net, "get_text", side_effect=self.fake_get):
            result = sources.plan(pkg, self.config)
        self.assertTrue(result.ok, result.errors)
        arch = result.arches[0]
        self.assertEqual(arch.version, "1.2.3")
        self.assertEqual(arch.platform, "amd64")
        self.assertEqual([r.name for r in arch.requests], ["demo_1.2.3-r1_x86_64.ipk", "luci-app-demo_26.1.2~abc-r1_all.ipk"])
        self.assertEqual(arch.requests[0].sha256, "b" * 64)

    def test_missing_required_package_fails(self):
        directory = self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), {
            "matrix": ["x86_64"],
            "source": {"type": "feed", "feed": "immortalwrt", "packages": ["nope"], "optional": ["demo"]},
        })
        with mock.patch.object(net, "get_bytes", side_effect=net.FetchError("u", "missing", "404")), \
                mock.patch.object(net, "get_text", side_effect=self.fake_get):
            result = sources.plan(package.Package(directory), self.config)
        self.assertFalse(result.ok)
        self.assertIn("nope is not in feed", result.errors[0])

    def test_pinned_version_must_match_the_feed(self):
        manifest = LUCI_MANIFEST.format(mgr="opkg") + "pkgver = 9.9\n"
        directory = self.add_package("opkg/demo", manifest, {
            "matrix": ["x86_64"],
            "source": {"type": "feed", "feed": "immortalwrt", "packages": ["demo"]},
        })
        with mock.patch.object(net, "get_bytes", side_effect=net.FetchError("u", "missing", "404")), \
                mock.patch.object(net, "get_text", side_effect=self.fake_get):
            result = sources.plan(package.Package(directory), self.config)
        self.assertIn("pins 9.9", " ".join(result.errors))

    def test_unreachable_feed_is_flagged(self):
        directory = self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), {
            "matrix": ["x86_64"],
            "source": {"type": "feed", "feed": "immortalwrt", "packages": ["demo"]},
        })
        with mock.patch.object(net, "get_bytes", side_effect=net.FetchError("u", "unreachable", "HTTP 503")):
            result = sources.plan(package.Package(directory), self.config)
        self.assertTrue(result.unreachable)


RELEASES = [
    {"tag_name": "v2.0-beta", "prerelease": True, "assets": []},
    {"tag_name": "v1.5.0", "assets": [
        {"name": "demo_1.5.0_x86_64.ipk", "browser_download_url": "https://gh/demo_1.5.0_x86_64.ipk", "size": 5,
         "digest": "sha256:" + "c" * 64},
        {"name": "demo_1.5.0_aarch64.ipk", "browser_download_url": "https://gh/demo_1.5.0_aarch64.ipk", "size": 6},
        {"name": "luci-app-demo_1.5.0_all.ipk", "browser_download_url": "https://gh/luci-app-demo_1.5.0_all.ipk"},
    ]},
]


class GithubPlanning(TempRepo):
    def job(self, **source):
        base = {"type": "github", "repo": "o/demo", "arch_map": {"aarch64_generic": "aarch64"}, "assets": [
            {"match": r"^demo_{version}_(x86_64|aarch64)\.ipk$"},
            {"match": r"^luci-app-demo_.*_all\.ipk$"},
        ]}
        base.update(source)
        return {"matrix": ["x86_64", "aarch64_generic"], "source": base}

    def plan(self, manifest_extra, **source):
        directory = self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg") + manifest_extra, self.job(**source))
        with mock.patch.object(net, "github_api", return_value=RELEASES):
            return sources.plan(package.Package(directory), self.config)

    def test_pinned_tag_template(self):
        result = self.plan("pkgver = 1.5.0\n", tag="v{version}")
        self.assertTrue(result.ok, result.errors)
        x86, arm = result.arches
        self.assertEqual([r.name for r in x86.requests], ["demo_1.5.0_x86_64.ipk", "luci-app-demo_1.5.0_all.ipk"])
        self.assertEqual([r.name for r in arm.requests], ["demo_1.5.0_aarch64.ipk", "luci-app-demo_1.5.0_all.ipk"])
        self.assertEqual(x86.requests[0].sha256, "c" * 64)

    def test_follow_latest_stable(self):
        result = self.plan("", assets=[{"match": r"^demo_.*_(x86_64|aarch64)\.ipk$"}])
        self.assertTrue(result.ok, result.errors)
        self.assertEqual({a.version for a in result.arches}, {"1.5.0"})

    def test_upstream_moved_on(self):
        result = self.plan("pkgver = 1.4.0\n", assets=[{"match": r"^demo_.*_(x86_64|aarch64)\.ipk$"}])
        self.assertFalse(result.ok)
        self.assertIn("upstream moved on", result.errors[0])

    def test_missing_arch_asset(self):
        directory = self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg") + "pkgver = 1.5.0\n", {
            "matrix": ["x86_64", "mipsel_24kc"],
            "source": {"type": "github", "repo": "o/demo", "tag": "v{version}",
                       "assets": [{"match": r"^demo_{version}_(x86_64|mipsel_24kc)\.ipk$"}]},
        })
        with mock.patch.object(net, "github_api", return_value=RELEASES):
            result = sources.plan(package.Package(directory), self.config)
        self.assertIn("matches nothing for mipsel_24kc", " ".join(result.errors))


class Staging(TempRepo):
    def test_unpack_tar_with_strip_and_include(self):
        work = tempfile.mkdtemp(dir=self.root)
        archive = os.path.join(work, "a.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("pkg-1/bin/tool", "pkg-1/share/man/tool.1", "pkg-1/LICENSE"):
                source = os.path.join(work, "src")
                with open(source, "w") as handle:
                    handle.write(name)
                tar.add(source, arcname=name)
        dest = os.path.join(work, "out")
        written = stage.unpack(archive, dest, {"strip": 1, "include": ["bin/", "LICENSE"]})
        self.assertEqual(sorted(os.path.relpath(p, dest).replace(os.sep, "/") for p in written), ["LICENSE", "bin/tool"])

    def test_unpack_bare_file_with_rename(self):
        work = tempfile.mkdtemp(dir=self.root)
        bare = os.path.join(work, "tool-linux-amd64")
        with open(bare, "wb") as handle:
            handle.write(b"\x7fELF")
        written = stage.unpack(bare, os.path.join(work, "out"), {"rename": "tool"})
        self.assertTrue(written[0].endswith(os.path.join("out", "tool")))

    def test_unpack_refuses_traversal(self):
        work = tempfile.mkdtemp(dir=self.root)
        archive = os.path.join(work, "evil.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape", "x")
        with self.assertRaises(Exception):
            stage.unpack(archive, os.path.join(work, "out"), {})

    def test_fetch_and_verify(self):
        directory = self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), {
            "matrix": ["x86_64"],
            "source": {"type": "feed", "feed": "immortalwrt", "packages": ["demo", "luci-app-demo"]},
        }, {"scripts/install": "#!/bin/sh\n"})
        pkg = package.Package(directory)
        result = sources.Plan(pkg)
        arch = sources.ArchPlan("x86_64", "amd64", "1.2.3")
        arch.requests = [sources.Request("https://f/demo_1.2.3-r1_x86_64.ipk", "container"),
                         sources.Request("https://f/luci-app-demo_1_all.ipk", "container"),
                         sources.Request("https://f/luci-i18n-demo-zh-cn_1_all.ipk", "container", optional=True)]
        result.arches = [arch]
        fixtures = tempfile.mkdtemp(dir=self.root)
        make_ipk(os.path.join(fixtures, "demo"), "demo", "1.2.3-r1", "x86_64")
        make_ipk(os.path.join(fixtures, "luci"), "luci-app-demo", "1", "all")

        def fake_download(url, dest, size=None, sha256=None, cache_dir=None, attempts=3):
            if "i18n" in url:
                raise net.FetchError(url, "missing", "HTTP 404")
            shutil.copyfile(os.path.join(fixtures, "demo" if "/demo_" in url else "luci"), dest)

        stage_root = os.path.join(self.root, "stage")
        with mock.patch.object(net, "download", side_effect=fake_download), \
                mock.patch("ci.nest.util.warn") as warned:
            stages = stage.fetch(result, stage_root)
        self.assertIn("optional luci-i18n-demo-zh-cn_1_all.ipk skipped", warned.call_args[0][0])
        staged = stages["x86_64"]
        with open(os.path.join(staged, "manifest"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("pkgver = 1.2.3", text)
        self.assertIn("platform = amd64", text)
        self.assertIn("arch = x86_64", text)
        self.assertTrue(os.path.isfile(os.path.join(staged, "scripts", "install")))
        pkgs = os.path.join(staged, "pkgs", "opkg", "amd64", "x86_64")
        self.assertEqual(sorted(os.listdir(pkgs)), ["demo_1.2.3-r1_x86_64.ipk", "luci-app-demo_1_all.ipk"])
        errors, _notes = stage.verify(result, stages)
        self.assertEqual(errors, [])

        # A container nobody declares stops the build.
        make_ipk(os.path.join(pkgs, "intruder_1_all.ipk"), "intruder", "1", "all")
        errors, _ = stage.verify(result, stages)
        self.assertTrue(any("intruder" in e for e in errors))

    def test_wrong_arch_container(self):
        directory = self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), {
            "matrix": ["x86_64"], "source": {"type": "feed", "feed": "immortalwrt", "packages": ["demo"]}})
        result = sources.Plan(package.Package(directory))
        result.arches = [sources.ArchPlan("x86_64", "amd64", "1")]
        staged = os.path.join(self.root, "s")
        pkgs = os.path.join(staged, "pkgs", "opkg", "amd64", "x86_64")
        os.makedirs(pkgs)
        make_ipk(os.path.join(pkgs, "demo.ipk"), "demo", "1", "mipsel_24kc")
        errors, _ = stage.verify(result, {"x86_64": staged})
        self.assertIn("built for mipsel_24kc", " ".join(errors))


if __name__ == "__main__":
    unittest.main()
