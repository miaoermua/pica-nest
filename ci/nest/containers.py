"""Read name, version and architecture out of a downloaded `.ipk` / `.apk`.

The filename a job matched is not what the backend installs: only the
container's own metadata says so. Three formats occur:

    .ipk             gzip tar holding control.tar.gz -> ./control
    .apk (2.x)       concatenated gzip tar segments, one holds .PKGINFO
    .apk (3.x, ADB)  `ADBd` + one raw-deflate stream whose first segment is the
                     package database; read heuristically (name, length byte,
                     version) and only believed when all three agree

`None` means "could not read", which is reported as unverified — never as a
mismatch, and never as a pass.
"""

import io
import re
import tarfile
import zlib

_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]*$")
_VERSION = re.compile(r"^\d+(?:\.\d+)*(?:[-~_.][0-9A-Za-z.+~_-]*)?$")
_ADB_SIGNATURE = b"ADBd"
_ADB_PACKAGE_SEGMENT = b"ADB.pckg"
_ADB_SEGMENT_HEADER = 21


def _fields(text, separator):
    fields = {}
    for line in text.splitlines():
        if separator in line:
            key, _, value = line.partition(separator)
            fields.setdefault(key.strip(), value.strip())
    return fields


def _ipk(path):
    with tarfile.open(path, "r:gz") as outer:
        member = next((m for m in outer.getmembers() if m.name.lstrip("./").startswith("control.tar")), None)
        if member is None:
            return None
        data = outer.extractfile(member).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as control:
        member = next((m for m in control.getmembers() if m.name.lstrip("./") == "control"), None)
        if member is None:
            return None
        fields = _fields(control.extractfile(member).read().decode("utf-8", "replace"), ":")
    return {
        "package": fields.get("Package"),
        "version": fields.get("Version"),
        "architecture": fields.get("Architecture"),
    }


def _apk_legacy(path):
    with tarfile.open(path, "r:gz", ignore_zeros=True) as archive:
        member = next((m for m in archive.getmembers() if m.name == ".PKGINFO"), None)
        if member is None:
            return None
        fields = _fields(archive.extractfile(member).read().decode("utf-8", "replace"), "=")
    return {
        "package": fields.get("pkgname"),
        "version": fields.get("pkgver"),
        "architecture": fields.get("arch"),
    }


def _apk_adb(raw):
    try:
        inflated = zlib.decompressobj(-15).decompress(raw[4:])
    except zlib.error:
        return None
    if inflated[:8] != _ADB_PACKAGE_SEGMENT:
        return None
    body = inflated[_ADB_SEGMENT_HEADER:]
    end = 0
    while end < len(body) and body[end] >= 0x20:
        end += 1
    name = body[:end].decode("ascii", "replace")
    if not name or not _PACKAGE_NAME.match(name) or end >= len(body):
        return None
    length = body[end]
    version = body[end + 1:end + 1 + length].decode("ascii", "replace")
    if len(version) != length or not _VERSION.match(version):
        return None
    # The arch lives further into the record and is not read: None, not a guess.
    return {"package": name, "version": version, "architecture": None}


def metadata(path):
    """{"package", "version", "architecture"} or None when unreadable."""
    lowered = path.lower()
    try:
        if lowered.endswith(".ipk"):
            return _ipk(path)
        if lowered.endswith(".apk"):
            with open(path, "rb") as handle:
                raw = handle.read()
            if raw[:4] == _ADB_SIGNATURE:
                return _apk_adb(raw)
            return _apk_legacy(path)
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        return None
    return None
