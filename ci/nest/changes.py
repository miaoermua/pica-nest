"""Which packages a push has to rebuild.

    packages/<mgr>/<id>/**     that package (gone at head = removed)
    ci/config.json             every feed package whose feed URL changed
    anything else              nothing to rebuild (validation covers it)
"""

import json
import re
import subprocess

from . import package as package_module
from . import util

_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(*args):
    return subprocess.run(["git", *args], cwd=util.ROOT, capture_output=True, text=True, check=False)


def _config_at(revision):
    result = _git("show", f"{revision}:ci/config.json")
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def changed_files(base, head):
    if not _SHA.match(base or "") or set(base) == {"0"}:
        raise util.NestError(f"no usable base revision ({base!r}); name packages or rebuild all")
    if _git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        raise util.NestError(f"base revision {base} is not available in this checkout")
    result = _git("diff", "--name-only", "--no-renames", base, head)
    if result.returncode != 0:
        raise util.NestError(f"git diff failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()], _config_at(base), _config_at(head)


def classify(files, old_config, new_config, packages):
    """Return (rebuild keys, removed keys, reasons)."""
    existing = {package.key: package for package in packages}
    rebuild, removed, reasons = [], [], {}

    def add(key, reason):
        if key in existing:
            if key not in rebuild:
                rebuild.append(key)
                reasons[key] = reason
        elif key not in removed:
            removed.append(key)

    for file in files:
        parts = file.split("/")
        if len(parts) >= 3 and parts[0] == "packages":
            add(f"{parts[1]}/{parts[2]}", f"changed {file}")
    old_feeds = old_config.get("feeds") or {}
    new_feeds = new_config.get("feeds") or {}
    if "ci/config.json" in files and old_feeds != new_feeds:
        for package in packages:
            if package.source_type != "feed":
                continue
            feed = package.source.get("feed")
            before = (old_feeds.get(feed) or {}).get(package.mgr)
            after = (new_feeds.get(feed) or {}).get(package.mgr)
            if before != after:
                add(package.key, f"feed {feed}/{package.mgr} moved to {after}")
    return sorted(rebuild), sorted(removed), reasons


def detect(base, head):
    files, old_config, new_config = changed_files(base, head)
    packages = package_module.discover()
    return classify(files, old_config, new_config, packages)
