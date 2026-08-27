"""Deciding whether an answer is right.

Two mechanisms, and the cheap one carries most of the load:

* **Deterministic checks** - substring, regex and exact matching. Free, exact,
  perfectly reproducible, and *authoritative*: where they exist, a model's
  opinion does not override them.
* **Rubric grading** - a judge decides whether a written requirement was met.
  Used only for things a substring cannot express: refusing, asking a
  clarifying question, covering two points properly.

The judge is deliberately kept ignorant. It never sees the prompt under test or
which candidate produced the answer, so it cannot favour one.
"""

from __future__ import annotations

import json
import re

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_UNKNOWN = "UNKNOWN"


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower().rstrip(".")


_MD = [
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),      # headings
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),        # bold
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", re.S), r"\1"),  # italics
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),            # bullets
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),          # numbered lists
    (re.compile(r"`{1,3}"), ""),                        # code ticks
    (re.compile(r"\n{3,}"), "\n\n"),
]


def flatten_formatting(text: str) -> str:
    """Strip markdown scaffolding before judging.

    Judges reward formatting far more than they reward length - the measured
    effect is an order of magnitude larger than the famous verbosity bias. A
    bulleted answer and a prose answer with identical content should score the
    same, so remove the difference before the judge sees it.
    """
    out = text or ""
    for pattern, repl in _MD:
        out = pattern.sub(repl, out)
    return out.strip()


def deterministic(case: dict, answer: str) -> tuple[str | None, list[str]]:
    """Run the free checks. Returns (verdict or None, reasons)."""
    reasons: list[str] = []
    verdict: str | None = None
    got = normalise(answer)

    if case.get("expect_exact"):
        verdict = VERDICT_PASS if got == normalise(str(case["expect_exact"])) else VERDICT_FAIL
        reasons.append("exact match" if verdict == VERDICT_PASS else "not an exact match")

    if case.get("expect_regex"):
        try:
            hit = re.search(str(case["expect_regex"]), answer or "", re.I | re.S) is not None
        except re.error as exc:
            return VERDICT_FAIL, [f"invalid expect_regex: {exc}"]
        reasons.append("regex matched" if hit else "regex did not match")
        verdict = VERDICT_PASS if hit and verdict != VERDICT_FAIL else VERDICT_FAIL

    missing = [s for s in (case.get("expect_contains") or []) if normalise(str(s)) not in got]
    if case.get("expect_contains"):
        if missing:
            reasons.append("missing: " + ", ".join(repr(m) for m in missing[:3]))
            verdict = VERDICT_FAIL
        else:
            reasons.append("all required substrings present")
            verdict = VERDICT_PASS if verdict != VERDICT_FAIL else VERDICT_FAIL

    present = [s for s in (case.get("expect_absent") or []) if normalise(str(s)) in got]
    if case.get("expect_absent"):
        if present:
            reasons.append("must not contain: " + ", ".join(repr(p) for p in present[:3]))
            verdict = VERDICT_FAIL
        else:
            reasons.append("no forbidden substrings")
            verdict = VERDICT_PASS if verdict != VERDICT_FAIL else VERDICT_FAIL

    return verdict, reasons


def needs_judge(case: dict, det_verdict: str | None) -> bool:
    """A rubric is graded only when the deterministic checks did not already fail."""
    return bool(case.get("rubric")) and det_verdict != VERDICT_FAIL


JUDGE_INSTRUCTIONS = """\
You are grading answers produced by an assistant. Grade only against the stated
requirement for each case. You are NOT being asked whether you like the answer,
whether it is well written, or how you would have answered.

Rules:
- Judge substance, not style, length or formatting.
- Where a reference answer is given, it is the ground truth. An answer that
  contradicts it FAILS even if it sounds better. An answer that adds correct
  extra detail still PASSES.
- Where a requirement says the assistant must decline, refuse or ask a
  clarifying question, doing so is a PASS and answering anyway is a FAIL.
- An empty answer is always FAIL.
- Decide PASS or FAIL. There is no partial credit and no "it depends".
"""

JUDGE_FORMAT = """\
Write one JSON object per line, one line per case, and nothing else - no prose
before or after, no markdown fence, no trailing commas.

{"id": "<case id exactly as given>", "verdict": "PASS", "why": "<12 words or fewer>"}

Include every case id listed below, exactly once. `verdict` must be the literal
string PASS or FAIL.
"""


def judge_brief(items: list[dict], *, flatten: bool = True) -> str:
    """Build one grading brief covering every case that needs a rubric verdict.

    Batching matters: it is one turn for the whole suite rather than one per
    case, which is what makes host-agent grading practical at all.
    """
    blocks = []
    for item in items:
        answer = item.get("answer") or ""
        if flatten:
            answer = flatten_formatting(answer)
        parts = [
            f"### CASE {item['id']}",
            "",
            "REQUIREMENT (what a correct answer must do):",
            str(item.get("rubric") or "").strip(),
            "",
            "USER ASKED:",
            str(item.get("input") or "").strip(),
        ]
        if item.get("reference"):
            parts += ["", "REFERENCE ANSWER (ground truth):", str(item["reference"]).strip()]
        parts += ["", "ASSISTANT ANSWERED:", (answer.strip() or "(empty)")]
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def parse_verdicts(text: str) -> dict[str, dict]:
    """Read judge output into {case_id: {'verdict':..., 'why':...}}."""
    from .turns import parse_jsonl  # noqa: PLC0415 - avoid a circular import at module load

    out: dict[str, dict] = {}
    for row in parse_jsonl(text):
        cid = row.get("id")
        raw = str(row.get("verdict") or "").strip().upper()
        if not cid:
            continue
        verdict = VERDICT_PASS if raw.startswith("PASS") else (
            VERDICT_FAIL if raw.startswith("FAIL") else VERDICT_UNKNOWN
        )
        out[str(cid)] = {"verdict": verdict, "why": str(row.get("why") or "")[:200]}
    return out


def final_verdict(det: str | None, judged: dict | None) -> tuple[str, str]:
    """Collapse a case to one verdict, with the reason.

    Ordering is deliberate: the cheap reproducible signal wins wherever it
    exists, and a case nobody graded is a failure, not a free pass.
    """
    if det == VERDICT_FAIL:
        return VERDICT_FAIL, "deterministic check failed"
    if judged:
        if judged["verdict"] == VERDICT_UNKNOWN:
            return VERDICT_FAIL, "judge returned no usable verdict"
        return judged["verdict"], judged.get("why") or "judged"
    if det == VERDICT_PASS:
        return VERDICT_PASS, "deterministic checks passed"
    return VERDICT_FAIL, "no check produced a verdict"


def json_line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)
