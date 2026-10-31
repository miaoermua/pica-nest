"""Static checks over the whole repository. No network."""

import os
import subprocess

from . import index, util

FORBIDDEN_SUFFIXES = (util.PKG_EXTENSION, ".ipk", ".apk", ".adb", ".zip", ".tgz", ".tar.gz", ".tar.xz")
MAX_TRACKED_BYTES = 512 * 1024


def tracked_files():
    """Files Git tracks or would add; None when this is not a Git checkout."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=util.ROOT, capture_output=True, check=False,
    )
    if result.returncode != 0:
        return None
    return [name for name in result.stdout.decode("utf-8", "replace").split("\0") if name]


def binary_errors(files=None):
    """The clone of the old repository was 1.38 GB because build archives were
    committed. Nothing that looks like one, and nothing large, enters Git."""
    files = tracked_files() if files is None else files
    if files is None:
        return []
    problems = []
    for name in files:
        lowered = name.lower()
        if lowered.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"{name}: archives are build output; publish them to the release instead")
            continue
        file = os.path.join(util.ROOT, name)
        if os.path.isfile(file) and os.path.getsize(file) > MAX_TRACKED_BYTES:
            problems.append(f"{name}: {os.path.getsize(file)} bytes; keep large files out of Git")
    return problems


def repository_errors(config, packages, check_binaries=True):
    problems = list(config.errors())
    owners = {}
    for package in packages:
        problems.extend(package.errors(config))
        for target in package.targets:
            key = (target, package.pkgname)
            if key in owners:
                problems.append(
                    f"{package.key}: {target} package {package.pkgname!r} is also built by {owners[key]}"
                )
            owners[key] = package.key
    try:
        descriptor = index.load_descriptor()
        declared = index.manager_paths(descriptor)
        for target in sorted({target for target, _ in owners}):
            if target not in declared:
                problems.append(f"r/index.json: packages target {target}, but managers does not declare it")
    except util.NestError as err:
        problems.append(str(err))
    problems.extend(index.layout_errors(release_base=config.release_base()))
    if check_binaries:
        problems.extend(binary_errors())
    return problems
