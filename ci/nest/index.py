"""The published source under `r/`.

    r/index.json            hand-written entry descriptor: name, description and
                            `managers.<mgr>.path` — it decides where each backend
                            index lives, nothing else does
    r/<path>                one backend index per declared manager, generated

Backend indexes hold entries only; the artifacts live in the GitHub release
named in ci/config.json and every entry's `download_url` points there.
"""

import datetime
import json
import os
import re

from . import util

R_DIR = util.path("r")
DESCRIPTOR = "index.json"
ENTRY_SCHEMA = 1
BACKEND_SCHEMA = 1
REQUIRED = ("pkgname", "pkgver", "pkgrel", "pkgmgr", "os", "platform", "arch", "filename", "sha256")
SUFFIX = {"opkg": "-opkg", "apk": "-apk", "pica": "-pica"}
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def expected_filename(entry):
    """`<pkgname>-<pkgver>-<pkgrel>-[<platform>-]<arch><suffix>.pkg.tar.gz`."""
    head = f"{entry['pkgname']}-{entry['pkgver']}-{entry['pkgrel']}"
    middle = entry["arch"] if entry["platform"] == "all" else f"{entry['platform']}-{entry['arch']}"
    return f"{head}-{middle}{SUFFIX[entry['pkgmgr']]}{util.PKG_EXTENSION}"


def path_problem(value):
    """Why a manager path is unsafe, using the client's own rules, or None."""
    if not isinstance(value, str) or not value:
        return "must be a non-empty string"
    if value.startswith(("/", "\\")):
        return "must be relative"
    for segment in value.split("/"):
        if segment in ("", ".", ".."):
            return "has an empty or traversing segment"
        if not _PATH_SEGMENT.match(segment):
            return f"segment {segment!r} may only use ASCII letters, digits and . _ -"
    if value == DESCRIPTOR:
        return "would overwrite the entry descriptor"
    return None


# --- descriptor ------------------------------------------------------------

def load_descriptor(r_dir=R_DIR):
    file = os.path.join(r_dir, DESCRIPTOR)
    try:
        descriptor = util.read_json(file)
    except (OSError, ValueError) as err:
        raise util.NestError(f"{util.rel(file)}: {err}") from err
    problems = descriptor_errors(descriptor)
    if problems:
        raise util.NestError(f"{util.rel(file)}: " + "; ".join(problems))
    return descriptor


def descriptor_errors(descriptor):
    problems = []
    if not isinstance(descriptor, dict):
        return ["must be a JSON object"]
    if descriptor.get("schema") != ENTRY_SCHEMA:
        problems.append(f"schema must be {ENTRY_SCHEMA}")
    if not isinstance(descriptor.get("name"), str) or not descriptor.get("name"):
        problems.append("name is required")
    managers = descriptor.get("managers")
    if not isinstance(managers, dict) or not managers:
        return problems + ["managers must declare at least one backend"]
    paths = {}
    for mgr, value in managers.items():
        if mgr not in util.BACKENDS:
            problems.append(f"managers.{mgr}: not a backend")
            continue
        path = value.get("path") if isinstance(value, dict) else None
        problem = path_problem(path)
        if problem:
            problems.append(f"managers.{mgr}.path {problem}")
        elif path in paths:
            problems.append(f"managers.{mgr}.path is also used by {paths[path]}")
        else:
            paths[path] = mgr
    return problems


def manager_paths(descriptor):
    return {mgr: value["path"] for mgr, value in descriptor["managers"].items()}


# --- backend indexes -------------------------------------------------------

def _backend_files(r_dir):
    """Every generated backend index under r/: {relative path: document}."""
    found = {}
    for directory, _dirs, files in os.walk(r_dir):
        for name in files:
            if not name.endswith(".json"):
                continue
            file = os.path.join(directory, name)
            relative = os.path.relpath(file, r_dir).replace(os.sep, "/")
            if relative == DESCRIPTOR:
                continue
            try:
                document = util.read_json(file)
            except (OSError, ValueError):
                continue
            if isinstance(document, dict) and "mgr" in document and "packages" in document:
                found[relative] = document
    return found


def read_entries(r_dir=R_DIR):
    """{mgr: {(pkgname, platform, arch): entry}} from every backend index on disk.

    Indexes at paths the descriptor no longer declares are read too, so moving a
    manager's path in r/index.json moves its entries instead of dropping them.
    """
    entries = {mgr: {} for mgr in util.BACKENDS}
    for document in _backend_files(r_dir).values():
        mgr = document.get("mgr")
        if mgr not in entries:
            continue
        for entry in document.get("packages") or []:
            entries[mgr][entry_key(entry)] = entry
    return entries


def entry_key(entry):
    return (entry.get("pkgname", ""), entry.get("platform", ""), entry.get("arch", ""))


def write_layout(r_dir, descriptor, entries):
    """Write one index per declared manager; remove generated files elsewhere.

    Returns (written, removed) relative paths. `generated_at` moves only when a
    manager's entries changed, so an identical rebuild produces no commit.
    """
    paths = manager_paths(descriptor)
    for mgr, bucket in entries.items():
        if bucket and mgr not in paths:
            raise util.NestError(f"{len(bucket)} {mgr} entries, but r/index.json declares no {mgr} manager")
    existing = _backend_files(r_dir)
    written, removed = [], []
    for mgr, relative in sorted(paths.items()):
        packages = sorted(entries.get(mgr, {}).values(), key=lambda e: (e["pkgname"], e["platform"], e["arch"]))
        previous = existing.get(relative)
        if previous and previous.get("mgr") == mgr and previous.get("packages") == packages:
            continue
        document = {"schema": BACKEND_SCHEMA, "mgr": mgr, "generated_at": now(), "packages": packages}
        util.write_json(os.path.join(r_dir, *relative.split("/")), document)
        written.append(relative)
    for relative in existing:
        if relative not in paths.values():
            os.remove(os.path.join(r_dir, *relative.split("/")))
            removed.append(relative)
            directory = os.path.dirname(os.path.join(r_dir, *relative.split("/")))
            while os.path.abspath(directory) != os.path.abspath(r_dir) and not os.listdir(directory):
                os.rmdir(directory)
                directory = os.path.dirname(directory)
    return written, removed


def entry_errors(mgr, entry, release_base=None):
    label = entry.get("filename") or entry.get("pkgname") or "<entry>"
    problems = []
    for field in REQUIRED:
        if not isinstance(entry.get(field), str) or not entry.get(field):
            problems.append(f"{label}: {field} is missing")
    if problems:
        return problems
    if entry["pkgmgr"] != mgr:
        problems.append(f"{label}: pkgmgr {entry['pkgmgr']} in the {mgr} index")
        return problems
    if not _SHA256.match(entry["sha256"]):
        problems.append(f"{label}: sha256 is not 64 lowercase hex digits")
    if entry["filename"] != expected_filename(entry):
        problems.append(f"{label}: filename should be {expected_filename(entry)}")
    if not util.PUBLISHABLE.match(entry["filename"]):
        problems.append(f"{label}: filename has characters a GitHub release asset cannot keep")
    for field in ("size", "csize"):
        if not isinstance(entry.get(field), int):
            problems.append(f"{label}: {field} must be a number")
    url = entry.get("download_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        problems.append(f"{label}: download_url must be an https URL")
    elif release_base and url != f"{release_base}/{entry['filename']}":
        problems.append(f"{label}: download_url is not {release_base}/{entry['filename']}")
    return problems


def layout_errors(r_dir=R_DIR, release_base=None):
    """Everything wrong with the committed r/ tree."""
    try:
        descriptor = load_descriptor(r_dir)
    except util.NestError as err:
        return [str(err)]
    problems = []
    files = _backend_files(r_dir)
    for mgr, relative in manager_paths(descriptor).items():
        document = files.get(relative)
        if document is None:
            continue  # nothing published for it yet
        if document.get("schema") != BACKEND_SCHEMA or document.get("mgr") != mgr:
            problems.append(f"r/{relative}: must be schema {BACKEND_SCHEMA} with mgr {mgr}")
            continue
        seen = set()
        for entry in document.get("packages") or []:
            for problem in entry_errors(mgr, entry, release_base):
                problems.append(f"r/{relative}: {problem}")
            key = entry_key(entry)
            if key in seen:
                problems.append(f"r/{relative}: {key} appears twice")
            seen.add(key)
    for relative in files:
        if relative not in manager_paths(descriptor).values():
            problems.append(f"r/{relative}: not declared in r/index.json (run `nest index` to relayout)")
    return problems


# --- merging a build -------------------------------------------------------

def build_entries(build_output):
    """{mgr: [(entry, artifact path)]} from what `nest build` wrote."""
    found = {}
    for mgr in util.BACKENDS:
        file = os.path.join(build_output, mgr, "index.json")
        if not os.path.isfile(file):
            continue
        document = util.read_json(file)
        for entry in document.get("packages") or []:
            artifact = os.path.join(build_output, mgr, entry.get("filename", ""))
            found.setdefault(mgr, []).append((entry, artifact))
    return found


def expected_keys(packages, config):
    """Every (mgr, pkgname, platform, arch) the current packages/ tree publishes."""
    keys = set()
    for package in packages:
        for target in package.targets:
            for arch in package.arches:
                keys.add((target, package.pkgname, config.platform(arch), arch))
    return keys


def merge(r_dir, build_output, packages, config, release_base, release_list=None):
    """Fold a build into r/, drop entries of removed packages, relayout. Returns a summary.

    A published filename is immutable. pica-pack stamps `builddate` into every
    artifact, so rebuilding an unchanged package yields the same name with new
    bytes; overwriting the asset would hand every device still holding the old
    index a file whose sha256 no longer matches. Such a rebuild keeps the
    published entry and is reported in `kept` — to republish a package under
    the same version, bump its `pkgrel`.
    """
    descriptor = load_descriptor(r_dir)
    entries = read_entries(r_dir)
    declared = manager_paths(descriptor)
    released = []
    changed = []
    kept = []
    built = build_entries(build_output) if build_output and os.path.isdir(build_output) else {}
    for mgr, items in built.items():
        if mgr not in declared:
            raise util.NestError(f"built {mgr} packages, but r/index.json declares no {mgr} manager")
        for entry, artifact in items:
            if not os.path.isfile(artifact):
                raise util.NestError(f"{entry.get('filename')}: artifact is missing from {util.rel(build_output)}")
            entry = dict(entry)
            entry["download_url"] = f"{release_base}/{entry['filename']}"
            if util.sha256_file(artifact) != entry.get("sha256"):
                raise util.NestError(f"{entry['filename']}: sha256 does not match the artifact")
            if os.path.getsize(artifact) != entry.get("csize"):
                raise util.NestError(f"{entry['filename']}: csize does not match the artifact")
            problems = entry_errors(mgr, entry, release_base)
            if problems:
                raise util.NestError("; ".join(problems))
            key = entry_key(entry)
            previous = entries[mgr].get(key)
            if previous is not None and previous.get("filename") == entry["filename"]:
                if previous.get("sha256") != entry["sha256"]:
                    kept.append(f"{mgr} {entry['filename']}")
                continue
            if previous is None:
                changed.append(f"+ {mgr} {entry['filename']}")
            else:
                changed.append(f"~ {mgr} {previous.get('filename')} -> {entry['filename']}")
            entries[mgr][key] = entry
            released.append(os.path.abspath(artifact))
    wanted = expected_keys(packages, config)
    for mgr, bucket in entries.items():
        for key in list(bucket):
            if (mgr, *key) not in wanted:
                changed.append(f"- {mgr} {bucket[key].get('filename')} (package or arch removed)")
                del bucket[key]
    written, removed = write_layout(r_dir, descriptor, entries)
    if release_list:
        os.makedirs(os.path.dirname(os.path.abspath(release_list)) or ".", exist_ok=True)
        with open(release_list, "w", encoding="utf-8", newline="\n") as handle:
            for artifact in sorted(released):
                handle.write(artifact + "\n")
    return {"changed": changed, "kept": kept, "written": written, "removed": removed, "released": released}


def referenced_filenames(r_dir):
    names = set()
    for document in _backend_files(r_dir).values():
        for entry in document.get("packages") or []:
            if entry.get("filename"):
                names.add(entry["filename"])
    return names


def entry_by_filename(r_dir):
    found = {}
    for document in _backend_files(r_dir).values():
        for entry in document.get("packages") or []:
            found[entry.get("filename")] = entry
    return found


def dumps(value):
    return json.dumps(value, indent=2, ensure_ascii=False)
