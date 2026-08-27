#!/usr/bin/env python3
"""Run the read-only commands documented in the skills against a real installation.

Documentation drifts. This harness extracts every `bud api GET ...` command
from the skill files, resolves placeholders from live data, executes them, and
reports which documented commands do not actually work.

Only safe verbs run: GET, plus a small allowlist of read-only POSTs. Anything
that mutates is listed as skipped, never executed.

    export BUD_API_URL=... BUD_EMAIL=... BUD_PASSWORD=...
    python3 tools/verify_examples.py            # verify everything
    python3 tools/verify_examples.py bud-models # one skill
    python3 tools/verify_examples.py --list     # show what would run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# POSTs that only read. Everything else that mutates is skipped.
READONLY_POSTS = {"/models/top-leaderboards"}

BUD = "bud"
CODE_BLOCK = re.compile(r"```bash\n(.*?)```", re.S)
# A `bud api` invocation, possibly spanning continuation lines.
BUD_API = re.compile(
    r"\bbud api\s+(GET|POST|PUT|PATCH|DELETE)\s+(\S+)"
    r"((?:\s+-q\s+\S+)*)"
    r"(?:\s+-d\s+'([^']*)')?"
)

# Endpoints where a 404 is a documented, normal state rather than a defect.
EXPECTED_404 = (
    "/pricing",
    "/security-scan/export",
    "/models/catalog/",          # 404 unless the deployment is published
    "/settings",                 # cluster settings are absent until first set
)

# Names used in worked examples. They read far better in documentation than an
# angle-bracket placeholder, but they do not exist on any given installation,
# so a 404 on one is the example doing its job -- not a broken document.
EXAMPLE_NAMES = (
    "hr-assistant",
    "vendor-research-agent",
    "support-7b",
    "hr-01",
    "my-llama-prod",
    "gpt4o-prod",
)

# Endpoints that are broken on the platform itself, not in the documentation.
# Each is recorded in bugs.md; they are reported separately so a genuine
# documentation regression is not lost in the noise.
KNOWN_PLATFORM_FAULTS = {
    "/metrics/gateway/blocking-rules-stats": "BUG-032 - returns 500",
    "/approvals": "BUG-024 - 502, operator token not configured",
    "/runs": "BUG-024 - 502, operator token not configured",
    "/workers": "BUG-016 - 400 when a dependency is running without its sidecar",
    "/clusters/recommended/": "needs a deployment job id; the harness supplies any recent job",
}


def known_fault(path: str) -> str | None:
    for frag, why in KNOWN_PLATFORM_FAULTS.items():
        if frag in path:
            return why
    return None


class Resolver:
    """Fills documentation placeholders with real ids from the installation."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def _run(self, args: list[str]) -> object | None:
        try:
            out = subprocess.run(
                [BUD, *args], capture_output=True, text=True, timeout=90
            )
            if out.returncode != 0:
                return None
            return json.loads(out.stdout)
        except (subprocess.SubprocessError, ValueError):
            return None

    def prime(self) -> None:
        projects = self._run(["api", "GET", "/projects/", "-q", "page=1", "-q", "limit=1"])
        if isinstance(projects, dict) and projects.get("projects"):
            self.values["project_id"] = projects["projects"][0]["project"]["id"]
        models = self._run(
            ["api", "GET", "/models/", "-q", "table_source=model", "-q", "page=1", "-q", "limit=1"]
        )
        if isinstance(models, dict) and models.get("models"):
            self.values["model_id"] = models["models"][0]["model"]["id"]
        endpoints = self._run(["api", "GET", "/endpoints/", "-q", "page=1", "-q", "limit=1"])
        if isinstance(endpoints, dict) and endpoints.get("endpoints"):
            ep = endpoints["endpoints"][0]
            self.values["endpoint_id"] = (ep.get("endpoint") or ep).get("id", "")
        clusters = self._run(["api", "GET", "/clusters/clusters", "-q", "page=1", "-q", "limit=1"])
        if isinstance(clusters, dict):
            items = clusters.get("clusters") or []
            if items:
                inner = items[0].get("cluster") or items[0]
                self.values["cluster_id"] = inner.get("id", "")
        me = self._run(["api", "GET", "/users/me"])
        if isinstance(me, dict):
            self.values["user_id"] = (me.get("user") or {}).get("id", "")
        jobs = self._run(["jobs", "--limit", "1"])
        if isinstance(jobs, list) and jobs:
            self.values["workflow_id"] = jobs[0].get("id", "")

    def resolve(self, text: str) -> str | None:
        """Substitute placeholders; return None when one cannot be filled."""
        # Shell variables: match the WHOLE name, longest first, so that a short
        # name like $P never eats part of $PROMPT_ID.
        shell_vars = {
            "BUD_PROJECT_ID": "project_id",
            "PROJECT_ID": "project_id",
            "PID": "project_id",
            "P": "project_id",
            "ENDPOINT_ID": "endpoint_id",
            "EP": "endpoint_id",
            "MODEL_ID": "model_id",
            "MODEL": "model_id",
            "WORKFLOW_ID": "workflow_id",
            "W": "workflow_id",
            "USER_ID": "user_id",
            "CLUSTER_ID": "cluster_id",
        }

        def sub_shell(match: re.Match) -> str:
            name = match.group(1) or match.group(2)
            key = shell_vars.get(name)
            return self.values.get(key, match.group(0)) if key else match.group(0)

        out = re.sub(r"\$\{([A-Z_]+)\}|\$([A-Z_]+)\b", sub_shell, text)

        # Documentation placeholders like <project-id>.
        def sub_angle(match: re.Match) -> str:
            key = match.group(1).replace("-", "_").lower()
            return self.values.get(key, match.group(0))

        out = re.sub(r"<([A-Za-z_-]+)>", sub_angle, out)
        if re.search(r"<[A-Za-z_-]+>|\$[A-Z_]+|\$\{", out):
            return None
        return out


def extract(path: Path) -> list[tuple[str, str, str, str]]:
    """Return (method, path, query-args, body) for each documented bud api call."""
    found = []
    text = path.read_text(encoding="utf-8")
    for block in CODE_BLOCK.findall(text):
        joined = block.replace("\\\n", " ")
        for m in BUD_API.finditer(joined):
            found.append((m.group(1), m.group(2), m.group(3) or "", m.group(4) or ""))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("skill", nargs="?", help="limit to one skill")
    ap.add_argument("--list", action="store_true", help="show commands without running them")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent / "skills"
    skills = sorted(p for p in root.iterdir() if p.is_dir())
    if args.skill:
        skills = [p for p in skills if p.name == args.skill]
        if not skills:
            print(f"no such skill: {args.skill}", file=sys.stderr)
            return 2

    resolver = Resolver()
    if not args.list:
        print("Priming live ids...", file=sys.stderr)
        resolver.prime()
        missing = [k for k in ("project_id", "model_id") if not resolver.values.get(k)]
        if missing:
            print(f"warning: could not resolve {missing}; some checks will be skipped", file=sys.stderr)

    passed = failed = skipped = known = examples = 0
    failures: list[str] = []

    for skill in skills:
        docs = [skill / "SKILL.md", *sorted(skill.glob("references/*.md"))]
        calls: list[tuple[str, str, str, str, str]] = []
        for doc in docs:
            if doc.exists():
                for method, path, query, body in extract(doc):
                    calls.append((doc.name, method, path, query, body))
        if not calls:
            continue
        print(f"\n{skill.name}")
        for source, method, path, query, body in calls:
            label = f"{method} {path}{query}"
            if method != "GET" and path not in READONLY_POSTS:
                skipped += 1
                if args.list:
                    print(f"  skip (mutating)  {label}")
                continue
            if args.list:
                print(f"  would run        {label}")
                continue
            path = path.rstrip("`.,;)")
            resolved = resolver.resolve(f"{path}{query}")
            if resolved is None:
                skipped += 1
                print(f"  ? unresolved     {label}   [{source}]")
                continue
            parts = resolved.split()
            cmd = [BUD, "api", method, parts[0]]
            for token in parts[1:]:
                if token != "-q":
                    cmd += ["-q", token]
            if body:
                cmd += ["-d", body]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            combined = (proc.stderr or "") + (proc.stdout or "")
            expected_404 = any(sfx in path for sfx in EXPECTED_404) and "404" in combined
            is_example = any(name in path for name in EXAMPLE_NAMES) and "404" in combined
            if is_example:
                examples += 1
                print(f"  ~ example        {label}   (name not present on this installation)")
                continue
            if proc.returncode == 0 or expected_404:
                passed += 1
                note = "  (404 is documented as normal here)" if expected_404 else ""
                print(f"  ok               {label}{note}")
            else:
                detail = (proc.stderr or proc.stdout).strip().splitlines()
                msg = detail[0] if detail else "no output"
                why = known_fault(path)
                if why:
                    known += 1
                    print(f"  known fault      {label}\n                   -> {why}")
                else:
                    failed += 1
                    print(f"  FAIL             {label}\n                   -> {msg}")
                    failures.append(f"{skill.name} [{source}] {label}: {msg}")

    print(
        f"\n{passed} passed, {failed} failed, {known} known platform faults, "
        f"{examples} example names, {skipped} skipped"
    )
    if failed:
        print(
            "\nNot every failure is a documentation defect. Before fixing a skill, check:\n"
            "  * 404 on a name like 'hr-assistant' - a worked-example name that does not\n"
            "    exist here. Harmless.\n"
            "  * 502 naming an unavailable component - the installation, not the doc.\n"
            "  * 404 on a resource that was never configured (cluster settings, pricing) -\n"
            "    often a documented-normal state.\n"
            "Real defects look like: a wrong path, a rejected parameter (422), or a 404 on\n"
            "an id this tool resolved from live data."
        )
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  x {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
