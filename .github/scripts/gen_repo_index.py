#!/usr/bin/env python3
"""Merge the repo.json files that `pica-pack` emits into one repo index.

`pica-pack` already writes a schema 2 `repo.json` next to each artifact, built
from `pica_pkg_core::manifest::Manifest`. That output is the authority on what
went into a package: it carries the required `pkgmgr`, keeps repeated manifest
fields such as `base`/`app` as arrays, and derives the filename suffix from
`pkgmgr`. Re-parsing manifests here would undo all of that, so this script only
merges, validates and copies.

Packages are keyed by `(pkgmgr, pkgname)` because `pkgmgr` is part of a
package's identity: the same application may be published once per backend.

Usage:
    gen_repo_index.py [--repo REPO_JSON] <repo.json> [<repo.json> ...]
"""
import argparse
import datetime
import json
import os
import shutil
import sys

# Mirrors REPO_SCHEMA in crates/pica-pkg-core/src/repo.rs.
REPO_SCHEMA = 2
PKG_EXTENSION = ".pkg.tar.gz"
# Mirrors SUPPORTED_PKGMGRS in the same file.
SUPPORTED_PKGMGRS = ("opkg", "apk", "pica")
DEFAULT_REPO_JSON = "r/repo.json"


def load_pack_repo(path):
    """Load one `pica-pack` repo.json, verifying it is the schema we expect."""
    with open(path, "r", encoding="utf-8") as handle:
        index = json.load(handle)

    schema = index.get("schema")
    if schema != REPO_SCHEMA:
        raise ValueError(f"{path}: schema must be {REPO_SCHEMA}, got {schema!r}")
    if not isinstance(index.get("packages"), list):
        raise ValueError(f"{path}: packages must be an array")

    for pkg in index["packages"]:
        label = pkg.get("pkgname", "<unnamed>")
        for field in ("pkgname", "pkgver", "pkgrel", "pkgmgr", "filename", "sha256"):
            if not pkg.get(field):
                raise ValueError(f"{path}: package {label} missing {field}")
        if pkg["pkgmgr"] not in SUPPORTED_PKGMGRS:
            raise ValueError(f"{path}: package {label} has unsupported pkgmgr {pkg['pkgmgr']!r}")
        if not pkg["filename"].endswith(PKG_EXTENSION):
            raise ValueError(f"{path}: {label} filename must end with {PKG_EXTENSION}")
    return index


def copy_artifacts(index, source_dir, repo_dir):
    """Place each package file where `pica -S` looks for it.

    `pica -S` fetches `<repo>/repo.json` and then `<repo>/packages/<filename>`,
    so artifacts have to travel with the index instead of staying in the CI
    workspace.
    """
    packages_dir = os.path.join(repo_dir, "packages")
    os.makedirs(packages_dir, exist_ok=True)

    for pkg in index["packages"]:
        source = os.path.join(source_dir, pkg["filename"])
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(packages_dir, pkg["filename"]))
        else:
            print(f"warning: {pkg['pkgname']}: artifact missing, index will point at {pkg['filename']}")


def main():
    parser = argparse.ArgumentParser(description="Merge pica-pack repo.json files into one index.")
    parser.add_argument("inputs", nargs="+", help="repo.json files written by pica-pack")
    parser.add_argument("--repo", default=DEFAULT_REPO_JSON, help="merged index to write")
    parser.add_argument("--no-copy", action="store_true", help="skip copying artifacts next to the index")
    parser.add_argument(
        "--release-list",
        help="write the repo-relative artifacts produced by this run, one per line",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.repo):
        print(
            f"error: {args.repo} does not exist. Create a schema {REPO_SCHEMA} index first"
            " (the generator no longer invents one, because it cannot know the fields).",
            file=sys.stderr,
        )
        return 1

    repo_dir = os.path.dirname(os.path.abspath(args.repo))

    with open(args.repo, "r", encoding="utf-8") as handle:
        existing = json.load(handle)
    if existing.get("schema") != REPO_SCHEMA:
        print(
            f"error: {args.repo} is schema {existing.get('schema')!r} but {REPO_SCHEMA} is required."
            " Rewrite it to schema 2 or delete it to regenerate.",
            file=sys.stderr,
        )
        return 1

    merged = {(pkg["pkgmgr"], pkg["pkgname"]): pkg for pkg in existing.get("packages", [])}
    released = set()

    for path in args.inputs:
        try:
            index = load_pack_repo(path)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 1

        if not args.no_copy:
            copy_artifacts(index, os.path.dirname(os.path.abspath(path)), repo_dir)

        for pkg in index["packages"]:
            if not args.no_copy:
                # The workflow consumes these paths from the repository root.
                # Basename keeps `r/packages/...` portable across the macOS
                # `/var` -> `/private/var` symlink and the Linux runner.
                released.add(os.path.join(os.path.basename(repo_dir), "packages", pkg["filename"]))
            key = (pkg["pkgmgr"], pkg["pkgname"])
            previous = merged.get(key)
            if previous and previous.get("pkgver") != pkg["pkgver"]:
                print(f"note: {key[1]} ({key[0]}): {previous.get('pkgver')} -> {pkg['pkgver']}")
            merged[key] = pkg

    packages = sorted(merged.values(), key=lambda pkg: (pkg["pkgname"], pkg["pkgmgr"]))
    existing["packages"] = packages
    existing["generated_at"] = (
        datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    # Repo-level metadata the operator owns.
    existing.setdefault("repo_name", "Pica 官方社区软件源")
    existing.setdefault("repo_desc", "Pica Official Community Software Source")

    with open(args.repo, "w", encoding="utf-8") as handle:
        json.dump(existing, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    if args.release_list:
        os.makedirs(os.path.dirname(os.path.abspath(args.release_list)) or ".", exist_ok=True)
        with open(args.release_list, "w", encoding="utf-8") as handle:
            for artifact in sorted(released):
                handle.write(artifact + "\n")

    print(f"updated {args.repo} with {len(packages)} package(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())