"""Read package identity out of a built `.ipk` or `.apk` container.

The filename a job matches upstream with is not the name Pica installs. An
`luci-app-openclash_0.47.075_all.ipk` asset installs as `luci-app-openclash`,
and only the container's control metadata says so authoritatively.

`.ipk` is read here: an outer tar whose `control.tar.gz` member holds `control`.

`.apk` is **not** read, and that is a real limitation rather than an oversight.
An `.apk` produced by apk-tools 3.x starts with `ADBd`, the ADB signature block
of the apk database format. It is not the concatenated-gzip layout that
`tar --ignore-zeros` was written for, so neither GNU tar nor BusyBox tar can
open it — `unrecognized archive format` / `unrecognized option: ignore-zeros`
respectively. Reading it needs an ADB-aware parser, and `apk adbdump` is the
only tool that provides one. When that is unavailable the check is skipped
with a warning instead of guessing from the filename, because a guessed name
would defeat the purpose of the check.

The package name and architecture are read; dependency metadata is deliberately
ignored because no backend in this repository resolves dependency graphs.
"""
import os
import re
import shutil
import subprocess
import tarfile
import tempfile

# APK signature block of the apk-tools 3.x (ADB) format, which tar cannot open.
ADB_SIGNATURE = b"ADBd"

DEPENDENCY_KEYS = ("app", "base", "kmod", "app_i18n")


def _extract(path, dest):
    """Unpack `path` into `dest`, returning True on success."""
    try:
        # bsdtar (macOS) rejects --ignore-zeros; busybox tar needs it. Try the
        # portable spelling last so a single-member archive still unpacks.
        for extra in (["--ignore-zeros"], []):
            result = subprocess.run(
                ["tar", "-xzf", path, "-C", dest] + extra,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return True
    except FileNotFoundError:
        print(f"warning: tar not available, cannot inspect {os.path.basename(path)}")
    return False


def _read_ipk_control(path, dest):
    """Return the raw `control` text of an .ipk, or None."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()
            control_member = next(
                (m for m in members if os.path.basename(m.name).startswith("control.tar")),
                None,
            )
            if control_member is not None:
                extracted = os.path.join(dest, "control.tar")
                with open(extracted, "wb") as handle:
                    shutil.copyfileobj(tar.extractfile(control_member), handle)
            else:
                # Some builders inline control as a plain file.
                plain = next((m for m in members if os.path.basename(m.name) == "control"), None)
                if plain is None:
                    return None
                return tar.extractfile(plain).read().decode("utf-8", "replace")
    except (tarfile.TarError, OSError) as err:
        print(f"warning: cannot read {os.path.basename(path)}: {err}")
        return None

    inner = os.path.join(dest, "control")
    os.makedirs(inner, exist_ok=True)
    if not _extract(extracted, inner):
        return None
    for root, _, files in os.walk(inner):
        if "control" in files:
            with open(os.path.join(root, "control"), "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
    return None


def _read_apk_control(path, dest):
    """Return the control text of an .apk, or None when it cannot be read.

    Handles the legacy concatenated-gzip layout. The apk-tools 3.x (ADB)
    layout is recognised and refused rather than half-parsed, because `tar`
    cannot open it at all.
    """
    try:
        with open(path, "rb") as handle:
            if handle.read(4) == ADB_SIGNATURE:
                print(
                    f"note: {os.path.basename(path)} is the apk-tools 3.x (ADB) format;"
                    " tar cannot read it. Install apk-tools and use `apk adbdump`,"
                    " or check the name against the manifest by hand."
                )
                return None
    except OSError:
        return None

    if not _extract(path, dest):
        return None
    chunks = []
    for entry in sorted(os.listdir(dest)):
        if entry.startswith(".") and os.path.isfile(os.path.join(dest, entry)):
            try:
                with open(os.path.join(dest, entry), "r", encoding="utf-8", errors="replace") as handle:
                    chunks.append(handle.read())
            except OSError:
                continue
    return "\n".join(chunks) if chunks else None


def container_pkgname(path):
    """Return the `Package:` declared inside a container, or None.

    Only the package name is read here; dependency metadata is deliberately
    ignored because no backend in this repo resolves dependency graphs.
    """
    metadata = container_metadata(path)
    return metadata["package"] if metadata else None


def container_metadata(path):
    """Return the `Package:` and `Architecture:` of a container, or None.

    `None` means the container could not be read: an ADB-format apk (apk-tools
    3.x), a corrupt archive, or a missing `tar`. That is a distinct outcome
    from "read and found no metadata"; callers report it as unverified instead
    of treating it as a successful check.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension not in (".ipk", ".apk"):
        return None

    with tempfile.TemporaryDirectory(prefix="pica-inspect-") as tmp:
        text = _read_apk_control(path, tmp) if extension == ".apk" else _read_ipk_control(path, tmp)
        if not text:
            return None
        package = re.search(r"^Package:\s*(\S+)\s*$", text, re.MULTILINE)
        architecture = re.search(r"^Architecture:\s*(\S+)\s*$", text, re.MULTILINE)
        if package is None and architecture is None:
            return None
        return {
            "package": package.group(1) if package else None,
            "architecture": architecture.group(1) if architecture else None,
        }


def manifest_dependencies(manifest_path):
    """Map each declared key to every value declared for it.

    A manifest may declare `app = a` twice, or a backend-scoped
    `app@opkg = b` / arch-scoped `app.<arch> = c`, so all scoped forms are
    collected under their base key. `app_i18n` is collected with its `{lang}`
    template intact; [`declares_package`] matches it against a concrete
    language package.
    """
    declared = {key: set() for key in DEPENDENCY_KEYS}
    if not os.path.isfile(manifest_path):
        return declared

    with open(manifest_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().split(".", 1)[0].split("@", 1)[0]
            if key in declared and value.strip():
                declared[key].add(value.strip())
    return declared


def declares_package(declared, name):
    """Whether a manifest declares `name` as an app/base/kmod/i18n package.

    `app_i18n` is a template (`luci-i18n-demo-{lang}`), so the language segment
    is matched as a wildcard rather than requiring the template literally.
    """
    known = declared.get("app", set()) | declared.get("base", set()) | declared.get("kmod", set())
    if name in known:
        return True
    for template in declared.get("app_i18n", ()):
        pattern = "^" + re.escape(template).replace(re.escape("{lang}"), ".+") + "$"
        if re.match(pattern, name):
            return True
    return False


def check_downloaded(pkg_dir, build_config):
    """Fail when a fetched container's package name is not declared in the manifest.

    Upstream renames a package without touching this repo often enough that a
    silent mismatch would ship a package that installs a name nothing else
    references. Declared names are the contract; this makes a violation loud.

    Containers whose name cannot be read are reported separately rather than
    counted as mismatches, so an unreadable format does not fail the build.

    Returns (problems, skipped).
    """
    backend = build_config.get("mgr", "opkg")
    pkgs_root = os.path.join(pkg_dir, "pkgs", backend)
    declared = manifest_dependencies(build_config.get("manifest") or os.path.join(pkg_dir, "manifest"))
    known = declared["app"] | declared["base"] | declared["kmod"]

    problems = []
    skipped = []
    if not os.path.isdir(pkgs_root):
        return problems, skipped
    has_declarations = any(declared.values())

    for platform in sorted(os.listdir(pkgs_root)):
        platform_dir = os.path.join(pkgs_root, platform)
        if not os.path.isdir(platform_dir):
            continue
        for arch in sorted(os.listdir(platform_dir)):
            arch_dir = os.path.join(platform_dir, arch)
            if not os.path.isdir(arch_dir):
                continue
            for entry in sorted(os.listdir(arch_dir)):
                path = os.path.join(arch_dir, entry)
                if not os.path.isfile(path):
                    continue
                name = container_pkgname(path)
                if name is None:
                    # An unreadable container is not proof of a mismatch; only a
                    # confirmed mismatch should fail the build.
                    skipped.append(f"{platform}/{arch}/{entry}")
                elif has_declarations and not declares_package(declared, name):
                    problems.append(
                        f"{platform}/{arch}/{entry}: installs as '{name}', which the manifest does not"
                        f" declare. Add it (app/base/kmod, or '<key>.{arch} =' when it varies by arch)"
                        f" or update the asset regex."
                    )
                else:
                    print(f"checked {platform}/{arch}/{entry}: installs as '{name}'")

    return problems, skipped