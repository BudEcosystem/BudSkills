"""Shared plumbing for the prompt-optimisation toolkit.

Sign-in, the gateway origin and the gateway token all come from the shared
toolkit in ``bud-platform/scripts`` - nothing here re-implements authentication.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# find the shared toolkit (bud-platform/scripts) and put it on sys.path
# --------------------------------------------------------------------------


def _toolkit_dir() -> Path | None:
    """Locate the directory containing the shared ``budkit`` package."""
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    env = os.environ.get("BUD_TOOLKIT_DIR")
    if env:
        candidates.append(Path(env))
    # .../<skills-root>/bud-prompt-optimization/scripts/budopt/common.py
    for up in (3, 4):
        if len(here.parents) > up:
            candidates.append(here.parents[up] / "bud-platform" / "scripts")
    for cand in candidates:
        if (cand / "budkit" / "__init__.py").is_file():
            return cand
    return None


_TOOLKIT = _toolkit_dir()
if _TOOLKIT and str(_TOOLKIT) not in sys.path:
    sys.path.insert(0, str(_TOOLKIT))


def toolkit_path() -> Path | None:
    return _TOOLKIT


def budkit() -> Any:
    if _TOOLKIT is None:
        raise SystemExit(
            "This toolkit needs the shared Bud client from the bud-platform skill.\n"
            "Run the skill set's ./install.sh, or set BUD_TOOLKIT_DIR to the directory\n"
            "that contains budkit/."
        )
    import budkit  # noqa: PLC0415

    return budkit


# --------------------------------------------------------------------------
# inference gateway
# --------------------------------------------------------------------------


def gateway_base() -> str:
    """Origin of the inference gateway - a different host from the Bud API."""
    for var in ("BUD_GATEWAY_URL", "BUD_INFERENCE_URL"):
        val = os.environ.get(var)
        if val:
            return val.rstrip("/")
    api = os.environ.get("BUD_API_URL", "")
    if not api and _TOOLKIT:
        try:
            api = budkit().Config().api_url or ""
        except Exception:  # noqa: BLE001 - config lookup is best effort
            api = ""
    if api:
        scheme, _, host = api.rstrip("/").partition("://")
        if host.startswith("app."):
            return f"{scheme}://gateway.{host[len('app.') :]}"
    raise SystemExit(
        "Cannot work out the inference gateway origin.\n"
        "Set BUD_GATEWAY_URL (usually https://gateway.<your-domain>), or set\n"
        "BUD_API_URL so it can be derived."
    )


def gateway_token() -> str:
    """A bearer the gateway accepts.

    Minting is two calls and the second is the one people miss: the token must
    also be *registered* with the gateway. An unregistered token is rejected as
    an invalid key even though it is perfectly valid.
    """
    tok = os.environ.get("BUD_GATEWAY_TOKEN")
    if tok:
        return tok
    bk = budkit()
    client = bk.BudClient(bk.Config())
    payload = client.get("/auth/redirect/ws-token")
    token = payload.get("ws_token") if isinstance(payload, dict) else None
    if not token:
        raise SystemExit("Could not mint a gateway token. Try: bud login --force")
    client.post("/playground/initialize-with-token", {"access_token": token})
    os.environ["BUD_GATEWAY_TOKEN"] = token
    return token


RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class GatewayError(RuntimeError):
    """A gateway call that did not produce an answer."""


def gateway_post(path: str, body: dict, *, timeout: int = 180, retries: int = 4) -> dict:
    """POST to the gateway with exponential backoff."""
    url = gateway_base() + path
    token = gateway_token()
    payload = json.dumps(body).encode()
    delay, last = 1.0, "no attempt made"
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            last = f"HTTP {exc.code}: {detail}"
            if exc.code == 401:
                raise GatewayError(
                    "401 from the gateway. Mint AND register a token:\n"
                    "  export BUD_GATEWAY_TOKEN=$(bud token)\n"
                    "An unregistered token always reads as an invalid key."
                ) from None
            if exc.code in (400, 404, 422) or exc.code not in RETRY_STATUSES or attempt == retries:
                raise GatewayError(last) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt == retries:
                raise GatewayError(last) from None
        time.sleep(delay)
        delay = min(delay * 2, 20)
    raise GatewayError(last)


# Some models reject an explicit temperature outright ("does not support 0 with
# this model. Only the default (1) value is supported"). Sending none is both
# safer and the only portable default.
_TEMP_REJECTED = "does not support"


def chat(model: str, messages: list[dict], *, max_tokens: int | None = None,
         temperature: float | None = None, timeout: int = 180) -> tuple[str, dict]:
    """One chat completion. Returns (text, usage)."""
    body: dict[str, Any] = {"model": model, "messages": messages}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    try:
        out = gateway_post("/v1/chat/completions", body, timeout=timeout)
    except GatewayError as exc:
        if temperature is not None and _TEMP_REJECTED in str(exc):
            body.pop("temperature", None)
            out = gateway_post("/v1/chat/completions", body, timeout=timeout)
        else:
            raise
    err = out.get("error")
    if isinstance(err, dict) and err.get("message"):
        raise GatewayError(str(err["message"])[:300])
    try:
        return (out["choices"][0]["message"]["content"] or ""), (out.get("usage") or {})
    except (KeyError, IndexError, TypeError):
        raise GatewayError(f"unexpected chat response shape: {json.dumps(out)[:200]}") from None


def responses_text(out: dict) -> str:
    """Pull the assistant text out of a Responses envelope."""
    if isinstance(out.get("output_text"), str) and out["output_text"]:
        return out["output_text"]
    chunks: list[str] = []
    for item in out.get("output") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "\n".join(chunks).strip()


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def read_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith(("//", "#")):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path} line {n} is not valid JSON: {exc}") from None
            if not isinstance(obj, dict):
                raise SystemExit(f"{path} line {n} is not a JSON object")
            rows.append(obj)
    return rows


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    tmp = Path(f"{path}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def write_json(path: str | Path, obj: Any) -> None:
    tmp = Path(f"{path}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def read_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def note(msg: str) -> None:
    print(msg, file=sys.stderr)
