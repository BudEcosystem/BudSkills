#!/usr/bin/env python3
"""Quality gate for the Bud Foundry skill set.

Checks that every skill is structurally valid, that its trigger description is
usable, that referenced files exist, and - most importantly - that no internal
component names leak into user-facing text.

    python3 tools/lint_skills.py [--fix-list] [skills-dir]

Exit status is non-zero when any error-level problem is found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Names of internal components and third-party building blocks that must never
# appear in user-facing skill text. Bud presents capabilities, not services.
FORBIDDEN = [
    "budapp", "budcluster", "budsim", "budmodel", "budmetrics", "budprompt",
    "budeval", "budgateway", "budnotify", "budpipeline", "budevent", "buddoc",
    "budcodeinterpreter", "ask-bud", "budadmin", "budplayground", "budcustomer",
    "dapr", "keycloak", "clickhouse", "novu", "tensorzero", "valkey",
    "sqlalchemy", "alembic", "fastapi", "pydantic", "uvicorn", "helm", "argocd",
]
# Contexts where a hit is legitimate rather than a leak.
ALLOWED_CONTEXT = re.compile(
    r"(budapp/|services/bud|/bud[a-z]+/|budkit|bud-[a-z]+|BUD_[A-Z_]+)", re.IGNORECASE
)

REQUIRED_FRONTMATTER = ("name", "description")
MAX_SKILL_LINES = 400
MIN_DESC_CHARS = 80
MAX_DESC_CHARS = 1024


def parse_frontmatter(text: str) -> tuple[dict, int]:
    if not text.startswith("---"):
        return {}, 0
    end = text.find("\n---", 3)
    if end == -1:
        return {}, 0
    block = text[3:end]
    data: dict[str, str] = {}
    key = None
    for line in block.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([a-zA-Z_-]+):\s*(.*)$", line)
        if m:
            key = m.group(1)
            data[key] = m.group(2).strip()
        elif key:
            data[key] += " " + line.strip()
    return data, text[: end + 4].count("\n") + 1


def check_leaks(path: Path, text: str) -> list[str]:
    """Flag internal component names used in prose.

    Three things are deliberately NOT leaks and are excluded:

    * fenced code blocks and inline `code` spans - a literal wire value such as
      an ``x-tensorzero-*`` header name or a ``source:budapp`` filter value is
      part of the public contract and cannot be renamed away;
    * long comma-separated enumerations - these are catalogues of third-party
      products the platform *connects to* (argocd, grafana, jira ...), which
      users must be able to see by name;
    * the skill's own frontmatter name.
    """
    problems = []
    scrubbed = re.sub(r"```.*?```", "", text, flags=re.S)
    scrubbed = re.sub(r"`[^`\n]*`", "", scrubbed)
    for line_no, line in enumerate(scrubbed.splitlines(), 1):
        # A catalogue of connectable third-party products, not a leak.
        if line.count(",") >= 5:
            continue
        lowered = line.lower()
        for term in FORBIDDEN:
            for m in re.finditer(rf"\b{re.escape(term)}\b", lowered):
                window = line[max(0, m.start() - 24) : m.end() + 24]
                if ALLOWED_CONTEXT.search(window):
                    continue
                problems.append(f"{path.name}:{line_no}: internal name '{term}' in: {line.strip()[:90]}")
    return problems


def lint_skill(skill_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return [f"{skill_dir.name}: no SKILL.md"], []

    text = md.read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(text)

    for field in REQUIRED_FRONTMATTER:
        if not fm.get(field):
            errors.append(f"{skill_dir.name}: frontmatter missing '{field}'")

    if fm.get("name") and fm["name"] != skill_dir.name:
        errors.append(f"{skill_dir.name}: frontmatter name '{fm['name']}' != directory name")

    desc = fm.get("description", "")
    if desc:
        if len(desc) < MIN_DESC_CHARS:
            warnings.append(f"{skill_dir.name}: description is short ({len(desc)} chars) - weak triggering")
        if len(desc) > MAX_DESC_CHARS:
            errors.append(f"{skill_dir.name}: description too long ({len(desc)} chars)")
        if "use when" not in desc.lower() and "use this" not in desc.lower():
            warnings.append(f"{skill_dir.name}: description has no explicit 'Use when...' trigger clause")

    lines = text.count("\n")
    if lines > MAX_SKILL_LINES:
        warnings.append(f"{skill_dir.name}: SKILL.md is {lines} lines (>{MAX_SKILL_LINES}); move depth into references/")

    # Every referenced file must exist.
    for ref in re.findall(r"`(references/[A-Za-z0-9_.-]+\.md)`", text):
        if not (skill_dir / ref).exists():
            errors.append(f"{skill_dir.name}: SKILL.md references missing file {ref}")
    for ref in re.findall(r"`(scripts/[A-Za-z0-9_.-]+)`", text):
        if not (skill_dir / ref).exists():
            errors.append(f"{skill_dir.name}: SKILL.md references missing file {ref}")

    for doc in [md, *sorted(skill_dir.glob("references/*.md"))]:
        errors.extend(check_leaks(doc, doc.read_text(encoding="utf-8")))

    for script in sorted(skill_dir.glob("scripts/*")):
        if script.is_file() and script.suffix in ("", ".sh", ".py"):
            if not script.stat().st_mode & 0o111:
                warnings.append(f"{skill_dir.name}: {script.name} is not executable")

    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("skills_dir", nargs="?", default=None)
    args = ap.parse_args()

    root = Path(args.skills_dir) if args.skills_dir else Path(__file__).resolve().parent.parent / "skills"
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 2

    all_errors: list[str] = []
    all_warnings: list[str] = []
    # A directory with content but no SKILL.md is a BROKEN skill, not a
    # non-skill. Skipping it silently is how a deleted SKILL.md goes unnoticed.
    skills = sorted(
        p for p in root.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and (
            (p / "SKILL.md").exists()
            or any((p / sub).is_dir() for sub in ("references", "scripts"))
        )
    )

    for skill in skills:
        errors, warnings = lint_skill(skill)
        all_errors += errors
        all_warnings += warnings
        refs = len(list(skill.glob("references/*.md")))
        scripts = len([p for p in skill.glob("scripts/*") if p.is_file()])
        md = skill / "SKILL.md"
        n = md.read_text(encoding="utf-8").count("\n") if md.exists() else 0
        flag = "FAIL" if errors else ("warn" if warnings else "ok")
        print(f"  {skill.name:22} {n:4} lines  {refs} refs  {scripts} scripts   [{flag}]")

    if all_warnings:
        print(f"\n{len(all_warnings)} warning(s):")
        for w in all_warnings:
            print(f"  ! {w}")
    if all_errors:
        print(f"\n{len(all_errors)} error(s):")
        for e in all_errors:
            print(f"  x {e}")

    print(f"\n{len(skills)} skills checked, {len(all_errors)} errors, {len(all_warnings)} warnings")
    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main())
