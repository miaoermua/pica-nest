"""One package = one directory: `packages/<mgr>/<id>/`.

    manifest      the pica manifest (identity, dependencies, scripts)
    build.json    where the payload comes from and which arches to build
    scripts/      lifecycle scripts, copied into the artifact
    root/         files mirrored onto the device, copied into the artifact

`<mgr>` is the backend whose containers the job fetches (`pkgs/<mgr>/`); the
manifest's `targets` decides which artifacts come out and must include it.
"""

import json
import os
import re

from . import util
from .manifest import Manifest

PACKAGES_DIR = util.path("packages")

SOURCE_TYPES = ("feed", "github", "url", "archive")
JOB_KEYS = {"$schema", "matrix", "source", "upstream"}
SOURCE_KEYS = {
    "feed": {"type", "feed", "packages", "optional", "version_from"},
    "github": {"type", "repo", "tag", "tag_regex", "prerelease", "arch_map", "assets"},
    "url": {"type", "urls", "arch_map"},
    "archive": {"type", "url", "arch_map", "strip", "include", "rename", "dest"},
}
ASSET_KEYS = {"match", "optional", "unzip"}
# Fields the build writes, or that the matrix decides. A value here would be
# overwritten or, worse, contradict the artifact it ends up in.
MANAGED_FIELDS = {
    "pkgmgr": "the packer writes it; declare `targets` instead",
    "platform": "set from build.json `matrix`",
    "arch": "set from build.json `matrix`",
    "builddate": "the packer writes it",
    "size": "the packer computes it",
    "visibility": "retired; use `source_availability`",
    "source": "pica records the install channel itself",
}
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]*$")
_UPSTREAM = re.compile(r"^github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Package:
    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        self.id = os.path.basename(self.dir)
        self.mgr = os.path.basename(os.path.dirname(self.dir))
        self.key = f"{self.mgr}/{self.id}"
        self.manifest_file = os.path.join(self.dir, "manifest")
        self.job_file = os.path.join(self.dir, "build.json")
        self.load_problems = []
        self.manifest = None
        self.job = {}
        if os.path.isfile(self.manifest_file):
            self.manifest = Manifest.read(self.manifest_file)
        else:
            self.load_problems.append("manifest is missing")
        if os.path.isfile(self.job_file):
            try:
                self.job = util.read_json(self.job_file)
            except (OSError, json.JSONDecodeError) as err:
                self.load_problems.append(f"build.json is not valid JSON: {err}")
        else:
            self.load_problems.append("build.json is missing")

    def __repr__(self):
        return f"Package({self.key})"

    @property
    def source(self):
        return self.job.get("source") or {}

    @property
    def source_type(self):
        return self.source.get("type")

    @property
    def arches(self):
        matrix = self.job.get("matrix") or []
        return [arch for arch in matrix if isinstance(arch, str)]

    @property
    def pkgname(self):
        return self.manifest.get("pkgname") if self.manifest else ""

    @property
    def pkgver(self):
        """The pinned version, or "" when the version follows the source."""
        return self.manifest.get("pkgver") if self.manifest else ""

    @property
    def follows_source(self):
        return not self.pkgver

    @property
    def targets(self):
        return self.manifest.targets() if self.manifest else []

    def arch_map(self, arch):
        return (self.source.get("arch_map") or {}).get(arch, arch)

    def payload_dirs(self):
        return [name for name in ("scripts", "root") if os.path.isdir(os.path.join(self.dir, name))]

    # --- validation ------------------------------------------------------

    def errors(self, config):
        """Every static problem: nothing here touches the network."""
        problems = list(self.load_problems)
        if self.mgr not in util.BACKENDS:
            problems.append(f"directory packages/{self.mgr}/ is not a backend ({', '.join(util.BACKENDS)})")
        if not _PACKAGE_ID.match(self.id):
            problems.append(f"directory name {self.id!r} is not a valid package id")
        if self.manifest is not None:
            problems.extend(self._manifest_errors())
        if self.job:
            problems.extend(self._job_errors(config))
        for name in os.listdir(self.dir):
            if name not in ("manifest", "build.json", "scripts", "root", "README.md", "LICENSE"):
                problems.append(f"unexpected entry {name!r}; a package holds manifest, build.json, scripts/, root/")
        return [f"{self.key}: {problem}" for problem in problems]

    def _manifest_errors(self):
        m = self.manifest
        problems = [f"manifest {problem}" for problem in m.problems]
        for key in ("pkgname", "appname", "os"):
            if not m.get(key):
                problems.append(f"manifest needs `{key}`")
        for key, reason in MANAGED_FIELDS.items():
            if m.has(key):
                problems.append(f"manifest must not declare `{key}`: {reason}")
        pkgname, appname = m.get("pkgname"), m.get("appname")
        if pkgname and not util.PKGNAME.match(pkgname):
            problems.append(f"pkgname {pkgname!r} is not a valid package name")
        if appname and (not util.NAME.match(appname)):
            problems.append(f"appname {appname!r} is not valid (`@` and `-bin` belong in pkgname only)")
        targets = m.targets()
        if not targets:
            problems.append("manifest needs `targets` (opkg, apk and/or pica)")
        for target in targets:
            if target not in util.BACKENDS:
                problems.append(f"unknown target {target!r}")
        if targets and self.mgr not in targets:
            problems.append(f"targets {targets} must include {self.mgr!r}, the backend this job fetches for")
        pkgver = m.get("pkgver")
        if pkgver:
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", pkgver):
                problems.append(
                    f"pkgver {pkgver!r} may only use letters, digits and . _ + - "
                    "(a `~` is renamed by GitHub on upload and breaks the download URL)"
                )
            if "-" in pkgver and not m.get("pkgrel"):
                problems.append(f"pkgver {pkgver!r} contains `-`; set `pkgrel` or pica splits it")
        elif m.has("pkgver"):
            problems.append("pkgver is empty; remove the line to follow the source version")
        pkgrel = m.get("pkgrel")
        if pkgrel and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+]*$", pkgrel):
            problems.append(f"pkgrel {pkgrel!r} is not valid")
        availability = m.get("source_availability")
        if availability and availability not in ("open", "partial", "closed"):
            problems.append(f"source_availability must be open, partial or closed, got {availability!r}")
        if "luci" in m.get_all("type") and m.get("luci") not in ("lua1", "js2"):
            problems.append("type = luci needs `luci = lua1` or `luci = js2`")
        if "pica" in targets:
            for key, values in m.dependencies("pica").items():
                if values:
                    problems.append(
                        f"the pica target cannot install dependencies, but reads `{key}` "
                        f"({', '.join(values)}); scope them as `{key}@{self.mgr}`"
                    )
        for script in ("script_install", "script_update", "script_remove"):
            value = m.get(script)
            if value and not os.path.isfile(os.path.join(self.dir, value)):
                problems.append(f"{script} = {value}, but that file does not exist")
        return problems

    def _job_errors(self, config):
        job = self.job
        problems = []
        for key in job:
            if key not in JOB_KEYS:
                problems.append(f"build.json: unknown key {key!r}")
        matrix = job.get("matrix")
        if not isinstance(matrix, list) or not matrix:
            problems.append("build.json: `matrix` must list at least one arch")
            matrix = []
        seen = set()
        for arch in matrix:
            if not isinstance(arch, str) or config.platform(arch) is None:
                problems.append(f"build.json: matrix arch {arch!r} is not in ci/config.json `platforms`")
            if arch in seen:
                problems.append(f"build.json: matrix lists {arch!r} twice")
            seen.add(arch)
        if "all" in matrix and len(matrix) > 1:
            problems.append("build.json: `all` cannot be combined with concrete arches")
        upstream = job.get("upstream")
        if upstream is not None and (not isinstance(upstream, str) or not _UPSTREAM.match(upstream)):
            problems.append("build.json: `upstream` must look like github:owner/repo")

        source = job.get("source")
        if not isinstance(source, dict):
            return problems + ["build.json: `source` is required"]
        kind = source.get("type")
        if kind not in SOURCE_TYPES:
            return problems + [f"build.json: source.type must be one of {', '.join(SOURCE_TYPES)}"]
        for key in source:
            if key not in SOURCE_KEYS[kind]:
                problems.append(f"build.json: source.{key} is not a {kind} field")
        arch_map = source.get("arch_map")
        if arch_map is not None:
            if not isinstance(arch_map, dict):
                problems.append("build.json: source.arch_map must map matrix arch -> upstream name")
            else:
                for arch in arch_map:
                    if arch not in matrix:
                        problems.append(f"build.json: source.arch_map names {arch!r}, which is not in matrix")

        if self.follows_source and kind not in ("feed", "github"):
            problems.append(
                f"manifest has no pkgver; only feed and github sources can supply one, {kind} cannot"
            )
        if self.follows_source and upstream:
            problems.append("`upstream` is for bumping a pinned pkgver; this package has none")

        if kind == "feed":
            problems.extend(self._feed_errors(source, config, matrix))
        elif kind == "github":
            problems.extend(self._github_errors(source))
        elif kind == "url":
            urls = source.get("urls")
            if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
                problems.append("build.json: source.urls must list at least one URL")
            else:
                for url in urls:
                    problems.extend(self._template_errors(url, matrix))
            if self.mgr == "pica":
                problems.append("build.json: a url source fetches containers; pica has none (use archive)")
        elif kind == "archive":
            url = source.get("url")
            if not isinstance(url, str) or not url:
                problems.append("build.json: source.url is required")
            else:
                problems.extend(self._template_errors(url, matrix))
            strip = source.get("strip", 0)
            if not isinstance(strip, int) or strip < 0:
                problems.append("build.json: source.strip must be a non-negative integer")
            include = source.get("include", [])
            if not isinstance(include, list) or not all(isinstance(i, str) and i for i in include):
                problems.append("build.json: source.include must list path prefixes")
            for key in ("rename", "dest"):
                value = source.get(key)
                if value is not None and (not isinstance(value, str) or ".." in value.split("/")
                                          or value.startswith("/") or not value):
                    problems.append(f"build.json: source.{key} must be a relative path")
        return problems

    def _template_errors(self, url, matrix):
        problems = []
        if not url.startswith("https://"):
            problems.append(f"build.json: {url!r} must be an https URL")
        unknown = set(re.findall(r"\{([^}]*)\}", url)) - {"arch", "platform", "version"}
        if unknown:
            problems.append(f"build.json: {url!r} uses unknown placeholders {sorted(unknown)}")
        if len(matrix) > 1 and "{arch}" not in url and "{platform}" not in url:
            problems.append(f"build.json: {url!r} has no {{arch}} but the matrix has several arches")
        if "{version}" in url and self.follows_source:
            problems.append(f"build.json: {url!r} uses {{version}} but the manifest pins no pkgver")
        return problems

    def _feed_errors(self, source, config, matrix):
        problems = []
        feed = source.get("feed")
        if self.mgr not in util.CONTAINER_BACKENDS:
            problems.append("build.json: a feed source fetches containers; pica has none")
            return problems
        if feed not in config.feeds:
            problems.append(f"build.json: feed {feed!r} is not defined in ci/config.json")
        elif not config.feeds[feed].get(self.mgr):
            problems.append(f"build.json: feed {feed!r} has no {self.mgr} URL in ci/config.json")
        packages = source.get("packages")
        if not isinstance(packages, list) or not packages or not all(isinstance(p, str) and p for p in packages):
            problems.append("build.json: source.packages must list the feed package names")
            packages = []
        optional = source.get("optional", [])
        if not isinstance(optional, list) or not all(isinstance(p, str) and p for p in optional):
            problems.append("build.json: source.optional must list package names")
            optional = []
        both = set(packages) & set(optional)
        if both:
            problems.append(f"build.json: {sorted(both)} listed as both required and optional")
        version_from = source.get("version_from")
        if version_from is not None and version_from not in packages:
            problems.append(f"build.json: version_from {version_from!r} must be one of source.packages")
        if "all" in matrix:
            problems.append("build.json: a feed is read per arch; list concrete arches, not `all`")
        return problems

    def _github_errors(self, source):
        problems = []
        repo = source.get("repo")
        if not isinstance(repo, str) or not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            problems.append("build.json: source.repo must be owner/name")
        tag = source.get("tag")
        if tag is not None:
            if not isinstance(tag, str) or not tag:
                problems.append("build.json: source.tag must be a tag name or template")
            elif "{version}" in tag and self.follows_source:
                problems.append("build.json: source.tag uses {version} but the manifest pins no pkgver")
        tag_regex = source.get("tag_regex")
        if tag_regex is not None:
            try:
                re.compile(tag_regex)
            except (re.error, TypeError) as err:
                problems.append(f"build.json: source.tag_regex does not compile: {err}")
        if tag is not None and tag_regex is not None:
            problems.append("build.json: give source.tag or source.tag_regex, not both")
        if "prerelease" in source and not isinstance(source["prerelease"], bool):
            problems.append("build.json: source.prerelease must be true or false")
        assets = source.get("assets")
        if not isinstance(assets, list) or not assets:
            return problems + ["build.json: source.assets must list at least one asset"]
        if self.mgr == "pica":
            problems.append("build.json: github assets are containers; pica has none (use archive)")
        for index, asset in enumerate(assets):
            label = f"build.json: source.assets[{index}]"
            if not isinstance(asset, dict):
                problems.append(f"{label} must be an object")
                continue
            for key in asset:
                if key not in ASSET_KEYS:
                    problems.append(f"{label}.{key} is not an asset field")
            match = asset.get("match")
            try:
                compiled = re.compile(expand_pattern(match or "", "0"))
            except (re.error, TypeError) as err:
                problems.append(f"{label}.match does not compile: {err}")
                continue
            if not match:
                problems.append(f"{label}.match is required")
            if "{version}" in (match or "") and self.follows_source:
                problems.append(f"{label}.match uses {{version}} but the manifest pins no pkgver")
            if compiled.groups > 1:
                problems.append(f"{label}.match may capture one group (the arch), it captures {compiled.groups}")
            if compiled.groups == 1 and "all" in self.arches:
                problems.append(f"{label}.match captures an arch, but the matrix is `all`")
            unzip = asset.get("unzip")
            if unzip is not None:
                try:
                    re.compile(unzip)
                except (re.error, TypeError) as err:
                    problems.append(f"{label}.unzip does not compile: {err}")
            if "optional" in asset and not isinstance(asset["optional"], bool):
                problems.append(f"{label}.optional must be true or false")
        return problems


def expand_pattern(pattern, version):
    """Substitute `{version}` into a regex, escaped."""
    return pattern.replace("{version}", re.escape(version))


def discover(root=PACKAGES_DIR):
    """Every `packages/<mgr>/<id>/` directory, sorted."""
    found = []
    if not os.path.isdir(root):
        return found
    for mgr in sorted(os.listdir(root)):
        mgr_dir = os.path.join(root, mgr)
        if not os.path.isdir(mgr_dir):
            continue
        for pkg in sorted(os.listdir(mgr_dir)):
            pkg_dir = os.path.join(mgr_dir, pkg)
            if os.path.isdir(pkg_dir):
                found.append(Package(pkg_dir))
    return found


def select(selectors, root=PACKAGES_DIR):
    """Packages named by `<mgr>/<id>` or `packages/<mgr>/<id>`; all when empty."""
    packages = discover(root)
    if not selectors:
        return packages
    by_key = {package.key: package for package in packages}
    chosen = []
    for selector in selectors:
        key = selector.replace("\\", "/").strip("/")
        if key.startswith("packages/"):
            key = key[len("packages/"):]
        if key not in by_key:
            raise util.NestError(f"no such package: {selector}")
        if by_key[key] not in chosen:
            chosen.append(by_key[key])
    return chosen
