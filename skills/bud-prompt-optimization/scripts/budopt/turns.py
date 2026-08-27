"""Two ways to get an LLM to do generative work, neither needing a third-party key.

``agent``  (default) - the coding agent driving this toolkit *is* the model.
           The script writes a request file, stops, and asks to be re-run. The
           agent reads the request, writes the response file, re-runs the same
           command, and the script picks up exactly where it left off. No
           network call happens at all.

``model``  (opt-in)  - a model already deployed in the user's own installation,
           reached through the inference gateway. This spends the user's own
           compute, so it must be asked for explicitly.

The request text is identical either way, so the two paths produce
interchangeable artefacts.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path

from . import common

NEEDS_AGENT_TURN = 10  # distinguished exit code: "your turn, then re-run me"


class AgentTurnRequired(Exception):
    """Raised when the host agent must write a response file before we continue."""

    def __init__(self, request_path: Path, response_path: Path, summary: str) -> None:
        self.request_path = request_path
        self.response_path = response_path
        self.summary = summary
        super().__init__(summary)

    def report(self, argv: list[str] | None = None) -> int:
        cmd = " ".join(argv or sys.argv)
        common.note(
            "\n"
            "=================== YOUR TURN (no API call is made) ===================\n"
            f"{self.summary}\n\n"
            f"  1. Read   {self.request_path}\n"
            f"  2. Write  {self.response_path}\n"
            f"  3. Re-run {cmd}\n\n"
            "The request file contains the full brief and the exact output format.\n"
            "Nothing is sent anywhere; you are the generator.\n"
            "======================================================================="
        )
        return NEEDS_AGENT_TURN


class Reflector:
    """Produces text from a brief, via the host agent or a deployed model."""

    def __init__(self, kind: str, workdir: Path, *, model: str | None = None,
                 max_tokens: int = 4000, temperature: float | None = None) -> None:
        if kind not in ("agent", "model"):
            raise SystemExit(f"unknown generator kind {kind!r} (use 'agent' or 'model')")
        if kind == "model" and not model:
            raise SystemExit(
                "--via model needs --model <deployment-name>.\n"
                "List candidates with:  bud api GET /playground/deployments -q limit=100"
            )
        self.kind = kind
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.turns = Path(workdir) / "turns"
        self.turns.mkdir(parents=True, exist_ok=True)

    # -- identity, recorded in every artefact so comparisons can be policed ---
    @property
    def identity(self) -> str:
        return "host-agent" if self.kind == "agent" else f"model:{self.model}"

    def ask(self, slot: str, brief: str, *, summary: str, ext: str = "txt") -> str:
        """Return generated text for ``brief``.

        With ``kind='agent'`` this either returns a response the agent already
        wrote for this exact brief, or raises AgentTurnRequired.
        """
        if self.kind == "model":
            text, _usage = common.chat(
                self.model,  # type: ignore[arg-type]
                [{"role": "user", "content": brief}],
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
            (self.turns / f"{slot}.response.{ext}").write_text(text, encoding="utf-8")
            return text

        req = self.turns / f"{slot}.request.md"
        resp = self.turns / f"{slot}.response.{ext}"
        state = self.turns / f"{slot}.state.json"
        want = common.sha(brief)

        if state.is_file() and resp.is_file():
            try:
                have = json.loads(state.read_text(encoding="utf-8")).get("request_sha")
            except (OSError, ValueError):
                have = None
            body = resp.read_text(encoding="utf-8").strip()
            if have == want and body:
                return body
            if have != want and body:
                common.note(
                    f"note: {resp.name} answers an older version of this request; discarding it."
                )

        req.write_text(brief, encoding="utf-8")
        common.write_json(state, {"request_sha": want, "response_file": str(resp)})
        raise AgentTurnRequired(req, resp, summary)


# --------------------------------------------------------------------------
# parsing generated output
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.DOTALL)


def strip_fences(text: str) -> str:
    """Return fenced-block contents if the text is mostly a fenced block."""
    blocks = _FENCE.findall(text)
    if blocks:
        return "\n".join(b.strip() for b in blocks)
    return text


def parse_jsonl(text: str) -> list[dict]:
    """Pull JSON objects out of generated text, tolerating prose and fences.

    Accepts one object per line, a JSON array, or a mixture buried in commentary.
    """
    body = strip_fences(text).strip()
    if body.startswith("["):
        try:
            arr = json.loads(body)
            if isinstance(arr, list):
                return [r for r in arr if isinstance(r, dict)]
        except json.JSONDecodeError:
            pass
    rows: list[dict] = []
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def format_brief(title: str, sections: list[tuple[str, str]]) -> str:
    """Assemble a request file: a title, then '## heading' + body sections."""
    parts = [f"# {title}", ""]
    for heading, body in sections:
        parts.append(f"## {heading}")
        parts.append("")
        parts.append(textwrap.dedent(body).strip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
