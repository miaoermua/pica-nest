"""Say whether a deployed mirror still serves this repository.

A Cloudflare Worker deployment carries its own `GITHUB_REPO`, so renaming the
repository leaves the deployment reading the name it was deployed with: the
indexes there stop where the rename happened, the artifacts they name answer
404, and every device pointed at the mirror fails — while `r/` in this
repository is perfectly healthy. Nothing inside `r/` can show that. Only the
deployment can, so it is asked from outside: `__self` for which repository it
reads, then the indexes for how current they are.
"""

from . import index, net, util


def self_report(base, get_json=net.get_json):
    """`{"repo", "branch"}` as the deployment itself reports it."""
    return get_json(f"{base.rstrip('/')}/__self")


def drift(base, expected_repo, r_dir=index.R_DIR, get_json=net.get_json):
    """Reasons this mirror would hand a device something other than `r/`.

    A deployment built for another repository is reported once and stops the
    comparison: everything else it would say is a difference between two
    unrelated repositories, which says nothing about this one.
    """
    try:
        reported = self_report(base, get_json=get_json)
    except net.FetchError as err:
        if err.status == "missing":
            return [
                f"{base}: no __self route, so this deployment predates the code in mirror/",
                "  deploy it: npx wrangler deploy --config mirror/wrangler.toml",
            ]
        return [f"{base}: __self unreadable: {err.reason}"]

    if reported.get("repo") != expected_repo:
        return [
            f"{base}: deployed for {reported.get('repo')!r}, this repository is {expected_repo!r}",
            "  the deployment names its repository in wrangler.toml; deploy it: "
            "npx wrangler deploy --config mirror/wrangler.toml",
        ]

    problems = []
    for path in index.manager_paths(index.load_descriptor(r_dir)).values():
        local = util.read_json(util.path(r_dir, path))
        try:
            remote = get_json(f"{base.rstrip('/')}/{path}")
        except net.FetchError as err:
            problems.append(f"{path}: unreadable from the mirror: {err.reason}")
            continue
        if remote.get("generated_at") != local.get("generated_at"):
            problems.append(
                f"{path}: mirror serves {remote.get('generated_at')}, r/ has {local.get('generated_at')}"
            )
        remote_count = len(remote.get("packages") or [])
        local_count = len(local.get("packages") or [])
        if remote_count != local_count:
            problems.append(f"{path}: mirror lists {remote_count} package(s), r/ has {local_count}")
    return problems
