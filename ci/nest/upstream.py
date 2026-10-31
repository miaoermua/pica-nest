"""Bump pinned versions to upstream releases — only when the move is provably safe.

A candidate becomes a bump only if all of this holds; anything else is reported
with the reason and left for a human:

- the release tag reads as a version (`v3.1.1`, `2026.10.6`);
- it is strictly newer than the manifest's pkgver, numerically;
- for a github source, every required asset pattern matches the new release;
- for a url/archive source (`"upstream": "github:owner/repo"`), every URL the
  new version expands to answers.

Only `pkgver` (and `pkgrel`, reset to 1) in the manifest changes: URLs, tags and
asset patterns spell the version as `{version}`, so nothing else has to move.
Feed packages are not bumped — they follow the feed configured in ci/config.json.
"""

import re

from . import net, util
from .package import expand_pattern


class Bump:
    def __init__(self, package, current, candidate, origin):
        self.package = package
        self.current = current
        self.candidate = candidate
        self.origin = origin

    def to_dict(self):
        return {"package": self.package.key, "from": self.current, "to": self.candidate, "origin": self.origin}


def _releases(repo):
    return net.github_api(f"repos/{repo}/releases?per_page=30")


def _newest(releases, prerelease=False, tag_regex=None):
    pattern = re.compile(tag_regex) if tag_regex else None
    for release in releases:
        if release.get("draft") or (release.get("prerelease") and not prerelease):
            continue
        if pattern and not pattern.match(release.get("tag_name", "")):
            continue
        return release
    return None


def _candidate(package, release):
    """(version, None) or (None, reason)."""
    if release is None:
        return None, "no stable release"
    tag = release.get("tag_name", "")
    if not util.VERSION_TAG.match(tag):
        return None, f"tag {tag} is not a plain version"
    candidate = tag[1:] if tag[:1] in "vV" else tag
    current = package.pkgver
    if util.normalize_version(candidate) == util.normalize_version(current):
        return None, None
    if util.version_key(candidate) <= util.version_key(current):
        return None, f"{candidate} is not newer than {current}"
    return candidate, None


def decide(package, config):
    """Bump, or (None, reason). reason None means up to date."""
    source = package.source
    kind = package.source_type
    if not package.pkgver:
        return None, None
    if kind == "github":
        template = source.get("tag")
        if template is not None and "{version}" not in template:
            return None, f"tag is pinned to {template!r}; write it as a {{version}} template to automate"
        release = _newest(_releases(source["repo"]), source.get("prerelease", False), source.get("tag_regex"))
        candidate, reason = _candidate(package, release)
        if candidate is None:
            return None, reason
        if template is not None and template.replace("{version}", candidate) != release.get("tag_name"):
            return None, f"{release.get('tag_name')} does not fit the tag template {template!r}"
        names = [asset.get("name", "") for asset in release.get("assets") or []]
        for spec in source["assets"]:
            if spec.get("optional"):
                continue
            pattern = re.compile(expand_pattern(spec["match"], candidate))
            if not any(pattern.match(name) for name in names):
                return None, f"{release.get('tag_name')}: nothing matches {spec['match']!r}"
        return Bump(package, package.pkgver, candidate, f"{source['repo']}@{release.get('tag_name')}"), None
    upstream = package.job.get("upstream")
    if kind in ("url", "archive") and upstream:
        repo = upstream.split(":", 1)[1]
        release = _newest(_releases(repo))
        candidate, reason = _candidate(package, release)
        if candidate is None:
            return None, reason
        templates = source.get("urls") or [source.get("url")]
        for arch in package.arches:
            for template in templates:
                url = (template.replace("{arch}", package.arch_map(arch))
                       .replace("{version}", candidate)
                       .replace("{platform}", config.platform(arch) or ""))
                status, why = net.probe(url)
                if status != "valid":
                    return None, f"{candidate}: {url} is {status} ({why})"
        return Bump(package, package.pkgver, candidate, f"{repo}@{release.get('tag_name')}"), None
    if kind == "feed":
        return None, "pinned feed package; remove pkgver to follow the feed"
    return None, "no upstream to check (add \"upstream\": \"github:owner/repo\")"


def apply(bump):
    manifest = bump.package.manifest
    manifest.set("pkgver", bump.candidate)
    if manifest.has("pkgrel"):
        manifest.set("pkgrel", "1")
    with open(bump.package.manifest_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(manifest.text())


def check(packages, config):
    """[(Bump)], [(package key, reason)] for every pinned package."""
    bumps, skipped = [], []
    for package in packages:
        try:
            bump, reason = decide(package, config)
        except net.FetchError as err:
            bump, reason = None, f"could not ask upstream: {err.reason} ({err.status})"
        if bump:
            bumps.append(bump)
        elif reason:
            skipped.append((package.key, reason))
    return bumps, skipped



_RELEASE_URL = re.compile(r"^(https://.+?/releases/)(\d+\.\d+\.\d+)(/.*)$")


def feed_releases(config):
    """For each feed URL pinned to a point release: what newer releases exist.

    Reported, never applied. Moving a feed changes every package it serves, so
    the release in ci/config.json only moves when a person edits it.
    Returns [(feed, backend, current, newest in the same series, newest overall)].
    """
    rows = []
    for feed, backends in sorted(config.feeds.items()):
        for backend, url in sorted(backends.items()):
            match = _RELEASE_URL.match(url)
            if not match:
                continue
            current = match.group(2)
            try:
                listing = net.get_text(match.group(1))
            except net.FetchError as err:
                rows.append((feed, backend, current, f"? ({err.status})", "?"))
                continue
            versions = sorted(set(re.findall(r'href="(\d+\.\d+\.\d+)/"', listing)), key=util.version_key)
            series = [v for v in versions if v.rsplit(".", 1)[0] == current.rsplit(".", 1)[0]]
            rows.append((feed, backend, current, series[-1] if series else current, versions[-1] if versions else current))
    return rows
