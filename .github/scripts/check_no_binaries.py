#!/usr/bin/env python3
"""Fail when a built package archive is about to enter Git.

The distribution design is: binaries live in GitHub Releases, the repository
tracks only metadata (`r/repo.json`, `src/**/manifest`, `ci/**/build.json`).
A `.pkg.tar.gz` under version control is therefore never intentional — it is a
build directory that leaked into a commit, and once committed it stays in the
packable history forever.

Two checks, both hard failures:

* **tracked** — `git ls-files`: something is already in the tree.
* **pending** — staged, unstaged and untracked-but-not-ignored paths: something
  about to be committed, which is what a `git add -A` in a workflow collects.

The suffix list is the project's own artefact set, not "any archive":
`.pkg.tar.gz` is what `pica-pack` writes, `.ipk`/`.apk` are the backend
containers it embeds, `.adb` is apk-tools 3.x's index. A plain `.tar.gz` is not
listed — upstream source archives are legitimate inputs and must keep working.

Usage:
    check_no_binaries.py                 # tracked + pending
    check_no_binaries.py --tracked       # only what is already in the tree
    check_no_binaries.py --pending       # only what a commit would add
"""
import argparse
import os
import subprocess
import sys

# Artefacts produced by the packer or handed to a backend's own package manager.
# Anything with one of these endings is downloadable output, never source.
FORBIDDEN_SUFFIXES = (".pkg.tar.gz", ".ipk", ".apk", ".adb")

# Where a build is allowed to leave artifacts on disk. All of these are ignored
# by .gitignore. `r/packages/` is deliberately absent: it is the retired
# Git-tracked directory this guard exists to keep retired.
BUILD_DIRS = ("build_output/", "pkgs/", "dist/", "src/*/out/")

DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def git(*args, cwd=None):
    """Run git and return (ok, stdout). A missing git is a setup error, not a pass."""
    process = subprocess.run(
        ("git",) + args, cwd=cwd, capture_output=True, text=True, check=False
    )
    if process.returncode != 0:
        return False, process.stderr.strip()
    # -z separates records with NUL; drop the trailing empty field.
    return True, [item for item in process.stdout.split("\0") if item]


def forbidden_reason(path, max_bytes, allow_size):
    lowered = path.lower()
    for suffix in FORBIDDEN_SUFFIXES:
        if lowered.endswith(suffix):
            return f"package archive ({suffix})"
    if allow_size or not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > max_bytes:
        return f"{size} bytes, over the {max_bytes} byte limit for a tracked file"
    return None


def report(title, offenders):
    if not offenders:
        return
    print(f"{title}:", file=sys.stderr)
    for path, reason in sorted(offenders):
        print(f"  {path}  — {reason}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracked", action="store_true", help="check files already in the index")
    parser.add_argument("--pending", action="store_true", help="check files a commit would add")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"size limit for files a commit would add (default {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument(
        "--allow-size",
        action="store_true",
        help="only check forbidden suffixes, skip the size limit",
    )
    parser.add_argument("--root", default=".", help="repository root")
    args = parser.parse_args()

    # Neither flag given means both: a bare run is the useful default.
    check_tracked = args.tracked or not args.pending
    check_pending = args.pending or not args.tracked

    tracked, pending = [], []

    if check_tracked:
        ok, files = git("ls-files", "-z", cwd=args.root)
        if not ok:
            print(f"error: git ls-files failed: {files}", file=sys.stderr)
            return 1
        for path in files:
            reason = forbidden_reason(os.path.join(args.root, path), args.max_bytes, True)
            if reason:
                tracked.append((path, reason))

    if check_pending:
        # Staged additions/modifications are what the auto-commit step stages;
        # untracked-not-ignored are what `git add -A` would sweep in. Both are
        # checked because the workflow's guard runs before `git commit`.
        queries = (
            ("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"),
            ("diff", "--name-only", "--diff-filter=ACMR", "-z"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        )
        seen = set()
        for query in queries:
            ok, files = git(*query, cwd=args.root)
            if not ok:
                print(f"error: git {' '.join(query)} failed: {files}", file=sys.stderr)
                return 1
            for path in files:
                if path in seen:
                    continue
                seen.add(path)
                reason = forbidden_reason(
                    os.path.join(args.root, path), args.max_bytes, args.allow_size
                )
                if reason:
                    pending.append((path, reason))

    if not tracked and not pending:
        scope = " and ".join(
            name
            for name, enabled in (("tracked", check_tracked), ("pending", check_pending))
            if enabled
        )
        print(f"ok: no forbidden binaries in {scope} paths")
        return 0

    report("error: binaries already tracked by git", tracked)
    report("error: binaries a commit would add", pending)
    print(
        "\nBuilt packages belong in GitHub Releases, never in the repository.\n"
        "Remove them from the index and keep them out of the commit:\n"
        "  git rm --cached <path>          # untrack, keep the local file\n"
        f"Build output should stay in one of: {', '.join(BUILD_DIRS)}\n"
        "which .gitignore already excludes.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
