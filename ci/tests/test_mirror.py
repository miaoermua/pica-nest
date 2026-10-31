import json
import os
import unittest

from ci.nest import mirror, net

from .helpers import TempRepo

GENERATED = "2026-10-31T12:00:00Z"


class Drift(TempRepo):
    def publish(self, manager="opkg", generated_at=GENERATED, packages=1):
        self.write_descriptor({manager: {"path": f"{manager}/index.json"}})
        payload = {
            "schema": 1,
            "mgr": manager,
            "generated_at": generated_at,
            "packages": [{"filename": f"demo-{index}.pkg.tar.gz"} for index in range(packages)],
        }
        os.makedirs(os.path.join(self.r, manager), exist_ok=True)
        with open(os.path.join(self.r, manager, "index.json"), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def check(self, served, repo="owner/nest", expected="owner/nest"):
        """`served` maps a path to its body; `__self` is filled in from `repo`."""
        bodies = {"__self": {"repo": repo, "branch": "main"}, **served}

        def get_json(url, attempts=3):
            path = url.split("/", 3)[3] if url.count("/") >= 3 else ""
            if path not in bodies:
                raise net.FetchError(url, "missing", "404")
            return bodies[path]

        return mirror.drift("https://mirror.example", expected, r_dir=self.r, get_json=get_json)

    def test_a_current_deployment_of_this_repository_is_silent(self):
        self.publish()
        served = {"opkg/index.json": {"generated_at": GENERATED, "packages": [{"filename": "a"}]}}
        self.assertEqual(self.check(served), [])

    def test_a_deployment_of_the_previous_name_is_the_whole_problem(self):
        self.publish()
        problems = self.check({}, repo="owner/old-name")
        self.assertTrue(any("owner/old-name" in line for line in problems), problems)
        self.assertTrue(any("wrangler deploy" in line for line in problems), "says how to fix it")
        # Nothing else is compared: the two repositories share no history.
        self.assertEqual(len(problems), 2, problems)

    def test_an_index_older_than_r_is_reported(self):
        self.publish()
        served = {"opkg/index.json": {"generated_at": "2026-10-25T08:55:48Z", "packages": [{"filename": "a"}]}}
        problems = self.check(served)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("2026-10-25T08:55:48Z", problems[0])

    def test_a_different_package_count_is_reported_even_when_the_stamp_matches(self):
        self.publish(packages=3)
        served = {"opkg/index.json": {"generated_at": GENERATED, "packages": [{"filename": "a"}]}}
        problems = self.check(served)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("mirror lists 1 package(s), r/ has 3", problems[0])

    def test_an_unreachable_mirror_says_so_instead_of_passing(self):
        self.publish()

        def get_json(url, attempts=3):
            raise net.FetchError(url, "unreachable", "connection refused")

        problems = mirror.drift("https://mirror.example", "owner/nest", r_dir=self.r, get_json=get_json)
        self.assertEqual(len(problems), 1)
        self.assertIn("connection refused", problems[0])
