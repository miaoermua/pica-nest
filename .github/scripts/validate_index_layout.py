#!/usr/bin/env python3
"""Verify the `r/` index layout holds together.

Three questions, in the order a client would ask them:

1. Is `r/index.json` a descriptor this source may publish — right schema, paths
   that stay inside `r/`, no host or cache policy smuggled in?
2. Does every declared backend index exist, parse, and belong to the manager
   that declared it?
3. Is each backend index still a faithful view of `r/repo.json`?

The third is the one that decays. The first two fail loudly the moment the
layout breaks; the third is what catches a backend index that was regenerated
by an older generator, or edited by hand, while `repo.json` moved on. Publishing
that would hand a client a filename the schema 2 index does not know.

`--select` answers the same question the future client will ask: given the
managers this device runs, which indexes apply? Anything not declared comes back
as unavailable rather than silently resolving to a neighbour's index.

Usage:
    validate_index_layout.py [--root r] [--select apk,pica]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import repo_layout  # noqa: E402  (path set above so the script runs from anywhere)

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "schema",
    "index-entry-v1.schema.json",
)


def schema_errors(instance, label):
    """Validate the descriptor against `schema/index-entry-v1.schema.json`.

    Optional, like `validate_repo.py`: `jsonschema` is installed in CI and the
    checks below stand on their own without it. The schema pins the shape; the
    path rules are the part it cannot express, and `repo_layout.validate_entry`
    owns those either way.
    """
    try:
        import jsonschema
    except ImportError:
        return []
    if not os.path.isfile(SCHEMA_PATH):
        return []
    with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{label}: {'/'.join(str(part) for part in err.absolute_path) or '<root>'}: {err.message}"
        for err in validator.iter_errors(instance)
    ]


def main():
    parser = argparse.ArgumentParser(description="Validate the r/ index layout.")
    parser.add_argument("--root", default="r", help="directory holding index.json and repo.json")
    parser.add_argument(
        "--select",
        help="comma-separated managers a client runs; prints the indexes it should read",
    )
    parser.add_argument("--quiet", action="store_true", help="print only failures")
    args = parser.parse_args()

    entry_path = os.path.join(args.root, repo_layout.ENTRY_FILE)
    if not os.path.isfile(entry_path):
        print(f"error: {entry_path} does not exist", file=sys.stderr)
        return 1

    try:
        entry = repo_layout.load_json(entry_path)
    except (OSError, ValueError) as err:
        print(f"error: {entry_path}: {err}", file=sys.stderr)
        return 1

    errors = []
    errors.extend(schema_errors(entry, entry_path))
    resolved, entry_errors = repo_layout.validate_entry(entry, args.root)
    errors.extend(entry_errors)

    repo_path = os.path.join(args.root, repo_layout.REPO_FILE)
    counts = {}
    if not os.path.isfile(repo_path):
        errors.append(f"{repo_path}: missing; the backend indexes are views of it")
    else:
        try:
            repo = repo_layout.load_json(repo_path)
        except (OSError, ValueError) as err:
            errors.append(f"{repo_path}: {err}")
            repo = None
        if repo is not None:
            if repo.get("schema") != 2:
                errors.append(f"{repo_path}: schema must be 2, got {repo.get('schema')!r}")
            packages = repo.get("packages", [])
            for pkg in packages:
                counts[pkg.get("pkgmgr")] = counts.get(pkg.get("pkgmgr"), 0) + 1
            errors.extend(repo_layout.consistency_errors(packages, resolved, args.root))

    if args.select:
        selected, missing = repo_layout.select_managers(entry, args.select)
        if missing:
            errors.append(
                f"--select {args.select}: {', '.join(missing)} not declared by index.json"
            )
        for manager, relative in sorted(selected.items()):
            print(f"{manager}\t{relative}")

    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if not errors and not args.quiet:
        declared = ", ".join(f"{manager}={counts.get(manager, 0)}" for manager in sorted(resolved))
        print(f"ok: {entry_path} declares {len(resolved)} backend index(es): {declared}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
