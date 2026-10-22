#!/usr/bin/env python3
"""Print a short summary of a plan file, for a workflow step to show.

The plan is the record of what the preflight decided. Printing it in the job
log is what makes a run diagnosable without downloading the artifact, and it
separates the two things a reader needs: what blocked, and what was merely
noted because it could not be verified.
"""
import json
import sys


def main(argv):
    if len(argv) != 2:
        print("usage: summarize_plan.py PLAN.json", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        plan = json.load(handle)

    summary = plan.get("summary", {})
    print("Preflight summary:")
    for key in ("checked", "packages_planned", "requests",
                "validation_failures", "packages_blocked", "offline", "probe"):
        if key in summary:
            print(f"- {key}: {summary[key]}")
    if summary.get("by_status"):
        print(f"- failures by status: {summary['by_status']}")

    errors = plan.get("errors", [])
    blocking = [e for e in errors if e.get("blocks", True)]
    noted = [e for e in errors if not e.get("blocks", True)]

    if blocking:
        print(f"- blocking failures ({len(blocking)}):")
        for error in blocking[:20]:
            print(f"    {error.get('pkg')}/{error.get('phase')}: {error.get('reason')}")
        if len(blocking) > 20:
            print(f"    ... and {len(blocking) - 20} more")

    if noted:
        print(f"- non-blocking notes ({len(noted)}), none of them a missing resource:")
        for error in noted[:10]:
            print(f"    {error.get('pkg')}/{error.get('phase')}: {error.get('reason')}")
        if len(noted) > 10:
            print(f"    ... and {len(noted) - 10} more")

    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
