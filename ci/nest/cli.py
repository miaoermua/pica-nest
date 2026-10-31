"""`python3 -m ci.nest <command>` — run from the repository root.

    validate                     static checks: config, every package, r/, no binaries in Git
    plan [PKG...]                resolve sources without downloading (--probe HEADs every URL)
    build [PKG...] --out DIR     plan, fetch, verify and pica-pack into DIR
    index [--build DIR]          fold a build into r/, drop removed packages, relayout
    changed --base A --head B    packages a push has to rebuild
    bump [PKG...] [--apply]      move pinned versions to upstream releases
    feeds                        newer point releases of the feeds in ci/config.json
    outdated [PKG...] [--list]   packages whose upstream has a build r/ does not publish
    new MGR/ID --source TYPE     scaffold a package from ci/templates
    report FILE                  print a build report; non-zero when anything failed
    verify-published --list F    download what was uploaded and compare with r/
    mirror --url U               is the deployment this repository, and is it current
    stale --assets F             release assets no index references any more
    config KEY                   print a value from ci/config.json (e.g. pica.ref)

PKG is `<mgr>/<id>` (or `packages/<mgr>/<id>`); none means every package.
"""

import argparse
import json
import os
import shutil
import sys

from . import build as build_module
from . import changes, config as config_module, index, mirror, package, publish, sources, upstream, util, validate


def _select(names, every):
    if every or not names:
        return package.discover()
    return package.select(names)


def cmd_mirror(args, config):
    expected = args.repo or config.release.get("repo")
    problems = mirror.drift(args.url, expected)
    for problem in problems:
        util.error(problem)
    util.log(f"{args.url}: {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_validate(args, config):
    packages = package.discover()
    problems = validate.repository_errors(config, packages, check_binaries=not args.no_binaries)
    for problem in problems:
        util.error(problem)
    util.log(f"{len(packages)} package(s) checked, {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_plan(args, config):
    packages = _select(args.packages, args.all)
    plans = []
    for item in packages:
        problems = item.errors(config)
        result = sources.plan(item, config, arch_filter=args.arch, offline=args.offline or bool(problems))
        result.errors[:0] = problems
        plans.append(result)
    if args.probe and not args.offline:
        sources.probe([result for result in plans if result.ok])
    failed = blocking = 0
    rows = []
    for result in plans:
        versions = sorted({arch.version for arch in result.arches if arch.version})
        status = "ok" if result.ok else ("unreachable" if result.unreachable else "FAILED")
        rows.append((result.package.key, ", ".join(versions) or "-", status))
        if not result.ok:
            failed += 1
            if not (args.allow_unreachable and result.unreachable):
                blocking += 1
            for problem in result.errors:
                (util.warn if args.allow_unreachable and result.unreachable else util.error)(
                    f"{result.package.key}: {problem}")
        for note in result.notes:
            util.log(f"note: {result.package.key}: {note}")
    width = max([len(row[0]) for row in rows] + [7])
    util.log(f"{'package':<{width}}  {'pkgver':<28}  status")
    for key, versions, status in rows:
        util.log(f"{key:<{width}}  {versions:<28}  {status}")
    util.log(f"{len(plans)} planned, {failed} failed")
    if args.output:
        util.write_json(args.output, {"packages": [result.to_dict() for result in plans]})
    return 1 if blocking else 0


def cmd_build(args, config):
    pica_pack = args.pica_pack or shutil.which("pica-pack")
    if not pica_pack:
        raise util.NestError("pica-pack not found; pass --pica-pack or put it on PATH")
    packages = _select(args.packages, args.all)
    report = build_module.build(
        packages, config, args.out, pica_pack=pica_pack, cache_dir=args.cache,
        arch=args.arch, keep_stage=args.keep_stage,
    )
    util.log(build_module.summarize(report))
    util.github_output("built", str(len(report["built"])))
    if report["failed"] and not (args.allow_partial and report["built"]):
        return 1
    return 0


def cmd_index(args, config):
    packages = package.discover()
    summary = index.merge(
        index.R_DIR, args.build, packages, config, config.release_base(), release_list=args.release_list,
    )
    for line in summary["changed"]:
        util.log(line)
    if summary["kept"]:
        util.warn(
            f"{len(summary['kept'])} artifact(s) rebuilt under a name already published; the published "
            "file is kept. Bump pkgrel to republish: " + ", ".join(summary["kept"])
        )
    for relative in summary["written"]:
        util.log(f"wrote r/{relative}")
    for relative in summary["removed"]:
        util.log(f"removed r/{relative}")
    util.log(f"{len(summary['released'])} artifact(s) to upload")
    problems = index.layout_errors(release_base=config.release_base())
    for problem in problems:
        util.error(problem)
    return 1 if problems else 0


def cmd_changed(args, config):
    if args.all:
        rebuild, removed, reasons = [p.key for p in package.discover()], [], {}
    else:
        rebuild, removed, reasons = changes.detect(args.base, args.head)
    if args.list:
        print(" ".join(rebuild))
        return 0
    for key in rebuild:
        util.log(f"rebuild {key}: {reasons.get(key, 'requested')}")
    for key in removed:
        util.log(f"removed {key}")
    if not rebuild and not removed:
        util.log("no package changed")
    util.github_output("packages", " ".join(rebuild))
    util.github_output("removed", " ".join(removed))
    return 0


def cmd_bump(args, config):
    packages = _select(args.packages, False)
    bumps, skipped = upstream.check(packages, config)
    for bump in bumps:
        util.log(f"{bump.package.key}: {bump.current} -> {bump.candidate} ({bump.origin})")
        if args.apply:
            upstream.apply(bump)
    for key, reason in skipped:
        util.log(f"skip {key}: {reason}")
    util.log(f"{len(bumps)} bump(s){' applied' if args.apply and bumps else ''}")
    if args.json:
        util.write_json(args.json, {"bumps": [b.to_dict() for b in bumps], "skipped": skipped})
    return 0


def cmd_feeds(args, config):
    rows = upstream.feed_releases(config)
    lines = []
    for feed, backend, current, series, newest in rows:
        hint = ""
        if series != current:
            hint = f"  <- {series} available in the same series"
        elif newest != current:
            hint = f"  (newest series: {newest})"
        lines.append(f"{feed}.{backend:<5} {current:<10}{hint}")
        if series != current:
            util.warn(f"feeds.{feed}.{backend}: {series} is out, ci/config.json uses {current}")
    for line in lines:
        util.log(line)
    util.github_summary("### Feed releases\n\n```\n" + "\n".join(lines) + "\n```")
    return 0


def cmd_outdated(args, config):
    """Packages whose current upstream would publish a file r/ does not have.

    Covers what a push cannot see: a snapshot or branch feed that moved in
    place, and a package whose last build failed. Only packages that plan
    cleanly are listed; one that cannot be planned is reported, not retried.
    """
    published = index.referenced_filenames(index.R_DIR)
    outdated = []
    for item in _select(args.packages, False):
        if item.errors(config):
            continue
        result = sources.plan(item, config)
        if not result.ok:
            util.warn(f"{item.key}: cannot plan: {'; '.join(result.errors)}")
            continue
        missing = []
        for arch_plan in result.arches:
            for target in item.targets:
                name = index.expected_filename({
                    "pkgname": item.pkgname, "pkgver": arch_plan.version,
                    "pkgrel": item.manifest.get("pkgrel") or "1", "pkgmgr": target,
                    "platform": arch_plan.platform, "arch": arch_plan.arch,
                })
                if name not in published:
                    missing.append(name)
        if missing:
            outdated.append(item.key)
            if not args.list:
                util.log(f"{item.key}: {len(missing)} artifact(s) not published, e.g. {missing[0]}")
    if args.list:
        print(" ".join(outdated))
    else:
        util.log(f"{len(outdated)} package(s) outdated")
    return 0


def cmd_new(args, config):
    mgr, _, ident = args.package.replace("\\", "/").strip("/").removeprefix("packages/").partition("/")
    if mgr not in util.BACKENDS or not ident:
        raise util.NestError("name the package as <mgr>/<id>, e.g. opkg/hello")
    target = util.path("packages", mgr, ident)
    if os.path.exists(target):
        raise util.NestError(f"{util.rel(target)} already exists")
    templates = util.path("ci", "templates")
    manifest_template = "manifest-pica" if mgr == "pica" else "manifest-luci"
    job_template = f"build-{args.source}.json"
    if not os.path.isfile(os.path.join(templates, job_template)):
        raise util.NestError(f"no template {job_template}")
    os.makedirs(target)
    for source_name, dest_name in ((manifest_template, "manifest"), (job_template, "build.json")):
        with open(os.path.join(templates, source_name), "r", encoding="utf-8") as handle:
            text = handle.read().replace("@ID@", ident).replace("@MGR@", mgr)
        if dest_name == "manifest" and args.source in ("url", "archive") and "\npkgver" not in text:
            # These sources cannot supply a version, so the manifest pins one.
            text = text.replace("\nos = ", "\npkgver = 1.0.0\npkgrel = 1\nos = ", 1)
        with open(os.path.join(target, dest_name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    util.log(f"created {util.rel(target)}/manifest and build.json; edit them, then run validate and plan")
    return 0


def cmd_report(args, config):
    report = util.read_json(args.file)
    text = build_module.summarize(report)
    util.log(text)
    util.github_summary("### Build report\n\n" + text)
    return 1 if report.get("failed") else 0


def cmd_verify_published(args, config):
    problems = publish.verify(index.R_DIR, args.list)
    for problem in problems:
        util.error(problem)
    return 1 if problems else 0


def cmd_stale(args, config):
    with open(args.assets, "r", encoding="utf-8") as handle:
        names = [line.strip() for line in handle if line.strip()]
    for name in publish.stale(names, index.R_DIR, args.previous):
        print(name)
    return 0


def cmd_config(args, config):
    value = config.data
    for part in args.key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise util.NestError(f"ci/config.json has no {args.key}")
        value = value[part]
    print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return 0


def parser():
    root = argparse.ArgumentParser(prog="python3 -m ci.nest", description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = root.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="static checks")
    p.add_argument("--no-binaries", action="store_true", help="skip the Git binary guard")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="resolve sources without downloading")
    p.add_argument("packages", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--arch")
    p.add_argument("--probe", action="store_true", help="HEAD every planned URL")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--allow-unreachable", action="store_true", help="unreachable upstream is a warning")
    p.add_argument("--output", help="write the plan as JSON")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("build", help="plan, fetch, verify and pack")
    p.add_argument("packages", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--out", required=True)
    p.add_argument("--arch")
    p.add_argument("--pica-pack")
    p.add_argument("--cache", help="download cache directory")
    p.add_argument("--keep-stage", help="stage here and keep it, for inspection")
    p.add_argument("--allow-partial", action="store_true", help="exit 0 when at least one package built")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("index", help="fold a build into r/")
    p.add_argument("--build", help="output directory of `nest build`")
    p.add_argument("--release-list", help="write the artifacts to upload, one path per line")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("changed", help="packages a push has to rebuild")
    p.add_argument("--base")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--all", action="store_true")
    p.add_argument("--list", action="store_true", help="print only the packages to rebuild, space-separated")
    p.set_defaults(func=cmd_changed)

    p = sub.add_parser("bump", help="move pinned versions to upstream releases")
    p.add_argument("packages", nargs="*")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--json")
    p.set_defaults(func=cmd_bump)

    p = sub.add_parser("feeds", help="newer point releases of the configured feeds")
    p.set_defaults(func=cmd_feeds)

    p = sub.add_parser("outdated", help="packages whose upstream has a build r/ does not publish")
    p.add_argument("packages", nargs="*")
    p.add_argument("--list", action="store_true", help="print only the package keys, space-separated")
    p.set_defaults(func=cmd_outdated)

    p = sub.add_parser("new", help="scaffold a package")
    p.add_argument("package", help="<mgr>/<id>")
    p.add_argument("--source", required=True, choices=sorted(package.SOURCE_TYPES))
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("report", help="print a build report")
    p.add_argument("file")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("mirror", help="check a deployed mirror against r/")
    p.add_argument("--url", required=True, help="mirror origin, e.g. https://pica-nest.miaoer.net")
    p.add_argument("--repo", help="expected owner/name (default: release.repo)")
    p.set_defaults(func=cmd_mirror)

    p = sub.add_parser("verify-published", help="compare uploaded assets with r/")
    p.add_argument("--list", required=True)
    p.set_defaults(func=cmd_verify_published)

    p = sub.add_parser("stale", help="release assets no index references")
    p.add_argument("--assets", required=True, help="file with one asset name per line")
    p.add_argument("--previous", help="r/ directory of the previous commit, kept for one generation")
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser("config", help="print a config value")
    p.add_argument("key")
    p.set_defaults(func=cmd_config)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "changed" and not args.all and not args.base:
        parser().error("changed needs --base (or --all)")
    try:
        config = config_module.load()
        return args.func(args, config)
    except util.NestError as err:
        util.error(str(err))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
