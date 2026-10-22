#!/usr/bin/env python3
"""The `r/` index layout: one entry descriptor, one index per backend.

`r/repo.json` is what a Pica client fetches today, and it stays the schema 2
index it has always been. It is also the only file under `r/` whose contents
come from real build output, so everything else there is derived from it:

    r/
    ├── index.json          entry descriptor: schema, name, manager -> path
    ├── repo.json           schema 2, every package — unchanged for clients
    ├── apk/index.json      the apk entries from repo.json
    ├── opkg/index.json     the opkg entries
    └── pica/index.json     the pica entries

The per-backend files are views, not a second source of truth. They are
regenerated from `repo.json` in the same run that rewrites it, and
`validate_index_layout.py` fails when the two disagree, so nobody can hand-edit
one of them into a state the others do not describe.

A backend with no packages gets neither a file nor a declaration: an empty
index would advertise a manager this source does not actually serve, and a
client that trusted it would resolve nothing.

Paths in the descriptor are relative to `r/` and carry no scheme, host, leading
slash or traversing segment. That is what lets the same tree sit behind GitHub
raw, the Cloudflare Worker, or a self-hosted mirror without being rewritten —
and it is why the path rules below are strict enough to be worth enforcing
rather than merely documenting.
"""
import json
import os
import re

# A descriptor written by this module. Bumped only if the shape below changes.
ENTRY_SCHEMA = 1
# A per-backend index written by this module.
BACKEND_SCHEMA = 1

ENTRY_FILE = "index.json"
BACKEND_FILE = "index.json"
REPO_FILE = "repo.json"

# Mirrors SUPPORTED_PKGMGRS in pica-pkg-core/src/repo.rs.
SUPPORTED_MANAGERS = ("apk", "opkg", "pica")

# Fields `pica -S` refuses to do without. Kept here so a backend index cannot
# quietly become thinner than the schema 2 index it is a view of.
REQUIRED_FIELDS = (
    "pkgname",
    "pkgver",
    "pkgrel",
    "pkgmgr",
    "platform",
    "arch",
    "filename",
    "sha256",
)

PKG_EXTENSION = ".pkg.tar.gz"

# One path segment: a plain name. No dots-only, no separators, no escapes.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+~-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LayoutError(ValueError):
    """A path or descriptor that must not be published."""


def manager_index_path(manager):
    """Where one backend's index lives, relative to `r/`."""
    if manager not in SUPPORTED_MANAGERS:
        raise LayoutError(f"unsupported manager {manager!r}")
    return f"{manager}/{BACKEND_FILE}"


def check_relative_path(path, label="path"):
    """Reject anything that is not a plain relative path under `r/`.

    The checks are ordered from the ones a mistaken config produces to the ones
    only a hostile one would: an absolute URL because someone pasted a download
    link, then traversal, then the exotic forms.

    An absolute URL is the failure that matters most. `path` is resolved
    against a base the *client* chooses, so a host baked in here would pin every
    mirror to whichever one was configured first.
    """
    if not isinstance(path, str) or not path:
        raise LayoutError(f"{label}: must be a non-empty string")
    if "://" in path or ":" in path:
        raise LayoutError(f"{label}: {path!r} must not carry a scheme, host or drive")
    if path.startswith(("/", "\\")):
        raise LayoutError(f"{label}: {path!r} must be relative, not absolute")
    if "\\" in path:
        raise LayoutError(f"{label}: {path!r} must use forward slashes")
    if "?" in path or "#" in path:
        raise LayoutError(f"{label}: {path!r} must not carry a query or fragment")
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise LayoutError(f"{label}: {path!r} has an empty or traversing segment")
        if not _SEGMENT.match(segment):
            raise LayoutError(
                f"{label}: {path!r} has a segment {segment!r} that is not a plain name"
            )
    if not segments[-1].endswith(".json"):
        raise LayoutError(f"{label}: {path!r} must name a .json file")
    return path


def resolve(root, relative):
    """Turn a descriptor path into a path on disk, inside `root` and nowhere else.

    `check_relative_path` already rejects `..`, so this second barrier is for
    the case where the filesystem itself redirects — a symlinked backend
    directory would otherwise let a well-formed relative path read outside the
    tree.
    """
    check_relative_path(relative)
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, *relative.split("/")))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise LayoutError(f"path {relative!r} resolves outside {root}")
    return target


def build_entry(packages, name, description=None):
    """The descriptor for a source holding `packages` (any number, from repo.json)."""
    managers = sorted({pkg["pkgmgr"] for pkg in packages})
    entry = {"schema": ENTRY_SCHEMA, "name": name}
    if description:
        entry["description"] = description
    entry["managers"] = {manager: {"path": manager_index_path(manager)} for manager in managers}
    return entry


def build_backend(manager, packages, generated_at=None):
    """One backend's view of the index.

    Entries are copied whole rather than projected onto a shorter field list:
    a client that has the backend index and not `repo.json` must still be able
    to resolve an asset, which needs `filename`, `sha256` and `download_url`
    together.
    """
    ordered = sorted(
        packages,
        key=lambda pkg: (pkg["pkgname"], pkg.get("platform", ""), pkg.get("arch", ""), pkg["filename"]),
    )
    index = {"schema": BACKEND_SCHEMA, "mgr": manager}
    if generated_at:
        index["generated_at"] = generated_at
    index["packages"] = ordered
    return index


def _write_if_changed(path, payload):
    """Write canonical JSON, leaving an identical file untouched."""
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def write_layout(root, packages, name, description=None, generated_at=None):
    """Write `index.json` plus one index per backend that has packages.

    Returns `(entry, written, pruned)` — the descriptor, the relative paths this
    call wrote, and the backend indexes it removed because their manager no
    longer has anything in it.
    """
    by_manager = {}
    for pkg in packages:
        manager = pkg.get("pkgmgr")
        if manager not in SUPPORTED_MANAGERS:
            raise LayoutError(f"{pkg.get('pkgname', '<unnamed>')}: unsupported pkgmgr {manager!r}")
        by_manager.setdefault(manager, []).append(pkg)

    written = []
    pruned = []
    for manager in SUPPORTED_MANAGERS:
        relative = manager_index_path(manager)
        target = resolve(root, relative)
        group = by_manager.get(manager, [])
        if group:
            if _write_if_changed(target, build_backend(manager, group, generated_at)):
                written.append(relative)
        elif os.path.isfile(target):
            # Nothing left to declare. Leaving the file behind would keep a
            # manager visible that this source no longer serves.
            os.remove(target)
            pruned.append(relative)

    entry = build_entry(packages, name, description)
    if _write_if_changed(resolve(root, ENTRY_FILE), entry):
        written.append(ENTRY_FILE)
    return entry, written, pruned


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_entry(entry, root):
    """Check the descriptor and every index it names. Returns `(managers, errors)`."""
    errors = []
    if not isinstance(entry, dict):
        return {}, ["index.json: must be a JSON object"]
    if entry.get("schema") != ENTRY_SCHEMA:
        errors.append(f"index.json: schema must be {ENTRY_SCHEMA}, got {entry.get('schema')!r}")
    if not isinstance(entry.get("name"), str) or not entry.get("name"):
        errors.append("index.json: name must be a non-empty string")
    managers = entry.get("managers")
    if not isinstance(managers, dict):
        return {}, errors + ["index.json: managers must be an object"]

    resolved = {}
    for manager, value in managers.items():
        if manager not in SUPPORTED_MANAGERS:
            errors.append(f"index.json: {manager!r} is not a supported manager")
            continue
        if not isinstance(value, dict):
            errors.append(f"index.json: managers.{manager} must be an object")
            continue
        extra = set(value) - {"path"}
        if extra:
            # Download hosts and cache policy belong to the serving layer. A
            # mirror that honoured them would be pinned to this repository's.
            errors.append(
                f"index.json: managers.{manager} carries {sorted(extra)}; only `path` belongs here"
            )
        raw = value.get("path")
        try:
            check_relative_path(raw, f"index.json: managers.{manager}.path")
            target = resolve(root, raw)
        except LayoutError as err:
            errors.append(str(err))
            continue
        if not os.path.isfile(target):
            errors.append(f"{raw}: declared by index.json but not present")
            continue
        try:
            index = load_json(target)
        except (OSError, json.JSONDecodeError) as err:
            errors.append(f"{raw}: cannot be read: {err}")
            continue
        errors.extend(validate_backend(index, manager, raw))
        resolved[manager] = raw
    return resolved, errors


def validate_backend(index, manager, label):
    errors = []
    if not isinstance(index, dict):
        return [f"{label}: must be a JSON object"]
    if index.get("schema") != BACKEND_SCHEMA:
        errors.append(f"{label}: schema must be {BACKEND_SCHEMA}, got {index.get('schema')!r}")
    if index.get("mgr") != manager:
        errors.append(f"{label}: mgr {index.get('mgr')!r} does not match its declaration {manager!r}")
    packages = index.get("packages")
    if not isinstance(packages, list) or not packages:
        return errors + [f"{label}: packages must be a non-empty array"]
    for pkg in packages:
        name = pkg.get("pkgname", "<unnamed>") if isinstance(pkg, dict) else "<unnamed>"
        if not isinstance(pkg, dict):
            errors.append(f"{label}: {name} is not an object")
            continue
        for field in REQUIRED_FIELDS:
            if not pkg.get(field):
                errors.append(f"{label}: {name} missing {field}")
        if pkg.get("pkgmgr") != manager:
            errors.append(f"{label}: {name} has pkgmgr {pkg.get('pkgmgr')!r}, expected {manager!r}")
        filename = str(pkg.get("filename", ""))
        if filename and (not filename.endswith(PKG_EXTENSION) or "/" in filename):
            errors.append(f"{label}: {name} filename {filename!r} is not a bare {PKG_EXTENSION} name")
        digest = str(pkg.get("sha256", ""))
        if digest and not _SHA256.match(digest.lower()):
            errors.append(f"{label}: {name} sha256 {digest!r} is not a hex digest")
    return errors


def _canonical(pkg):
    """One entry, serialised stably, for set comparison between the two trees."""
    return json.dumps(pkg, sort_keys=True, ensure_ascii=False)


def consistency_errors(repo_packages, resolved, root):
    """Every backend index must equal the slice of `repo.json` it claims to be.

    This is the check that keeps "one source of truth" from being an intention.
    Without it a hand-edited backend index stays green until a client resolves
    a filename that `repo.json` does not know about.
    """
    errors = []
    by_manager = {}
    for pkg in repo_packages:
        by_manager.setdefault(pkg.get("pkgmgr"), []).append(_canonical(pkg))

    declared = set(resolved)
    present = {manager for manager, entries in by_manager.items() if entries}
    for manager in sorted(present - declared):
        errors.append(f"{manager}: {len(by_manager[manager])} package(s) in repo.json but no index is declared")
    for manager in sorted(declared - present):
        errors.append(f"{manager}: index is declared but repo.json holds no {manager} package")

    for manager, relative in sorted(resolved.items()):
        target = resolve(root, relative)
        try:
            index = load_json(target)
        except (OSError, json.JSONDecodeError) as err:
            errors.append(f"{relative}: cannot be read: {err}")
            continue
        listed = [_canonical(pkg) for pkg in index.get("packages", []) if isinstance(pkg, dict)]
        expected = by_manager.get(manager, [])
        if sorted(listed) != sorted(expected):
            shared = set(listed) & set(expected)
            errors.append(
                f"{relative}: differs from repo.json"
                f" ({len(listed) - len(shared)} entr(y|ies) only in the backend index,"
                f" {len(expected) - len(shared)} only in repo.json)"
            )
    return errors


def select_managers(entry, wanted):
    """Resolve a client's manager combination against the descriptor.

    `wanted` is what the device asked for — say `apk + pica`. The return value
    is `(selected, missing)`: paths for the managers this source declares, and
    the names it does not. A caller must treat `missing` as "not available"
    rather than falling back to some other index, which is the whole point of
    the descriptor declaring a subset.
    """
    if isinstance(wanted, str):
        wanted = [part.strip() for part in wanted.split(",")]
    managers = entry.get("managers") if isinstance(entry, dict) else None
    if not isinstance(managers, dict):
        return {}, [name for name in wanted if name]

    selected = {}
    missing = []
    seen = set()
    for raw in wanted:
        manager = (raw or "").strip().lower()
        if not manager or manager in seen:
            continue
        seen.add(manager)
        declared = managers.get(manager)
        if isinstance(declared, dict):
            check_relative_path(declared.get("path"), f"index.json: managers.{manager}.path")
            selected[manager] = declared["path"]
        else:
            missing.append(manager)
    return selected, missing
