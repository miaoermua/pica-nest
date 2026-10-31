"""Shared fixtures: a throwaway repository layout and fake containers."""

import gzip
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest

from ci.nest import config as config_module

CONFIG = {
    "pica": {"repo": "miaoermua/pica", "ref": "main"},
    "release": {"repo": "owner/nest", "tag": "latest"},
    "feeds": {
        "immortalwrt": {
            "opkg": "https://feed.example/24.10/packages/{arch}",
            "apk": "https://feed.example/25.12/packages/{arch}",
        }
    },
    "feed_repos": ["base", "packages", "luci"],
    "platforms": {"all": "all", "x86_64": "amd64", "aarch64_generic": "arm64", "mipsel_24kc": "mipsel"},
}

LUCI_MANIFEST = """\
pkgname = demo
appname = demo
os = openwrt
targets = {mgr}
type = luci
luci = js2
app = demo
app = luci-app-demo
app_i18n = luci-i18n-demo-{{lang}}
base = luci-base?
"""


def config(**overrides):
    data = json.loads(json.dumps(CONFIG))
    data.update(overrides)
    return config_module.Config(data, "ci/config.json")


class TempRepo(unittest.TestCase):
    """A temporary directory with `packages/` and `r/`."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nest-test-")
        self.packages = os.path.join(self.root, "packages")
        self.r = os.path.join(self.root, "r")
        os.makedirs(self.packages)
        os.makedirs(self.r)
        self.config = config()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def add_package(self, key, manifest, job, files=None):
        directory = os.path.join(self.packages, *key.split("/"))
        os.makedirs(directory)
        with open(os.path.join(directory, "manifest"), "w", encoding="utf-8") as handle:
            handle.write(manifest)
        if job is not None:
            with open(os.path.join(directory, "build.json"), "w", encoding="utf-8") as handle:
                json.dump(job, handle)
        for relative, content in (files or {}).items():
            target = os.path.join(directory, *relative.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)
        return directory

    def write_descriptor(self, managers=None):
        managers = managers or {mgr: {"path": f"{mgr}/index.json"} for mgr in ("apk", "opkg", "pica")}
        with open(os.path.join(self.r, "index.json"), "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "name": "test", "managers": managers}, handle)


def _tar_bytes(files, gz=True):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz" if gz else "w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def make_ipk(path, package, version, arch):
    control = f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\n".encode()
    control_tar = _tar_bytes({"./control": control})
    data_tar = _tar_bytes({"./usr/bin/" + package: b"#!/bin/sh\n"})
    outer = _tar_bytes({"./debian-binary": b"2.0\n", "./control.tar.gz": control_tar, "./data.tar.gz": data_tar})
    with open(path, "wb") as handle:
        handle.write(outer)


def make_legacy_apk(path, package, version, arch):
    info = f"pkgname = {package}\npkgver = {version}\narch = {arch}\n".encode()
    with open(path, "wb") as handle:
        handle.write(_tar_bytes({".PKGINFO": info}))


def gzip_bytes(data):
    return gzip.compress(data)
