"""Build: plan -> fetch -> verify -> pica-pack, one package at a time.

A package that fails at any step contributes nothing to the output, and the
others carry on: one upstream that moved does not hold back every other
package of the run. `report.json` records what was built and why the rest was
not; the workflow publishes what was built and then fails on the report.

Output, ready for `nest index`:

    <out>/<backend>/<artifact>.pkg.tar.gz
    <out>/<backend>/index.json        the entries pica-pack wrote, merged
    <out>/report.json
"""

import os
import shutil
import subprocess
import tempfile

from . import sources, stage, util


def _pica_pack(binary, stage_dir, outdir):
    result = subprocess.run(
        [binary, "build", stage_dir, "--outdir", outdir],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise util.NestError(f"pica-pack failed for {util.rel(stage_dir)}:\n{output}")
    return output


def _collect(package_out, out):
    """Move one package's artifacts into the shared output, merging the indexes."""
    produced = []
    for mgr in util.BACKENDS:
        index_file = os.path.join(package_out, mgr, "index.json")
        if not os.path.isfile(index_file):
            continue
        document = util.read_json(index_file)
        target_dir = os.path.join(out, mgr)
        os.makedirs(target_dir, exist_ok=True)
        merged_file = os.path.join(target_dir, "index.json")
        merged = util.read_json(merged_file) if os.path.isfile(merged_file) else {
            "schema": document.get("schema", 1), "mgr": mgr, "packages": []}
        names = {entry["filename"] for entry in document.get("packages") or []}
        merged["packages"] = [e for e in merged["packages"] if e.get("filename") not in names]
        for entry in document.get("packages") or []:
            shutil.move(os.path.join(package_out, mgr, entry["filename"]), os.path.join(target_dir, entry["filename"]))
            merged["packages"].append(entry)
            produced.append(entry)
        util.write_json(merged_file, merged)
    return produced


def _check_outputs(package, plan, produced, config):
    """Every target x arch came out, under the name and version that was planned."""
    problems = []
    found = {(e["pkgmgr"], e["platform"], e["arch"]): e for e in produced}
    for target in package.targets:
        for arch_plan in plan.arches:
            entry = found.get((target, arch_plan.platform, arch_plan.arch))
            if entry is None:
                problems.append(f"pica-pack produced no {target} artifact for {arch_plan.arch}")
            elif entry.get("pkgver") != arch_plan.version or entry.get("pkgname") != package.pkgname:
                problems.append(
                    f"{entry.get('filename')}: expected {package.pkgname} {arch_plan.version}, "
                    f"got {entry.get('pkgname')} {entry.get('pkgver')}"
                )
            elif not util.PUBLISHABLE.match(entry.get("filename", "")):
                problems.append(f"{entry.get('filename')}: not a name a GitHub release asset keeps")
    return problems


def build(packages, config, out, pica_pack="pica-pack", cache_dir=None, arch=None, keep_stage=None):
    os.makedirs(out, exist_ok=True)
    stage_root = keep_stage or tempfile.mkdtemp(prefix="nest-stage-")
    report = {"built": [], "failed": {}, "unreachable": [], "notes": {}}
    try:
        for package in packages:
            util.log(f"== {package.key}")
            problems = package.errors(config)
            if problems:
                report["failed"][package.key] = problems
                for problem in problems:
                    util.error(problem)
                continue
            plan = sources.plan(package, config, arch_filter=arch)
            notes = list(plan.notes)
            try:
                if not plan.ok:
                    raise util.NestError("; ".join(plan.errors))
                util.log(f"  from {plan.origin}")
                stages = stage.fetch(plan, stage_root, cache_dir)
                errors, verify_notes = stage.verify(plan, stages)
                notes.extend(verify_notes)
                if errors:
                    raise util.NestError("; ".join(errors))
                package_out = tempfile.mkdtemp(prefix="nest-out-")
                try:
                    for arch_plan in plan.arches:
                        output = _pica_pack(pica_pack, stages[arch_plan.arch], package_out)
                        for line in output.splitlines():
                            if "WARNING" in line:
                                notes.append(f"{arch_plan.arch}: {line.strip()}")
                    produced = _collect(package_out, out)
                finally:
                    shutil.rmtree(package_out, ignore_errors=True)
                problems = _check_outputs(package, plan, produced, config)
                if problems:
                    _discard(out, produced)
                    raise util.NestError("; ".join(problems))
                report["built"].append(package.key)
                for entry in produced:
                    util.log(f"  built {entry['pkgmgr']}/{entry['filename']}")
            except util.NestError as err:
                report["failed"][package.key] = [str(err)]
                if plan.unreachable:
                    report["unreachable"].append(package.key)
                util.error(f"{package.key}: {err}")
            if notes:
                report["notes"][package.key] = notes
                for note in notes:
                    util.log(f"  note: {note}")
    finally:
        if keep_stage is None:
            shutil.rmtree(stage_root, ignore_errors=True)
    util.write_json(os.path.join(out, "report.json"), report)
    return report


def _discard(out, produced):
    for entry in produced:
        mgr_dir = os.path.join(out, entry["pkgmgr"])
        try:
            os.remove(os.path.join(mgr_dir, entry["filename"]))
        except OSError:
            pass
        index_file = os.path.join(mgr_dir, "index.json")
        if os.path.isfile(index_file):
            document = util.read_json(index_file)
            document["packages"] = [e for e in document["packages"] if e.get("filename") != entry["filename"]]
            util.write_json(index_file, document)


def summarize(report):
    lines = [f"Built {len(report['built'])} package(s), {len(report['failed'])} failed."]
    for key, problems in sorted(report["failed"].items()):
        tag = " (upstream unreachable)" if key in report["unreachable"] else ""
        lines.append(f"- `{key}`{tag}: " + " / ".join(problem.replace("\n", " ") for problem in problems))
    return "\n".join(lines)
