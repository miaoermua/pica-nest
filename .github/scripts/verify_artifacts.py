#!/usr/bin/env python3
"""Verify that the published index describes the artifacts it points at.

`gen_repo_index.py` copies what `pica-pack` wrote and validates the index
schema. That proves the JSON is well formed, not that `r/packages/<filename>`
is the artifact the JSON advertises. This script opens every artifact and
compares its embedded `manifest` with the index entry, then checks the SHA-256
and the payload directory.

Artifacts named in `--require-file` (the files built by this run) are strict:
a missing file, a missing index entry or any mismatch fails. Other existing
entries are verified best-effort with warnings, because a historical artifact
that is already absent from the checkout must not block a new build forever.
"""
import argparse
import hashlib
import json
import os
import sys
import tarfile

SUFFIXES = {"opkg": "-opkg", "apk": "-apk", "pica": "-pica"}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(archive):
    with tarfile.open(archive, "r:gz") as tar:
        member = next((item for item in tar.getmembers() if item.name == "manifest"), None)
        if member is None:
            return None
        return tar.extractfile(member).read().decode("utf-8", "replace")


def manifest_fields(text):
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields.setdefault(key.strip(), value.strip())
    return fields


def payload_paths(archive):
    with tarfile.open(archive, "r:gz") as tar:
        return [item.name for item in tar.getmembers()]


def expected_filename(pkg):
    suffix = SUFFIXES.get(pkg.get("pkgmgr"))
    if not suffix:
        return None
    platform = pkg.get("platform", "all")
    stem = (
        f"{pkg['pkgname']}-{pkg['pkgver']}-{pkg['pkgrel']}-{pkg['arch']}"
        if platform == "all"
        else f"{pkg['pkgname']}-{pkg['pkgver']}-{pkg['pkgrel']}-{platform}-{pkg['arch']}"
    )
    return stem + suffix + ".pkg.tar.gz"


def load_required(path):
    if not path or not os.path.isfile(path):
        return set()
    with open(path, "r", encoding="utf-8") as handle:
        return {line.strip() for line in handle if line.strip()}


def verify_one(pkg, packages_dir, required, errors, warnings):
    filename = pkg["filename"]
    archive = os.path.join(packages_dir, filename)
    relative = os.path.relpath(archive, os.getcwd())
    strict = relative in required or archive in required
    problem = errors if strict else warnings

    if not os.path.isfile(archive):
        problem.append(f"{relative}: missing artifact for index entry {pkg['pkgname']}")
        return
    size = os.path.getsize(archive)
    digest = sha256_file(archive)
    if digest != str(pkg.get("sha256", "")).lower():
        problem.append(f"{relative}: sha256 {digest} does not match index {pkg.get('sha256')}")
    csize = pkg.get("csize")
    if isinstance(csize, int) and csize > 0 and csize != size:
        problem.append(f"{relative}: size {size} does not match index csize {csize}")

    try:
        manifest_text = read_manifest(archive)
        members = payload_paths(archive)
    except (OSError, tarfile.TarError) as error:
        problem.append(f"{relative}: cannot read archive: {error}")
        return
    if manifest_text is None:
        problem.append(f"{relative}: archive has no manifest")
        return

    fields = manifest_fields(manifest_text)
    for key in ("pkgname", "pkgver", "pkgrel", "platform", "arch", "pkgmgr"):
        if fields.get(key) != str(pkg.get(key, "")):
            problem.append(
                f"{relative}: manifest {key}={fields.get(key)!r} does not match index "
                f"{pkg.get(key)!r}"
            )

    manager = pkg.get("pkgmgr")
    # Ask the packer's question, not a stricter one: can this artifact install
    # anything? `pica-pack` accepts a package that ships containers for its
    # backend, `root/` files, or `scripts/` that install it, and refuses only
    # when none of the three is present.
    #
    # Requiring `pkgs/` from every native backend gets that wrong. cattools
    # declares `targets = opkg, pica` with its runtime dependencies handed to
    # opkg (`base@opkg = bash` …) and its payload delivered by `scripts/install`
    # — there is deliberately no `.ipk` for it to carry. The packer built it;
    # this check called it empty.
    has_payload = any(
        name.startswith(("pkgs/", "root/", "scripts/")) for name in members
    )
    if not has_payload:
        problem.append(
            f"{relative}: no payload (none of pkgs/, root/ or scripts/) for {manager}"
        )

    expected = expected_filename(pkg)
    if expected and expected != filename:
        # Historical artifacts predate the backend suffix; only fail a strict
        # entry, where the current packer was expected to produce the name.
        (errors if strict else warnings).append(
            f"{relative}: filename does not match the current convention ({expected})"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default="r/repo.json")
    parser.add_argument("--packages-dir", default="r/packages")
    parser.add_argument(
        "--require-file",
        help="newline-separated artifact paths built by this run; missing or mismatched entries fail",
    )
    parser.add_argument("--strict-all", action="store_true", help="treat existing artifacts strictly too")
    args = parser.parse_args()

    with open(args.index, "r", encoding="utf-8") as handle:
        index = json.load(handle)
    packages = index.get("packages", [])
    required = load_required(args.require_file)
    if args.strict_all:
        required |= {os.path.join(args.packages_dir, pkg["filename"]) for pkg in packages}

    index_paths = {os.path.join(args.packages_dir, pkg["filename"]) for pkg in packages}
    errors = []
    warnings = []
    for path in sorted(required):
        if path not in index_paths:
            errors.append(f"{path}: built artifact has no index entry")

    for pkg in packages:
        verify_one(pkg, args.packages_dir, required, errors, warnings)

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    print(f"Verified {len(packages)} index entries ({len(required)} required).")
    if errors:
        print(f"{len(errors)} artifact error(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
