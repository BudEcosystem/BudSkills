"""Error types and human-readable diagnosis for Bud Foundry API failures.

The goal here is that an agent reading a failure message knows what to do next
without opening a browser or reading source. Every raised error carries the
status, the server's own message, and -- where the cause is a known one -- a
concrete remedy.
"""

from __future__ import annotations

import json
from typing import Any


class BudError(Exception):
    """Base class for every error this toolkit raises."""


class BudAuthError(BudError):
    """Sign-in failed, or the session expired and could not be renewed."""


class BudTimeout(BudError):
    """A long-running operation did not reach a terminal state in time."""


class BudWorkflowFailed(BudError):
    """A long-running operation finished in a failed state."""

    def __init__(self, message: str, workflow_id: str | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.workflow_id = workflow_id
        self.detail = detail


class BudAPIError(BudError):
    """A request returned a non-2xx status."""

    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        self.payload = _try_json(body)
        self.server_message = _extract_message(self.payload, body)
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [f"{self.method} {self.path} -> HTTP {self.status}"]
        if self.server_message:
            lines.append(f"  {self.server_message}")
        remedy = diagnose(self.status, self.server_message, self.path)
        if remedy:
            lines.append(f"  -> {remedy}")
        return "\n".join(lines)


def _try_json(body: str) -> Any:
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _extract_message(payload: Any, body: str) -> str:
    """Pull the most useful human message out of an error body.

    Bud returns errors in a few shapes depending on which layer rejected the
    request, so check each in turn.
    """
    if isinstance(payload, dict):
        for key in ("message", "detail", "error", "reason"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
            # FastAPI validation errors: detail is a list of field problems.
            if isinstance(val, list) and val:
                parts = []
                for item in val:
                    if isinstance(item, dict):
                        loc = ".".join(str(x) for x in item.get("loc", [])[1:]) or "body"
                        parts.append(f"{loc}: {item.get('msg', '')}")
                    else:
                        parts.append(str(item))
                return "; ".join(parts)
    return (body or "").strip()[:400]


def diagnose(status: int, message: str, path: str) -> str:
    """Map a failure onto an actionable next step."""
    msg = (message or "").lower()

    if status == 401:
        return "Session expired or absent. Run: bud login"
    if status == 403:
        if "csrf" in msg:
            return (
                "CSRF check failed -- mutating calls must echo the CSRF cookie. "
                "Use the bud client rather than raw curl."
            )
        return "Signed in but not permitted. Check your role and project membership: bud api GET /users/me/permissions"
    if status == 404:
        if path.rstrip("/").endswith(("/clusters", "/projects", "/models")):
            return (
                "Path not found -- note several Bud collections are nested "
                "(for example clusters live under /clusters/clusters). "
                "List real paths with: bud paths <keyword>"
            )
        return "No such resource, or the id belongs to another project."
    if status == 409:
        return "Conflict -- the name already exists, or the resource is in use by something else."
    if status == 422:
        return "Request body failed validation. The field-level reasons are above."
    if status == 429:
        return "Rate limited. The client backs off automatically; reduce concurrency if this persists."
    if status in (502, 503, 504):
        return (
            "A backing component is unavailable or still starting. "
            "Retry after a short pause; if it persists, check platform health: bud health"
        )
    if status >= 500:
        return "Server-side failure. Capture this response when reporting the issue."
    return ""
