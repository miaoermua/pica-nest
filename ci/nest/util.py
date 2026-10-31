"""Small helpers shared by every module: paths, JSON, logging, versions."""

import hashlib
import json
import os
import re
import sys

BACKENDS = ("opkg", "apk", "pica")
CONTAINER_BACKENDS = ("opkg", "apk")
PKG_EXTENSION = ".pkg.tar.gz"
CONTAINER_EXTENSIONS = (".ipk", ".apk")

# Repository root: ci/nest/util.py -> ../..
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A name pica accepts for pkgname/appname (standard.md, 包命名).
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
PKGNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}$")
# What a GitHub release asset keeps verbatim. Anything else is rewritten on
# upload (`~` becomes `.`), and the index would then name a file that does not
# exist.
PUBLISHABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]*$")


class NestError(Exception):
    """A failure that is reported as one line, without a traceback."""


def path(*parts):
    return os.path.join(ROOT, *parts)


def rel(target):
    """Repository-relative, forward-slash path for messages."""
    try:
        return os.path.relpath(target, ROOT).replace(os.sep, "/")
    except ValueError:
        return target


def read_json(file):
    with open(file, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(file, value):
    os.makedirs(os.path.dirname(os.path.abspath(file)) or ".", exist_ok=True)
    tmp = file + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, file)


def sha256_file(file):
    digest = hashlib.sha256()
    with open(file, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def log(message=""):
    print(message, flush=True)


def warn(message):
    print(f"warning: {message}", file=sys.stderr, flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}", flush=True)


def error(message):
    print(f"error: {message}", file=sys.stderr, flush=True)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error::{message}", flush=True)


def github_output(name, value):
    """Append `name=value` to $GITHUB_OUTPUT when running in Actions."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        if "\n" in value:
            handle.write(f"{name}<<__NEST__\n{value}\n__NEST__\n")
        else:
            handle.write(f"{name}={value}\n")


def github_summary(markdown):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(markdown.rstrip() + "\n")


# --- versions -------------------------------------------------------------

def normalize_version(value):
    """Collapse the separators upstream spells interchangeably.

    `1.8-9`, `1.8.9`, `v1.8.9` and `26.156.15499~38397ed` / `26.156.15499.38397ed`
    are one version each. Used for comparison only, never for publishing.
    """
    value = value.strip().lower()
    if re.match(r"^v\d", value):
        value = value[1:]
    return re.sub(r"[._~+-]+", ".", value).strip(".")


def version_in(text, version):
    """Whether `text` (a filename, a tag) names `version` as a whole token."""
    needle = normalize_version(version)
    haystack = normalize_version(text)
    if not needle:
        return False
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index else ""
        end = index + len(needle)
        after = haystack[end] if end < len(haystack) else ""
        if not before.isalnum() and not (after.isdigit()):
            return True
        start = index + 1


def version_key(value):
    """Numeric tuple for "is this newer"; non-numeric chunks count as 0."""
    parts = []
    for chunk in normalize_version(value).split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


VERSION_TAG = re.compile(r"^v?\d+(?:\.\d+)*$")


def publishable_version(raw, strip_revision):
    """Turn an upstream version into a pkgver every consumer accepts.

    - an epoch (`1:2.3`) is dropped;
    - a feed revision (`-r1`, opkg's `-2`) is dropped when `strip_revision`,
      because pkgrel is the packaging revision of *this* source;
    - `~` (LuCI build ids) becomes `.`: GitHub rewrites it in asset names, and a
      filename that differs from `pkgver` is rejected by every client;
    - a leading `v` of a tag is dropped, and any other `-` becomes `_` so pica
      never splits the version into pkgver/pkgrel by itself.
    """
    value = raw.strip()
    value = re.sub(r"^\d+:", "", value)
    if re.match(r"^[vV]\d", value):
        value = value[1:]
    if strip_revision:
        value = re.sub(r"-r?\d+$", "", value)
    value = value.replace("~", ".")
    value = value.replace("-", "_")
    value = re.sub(r"[^A-Za-z0-9._+]", "_", value)
    return value
