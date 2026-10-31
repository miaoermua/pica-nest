import json
import os
import unittest
from unittest import mock

from ci.nest import changes, containers, index, net, package, publish, upstream, util, validate

from .helpers import LUCI_MANIFEST, TempRepo, make_ipk, make_legacy_apk

BASE = "https://github.com/owner/nest/releases/download/latest"


def entry(pkgname="demo", pkgver="1.0", mgr="opkg", platform="amd64", arch="x86_64", sha="a" * 64):
    item = {
        "pkgname": pkgname, "pkgver": pkgver, "pkgrel": "1", "pkgmgr": mgr, "os": "openwrt",
        "platform": platform, "arch": arch, "sha256": sha, "size": 1, "csize": 1,
    }
    item["filename"] = index.expected_filename(item)
    item["download_url"] = f"{BASE}/{item['filename']}"
    return item


class Containers(TempRepo):
    def test_ipk(self):
        file = os.path.join(self.root, "x.ipk")
        make_ipk(file, "luci-app-demo", "1.0-r1", "all")
        self.assertEqual(containers.metadata(file), {"package": "luci-app-demo", "version": "1.0-r1", "architecture": "all"})

    def test_legacy_apk(self):
        file = os.path.join(self.root, "x.apk")
        make_legacy_apk(file, "demo", "1.0-r1", "x86_64")
        self.assertEqual(containers.metadata(file)["package"], "demo")

    def test_garbage_is_unreadable(self):
        file = os.path.join(self.root, "x.apk")
        with open(file, "wb") as handle:
            handle.write(b"ADBd" + b"\x00" * 32)
        self.assertIsNone(containers.metadata(file))


class IndexLayout(TempRepo):
    def test_filename_rule(self):
        self.assertEqual(index.expected_filename(entry()), "demo-1.0-1-amd64-x86_64-opkg.pkg.tar.gz")
        self.assertEqual(index.expected_filename(entry(platform="all", arch="all", mgr="pica")), "demo-1.0-1-all-pica.pkg.tar.gz")

    def test_path_rules(self):
        self.assertIsNone(index.path_problem("opkg/index.json"))
        for bad in ("/abs.json", "../x.json", "a//b.json", "a/%41.json", "https://x/y.json", "index.json"):
            self.assertIsNotNone(index.path_problem(bad), bad)

    def test_descriptor_decides_the_paths(self):
        self.write_descriptor({"opkg": {"path": "v1/opkg.json"}})
        descriptor = index.load_descriptor(self.r)
        written, _ = index.write_layout(self.r, descriptor, {"opkg": {("demo", "amd64", "x86_64"): entry()}})
        self.assertEqual(written, ["v1/opkg.json"])
        self.assertTrue(os.path.isfile(os.path.join(self.r, "v1", "opkg.json")))
        # Unchanged entries do not rewrite the file (no timestamp churn).
        written, _ = index.write_layout(self.r, descriptor, index.read_entries(self.r))
        self.assertEqual(written, [])
        # Moving the path moves the entries and removes the old file.
        self.write_descriptor({"opkg": {"path": "opkg/index.json"}})
        written, removed = index.write_layout(self.r, index.load_descriptor(self.r), index.read_entries(self.r))
        self.assertEqual((written, removed), (["opkg/index.json"], ["v1/opkg.json"]))
        self.assertFalse(os.path.exists(os.path.join(self.r, "v1")))

    def test_undeclared_manager_with_entries_fails(self):
        self.write_descriptor({"opkg": {"path": "opkg/index.json"}})
        with self.assertRaises(util.NestError):
            index.write_layout(self.r, index.load_descriptor(self.r), {"apk": {("d", "all", "all"): entry(mgr="apk")}})

    def test_entry_errors(self):
        self.assertEqual(index.entry_errors("opkg", entry(), BASE), [])
        bad = entry(pkgver="1~x")
        self.assertTrue(any("cannot keep" in p for p in index.entry_errors("opkg", bad, BASE)))
        wrong = entry()
        wrong["download_url"] = "https://elsewhere/x"
        self.assertTrue(index.entry_errors("opkg", wrong, BASE))

    def test_merge_adds_replaces_and_drops(self):
        self.write_descriptor()
        self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg"), {
            "matrix": ["x86_64"], "source": {"type": "feed", "feed": "immortalwrt", "packages": ["demo"]}})
        stale = entry(pkgname="gone")
        util.write_json(os.path.join(self.r, "opkg", "index.json"),
                        {"schema": 1, "mgr": "opkg", "generated_at": "x", "packages": [stale]})
        out = os.path.join(self.root, "out")
        os.makedirs(os.path.join(out, "opkg"))
        artifact_entry = entry()
        artifact = os.path.join(out, "opkg", artifact_entry["filename"])
        with open(artifact, "wb") as handle:
            handle.write(b"x")
        artifact_entry["sha256"] = util.sha256_file(artifact)
        artifact_entry.pop("download_url")
        util.write_json(os.path.join(out, "opkg", "index.json"), {"schema": 1, "mgr": "opkg", "packages": [artifact_entry]})
        summary = index.merge(self.r, out, package.discover(self.packages), self.config, BASE,
                              release_list=os.path.join(self.root, "rel.txt"))
        document = util.read_json(os.path.join(self.r, "opkg", "index.json"))
        self.assertEqual([e["pkgname"] for e in document["packages"]], ["demo"])
        self.assertEqual(document["packages"][0]["download_url"], f"{BASE}/{artifact_entry['filename']}")
        self.assertTrue(any(line.startswith("- opkg") for line in summary["changed"]))
        self.assertEqual(index.layout_errors(self.r, BASE), [])

    def test_merge_refuses_a_lying_index(self):
        self.write_descriptor()
        out = os.path.join(self.root, "out")
        os.makedirs(os.path.join(out, "opkg"))
        item = entry()
        with open(os.path.join(out, "opkg", item["filename"]), "wb") as handle:
            handle.write(b"x")
        util.write_json(os.path.join(out, "opkg", "index.json"), {"schema": 1, "mgr": "opkg", "packages": [item]})
        with self.assertRaises(util.NestError):
            index.merge(self.r, out, [], self.config, BASE)


class Changes(TempRepo):
    def test_classify(self):
        self.add_package("opkg/a", LUCI_MANIFEST.format(mgr="opkg"), {
            "matrix": ["x86_64"], "source": {"type": "feed", "feed": "immortalwrt", "packages": ["a"]}})
        self.add_package("apk/b", LUCI_MANIFEST.format(mgr="apk"), {
            "matrix": ["x86_64"], "source": {"type": "feed", "feed": "immortalwrt", "packages": ["b"]}})
        packages = package.discover(self.packages)
        old = {"feeds": {"immortalwrt": {"opkg": "https://x/24.10.0/{arch}", "apk": "https://x/25.12.0/{arch}"}}}
        new = {"feeds": {"immortalwrt": {"opkg": "https://x/24.10.0/{arch}", "apk": "https://x/25.12.2/{arch}"}}}
        rebuild, removed, reasons = changes.classify(
            ["ci/config.json", "packages/opkg/gone/manifest", "docs/x.md"], old, new, packages)
        self.assertEqual(rebuild, ["apk/b"])
        self.assertEqual(removed, ["opkg/gone"])
        self.assertIn("25.12.2", reasons["apk/b"])
        rebuild, _, _ = changes.classify(["packages/opkg/a/root/etc/x"], old, old, packages)
        self.assertEqual(rebuild, ["opkg/a"])


class Bumps(TempRepo):
    def make(self, pkgver, source, extra=None):
        job = {"matrix": ["x86_64"], "source": source}
        job.update(extra or {})
        directory = self.add_package("opkg/demo", LUCI_MANIFEST.format(mgr="opkg") + f"pkgver = {pkgver}\npkgrel = 3\n", job)
        return package.Package(directory)

    def test_github_bump_and_apply(self):
        pkg = self.make("1.0.0", {"type": "github", "repo": "o/r", "tag": "v{version}",
                                  "assets": [{"match": r"^demo_{version}_(x86_64)\.ipk$"}]})
        releases = [{"tag_name": "v1.1.0", "assets": [{"name": "demo_1.1.0_x86_64.ipk"}]}]
        with mock.patch.object(net, "github_api", return_value=releases):
            bump, reason = upstream.decide(pkg, self.config)
        self.assertIsNone(reason)
        self.assertEqual(bump.candidate, "1.1.0")
        upstream.apply(bump)
        with open(pkg.manifest_file, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("pkgver = 1.1.0", text)
        self.assertIn("pkgrel = 1", text)

    def test_assets_must_still_match(self):
        pkg = self.make("1.0.0", {"type": "github", "repo": "o/r",
                                  "assets": [{"match": r"^demo_{version}_(x86_64)\.ipk$"}]})
        releases = [{"tag_name": "v1.1.0", "assets": [{"name": "renamed_1.1.0.ipk"}]}]
        with mock.patch.object(net, "github_api", return_value=releases):
            bump, reason = upstream.decide(pkg, self.config)
        self.assertIsNone(bump)
        self.assertIn("nothing matches", reason)

    def test_non_version_tags_and_older(self):
        pkg = self.make("1.0.0", {"type": "github", "repo": "o/r", "assets": [{"match": "x"}]})
        for tag, expected in (("r20260101", "not a plain version"), ("v0.9", "not newer")):
            with mock.patch.object(net, "github_api", return_value=[{"tag_name": tag, "assets": []}]):
                bump, reason = upstream.decide(pkg, self.config)
            self.assertIsNone(bump)
            self.assertIn(expected, reason)

    def test_archive_bump_probes_every_url(self):
        pkg = self.make("1.0", {"type": "archive", "url": "https://gh/v{version}/t-{arch}.tgz"},
                        {"upstream": "github:o/t"})
        with mock.patch.object(net, "github_api", return_value=[{"tag_name": "v1.1", "assets": []}]), \
                mock.patch.object(net, "probe", return_value=("missing", "HTTP 404")) as probe:
            bump, reason = upstream.decide(pkg, self.config)
        self.assertIsNone(bump)
        self.assertIn("missing", reason)
        probe.assert_called_once_with("https://gh/v1.1/t-x86_64.tgz")


class Guards(TempRepo):
    def test_binary_guard(self):
        problems = validate.binary_errors(["r/opkg/index.json", "x.pkg.tar.gz", "a/b.ipk", "README.md"])
        self.assertEqual(len(problems), 2)

    def test_stale_keeps_one_generation(self):
        self.write_descriptor()
        util.write_json(os.path.join(self.r, "opkg", "index.json"),
                        {"schema": 1, "mgr": "opkg", "packages": [entry(pkgver="2.0")]})
        previous = os.path.join(self.root, "prev")
        os.makedirs(os.path.join(previous, "opkg"))
        util.write_json(os.path.join(previous, "opkg", "index.json"),
                        {"schema": 1, "mgr": "opkg", "packages": [entry(pkgver="1.0")]})
        assets = [entry(pkgver=v)["filename"] for v in ("0.9", "1.0", "2.0")] + ["notes.txt"]
        self.assertEqual(publish.stale(assets, self.r, previous), [entry(pkgver="0.9")["filename"]])


class RepositoryTree(unittest.TestCase):
    """The committed tree itself must pass: same check CI runs."""

    def test_repository_validates(self):
        from ci.nest import config as config_module
        problems = validate.repository_errors(config_module.load(), package.discover(), check_binaries=False)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_schema_is_json(self):
        with open(util.path("ci", "schema", "build.schema.json"), encoding="utf-8") as handle:
            json.load(handle)


if __name__ == "__main__":
    unittest.main()
