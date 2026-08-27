"""The case file: schema, validation and the train/holdout split.

A case is one input plus what a good answer must do. The file is JSONL so it
diffs cleanly in version control - it is the experiment, and it should live
next to the prompt it grades.
"""

from __future__ import annotations

import hashlib
from typing import Any

CATEGORIES = ("typical", "edge", "out_of_scope", "adversarial")

# Roughly the mix that catches real failures: most agents are only ever tested
# on the first category and are then surprised in production.
CATEGORY_MIX = {"typical": 0.50, "edge": 0.25, "out_of_scope": 0.15, "adversarial": 0.10}

CHECK_FIELDS = ("expect_contains", "expect_absent", "expect_regex", "expect_exact", "rubric")

SCHEMA_DOC = """\
{"id": "leave-01",                    // stable, unique - regression tracking matches on it
 "category": "typical",               // typical | edge | out_of_scope | adversarial
 "input": "How many days of annual leave do I get?",
 "reference": "25 days per holiday year, pro-rated for part-time staff.",
 "expect_contains": ["25 days"],      // substrings that must appear (case-insensitive)
 "expect_absent": ["unlimited"],      // substrings that must NOT appear
 "rubric": "States the entitlement in days and cites the policy section.",
 "notes": "source: People Handbook s4.2"}

Required: id, input, and at least one of expect_contains / expect_absent /
expect_regex / expect_exact / rubric - a case nothing can check is not a case.
`reference` is optional but strongly recommended: a judge given a reference
answer is far more reliable than one asked to decide unaided.
"""


def _bucket(case_id: str) -> float:
    """Stable 0..1 position for a case id, so splits survive regeneration."""
    digest = hashlib.sha256(case_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF


def assign_splits(cases: list[dict], holdout_fraction: float = 0.4) -> list[dict]:
    """Deterministically mark each case `train` or `holdout`, stratified by category.

    Stratifying matters: an unstratified split on 50 cases routinely puts every
    adversarial case on one side, and then the holdout cannot see the failure
    mode you were most worried about.
    """
    by_cat: dict[str, list[dict]] = {}
    for case in cases:
        by_cat.setdefault(case.get("category") or "typical", []).append(case)
    for group in by_cat.values():
        ranked = sorted(group, key=lambda c: _bucket(str(c["id"])))
        cut = round(len(ranked) * holdout_fraction)
        for i, case in enumerate(ranked):
            case["split"] = "holdout" if i < cut else "train"
    return cases


def validate(cases: list[dict], *, strict: bool = True) -> list[str]:
    """Return a list of problems. Empty list means the file is usable."""
    problems: list[str] = []
    if not cases:
        return ["the case file is empty"]

    seen: dict[str, int] = {}
    for i, case in enumerate(cases, 1):
        where = f"case {i}"
        cid = case.get("id")
        if not cid or not str(cid).strip():
            problems.append(f"{where}: missing 'id'")
        else:
            cid = str(cid)
            where = f"case {cid!r}"
            if cid in seen:
                problems.append(f"{where}: duplicate id (also case {seen[cid]})")
            seen[cid] = i
        if not str(case.get("input") or "").strip():
            problems.append(f"{where}: missing 'input'")
        cat = case.get("category")
        if cat and cat not in CATEGORIES:
            problems.append(f"{where}: category {cat!r} is not one of {', '.join(CATEGORIES)}")
        if not any(case.get(f) for f in CHECK_FIELDS):
            problems.append(
                f"{where}: nothing to check - needs at least one of {', '.join(CHECK_FIELDS)}"
            )
        for field in ("expect_contains", "expect_absent"):
            val = case.get(field)
            if val is not None and not isinstance(val, list):
                problems.append(f"{where}: '{field}' must be a list of strings")

    if strict:
        cats = {c.get("category") for c in cases}
        missing = [c for c in CATEGORIES if c not in cats]
        if missing:
            problems.append(
                "no cases in category: " + ", ".join(missing)
                + " - a set that only covers typical questions will not catch "
                  "the failures that matter"
            )
    return problems


def normalise(cases: list[dict]) -> list[dict]:
    """Fill in defaults and coerce loose shapes into the canonical schema."""
    out: list[dict] = []
    for i, raw in enumerate(cases, 1):
        case: dict[str, Any] = dict(raw)
        case["id"] = str(case.get("id") or f"case-{i:03d}")
        case["input"] = str(case.get("input") or case.get("question") or case.get("prompt") or "")
        if "reference" not in case:
            for alias in ("expected", "answer", "gold"):
                if case.get(alias):
                    case["reference"] = str(case[alias])
                    break
        case.setdefault("category", "typical")
        for field in ("expect_contains", "expect_absent"):
            val = case.get(field)
            if isinstance(val, str):
                case[field] = [val]
        out.append(case)
    return out


def summary(cases: list[dict]) -> dict:
    by_cat: dict[str, int] = {}
    by_split: dict[str, int] = {}
    for case in cases:
        by_cat[case.get("category", "typical")] = by_cat.get(case.get("category", "typical"), 0) + 1
        by_split[case.get("split", "train")] = by_split.get(case.get("split", "train"), 0) + 1
    return {"total": len(cases), "by_category": by_cat, "by_split": by_split}
