#!/usr/bin/env python3
"""Tests for the `r/` index layout: descriptor, backend views, and selection."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import repo_layout  # noqa: E402


def package(name, manager, arch="x86_64", platform="amd64", sha="a" * 64):
    return {
        "pkgname": name,
        "pkgver": "1.0",
        "pkgrel": "1",
        "pkgmgr": manager,
        "platform": platform,
        "arch": arch,
        "filename": f"{name}-1.0-1-{platform}-{arch}-{manager}.pkg.tar.gz",
        "sha256": sha,
        "download_url": f"https://example.invalid/{name}.pkg.tar.gz",
    }


class PathSafetyTest(unittest.TestCase):
    """A descriptor path is resolved against a base the client chooses."""

    def test_plain_relative_paths_are_accepted(self):
        for path in ("index.json", "apk/index.json", "pica/index.json", "a/b/c.json"):
            self.assertEqual(repo_layout.check_relative_path(path), path)

    def test_absolute_and_hosted_paths_are_refused(self):
        for path in (
            "/apk/index.json",
            "https://example.com/apk/index.json",
            "//example.com/apk/index.json",
            "C:/apk/index.json",
            "apk\\index.json",
        ):
            with self.assertRaises(repo_layout.LayoutError, msg=path):
                repo_layout.check_relative_path(path)

    def test_traversal_and_empty_segments_are_refused(self):
        for path in ("../index.json", "apk/../../index.json", "apk//index.json", "apk/./index.json"):
            with self.assertRaises(repo_layout.LayoutError, msg=path):
                repo_layout.check_relative_path(path)

    def test_query_fragment_and_non_json_are_refused(self):
        for path in ("apk/index.json?v=1", "apk/index.json#x", "apk/index.txt", "apk", ""):
            with self.assertRaises(repo_layout.LayoutError, msg=path):
                repo_layout.check_relative_path(path)

    def test_a_symlink_out_of_the_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "r"
            outside = Path(tmp) / "elsewhere"
            outside.mkdir()
            (outside / "index.json").write_text("{}", encoding="utf-8")
            root.mkdir()
            os.symlink(outside, root / "apk")
            with self.assertRaises(repo_layout.LayoutError):
                repo_layout.resolve(str(root), "apk/index.json")


class WriteLayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "r"
        self.root.mkdir()

    def write(self, packages, name="Pica", description=None, generated_at="2026-10-22T00:00:00Z"):
        return repo_layout.write_layout(
            str(self.root), packages, name, description, generated_at
        )

    def test_one_index_per_backend_that_has_packages(self):
        packages = [package("demo", "apk"), package("demo", "opkg"), package("demo", "pica")]
        entry, written, pruned = self.write(packages)

        self.assertEqual(sorted(entry["managers"]), ["apk", "opkg", "pica"])
        self.assertEqual(sorted(written), ["apk/index.json", "index.json", "opkg/index.json", "pica/index.json"])
        self.assertEqual(pruned, [])
        for manager in ("apk", "opkg", "pica"):
            index = json.loads((self.root / manager / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["mgr"], manager)
            self.assertEqual([pkg["pkgmgr"] for pkg in index["packages"]], [manager])

    def test_a_backend_without_packages_is_not_declared(self):
        entry, _, _ = self.write([package("demo", "apk")])

        self.assertEqual(list(entry["managers"]), ["apk"])
        self.assertFalse((self.root / "pica" / "index.json").exists())

    def test_a_backend_that_loses_its_last_package_is_removed(self):
        self.write([package("demo", "apk"), package("demo", "pica")])
        entry, _, pruned = self.write([package("demo", "apk")])

        self.assertEqual(pruned, ["pica/index.json"])
        self.assertFalse((self.root / "pica" / "index.json").exists())
        self.assertNotIn("pica", entry["managers"])

    def test_an_unchanged_layout_is_not_rewritten(self):
        packages = [package("demo", "apk")]
        self.write(packages)
        entry, written, _ = self.write(packages)

        self.assertEqual(written, [], "a rebuild with identical content must not touch the files")
        self.assertEqual(entry["managers"]["apk"]["path"], "apk/index.json")

    def test_an_unknown_backend_is_refused(self):
        with self.assertRaises(repo_layout.LayoutError):
            self.write([package("demo", "npm")])


class ValidateLayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "r"
        self.root.mkdir()
        self.packages = [package("demo", "apk"), package("pica-one", "pica", platform="all", arch="all")]
        self.write_repo()
        repo_layout.write_layout(str(self.root), self.packages, "Pica", None, "2026-10-22T00:00:00Z")

    def write_repo(self):
        (self.root / "repo.json").write_text(
            json.dumps({"schema": 2, "packages": self.packages}, indent=2),
            encoding="utf-8",
        )

    def validate(self):
        entry = repo_layout.load_json(str(self.root / "index.json"))
        managers, errors = repo_layout.validate_entry(entry, str(self.root))
        repo = repo_layout.load_json(str(self.root / "repo.json"))
        errors += repo_layout.consistency_errors(repo["packages"], managers, str(self.root))
        return managers, errors

    def test_a_generated_layout_is_valid(self):
        managers, errors = self.validate()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(managers), ["apk", "pica"])

    def test_a_backend_index_that_drifted_from_repo_json_is_caught(self):
        path = self.root / "apk" / "index.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        index["packages"][0]["sha256"] = "c" * 64
        path.write_text(json.dumps(index), encoding="utf-8")

        _, errors = self.validate()
        self.assertTrue(any("differs from repo.json" in err for err in errors), errors)

    def test_a_declared_index_that_is_missing_is_caught(self):
        (self.root / "pica" / "index.json").unlink()

        _, errors = self.validate()
        self.assertTrue(any("not present" in err for err in errors), errors)

    def test_a_backend_index_for_the_wrong_manager_is_caught(self):
        path = self.root / "pica" / "index.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        index["mgr"] = "apk"
        path.write_text(json.dumps(index), encoding="utf-8")

        _, errors = self.validate()
        self.assertTrue(any("does not match its declaration" in err for err in errors), errors)

    def test_a_host_smuggled_into_the_descriptor_is_caught(self):
        path = self.root / "index.json"
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["managers"]["apk"]["download_base"] = "https://mirror.example"
        path.write_text(json.dumps(entry), encoding="utf-8")

        _, errors = self.validate()
        self.assertTrue(any("only `path` belongs here" in err for err in errors), errors)

    def test_a_package_in_repo_json_without_a_declared_backend_is_caught(self):
        self.packages.append(package("demo", "opkg"))
        self.write_repo()

        _, errors = self.validate()
        self.assertTrue(any("no index is declared" in err for err in errors), errors)

    def test_an_empty_source_declares_no_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "r"
            root.mkdir()
            entry, written, _ = repo_layout.write_layout(str(root), [], "Pica")
            self.assertEqual(entry["managers"], {})
            self.assertEqual(written, ["index.json"])
            errors = repo_layout.consistency_errors([], {}, str(root))
            self.assertEqual(errors, [])


class SelectManagersTest(unittest.TestCase):
    ENTRY = {
        "schema": 1,
        "name": "Pica",
        "managers": {
            "apk": {"path": "apk/index.json"},
            "opkg": {"path": "opkg/index.json"},
            "pica": {"path": "pica/index.json"},
        },
    }

    def test_a_device_picks_only_the_backends_it_runs(self):
        for wanted, expected in (
            ("apk,pica", {"apk": "apk/index.json", "pica": "pica/index.json"}),
            ("opkg,pica", {"opkg": "opkg/index.json", "pica": "pica/index.json"}),
            ("apk,opkg,pica", {"apk": "apk/index.json", "opkg": "opkg/index.json", "pica": "pica/index.json"}),
        ):
            selected, missing = repo_layout.select_managers(self.ENTRY, wanted)
            self.assertEqual(selected, expected, wanted)
            self.assertEqual(missing, [], wanted)

    def test_an_undeclared_backend_is_reported_rather_than_substituted(self):
        entry = {"schema": 1, "name": "Pica", "managers": {"apk": {"path": "apk/index.json"}}}
        selected, missing = repo_layout.select_managers(entry, "apk,opkg")

        self.assertEqual(selected, {"apk": "apk/index.json"})
        self.assertEqual(missing, ["opkg"])

    def test_duplicates_and_case_do_not_produce_a_second_entry(self):
        selected, _ = repo_layout.select_managers(self.ENTRY, "apk, APK ,apk")
        self.assertEqual(selected, {"apk": "apk/index.json"})


class CliTest(unittest.TestCase):
    """The validator has to fail, not warn, when a published index disagrees."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "r"
        self.root.mkdir()
        packages = [package("demo", "apk"), package("demo", "pica", platform="all", arch="all")]
        (self.root / "repo.json").write_text(
            json.dumps({"schema": 2, "packages": packages}, indent=2), encoding="utf-8"
        )
        repo_layout.write_layout(str(self.root), packages, "Pica", None, "2026-10-22T00:00:00Z")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "validate_index_layout.py"), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
        )

    def test_the_generated_layout_passes(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("declares 2 backend index(es)", result.stdout)

    def test_selection_prints_the_chosen_paths(self):
        result = self.run_cli("--select", "apk,pica")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("apk\tapk/index.json", result.stdout)
        self.assertIn("pica\tpica/index.json", result.stdout)

    def test_selecting_an_undeclared_backend_fails(self):
        result = self.run_cli("--select", "apk,opkg")
        self.assertEqual(result.returncode, 1)
        self.assertIn("opkg not declared", result.stderr)

    def test_drift_between_the_views_fails_the_run(self):
        path = self.root / "pica" / "index.json"
        index = json.loads(path.read_text(encoding="utf-8"))
        index["packages"].pop()
        path.write_text(json.dumps(index), encoding="utf-8")

        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("differs from repo.json", result.stderr)


if __name__ == "__main__":
    unittest.main()
