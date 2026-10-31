"""`ci/config.json`: the one file that decides where upstream lives.

    pica       which pica revision builds the artifacts (repo + ref)
    release    the GitHub release the artifacts are published to
    feeds      package feeds by name and backend, `{arch}` in the URL; the
               release segment of the URL is the version knob
    feed_repos the feed subdirectories searched for a package, in order
    platforms  OpenWrt arch -> pica platform, the display name in filenames
"""

import re

from . import util

CONFIG_FILE = util.path("ci", "config.json")
_FEED_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class Config:
    def __init__(self, data, origin=CONFIG_FILE):
        self.data = data
        self.origin = origin
        self.pica = data.get("pica") or {}
        self.release = data.get("release") or {}
        self.feeds = data.get("feeds") or {}
        self.feed_repos = list(data.get("feed_repos") or [])
        self.platforms = data.get("platforms") or {}

    def platform(self, arch):
        return self.platforms.get(arch)

    def feed_base(self, feed, backend, arch):
        """The arch directory of one feed, without a trailing slash."""
        template = (self.feeds.get(feed) or {}).get(backend)
        if not template:
            return None
        return template.replace("{arch}", arch).rstrip("/")

    def release_base(self, repo=None, tag=None):
        repo = repo or self.release.get("repo")
        tag = tag or self.release.get("tag")
        return f"https://github.com/{repo}/releases/download/{tag}"

    def errors(self):
        problems = []
        known = {"pica", "release", "feeds", "feed_repos", "platforms"}
        for key in self.data:
            if key not in known:
                problems.append(f"unknown key {key!r}")
        if not self.pica.get("repo") or not self.pica.get("ref"):
            problems.append("pica.repo and pica.ref are required")
        if not self.release.get("repo") or not self.release.get("tag"):
            problems.append("release.repo and release.tag are required")
        tag = self.release.get("tag", "")
        if tag and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", tag):
            problems.append(f"release.tag {tag!r} is not a plain tag name")
        if not self.feed_repos:
            problems.append("feed_repos must list at least one feed subdirectory")
        for name, backends in self.feeds.items():
            if not _FEED_NAME.match(name):
                problems.append(f"feed name {name!r} must be lowercase [a-z0-9_-]")
            if not isinstance(backends, dict) or not backends:
                problems.append(f"feeds.{name} must map a backend to a URL")
                continue
            for backend, url in backends.items():
                if backend not in util.CONTAINER_BACKENDS:
                    problems.append(f"feeds.{name}.{backend}: only opkg and apk have feeds")
                if not isinstance(url, str) or not url.startswith("https://"):
                    problems.append(f"feeds.{name}.{backend}: must be an https URL")
                elif "{arch}" not in url:
                    problems.append(f"feeds.{name}.{backend}: must contain {{arch}}")
        if self.platforms.get("all") != "all":
            problems.append('platforms must map "all" to "all"')
        for arch, platform in self.platforms.items():
            if not re.match(r"^[a-z0-9_-]+$", str(platform)):
                problems.append(f"platforms.{arch}: {platform!r} is not a plain name")
        return [f"{util.rel(self.origin)}: {problem}" for problem in problems]


def load(file=CONFIG_FILE):
    return Config(util.read_json(file), file)
