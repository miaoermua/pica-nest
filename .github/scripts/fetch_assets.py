import os
import sys
import json
import re
import urllib.request
import tarfile
import zipfile
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from container_metadata import check_downloaded

def fetch_json(url):
    print(f"Fetching {url}...")
    headers = {
        'User-Agent': 'Pica-CI-Asset-Fetcher',
        'Accept': 'application/vnd.github.v3+json'
    }
    # Pass GITHUB_TOKEN if available to avoid rate limits
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
        
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode())

def download_file(url, path):
    print(f"Downloading {url} to {path}...")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Use generic UA for general HTTP downloads
    headers = {'User-Agent': 'Mozilla/5.0 (Pica-CI-Asset-Fetcher)'}
    token = os.environ.get("GITHUB_TOKEN")
    # Only send token if it's GitHub
    if "github.com" in url and token:
       headers["Authorization"] = f"token {token}"
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            with open(path, 'wb') as out_file:
                out_file.write(response.read())
        return True
    except Exception as e:
        print(f"Failed to download from {url}: {e}")
        return False

def extract_from_zip(zip_path, inner_regex, final_path):
    """
    Download a zip, find a file inside matching inner_regex, and move it to final_path.
    """
    print(f"Extracting matching '{inner_regex}' from {zip_path}...")
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(tmp_dir)
                for root, _, files in os.walk(tmp_dir):
                    for f in files:
                        if re.search(inner_regex, f):
                            src = os.path.join(root, f)
                            os.makedirs(os.path.dirname(final_path), exist_ok=True)
                            shutil.move(src, final_path)
                            print(f"Successfully extracted to {final_path}")
                            return True
    except Exception as e:
        print(f"Extraction failed: {e}")
    return False

def fetch_github_assets(pkg_dir, build_config):
    source = build_config.get("source", {})
    repo = source.get("repo")
    release_cfg = source.get("release", {})
    tag_cfg = release_cfg.get("tag", {})
    tag_regex = tag_cfg.get("regex")
    allow_prerelease = release_cfg.get("allow_prerelease", False)

    api_url = f"https://api.github.com/repos/{repo}/releases"
    try:
        releases = fetch_json(api_url)
    except Exception as e:
        print(f"Error fetching releases from {repo}: {e}")
        return False

    target_release = None
    for r in releases:
        if r.get("prerelease") and not allow_prerelease:
            continue
        if tag_regex:
            if not re.match(tag_regex, r.get("tag_name", "")):
                continue
        target_release = r
        break

    if not target_release:
        print(f"No matching GitHub release found for {repo}")
        return False

    print(f"Matched GitHub release: {target_release['tag_name']}")
    
    assets_cfg = release_cfg.get("assets", [])
    matrix = build_config.get("build", {}).get("matrix", [])
    found_any = False

    for asset_meta in assets_cfg:
        asset_regex = asset_meta.get("regex")
        inner_regex = asset_meta.get("inner_regex") # For zip extraction
        capture_to = asset_meta.get("capture_to", {})
        fixed_target = asset_meta.get("target", {})
        required = asset_meta.get("required", True)

        found_this_asset_meta = False
        for a in target_release.get("assets", []):
            match = re.match(asset_regex, a["name"])
            if match:
                target_platform = fixed_target.get("platform")
                target_arch = fixed_target.get("arch")

                if capture_to:
                    for key, group_idx in capture_to.items():
                        val = match.group(int(group_idx))
                        if key == "arch": target_arch = val
                        elif key == "platform": target_platform = val

                # If we have an arch but no platform, look it up in the matrix
                if target_arch and not target_platform:
                    in_matrix = False
                    for m in matrix:
                        if m.get("arch") == target_arch:
                            target_platform = m.get("platform")
                            in_matrix = True
                            break
                    # STRICT FILTERING: If capture_to was used, we only download if it's in our matrix
                    if not in_matrix:
                        print(f"Skipping {a['name']}: Architecture {target_arch} not in matrix.")
                        continue
                
                if not target_platform: target_platform = "all"
                if not target_arch: target_arch = "all"

                # Pica 包布局：`pkgs/<backend>/<platform>/<arch>/`。后端取自
                # build.json 的 `mgr`，与 manifest 的 `targets` 必须一致，否则
                # pica-pack 会以"pkgs/<backend>/ present but <backend> is not
                # in targets"拒绝构建。
                backend = build_config.get("mgr", "opkg")
                dest_dir = os.path.join(pkg_dir, "pkgs", backend, target_platform, target_arch)
                
                # If we need to extract from zip, we download to a temporary location first
                if inner_regex and a["name"].endswith(".zip"):
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
                        zip_tmp_path = tmp_zip.name
                    
                    if download_file(a["browser_download_url"], zip_tmp_path):
                        # Determine final filename from inner regex or a default
                        final_filename = a["name"].replace(".zip", ".ipk") # Guessing .ipk if from zip
                        # Actually we can do better: if inner_regex matches something, we'll get it.
                        # Let's use the asset role + arch for a clean name if possible?
                        # For now, let's just use a placeholder and improve if needed.
                        final_path = os.path.join(dest_dir, final_filename)
                        if extract_from_zip(zip_tmp_path, inner_regex, final_path):
                            found_any = True
                            found_this_asset_meta = True
                        os.remove(zip_tmp_path)
                else:
                    dest_path = os.path.join(dest_dir, a["name"])
                    if download_file(a["browser_download_url"], dest_path):
                        found_any = True
                        found_this_asset_meta = True
        
        if not found_this_asset_meta and required:
            print(f"Required GitHub asset '{asset_regex}' not found.")
            return False

    return found_any

def fetch_http_template_assets(pkg_dir, build_config):
    source = build_config.get("source", {})
    url_templates = []
    if "url_template" in source:
        url_templates.append(source["url_template"])
    if "url_templates" in source:
        url_templates.extend(source["url_templates"])

    if not url_templates:
        print("Error: 'url_template' or 'url_templates' missing for http_template provider.")
        return False

    version = source.get("version", "latest")
    matrix = build_config.get("build", {}).get("matrix", [])
    if not matrix:
        print("Warning: No matrix defined for http_template fetch.")
        return False

    found_any = False
    for m in matrix:
        arch = m.get("arch")
        platform = m.get("platform", "all")
        if not arch: continue

        for url_template in url_templates:
            url = url_template.replace("{arch}", arch).replace("{version}", version).replace("{platform}", platform)
            filename = url.split("/")[-1]
            backend = build_config.get("mgr", "opkg")
            dest_dir = os.path.join(pkg_dir, "pkgs", backend, platform, arch)
            dest_path = os.path.join(dest_dir, filename)

            if download_file(url, dest_path):
                found_any = True
            else:
                print(f"Could not download asset for {arch} from {url}")

    return found_any

def extract_tarball(pkg_dir, build_config):
    """
    Download a release archive and unpack it into the package's root/.

    A source that ships no package of its own — a bare upstream tarball — has
    nowhere else to go: the artifacts a `pica` backend package carries are
    plain files under `root/`, and there is no container to put the download in.
    Unpacking it here is what makes that shape buildable.

    `strip_components` drops that many leading path components, because these
    archives wrap everything in one directory whose name is tied to the release
    (`fastfetch-musl-amd64/...`). Leaving it in place would put the binary one
    level too deep, and the command would not be where the manifest says it is.

    `include` narrows the unpack to the paths a device actually needs: a full
    archive also carries shell completions and man pages, which are dead weight
    on a router.
    """
    source = build_config["source"]
    url_template = source.get("url_template")
    url_templates = source.get("url_templates") or ([url_template] if url_template else [])
    if not url_templates:
        print("error: tarball provider needs url_template or url_templates", file=sys.stderr)
        return False

    strip = source.get("strip_components", 0)
    includes = source.get("include") or []
    matrix = build_config.get("build", {}).get("matrix", [{}])
    root_dir = os.path.join(pkg_dir, "root")

    ok = True
    for entry in matrix:
        platform = entry.get("platform", "all")
        arch = entry.get("arch", "all")
        matched = False

        for template in url_templates:
            url = (template
                   .replace("{platform}", str(platform))
                   .replace("{arch}", str(arch))
                   .replace("{version}", str(source.get("version", ""))))
            if "{arch}" not in template and len(matrix) > 1:
                # One URL cannot answer for several architectures, and picking
                # the first would ship one build labelled as the others.
                continue

            # A `pica` artifact has no per-architecture layout to put a build
            # in: `root/` is deployed as it stands, so `<platform>/<arch>/`
            # would reach the device as a directory at the root of the
            # filesystem. The matrix selects which archive to fetch, not where
            # it lands.
            subdir = str(source.get("dest_subdir", "")).strip("/")
            dest = os.path.join(root_dir, subdir) if subdir else root_dir

            os.makedirs(dest, exist_ok=True)
            archive = os.path.join(dest, os.path.basename(url.split("?")[0]))

            print(f"Fetching {url}...")
            if not download_file(url, archive):
                print(f"warning: could not fetch {url}", file=sys.stderr)
                ok = False
                continue

            if not unpack_into(dest, strip, includes):
                ok = False
                continue
            matched = True
            print(f"Unpacked into {os.path.relpath(dest, pkg_dir)}")
            break

        if not matched and ok:
            print(f"error: no archive matched {platform}/{arch}", file=sys.stderr)
            ok = False

    return ok


def unpack_into(dest, strip, includes):
    """
    Unpack a downloaded archive in place, leaving the files under `dest`.

    The archive is removed afterwards: it is an input to the build, and under
    `root/` it would otherwise be deployed to the device as a stray file.
    """
    archives = [n for n in os.listdir(dest) if n.endswith((".tar.gz", ".tgz", ".tar.xz"))]
    if not archives:
        print(f"error: no archive to unpack in {dest}", file=sys.stderr)
        return False

    for name in archives:
        path = os.path.join(dest, name)
        try:
            with tarfile.open(path, "r:*") as tar:
                members = []
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    parts = member.name.split("/")
                    if len(parts) <= strip:
                        continue
                    member.name = "/".join(parts[strip:])
                    if includes and not any(
                            member.name == pattern or member.name.startswith(pattern.rstrip("*"))
                            for pattern in includes):
                        continue
                    members.append(member)
                if not members:
                    print(f"error: {name} contains none of {includes}", file=sys.stderr)
                    return False
                tar.extractall(dest, members=members, filter="data")
        except (tarfile.TarError, OSError) as err:
            print(f"error: unpacking {name} failed: {err}", file=sys.stderr)
            return False

        # The archive is this package's build input, not part of it: left under
        # `root/` it would be deployed to the device as a stray megabyte.
        try:
            os.remove(path)
        except OSError as err:
            print(f"warning: could not remove {path}: {err}", file=sys.stderr)
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: fetch_assets.py <package_dir> [build_json_path]")
        sys.exit(1)
    
    pkg_dir = sys.argv[1]
    build_json_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(pkg_dir, "build.json")
    
    if not os.path.exists(build_json_path):
        print(f"Skipping: {build_json_path} not found.")
        return

    with open(build_json_path, 'r') as f:
        build_config = json.load(f)

    provider = build_config.get("source", {}).get("provider", "github")
    success = False

    if provider == "github":
        success = fetch_github_assets(pkg_dir, build_config)
    elif provider == "http_template":
        success = fetch_http_template_assets(pkg_dir, build_config)
    elif provider == "tarball":
        success = extract_tarball(pkg_dir, build_config)
    else:
        print(f"Unknown provider: {provider}")
        sys.exit(1)

    if not success:
        print("Fetch failed or no assets found.")
        sys.exit(1)

    # A container installs under the name in its control metadata, which is not
    # the name it was matched by. Verify the manifest declares that name, so a
    # rename upstream fails the build instead of shipping a broken package.
    problems, skipped = check_downloaded(pkg_dir, build_config)
    for path in skipped:
        print(f"warning: could not read the package name of {path}; not verified")
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print(f"\n{len(problems)} dependency mismatch(es)", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
