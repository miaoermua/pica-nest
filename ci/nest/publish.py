"""After the upload: prove the release holds what the index says, then tidy it.

`verify` downloads every artifact this run uploaded through its `download_url`
and compares size and SHA-256 with its entry — the only moment the published
bytes can be checked against the index before the index is committed.

`stale` lists release assets that neither the new index nor the previous one
names. One generation is kept so a device that synced just before this run can
still download what its index promised.
"""

import os
import tempfile

from . import index, net, util


def verify(r_dir, release_list, cache_dir=None):
    entries = index.entry_by_filename(r_dir)
    problems = []
    with open(release_list, "r", encoding="utf-8") as handle:
        names = [os.path.basename(line.strip()) for line in handle if line.strip()]
    with tempfile.TemporaryDirectory(prefix="nest-verify-") as work:
        for name in names:
            entry = entries.get(name)
            if entry is None:
                problems.append(f"{name}: uploaded, but no index entry names it")
                continue
            try:
                net.download(entry["download_url"], os.path.join(work, name), entry.get("csize"), entry["sha256"])
                util.log(f"  verified {name}")
            except net.FetchError as err:
                problems.append(f"{name}: {err.reason}")
    return problems


def stale(asset_names, r_dir, previous_r_dir=None):
    keep = index.referenced_filenames(r_dir)
    if previous_r_dir and os.path.isdir(previous_r_dir):
        keep |= index.referenced_filenames(previous_r_dir)
    return sorted(name for name in asset_names if name.endswith(util.PKG_EXTENSION) and name not in keep)
