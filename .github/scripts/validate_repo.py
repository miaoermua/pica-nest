#!/usr/bin/env python3
"""Validate CI job and index files against the schemas in schema/.

Uses `jsonschema` when it is installed. When it is not, falls back to checking
the constraints that actually break builds, so the script is useful both in CI
(where the dependency is installed) and in a bare shell.

Usage:
    validate_repo.py [--jobs ci/*/*/build.json] [--index r/repo.json]
"""
import argparse
import glob
import json
import os
import sys

REPO_SCHEMA = 2
SUPPORTED_PKGMGRS = ("opkg", "apk", "pica")
LAYOUT_DIRS = ("scripts", "root", "pkgs")


def try_jsonschema(path, instance_path):
    """Full JSON Schema validation. Returns a list of errors, empty when valid.

    Returns None when `jsonschema` is unavailable so the caller can fall back.
    """
    try:
        import jsonschema
    except ImportError:
        return None
    with open(path, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    with open(instance_path, "r", encoding="utf-8") as handle:
        instance = json.load(handle)
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{instance_path}: {'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in validator.iter_errors(instance)
    ]


def check_job_fallback(path):
    """The constraints a bad build.json turns into a failed or empty build."""
    errors = []
    with open(path, "r", encoding="utf-8") as handle:
        job = json.load(handle)

    def bad(message):
        errors.append(f"{path}: {message}")

    mgr = job.get("mgr")
    if mgr not in SUPPORTED_PKGMGRS:
        bad(f"mgr must be one of {SUPPORTED_PKGMGRS}, got {mgr!r}")

    job_id = job.get("id")
    if not job_id:
        bad("id is required")

    manifest_path = job.get("manifest")
    if not manifest_path:
        bad("manifest path is required")
    elif not os.path.isfile(manifest_path):
        bad(f"manifest not found: {manifest_path}")

    source = job.get("source") or {}
    if source.get("provider") not in ("github", "http_template"):
        bad(f"source.provider must be github or http_template, got {source.get('provider')!r}")
    if source.get("provider") == "github":
        if not source.get("repo"):
            bad("source.repo is required for the github provider")
        release = source.get("release") or {}
        if not release.get("assets"):
            bad("source.release.assets must not be empty")
        for asset in release.get("assets") or []:
            if not asset.get("regex"):
                bad(f"asset {asset.get('name')!r} is missing a regex")

    matrix = (job.get("build") or {}).get("matrix") or []
    if not matrix:
        bad("build.matrix must have at least one entry")

    for directory in (job.get("build") or {}).get("include_dirs") or []:
        if directory not in LAYOUT_DIRS:
            bad(f"build.include_dirs has {directory!r}; pica-pack only reads {LAYOUT_DIRS}")

    # The manifest must agree with the backend this job builds for.
    if manifest_path and os.path.isfile(manifest_path):
        targets = []
        with open(manifest_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("pkgmgr"):
                    bad("manifest declares pkgmgr; the packer writes it. Use targets instead")
                if line.startswith("targets"):
                    _, _, value = line.partition("=")
                    targets.extend(part.strip() for part in value.split(",") if part.strip())
        if mgr in SUPPORTED_PKGMGRS and not targets:
            bad(f"manifest has no targets but this job builds for {mgr}")
        elif targets and mgr not in targets:
            bad(f"manifest targets {targets} do not include this job's mgr {mgr}")

    return errors


def check_index_fallback(path):
    errors = []
    with open(path, "r", encoding="utf-8") as handle:
        index = json.load(handle)

    if index.get("schema") != REPO_SCHEMA:
        errors.append(f"{path}: schema must be {REPO_SCHEMA}, got {index.get('schema')!r}")

    for pkg in index.get("packages", []):
        label = pkg.get("pkgname", "<unnamed>")
        for field in ("pkgname", "pkgver", "pkgrel", "pkgmgr", "platform", "arch", "filename", "sha256"):
            if not pkg.get(field):
                errors.append(f"{path}: {label} missing {field}")
        if pkg.get("pkgmgr") and pkg["pkgmgr"] not in SUPPORTED_PKGMGRS:
            errors.append(f"{path}: {label} has unsupported pkgmgr {pkg['pkgmgr']!r}")
        if not pkg.get("filename", "").endswith(".pkg.tar.gz"):
            errors.append(f"{path}: {label} filename must end with .pkg.tar.gz")

    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", nargs="*", default=None, help="build.json files to check")
    parser.add_argument("--index", default="r/repo.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    job_files = args.jobs if args.jobs else sorted(glob.glob("ci/*/*/build.json"))
    if not job_files:
        print("error: no build.json files found under ci/", file=sys.stderr)
        return 1

    errors = []
    used_schema = False

    for path in job_files:
        schema_path = os.path.join("schema", "ci-job-v1.schema.json")
        if os.path.isfile(schema_path):
            result = try_jsonschema(schema_path, path)
            if result is not None:
                used_schema = True
                errors.extend(result)
                continue
        errors.extend(check_job_fallback(path))

    if os.path.isfile(args.index):
        schema_path = os.path.join("schema", "repo-index-v2.schema.json")
        if os.path.isfile(schema_path):
            result = try_jsonschema(schema_path, args.index)
            if result is not None:
                used_schema = True
                errors.extend(result)
                if not errors and not args.quiet:
                    print(f"ok: {args.index} matches repo-index-v2")
            else:
                errors.extend(check_index_fallback(args.index))
        else:
            errors.extend(check_index_fallback(args.index))

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        print(f"\n{len(errors)} problem(s) found", file=sys.stderr)
        return 1

    if not args.quiet:
        mode = "JSON Schema" if used_schema else "built-in checks"
        print(f"ok: {len(job_files)} job(s) and {args.index} valid ({mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())