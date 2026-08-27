#!/usr/bin/env python3
"""Find which guardrail profile guards a deployment, and what is inside it.

Bud indexes guardrail attachments by PROFILE, not by endpoint, so answering
"what guardrail is breaking this deployment?" means scanning every profile that
has attachments and reading its deployment rows. That is tedious by hand and
you usually need it during an outage. This does the scan.

`--probes` works around a second gap: `GET /guardrails/profile/{id}/probes`
returns HTTP 500 on current builds, so a profile's contents are recovered by
testing each catalog probe against the per-probe rules route, which does work.

Usage:
  guardrail-attachments.py <endpoint-id>     attachments for one deployment
  guardrail-attachments.py --all             every attachment, unhealthy first
  guardrail-attachments.py --unhealthy       only attachments that are not running
  guardrail-attachments.py --probes <id>     what a profile actually enforces
                                             (one call per catalog probe - ~120
                                             on the reference install, ~2 minutes)

Requires `bud` on PATH and an active session (`bud login`). Read-only.

Exit status: 0 if every reported attachment is `running`, 1 if any is not,
2 on error. Use `--all` plus the exit code as a quick health gate.

CAVEAT: `running` means the attachment rolled out, NOT that the guardrail can
evaluate. A deployment can answer 500 guardrail_failure on every request while
its attachment here reads `running`. Always follow up with one real request.
"""

import json
import subprocess
import sys

HEALTHY = "running"


def bud_get(path, **params):
    cmd = ["bud", "api", "GET", path]
    for key, value in params.items():
        cmd += ["-q", f"{key}={value}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


def profiles_with_attachments():
    page, out = 1, []
    while True:
        body = bud_get("/guardrails/profiles", page=page, limit=50)
        rows = body.get("profiles") or []
        if not rows:
            break
        out += [p for p in rows if (p.get("deployment_count") or 0) > 0]
        if len(out) >= (body.get("total_record") or 0) or page > 50:
            break
        page += 1
    return out


def attachments(profile):
    body = bud_get(
        f"/guardrails/profile/{profile['id']}/deployments", page=1, limit=100
    )
    for dep in body.get("deployments") or []:
        yield {
            "profile_id": profile["id"],
            "profile_name": profile.get("name"),
            "profile_type": profile.get("profile_type"),
            "deployment_id": dep.get("id"),
            "status": dep.get("status"),
            "endpoint_id": dep.get("endpoint_id"),
            "endpoint_name": dep.get("endpoint_name"),
            "standalone": dep.get("endpoint_id") is None,
        }


def report(rows, header):
    if not rows:
        print(f"{header} none found")
        return 0
    print(header)
    print()
    bad = 0
    for row in sorted(
        rows, key=lambda r: (r["status"] == HEALTHY, r["profile_name"] or "")
    ):
        flag = "  " if row["status"] == HEALTHY else "! "
        bad += row["status"] != HEALTHY
        target = (
            "standalone"
            if row["standalone"]
            else (row["endpoint_name"] or row["endpoint_id"])
        )
        print(
            f"{flag}{row['status']:<10} {row['profile_type']:<11} {row['profile_name']}"
        )
        print(f"    target      {target}")
        print(f"    profile     {row['profile_id']}")
        print(f"    deployment  {row['deployment_id']}")
    if bad:
        print()
        print(
            f"{bad} attachment(s) not '{HEALTHY}'. A deployment whose guardrail cannot"
        )
        print(
            "evaluate returns 500 guardrail_failure on EVERY request. To restore service:"
        )
        print("  bud api DELETE /guardrails/deployment/<deployment-id>")
    else:
        print()
        print(
            "All reported attachments are 'running' - which only means they rolled out."
        )
        print("Confirm with one real request before calling the deployment healthy.")
    return 1 if bad else 0


def profile_contents(profile_id):
    """Recover a profile's probes/rules by scanning the catalog.

    `GET /guardrails/profile/{id}/probes` 500s, but the per-probe rules route
    under a profile works and returns 0 records for probes the profile does not
    carry - so the catalog can be sieved against it.
    """
    profile = bud_get(f"/guardrails/profile/{profile_id}").get("profile") or {}
    print(
        f"{profile.get('name')}  [{profile.get('profile_type')}]  "
        f"status={profile.get('status')}  probes={profile.get('probe_count')}  "
        f"threshold={profile.get('severity_threshold')}  "
        f"guard_types={profile.get('guard_types')}"
    )
    catalog = bud_get(
        "/guardrails/probes", page=1, limit=200, order_by="name", include_governance="true"
    ).get("probes") or []
    print(f"scanning {len(catalog)} catalog probes ...", file=sys.stderr)

    found = 0
    for probe in catalog:
        try:
            body = bud_get(
                f"/guardrails/profile/{profile_id}/probe/{probe['id']}/rules",
                page=1,
                limit=100,
            )
        except RuntimeError:
            continue
        rules = body.get("rules") or []
        if not rules:
            continue
        found += 1
        print()
        print(f"  probe  {probe.get('name')}  ({probe['id']})")
        for rule in rules:
            model = rule.get("model_uri")
            suffix = f"  runs_on={model}" if model else ""
            print(f"    {rule.get('status'):<9} {rule.get('name')}{suffix}")
    if not found:
        print()
        print("No probes matched. The profile may be empty, or the catalog scan")
        print("missed it - re-run, or read the profile in the console.")
    else:
        print()
        print(
            "Rules with a runs_on model need that model's deployment to be running,"
        )
        print("or the whole profile fails closed.")
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    arg = argv[0]

    if arg == "--probes":
        if len(argv) < 2:
            print("error: --probes needs a profile id", file=sys.stderr)
            return 2
        try:
            return profile_contents(argv[1])
        except Exception as exc:  # noqa: BLE001 - surface the platform's own message
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        rows = [a for p in profiles_with_attachments() for a in attachments(p)]
    except Exception as exc:  # noqa: BLE001 - surface the platform's own message
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if arg == "--all":
        return report(rows, "All guardrail attachments:")
    if arg == "--unhealthy":
        return report(
            [r for r in rows if r["status"] != HEALTHY],
            "Unhealthy guardrail attachments:",
        )
    if arg.startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    return report(
        [r for r in rows if r["endpoint_id"] == arg],
        f"Guardrail attachments for endpoint {arg}:",
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
