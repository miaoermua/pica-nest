#!/usr/bin/env python3
"""Validate CI job and index files against the schemas in schema/.

Uses `jsonschema` when it is installed. When it is not, falls back to checking
the constraints that actually break builds, so the script is useful both in CI
(where the dependency is installed) and in a bare shell.

Usage:
    validate_repo.py [--jobs ci/*/*/build.json] [--index r/repo.json] [--check-pairs]
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


def job_layout_errors(path):
    """Check the job's place on disk against the backend it declares.

    The schema sees the file, not the tree: a job filed under `ci/opkg/` whose
    `mgr` is `apk` is valid field by field, yet the workflow walks `ci/<mgr>/`
    and `fetch_assets.py` writes into `pkgs/<mgr>/`, so the directory name is
    load-bearing. The same holds for the manifest: it is read from `manifest`,
    which must be the copy under the job's own `src/<mgr>/<id>/`.

    Returns None when the file is not valid JSON, so the caller reports that
    once instead of failing inside the checks that follow.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            job = json.load(handle)
    except json.JSONDecodeError:
        return None

    parts = path.replace("\\", "/").split("/")
    if len(parts) < 3:
        return []
    mgr_dir, job_dir = parts[-3], parts[-2]
    mgr = job.get("mgr")
    if mgr not in SUPPORTED_PKGMGRS:
        # An unsupported mgr is already an error; comparing paths with it would
        # only bury that error under a second one.
        return []

    errors = []
    if mgr_dir != mgr:
        errors.append(
            f"{path}: lives in ci/{mgr_dir}/ but mgr is {mgr!r}; the directory name must be the backend"
        )

    manifest_path = job.get("manifest")
    expected = f"src/{mgr}/{job_dir}/manifest"
    if manifest_path and os.path.normpath(manifest_path) != expected:
        errors.append(
            f"{path}: manifest is {manifest_path!r}; a job in ci/{mgr_dir}/{job_dir}/ reads {expected!r}"
        )

    return errors


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
    provider = source.get("provider")
    if provider not in ("github", "http_template", "tarball"):
        bad(f"source.provider must be github, http_template or tarball, got {provider!r}")
    if provider == "tarball" and not (source.get("url_template") or source.get("url_templates")):
        bad("source.url_template is required for the tarball provider")
    if provider == "github":
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


def pair_errors():
    """Every source package has its job, and every job has its source.

    File-by-file validation cannot see a deleted half: `src/<mgr>/<pkg>/` with
    no `ci/<mgr>/<pkg>/build.json` (or the reverse) leaves nothing invalid to
    read. The workflow assumes the pair, so a full validation checks it.
    """
    errors = []
    jobs = set(glob.glob("ci/*/*/build.json"))

    for manifest in sorted(glob.glob("src/*/*/manifest")):
        parts = manifest.replace("\\", "/").split("/")
        if len(parts) != 4:
            continue
        job = f"ci/{parts[1]}/{parts[2]}/build.json"
        if job not in jobs:
            errors.append(f"{manifest}: has no matching {job}")

    for job in sorted(jobs):
        parts = job.replace("\\", "/").split("/")
        if len(parts) != 4:
            continue
        manifest = f"src/{parts[1]}/{parts[2]}/manifest"
        if not os.path.isfile(manifest):
            errors.append(f"{job}: has no matching {manifest}")

    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", nargs="*", default=None, help="build.json files to check")
    parser.add_argument("--index", default="r/repo.json")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--check-pairs",
        action="store_true",
        help="also require every src package to have a ci job, and the reverse",
    )
    args = parser.parse_args()

    job_files = args.jobs if args.jobs else sorted(glob.glob("ci/*/*/build.json"))
    if not job_files:
        print("error: no build.json files found under ci/", file=sys.stderr)
        return 1

    errors = []
    used_schema = False

    for path in job_files:
        # Runs in both modes: the schema cannot see where the file lives, and
        # the fallback checks only the fields it happens to know about.
        layout = job_layout_errors(path)
        if layout is None:
            errors.append(f"{path}: not valid JSON")
            continue
        errors.extend(layout)

        schema_path = os.path.join("schema", "ci-job-v1.schema.json")
        if os.path.isfile(schema_path):
            result = try_jsonschema(schema_path, path)
            if result is not None:
                used_schema = True
                errors.extend(result)
                continue
        errors.extend(check_job_fallback(path))

    if args.check_pairs:
        errors.extend(pair_errors())

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