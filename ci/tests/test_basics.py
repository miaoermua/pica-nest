import unittest

from ci.nest import util
from ci.nest.manifest import Manifest, dependency_name

from .helpers import config


class Versions(unittest.TestCase):
    def test_publishable_version(self):
        cases = {
            ("0.107.76-r1", True): "0.107.76",
            ("26.236.50544~cb5d434", True): "26.236.50544.cb5d434",
            ("2025.05.02~79a6c62c-r3", True): "2025.05.02.79a6c62c",
            ("1.4.5-2", True): "1.4.5",
            ("1:2.3-r1", True): "2.3",
            ("v3.1.1", False): "3.1.1",
            ("3.9.7-beta2", False): "3.9.7_beta2",
            ("2.0", True): "2.0",
        }
        for (raw, strip), expected in cases.items():
            with self.subTest(raw=raw):
                published = util.publishable_version(raw, strip)
                self.assertEqual(published, expected)
                self.assertNotIn("~", published)
                self.assertNotIn("-", published)

    def test_version_in(self):
        self.assertTrue(util.version_in("adguardhome-0.107.76-r1.apk", "0.107.76"))
        self.assertTrue(util.version_in("v1.8.9", "1.8.9"))
        self.assertTrue(util.version_in("26.156.15499~38397ed", "26.156.15499.38397ed"))
        self.assertFalse(util.version_in("0.107.761", "0.107.76"))
        self.assertFalse(util.version_in("10.107.76", "0.107.76"))

    def test_version_key(self):
        self.assertGreater(util.version_key("1.3.0.30"), util.version_key("1.3"))
        self.assertGreater(util.version_key("3.1.1"), util.version_key("3.0.0"))
        self.assertEqual(util.version_key("1.0.0"), util.version_key("1"))


class ManifestTests(unittest.TestCase):
    TEXT = "# head\npkgname = demo\napp = a\napp = b\nbase@opkg = curl>=7.0\nbase = jq?\n"

    def test_reading(self):
        m = Manifest(self.TEXT)
        self.assertEqual(m.get("pkgname"), "demo")
        self.assertEqual(m.get_all("app"), ["a", "b"])
        self.assertEqual(m.dependencies("opkg")["base"], ["curl>=7.0"])
        self.assertEqual(m.dependencies("pica")["base"], ["jq?"])
        self.assertEqual(m.declared_names(), {"a", "b", "curl", "jq"})

    def test_set_keeps_comments_and_order(self):
        m = Manifest(self.TEXT)
        m.set("pkgname", "other")
        m.set("pkgver", "1.0")
        self.assertTrue(m.text().startswith("# head\npkgname = other\n"))
        self.assertTrue(m.text().endswith("pkgver = 1.0\n"))
        m.remove("app")
        self.assertEqual(m.get_all("app"), [])

    def test_i18n_template(self):
        m = Manifest("app_i18n = luci-i18n-demo-{lang}\n")
        self.assertTrue(m.declares("luci-i18n-demo-zh-cn"))
        self.assertFalse(m.declares("luci-i18n-other-zh-cn"))

    def test_dependency_name(self):
        self.assertEqual(dependency_name("curl>=7.0?"), "curl")
        self.assertEqual(dependency_name(" kmod-tun "), "kmod-tun")

    def test_bad_line_is_reported(self):
        self.assertTrue(Manifest("this is not a pair\n").problems)


class ConfigTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(config().errors(), [])

    def test_feed_needs_arch_and_https(self):
        bad = config(feeds={"x": {"opkg": "http://a/b"}, "y": {"pica": "https://a/{arch}"}})
        problems = " ".join(bad.errors())
        self.assertIn("https", problems)
        self.assertIn("only opkg and apk", problems)

    def test_feed_base(self):
        self.assertEqual(
            config().feed_base("immortalwrt", "apk", "x86_64"),
            "https://feed.example/25.12/packages/x86_64",
        )
        self.assertIsNone(config().feed_base("immortalwrt", "pica", "x86_64"))


if __name__ == "__main__":
    unittest.main()
