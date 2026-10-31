"""Stage: download a plan into one pica-pack input directory per arch, then verify.

    <stage-root>/<mgr>/<id>/<arch>/
        manifest                      pkgver/platform/arch filled in for this arch
        scripts/  root/               copied from the package
        pkgs/<mgr>/<platform>/<arch>/ the containers (feed, github, url)
        root/<dest>/                  the unpacked payload (archive)

One directory per arch is what lets each arch carry its own version and its own
root/ — a single directory can hold neither.
"""

import os
import re
import shutil
import tarfile
import tempfile
import zipfile

from . import containers, net, util
from .manifest import Manifest, dependency_name

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar", ".zip")


def stage_dir(stage_root, package, arch):
    return os.path.join(stage_root, package.mgr, package.id, arch)


def _write_manifest(package, arch_plan, dest):
    manifest = Manifest(package.manifest.text(), dest)
    if not package.pkgver:
        manifest.set("pkgver", arch_plan.version)
    manifest.set("platform", arch_plan.platform)
    manifest.set("arch", arch_plan.arch)
    with open(dest, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(manifest.text())
    return manifest


def _safe_member(name, strip):
    parts = [part for part in name.replace("\\", "/").split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise util.NestError(f"archive member {name!r} escapes the archive")
    if len(parts) <= strip:
        return None
    return "/".join(parts[strip:])


def _included(name, include):
    if not include:
        return True
    return any(name == pattern or name.startswith(pattern.rstrip("*")) for pattern in include)


def unpack(download, dest, extract):
    """Unpack an archive (or place a bare file) under `dest`. Returns written paths."""
    strip = extract.get("strip", 0)
    include = extract.get("include") or []
    rename = extract.get("rename")
    lowered = download.lower()
    written = []
    os.makedirs(dest, exist_ok=True)

    if not lowered.endswith(ARCHIVE_SUFFIXES):
        target = os.path.join(dest, rename or os.path.basename(download))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(download, target)
        os.chmod(target, 0o755)
        return [target]

    if lowered.endswith(".zip"):
        with zipfile.ZipFile(download) as archive:
            members = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = _safe_member(info.filename, strip)
                if name and _included(name, include):
                    members.append((info, name))
            if rename and len(members) != 1:
                raise util.NestError(f"rename needs exactly one file, the archive yields {len(members)}")
            for info, name in members:
                target = os.path.join(dest, rename or name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(info) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
                mode = (info.external_attr >> 16) & 0o777
                os.chmod(target, 0o755 if rename else (mode or 0o644))
                written.append(target)
        if not written:
            raise util.NestError(f"{os.path.basename(download)} holds none of {include or 'its files'}")
        return written

    with tarfile.open(download, "r:*") as archive:
        members = []
        for member in archive.getmembers():
            if not (member.isfile() or member.issym()):
                continue
            name = _safe_member(member.name, strip)
            if name and _included(name, include):
                members.append((member, name))
        if rename and len(members) != 1:
            raise util.NestError(f"rename needs exactly one file, the archive yields {len(members)}")
        for member, name in members:
            member.name = rename or name
            archive.extract(member, dest, filter="data")
            target = os.path.join(dest, member.name)
            if rename:
                os.chmod(target, 0o755)
            written.append(target)
    if not written:
        raise util.NestError(f"{os.path.basename(download)} holds none of {include or 'its files'}")
    return written


def _unzip(download, dest, pattern):
    regex = re.compile(pattern)
    written = []
    with zipfile.ZipFile(download) as archive:
        for info in archive.infolist():
            base = os.path.basename(info.filename)
            if info.is_dir() or not regex.match(base):
                continue
            target = os.path.join(dest, base)
            if os.path.exists(target):
                raise util.NestError(f"{base} is produced twice in {util.rel(dest)}")
            with archive.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            written.append(target)
    if not written:
        raise util.NestError(f"no member of {os.path.basename(download)} matches {pattern!r}")
    return written


def fetch(plan, stage_root, cache_dir=None):
    """Download one package's plan into its stage directories.

    Returns {arch: stage dir}. Raises NestError on the first failure; the
    package is then not built at all.
    """
    package = plan.package
    stages = {}
    downloaded = {}  # url -> local file, so one noarch file serves every arch
    work = tempfile.mkdtemp(prefix="nest-dl-")
    try:
        for arch_plan in plan.arches:
            stage = stage_dir(stage_root, package, arch_plan.arch)
            shutil.rmtree(stage, ignore_errors=True)
            os.makedirs(stage)
            for name in package.payload_dirs():
                shutil.copytree(os.path.join(package.dir, name), os.path.join(stage, name), symlinks=True)
            _write_manifest(package, arch_plan, os.path.join(stage, "manifest"))
            pkgs = os.path.join(stage, "pkgs", package.mgr, arch_plan.platform, arch_plan.arch)

            for request in arch_plan.requests:
                local = downloaded.get(request.url)
                if local is None:
                    local = os.path.join(work, f"{len(downloaded)}-{request.name}")
                    try:
                        net.download(request.url, local, request.size, request.sha256, cache_dir)
                    except net.FetchError as err:
                        if request.optional:
                            util.warn(f"{package.key} {arch_plan.arch}: optional {request.name} skipped: {err.reason}")
                            continue
                        raise
                    downloaded[request.url] = local
                if request.kind == "container":
                    os.makedirs(pkgs, exist_ok=True)
                    target = os.path.join(pkgs, request.name)
                    if os.path.exists(target):
                        raise util.NestError(f"{arch_plan.arch}: {request.name} is planned twice")
                    shutil.copyfile(local, target)
                elif request.kind == "zip":
                    os.makedirs(pkgs, exist_ok=True)
                    _unzip(local, pkgs, request.unzip)
                elif request.kind == "archive":
                    dest = os.path.join(stage, "root", request.extract.get("dest", ""))
                    unpack(local, dest, request.extract)
            stages[arch_plan.arch] = stage
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return stages


def verify(plan, stages):
    """Check the staged containers against the manifest. Returns (errors, notes)."""
    package = plan.package
    errors, notes = [], []
    deps = package.manifest.dependencies(package.mgr)
    has_declarations = any(deps.values()) or bool(package.manifest.get_all("app_i18n"))
    required_apps = [dependency_name(value) for value in deps["app"] if not value.strip().endswith("?")]
    for arch_plan in plan.arches:
        stage = stages.get(arch_plan.arch)
        pkgs = os.path.join(stage, "pkgs", package.mgr, arch_plan.platform, arch_plan.arch)
        if not os.path.isdir(pkgs):
            continue
        names, versions, unreadable = set(), [], 0
        for file in sorted(os.listdir(pkgs)):
            label = f"{arch_plan.arch}/{file}"
            meta = containers.metadata(os.path.join(pkgs, file))
            if meta is None or not meta.get("package"):
                notes.append(f"{label}: metadata unreadable, not verified")
                unreadable += 1
                continue
            name = meta["package"]
            names.add(name)
            if meta.get("version"):
                versions.append(meta["version"])
            if has_declarations and not package.manifest.declares(name):
                errors.append(f"{label} installs as {name!r}, which the manifest does not declare")
            arch = meta.get("architecture")
            if arch and arch_plan.arch != "all" and arch not in ("all", "noarch", arch_plan.arch):
                errors.append(f"{label} is built for {arch}, not {arch_plan.arch}")
        if not names:
            continue
        if package.pkgver and versions and not any(util.version_in(v, package.pkgver) for v in versions):
            errors.append(
                f"{arch_plan.arch}: manifest pins {package.pkgver}, the containers carry {', '.join(sorted(set(versions)))}"
            )
        if unreadable:
            continue  # coverage cannot be judged from a partial view
        for app in required_apps:
            if app.startswith("luci-") and app not in names:
                errors.append(f"{arch_plan.arch}: manifest installs {app}, but no container provides it")
        for pattern in package.manifest.i18n_patterns():
            if not any(pattern.match(name) for name in names):
                notes.append(f"{arch_plan.arch}: no container matches app_i18n {pattern.pattern}")
    return errors, notes
