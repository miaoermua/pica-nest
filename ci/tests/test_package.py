import os

from ci.nest import package

from .helpers import LUCI_MANIFEST, TempRepo

FEED_JOB = {
    "matrix": ["x86_64", "aarch64_generic"],
    "source": {"type": "feed", "feed": "immortalwrt", "packages": ["demo", "luci-app-demo"]},
}


class PackageValidation(TempRepo):
    def errors(self, key, manifest, job, files=None):
        directory = self.add_package(key, manifest, job, files)
        return package.Package(directory).errors(self.config)

    def test_a_good_feed_package(self):
        self.assertEqual(self.errors("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), FEED_JOB), [])

    def test_directory_mgr_must_be_in_targets(self):
        problems = self.errors("apk/demo", LUCI_MANIFEST.format(mgr="opkg"), FEED_JOB)
        self.assertTrue(any("must include 'apk'" in p for p in problems), problems)

    def test_managed_fields_are_refused(self):
        manifest = LUCI_MANIFEST.format(mgr="opkg") + "pkgmgr = opkg\narch = x86_64\nvisibility = open\n"
        problems = " ".join(self.errors("opkg/demo", manifest, FEED_JOB))
        for field in ("pkgmgr", "arch", "visibility"):
            self.assertIn(f"`{field}`", problems)

    def test_tilde_in_pkgver_is_refused(self):
        manifest = LUCI_MANIFEST.format(mgr="opkg") + "pkgver = 26.1~abc\n"
        job = {"matrix": ["all"], "source": {"type": "url", "urls": ["https://x/demo_{version}_all.ipk"]}}
        problems = " ".join(self.errors("opkg/demo", manifest, job))
        self.assertIn("`~`", problems)

    def test_url_source_needs_a_pinned_version(self):
        job = {"matrix": ["all"], "source": {"type": "url", "urls": ["https://x/demo_all.ipk"]}}
        problems = " ".join(self.errors("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), job))
        self.assertIn("only feed and github sources can supply one", problems)

    def test_unknown_feed_and_arch(self):
        job = {"matrix": ["riscv64"], "source": {"type": "feed", "feed": "nope", "packages": ["demo"]}}
        problems = " ".join(self.errors("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), job))
        self.assertIn("'riscv64' is not in ci/config.json", problems)
        self.assertIn("feed 'nope' is not defined", problems)

    def test_pica_target_cannot_read_dependencies(self):
        manifest = "pkgname = t\nappname = t\nos = openwrt\npkgver = 1\ntargets = opkg, pica\nbase = bash\n"
        job = {"matrix": ["all"], "source": {"type": "archive", "url": "https://x/t.sh"}}
        problems = " ".join(self.errors("opkg/t", manifest, job, {"root/x": "x"}))
        self.assertIn("scope them as `base@opkg`", problems)

    def test_scoped_dependencies_pass_for_pica(self):
        manifest = "pkgname = t\nappname = t\nos = openwrt\npkgver = 1\ntargets = opkg, pica\nbase@opkg = bash\n"
        job = {"matrix": ["all"], "source": {"type": "archive", "url": "https://x/t.sh"}}
        self.assertEqual(self.errors("opkg/t", manifest, job), [])

    def test_github_asset_rules(self):
        job = {
            "matrix": ["x86_64"],
            "source": {
                "type": "github", "repo": "o/r", "tag": "v1", "tag_regex": "x",
                "assets": [{"match": "(a)(b)"}, {"match": "[", "extra": 1}],
            },
        }
        problems = " ".join(self.errors("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), job))
        self.assertIn("not both", problems)
        self.assertIn("captures 2", problems)
        self.assertIn("does not compile", problems)
        self.assertIn("extra is not an asset field", problems)

    def test_missing_script_file(self):
        manifest = LUCI_MANIFEST.format(mgr="opkg") + "script_install = scripts/install\n"
        problems = " ".join(self.errors("opkg/demo", manifest, FEED_JOB))
        self.assertIn("scripts/install", problems)

    def test_stray_files_are_reported(self):
        problems = " ".join(self.errors("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), FEED_JOB, {"pkgs/x.ipk": ""}))
        self.assertIn("unexpected entry 'pkgs'", problems)

    def test_discover_and_select(self):
        self.add_package("opkg/a", LUCI_MANIFEST.format(mgr="opkg"), FEED_JOB)
        self.add_package("apk/b", LUCI_MANIFEST.format(mgr="apk"), FEED_JOB)
        found = package.discover(self.packages)
        self.assertEqual([p.key for p in found], ["apk/b", "opkg/a"])
        chosen = package.select(["packages/opkg/a"], self.packages)
        self.assertEqual([p.key for p in chosen], ["opkg/a"])
        with self.assertRaises(Exception):
            package.select(["opkg/zzz"], self.packages)
        self.assertTrue(os.path.isdir(found[0].dir))
