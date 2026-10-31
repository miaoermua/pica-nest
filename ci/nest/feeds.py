"""Resolve package names to files through a feed's own index.

A feed is named in `ci/config.json` with one URL per backend. The release in
that URL is the knob: change `24.10.0` to `24.10.6` and every feed job resolves
its packages again, by name, with no filename or version written anywhere else.

    opkg   <base>/<repo>/Packages    Package/Version/Filename/SHA256sum/Size
    apk    <base>/<repo>/index.json  {"packages": {name: version}}, matched to
                                     the real file through the directory listing

`<repo>` is each of `feed_repos` in order (base, packages, luci, ...): the first
directory that carries the name wins, so a job never has to know that
`ip-full` lives in `base/` while `qbittorrent` lives in `packages/`.
"""

import gzip
import re
import urllib.parse

from . import net

_INDEXES = {}


class FeedPackage:
    def __init__(self, name, version, url, arch=None, sha256=None, size=None, repo=""):
        self.name = name
        self.version = version
        self.url = url
        self.arch = arch
        self.sha256 = sha256
        self.size = size
        self.repo = repo

    @property
    def filename(self):
        return self.url.rsplit("/", 1)[-1]


def parse_packages(text, base_url, repo=""):
    """Parse an opkg `Packages` index into {name: FeedPackage}."""
    found = {}
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            if not line or line[0] in " \t" or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        name = fields.get("Package")
        filename = fields.get("Filename")
        if not name or not filename:
            continue
        size = fields.get("Size")
        found[name] = FeedPackage(
            name,
            fields.get("Version", ""),
            f"{base_url}/{filename}",
            arch=fields.get("Architecture"),
            sha256=(fields.get("SHA256sum") or "").lower() or None,
            size=int(size) if size and size.isdigit() else None,
            repo=repo,
        )
    return found


def parse_apk_index(document, base_url, repo="", listing=None):
    """Parse an apk feed `index.json` into {name: FeedPackage}.

    `index.json` keys a package by the name it provides, and the file is not
    always `<name>-<version>.apk`: an ABI-versioned package such as Node.js is
    listed as `node` but shipped as `node127-22.23.2-r1.apk`. When the
    directory `listing` (file names) is given, each entry is matched to its
    real file, and that file's own name becomes an alias — so a job may ask
    for `node` (follows ABI bumps) or `node127` (pins the ABI).
    """
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, dict):
        raise ValueError("index.json has no `packages` object")
    arch = document.get("architecture")
    files = set(listing or ())
    found = {}
    for name, version in packages.items():
        if not isinstance(version, str):
            continue
        filename = f"{name}-{version}.apk"
        if files and filename not in files:
            suffix = f"-{version}.apk"
            candidates = [f for f in files if f.endswith(suffix) and re.match(rf"^{re.escape(name)}\d+$", f[:-len(suffix)])]
            if len(candidates) == 1:
                filename = candidates[0]
                alias = filename[:-len(suffix)]
                found.setdefault(alias, FeedPackage(alias, version, f"{base_url}/{filename}", arch=arch, repo=repo))
        found[name] = FeedPackage(name, version, f"{base_url}/{filename}", arch=arch, repo=repo)
    return found


def parse_listing(html):
    """File names of `.apk`/`.ipk` links in a directory index page."""
    return [urllib.parse.unquote(name) for name in re.findall(r'href="([^"/?]+\.(?:apk|ipk))"', html)]


def _load_repo(backend, base_url, repo):
    """{name: FeedPackage} for one feed subdirectory; {} when it does not exist."""
    key = (backend, base_url, repo)
    if key in _INDEXES:
        return _INDEXES[key]
    directory = f"{base_url}/{repo}"
    try:
        if backend == "opkg":
            try:
                raw = net.get_bytes(f"{directory}/Packages.gz")
                text = gzip.decompress(raw).decode("utf-8", "replace")
            except net.FetchError as err:
                if err.status != "missing":
                    raise
                text = net.get_text(f"{directory}/Packages")
            index = parse_packages(text, directory, repo)
        else:
            document = net.get_json(f"{directory}/index.json")
            try:
                listing = parse_listing(net.get_text(f"{directory}/"))
            except net.FetchError:
                listing = []
            index = parse_apk_index(document, directory, repo, listing)
    except net.FetchError as err:
        if err.status != "missing":
            raise
        index = {}
    except (OSError, ValueError) as err:
        raise net.FetchError(directory, "invalid", f"unreadable index: {err}") from err
    _INDEXES[key] = index
    return index


def resolve(config, feed, backend, arch, names):
    """Map each name to a FeedPackage (or None when no repo carries it).

    Only the subdirectories needed are fetched: the search stops as soon as
    every name has been found.
    """
    base_url = config.feed_base(feed, backend, arch)
    if base_url is None:
        raise net.FetchError(f"{feed}/{backend}", "invalid", "feed is not configured for this backend")
    found = {name: None for name in names}
    for repo in config.feed_repos:
        missing = [name for name, value in found.items() if value is None]
        if not missing:
            break
        index = _load_repo(backend, base_url, repo)
        for name in missing:
            if name in index:
                found[name] = index[name]
    return found
