"""Plan: turn a package's `build.json` into concrete downloads, per arch.

Planning reads indexes and release lists but downloads no payload. Each matrix
arch gets its own list of requests and its own pkgver, because feeds rebuild
architectures independently and an arch that is one revision ahead must not be
labelled with another arch's version.

Sources:

    feed     packages by name from a feed in ci/config.json
    github   release assets by regex; one capture group = the arch
    url      fixed container URLs with {arch} / {platform} / {version}
    archive  one archive or bare binary per arch, unpacked into root/
"""

import re
import urllib.parse

from . import feeds, net, util
from .package import expand_pattern


class Request:
    def __init__(self, url, kind, optional=False, sha256=None, size=None, unzip=None, extract=None):
        self.url = url
        self.kind = kind  # container | zip | archive
        self.optional = optional
        self.sha256 = sha256
        self.size = size
        self.unzip = unzip
        self.extract = extract or {}
        self.name = urllib.parse.unquote(url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
        self.probe = None

    def to_dict(self):
        data = {"url": self.url, "name": self.name, "kind": self.kind}
        for key in ("optional", "sha256", "size", "unzip", "probe"):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.extract:
            data["extract"] = self.extract
        return data


class ArchPlan:
    def __init__(self, arch, platform, version):
        self.arch = arch
        self.platform = platform
        self.version = version
        self.requests = []

    def to_dict(self):
        return {
            "arch": self.arch,
            "platform": self.platform,
            "pkgver": self.version,
            "requests": [request.to_dict() for request in self.requests],
        }


class Plan:
    def __init__(self, package):
        self.package = package
        self.arches = []
        self.errors = []
        self.notes = []
        self.unreachable = False
        self.origin = ""  # e.g. the release tag or feed URL, for reports

    def fail(self, message):
        self.errors.append(message)

    def fetch_failed(self, err):
        if err.status == "unreachable":
            self.unreachable = True
        self.errors.append(f"{err.reason} ({err.status}): {err.url}")

    @property
    def ok(self):
        return not self.errors

    def to_dict(self):
        return {
            "package": self.package.key,
            "pkgname": self.package.pkgname,
            "source": self.package.source_type,
            "origin": self.origin,
            "ok": self.ok,
            "errors": self.errors,
            "notes": self.notes,
            "arches": [arch.to_dict() for arch in self.arches],
        }


def plan(package, config, arch_filter=None, offline=False):
    """Plan one package. Never raises for upstream problems; they go to errors."""
    result = Plan(package)
    arches = package.arches
    if arch_filter:
        if arch_filter not in arches:
            result.fail(f"arch {arch_filter!r} is not in the matrix {arches}")
            return result
        arches = [arch_filter]
    pinned = package.pkgver
    for arch in arches:
        result.arches.append(ArchPlan(arch, config.platform(arch), pinned))
    if offline:
        result.notes.append("offline: upstream was not consulted")
        return result
    kind = package.source_type
    try:
        if kind == "feed":
            _plan_feed(result, config)
        elif kind == "github":
            _plan_github(result)
        elif kind == "url":
            _plan_url(result)
        elif kind == "archive":
            _plan_archive(result)
        else:
            result.fail(f"unknown source type {kind!r}")
    except net.FetchError as err:
        result.fetch_failed(err)
    for arch_plan in result.arches:
        if result.ok and not arch_plan.version:
            result.fail(f"{arch_plan.arch}: no version could be determined")
        if result.ok and not any(not request.optional for request in arch_plan.requests):
            result.fail(f"{arch_plan.arch}: nothing to download")
    return result


def substitute(template, arch_plan, package):
    return (
        template.replace("{arch}", package.arch_map(arch_plan.arch))
        .replace("{platform}", arch_plan.platform)
        .replace("{version}", package.pkgver)
    )


def _plan_feed(result, config):
    package = result.package
    source = package.source
    required = list(source["packages"])
    optional = list(source.get("optional") or [])
    version_from = source.get("version_from") or required[0]
    result.origin = config.feed_base(source["feed"], package.mgr, "{arch}")
    for arch_plan in result.arches:
        found = feeds.resolve(config, source["feed"], package.mgr, arch_plan.arch, required + optional)
        for name in required + optional:
            entry = found[name]
            if entry is None:
                if name in required:
                    result.fail(f"{arch_plan.arch}: {name} is not in feed {source['feed']}")
                else:
                    result.notes.append(f"{arch_plan.arch}: optional {name} is not in the feed")
                continue
            if entry.arch and package.mgr == "opkg" and entry.arch not in ("all", arch_plan.arch):
                result.fail(f"{arch_plan.arch}: feed lists {name} for architecture {entry.arch}")
                continue
            arch_plan.requests.append(
                Request(entry.url, "container", optional=name in optional, sha256=entry.sha256, size=entry.size)
            )
        primary = found.get(version_from)
        if primary is None:
            continue
        if package.pkgver:
            if not util.version_in(primary.version, package.pkgver):
                result.fail(
                    f"{arch_plan.arch}: feed has {version_from} {primary.version}, the manifest pins "
                    f"{package.pkgver}; remove pkgver to follow the feed, or fix it"
                )
        else:
            arch_plan.version = util.publishable_version(primary.version, strip_revision=True)


def select_release(releases, source, pinned):
    """The release a github job builds from, or None."""
    tag = source.get("tag")
    if tag is not None:
        wanted = tag.replace("{version}", pinned)
        return next((release for release in releases if release.get("tag_name") == wanted), None)
    pattern = re.compile(source["tag_regex"]) if source.get("tag_regex") else None
    for release in releases:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not source.get("prerelease"):
            continue
        if pattern and not pattern.match(release.get("tag_name", "")):
            continue
        return release
    return None


def _plan_github(result):
    package = result.package
    source = package.source
    releases = net.github_api(f"repos/{source['repo']}/releases?per_page=100")
    release = select_release(releases, source, package.pkgver)
    if release is None and source.get("tag"):
        wanted = source["tag"].replace("{version}", package.pkgver)
        try:
            release = net.github_api(f"repos/{source['repo']}/releases/tags/{urllib.parse.quote(wanted)}")
        except net.FetchError as err:
            if err.status != "missing":
                raise
    if release is None:
        result.fail(f"no release of {source['repo']} matches {source.get('tag') or source.get('tag_regex') or 'any stable tag'}")
        return
    tag = release.get("tag_name", "")
    result.origin = f"{source['repo']}@{tag}"
    assets = release.get("assets") or []
    by_arch = {arch_plan.arch: arch_plan for arch_plan in result.arches}
    matched_names = []
    for index, spec in enumerate(source["assets"]):
        pattern = re.compile(expand_pattern(spec["match"], package.pkgver))
        covered = set()
        for asset in assets:
            name = asset.get("name", "")
            match = pattern.match(name)
            if not match:
                continue
            if pattern.groups:
                token = match.group(1)
                targets = [arch for arch in by_arch if package.arch_map(arch) == token]
                if not targets:
                    result.notes.append(f"skipped {name}: {token} is not in the matrix")
                    continue
            else:
                targets = list(by_arch)
            digest = (asset.get("digest") or "").removeprefix("sha256:")
            for arch in targets:
                by_arch[arch].requests.append(
                    Request(
                        asset.get("browser_download_url", ""),
                        "zip" if spec.get("unzip") else "container",
                        optional=bool(spec.get("optional")),
                        sha256=digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None,
                        size=asset.get("size"),
                        unzip=spec.get("unzip"),
                    )
                )
                covered.add(arch)
            if not spec.get("optional"):
                matched_names.append(name)
        missing = [arch for arch in by_arch if arch not in covered]
        if missing and not spec.get("optional"):
            result.fail(f"{tag}: assets[{index}] {spec['match']!r} matches nothing for {', '.join(missing)}")
    if package.pkgver:
        if not util.version_in(tag, package.pkgver) and not any(
            util.version_in(name, package.pkgver) for name in matched_names
        ):
            result.fail(
                f"{tag}: neither the tag nor the matched assets carry pkgver {package.pkgver}; "
                "upstream moved on — bump the manifest or pin `tag`"
            )
    else:
        version = util.publishable_version(tag, strip_revision=False)
        for arch_plan in result.arches:
            arch_plan.version = version


def _plan_url(result):
    package = result.package
    for arch_plan in result.arches:
        for template in package.source["urls"]:
            arch_plan.requests.append(Request(substitute(template, arch_plan, package), "container"))
    result.origin = package.source["urls"][0]


def _plan_archive(result):
    package = result.package
    source = package.source
    extract = {
        "strip": source.get("strip", 0),
        "include": source.get("include") or [],
        "rename": source.get("rename"),
        "dest": (source.get("dest") or "").strip("/"),
    }
    for arch_plan in result.arches:
        arch_plan.requests.append(Request(substitute(source["url"], arch_plan, package), "archive", extract=extract))
    result.origin = source["url"]


def probe(plans):
    """HEAD every planned URL once; missing required ones become errors."""
    import concurrent.futures

    unique = {}
    for result in plans:
        for arch_plan in result.arches:
            for request in arch_plan.requests:
                unique.setdefault(request.url, request.size)
    outcomes = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(net.probe, url, size): url for url, size in unique.items()}
        for future in concurrent.futures.as_completed(futures):
            outcomes[futures[future]] = future.result()
    for result in plans:
        for arch_plan in result.arches:
            for request in arch_plan.requests:
                status, reason = outcomes[request.url]
                request.probe = status
                if status == "valid" or request.optional:
                    continue
                if status == "unreachable":
                    result.unreachable = True
                result.fail(f"{arch_plan.arch}: {request.name} is {status} ({reason})")
