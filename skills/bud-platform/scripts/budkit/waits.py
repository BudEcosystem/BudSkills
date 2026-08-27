"""Waiting on Bud Foundry's long-running work.

Bud models long jobs as a **workflow**: a stepped session you drive forward,
while the platform reports background progress into it. Two facts shape
everything here:

  * A workflow's top-level ``status`` tracks the *session*, not the job. It
    stays ``in_progress`` while a session is open and only becomes
    ``completed`` when the final step is submitted. Waiting on it alone will
    hang forever on an abandoned session -- so we watch the per-step progress
    that the platform writes as background work advances.
  * Whether the *thing you asked for* is usable is answered by the resource
    itself (a deployment reaching ``running``, a model reaching ``active``),
    not by the workflow. Real readiness checks poll the resource.

So this module offers both, and callers should generally use ``wait_workflow``
to follow a job and ``wait_resource`` to confirm the result is usable.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any, Callable

from .client import BudClient, extract_list
from .errors import BudAPIError, BudTimeout, BudWorkflowFailed


# Terminal states seen on step payloads and on resources across the platform.
_STEP_OK = {"completed", "success", "succeeded", "done", "ready", "active", "running"}
_STEP_BAD = {"failed", "error", "errored", "cancelled", "canceled", "terminated", "deleted"}

# Internal component names appear in raw progress payloads. Bud presents these
# to users as capabilities, not services, so translate before displaying.
_COMPONENT_LABELS = {
    "budsim": "capacity planner",
    "budcluster": "cluster manager",
    "budmodel": "model registry",
    "budmetrics": "observability",
    "budprompt": "agent runtime",
    "budeval": "evaluation engine",
    "budgateway": "inference gateway",
    "budapp": "platform",
    "budnotify": "notifications",
    "budpipeline": "pipeline engine",
    "budevent": "event router",
    "dapr": "platform",
    "keycloak": "identity provider",
    "clickhouse": "analytics store",
    "novu": "notifications",
}
_COMPONENT_RE = re.compile(r"\b(" + "|".join(sorted(_COMPONENT_LABELS, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def present(text: Any) -> str:
    """Rewrite raw progress text into Bud's user-facing vocabulary."""
    if not isinstance(text, str):
        return "" if text is None else str(text)
    return _COMPONENT_RE.sub(lambda m: _COMPONENT_LABELS[m.group(1).lower()], text)


def _dig(obj: Any, path: str) -> Any:
    """Read a path out of nested dicts/lists; None when absent.

    Accepts both ``a.0.b`` and ``a[0].b`` for list indexing, because both are
    natural to write and silently returning None for one of them is a trap.
    """
    normalized = re.sub(r"\[(\d+)\]", r".\1", path)
    cur = obj
    for part in normalized.split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def fetch_job(client: BudClient, workflow_id: str, *, kind: str | None = None) -> dict:
    """Assemble one complete job record.

    The API splits a job across two representations and neither is sufficient
    alone:

      * ``GET /workflows/{id}`` -- authoritative session state (``status``,
        ``current_step``, ``total_steps``, ``reason``) plus the data collected
        so far, but **no progress feed**.
      * ``GET /workflows`` -- carries the ``progress.steps`` feed the platform
        writes as background work advances, but has no per-id filter, so the
        row has to be located by scanning recent jobs.

    We merge them, degrading gracefully when the job is too old to appear in
    the recent list.
    """
    record: dict = {}
    try:
        detail = client.get(f"/workflows/{workflow_id}")
        if isinstance(detail, dict):
            record = {
                "id": detail.get("workflow_id") or workflow_id,
                "status": detail.get("status"),
                "current_step": detail.get("current_step"),
                "total_steps": detail.get("total_steps"),
                "reason": detail.get("reason"),
                "data": detail.get("workflow_steps") or {},
            }
    except BudAPIError as exc:
        if exc.status != 404:
            raise
        record = {"id": workflow_id, "missing": True}

    # Overlay the progress feed from the recent-jobs listing.
    params: dict[str, Any] = {"page": 1, "limit": 50}
    if kind:
        params["workflow_type"] = kind
    try:
        listing = client.get("/workflows", params=params)
        for item in extract_list(listing):
            if isinstance(item, dict) and item.get("id") == workflow_id:
                record.setdefault("id", item.get("id"))
                record["kind"] = item.get("workflow_type")
                record["title"] = item.get("title")
                record["progress"] = item.get("progress")
                for key in ("status", "current_step", "total_steps", "reason"):
                    if record.get(key) in (None, ""):
                        record[key] = item.get(key)
                break
    except BudAPIError:
        # The feed is an enhancement; session state alone still lets us wait.
        pass
    return record


def workflow_steps(workflow: dict) -> list[dict]:
    """Normalize a workflow's progress into flat step records."""
    steps = _dig(workflow, "progress.steps") or []
    out = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        content = _dig(step, "payload.content") or {}
        out.append(
            {
                "id": step.get("id"),
                "title": step.get("title") or content.get("title"),
                "status": str(content.get("status") or "").lower(),
                "message": content.get("message"),
                "result": content.get("result"),
            }
        )
    return out


def summarize(workflow: dict) -> str:
    """One-line human summary of where a workflow is."""
    steps = workflow_steps(workflow)
    done = sum(1 for s in steps if s["status"] in _STEP_OK)
    bad = [s for s in steps if s["status"] in _STEP_BAD]
    total = workflow.get("total_steps") or len(steps) or "?"
    current = next(
        (s for s in reversed(steps) if s["status"] not in _STEP_OK),
        steps[-1] if steps else None,
    )
    label = present(current["title"] or current["id"]) if current else "starting"
    state = "FAILED" if bad else workflow.get("status", "?")
    eta = _dig(workflow, "progress.eta")
    eta_txt = f" eta~{eta}m" if isinstance(eta, (int, float)) and eta else ""
    return f"[{state}] {done}/{total} {label}{eta_txt}"


def wait_workflow(
    client: BudClient,
    workflow_id: str,
    *,
    timeout: int = 3600,
    interval: int = 10,
    on_update: Callable[[str], None] | None = None,
    require_steps: int | None = None,
    kind: str | None = None,
) -> dict:
    """Follow a workflow until its background work settles.

    Returns the final workflow record. Raises BudWorkflowFailed when a step or
    the workflow reports failure, BudTimeout when the deadline passes.

    Success means: the workflow reports ``completed``, or every reported step
    has reached a successful terminal state (and ``require_steps`` many steps
    exist, when given). The second case is what covers jobs whose session is
    still open while the real work has finished.
    """
    deadline = time.time() + timeout
    last_line = ""
    stall_since = time.time()
    seen_steps = -1

    while True:
        workflow = fetch_job(client, workflow_id, kind=kind)
        if workflow.get("missing"):
            raise BudWorkflowFailed(
                f"Job {workflow_id} no longer exists (it may have been cancelled).",
                workflow_id,
            )

        steps = workflow_steps(workflow)
        status = str(workflow.get("status") or "").lower()

        line = summarize(workflow)
        if line != last_line:
            last_line = line
            stall_since = time.time()
            if on_update:
                on_update(line)

        failed = [s for s in steps if s["status"] in _STEP_BAD]
        if failed or status == "failed":
            reason = present(workflow.get("reason")) or "; ".join(present(s["message"] or s["title"]) for s in failed)
            raise BudWorkflowFailed(
                f"Job {workflow_id} failed: {reason or 'no reason reported'}",
                workflow_id,
                detail=failed or workflow,
            )

        if status == "completed":
            return workflow

        if steps:
            settled = all(s["status"] in _STEP_OK for s in steps)
            enough = require_steps is None or len(steps) >= require_steps
            if settled and enough:
                # Give the platform one interval to append a further step
                # before declaring the background work done.
                if len(steps) == seen_steps:
                    return workflow
                seen_steps = len(steps)

        if time.time() > deadline:
            raise BudTimeout(
                f"Job {workflow_id} did not settle within {timeout}s. "
                f"Last seen: {line}\n"
                f"It may still be progressing -- re-run the wait, or inspect: "
                f"bud job show {workflow_id}"
            )
        # A job that reports nothing new for a very long time is usually
        # wedged; surface that rather than silently burning the timeout.
        if time.time() - stall_since > max(900, interval * 30):
            if on_update:
                on_update(f"no progress for {int(time.time() - stall_since)}s -- still waiting")
            stall_since = time.time()
        time.sleep(interval)


def wait_resource(
    client: BudClient,
    path: str,
    *,
    field: str = "status",
    equals: set[str] | None = None,
    fail_on: set[str] | None = None,
    timeout: int = 3600,
    interval: int = 10,
    params: dict | None = None,
    on_update: Callable[[str], None] | None = None,
) -> dict:
    """Poll a resource until a field reaches one of the desired values.

    This is the authoritative readiness check: a deployment is usable when the
    deployment says it is running, regardless of what any job record says.
    """
    want = {v.lower() for v in (equals or _STEP_OK)}
    bad = {v.lower() for v in (fail_on or _STEP_BAD)} - want
    deadline = time.time() + timeout
    last = None

    while True:
        try:
            payload = client.get(path, params=params)
        except BudAPIError as exc:
            # A resource can 404 briefly between creation and visibility.
            if exc.status == 404 and time.time() < deadline:
                time.sleep(interval)
                continue
            raise
        record = payload
        if isinstance(payload, dict):
            for key in ("endpoint", "model", "cluster", "result", "data"):
                inner = payload.get(key)
                if isinstance(inner, dict):
                    record = inner
                    break
        raw = _dig(record, field)
        # `or ""` would turn a real 0 or False into "", making --equals 0
        # impossible to satisfy. Only None means "not present".
        value = "" if raw is None else str(raw).lower()
        if value != last:
            last = value
            if on_update:
                on_update(f"{field}={value or 'unknown'}")
        if value in want:
            return record if isinstance(record, dict) else {}
        if value in bad:
            raise BudWorkflowFailed(
                f"{path} reported {field}={value}, which is a failure state.",
                detail=record,
            )
        if time.time() > deadline:
            raise BudTimeout(
                f"{path} still has {field}={value or 'unknown'} after {timeout}s "
                f"(wanted one of: {', '.join(sorted(want))})."
            )
        time.sleep(interval)


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: int = 900,
    interval: int = 10,
    description: str = "condition",
) -> Any:
    """Generic poll helper for conditions the other two do not cover."""
    deadline = time.time() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.time() > deadline:
            raise BudTimeout(f"Timed out after {timeout}s waiting for {description}.")
        time.sleep(interval)


def find_recent_workflow(client: BudClient, workflow_type: str, *, limit: int = 20) -> dict | None:
    """Locate the newest job of a given type -- useful when a call did not
    return an id directly.
    """
    payload = client.get("/workflows", params={"page": 1, "limit": limit})
    for item in extract_list(payload):
        if isinstance(item, dict) and item.get("workflow_type") == workflow_type:
            return item
    return None


def stderr_progress(prefix: str = "") -> Callable[[str], None]:
    """Progress callback that writes to stderr, leaving stdout clean for JSON."""

    def _emit(line: str) -> None:
        sys.stderr.write(f"{prefix}{line}\n")
        sys.stderr.flush()

    return _emit
