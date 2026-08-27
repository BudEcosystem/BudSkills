# Jobs: how Bud runs long work, and how to wait for it correctly

Most Bud operations that matter take minutes. Getting the waiting right is the
difference between a skill that works unattended and one that hangs.

## The model

A **job** in Bud is a *stepped session*, not a fire-and-forget task. You create
it, push data into it one step at a time, and finally tell it to execute. While
it executes, the platform writes progress events back into the job.

That design leads to one rule that catches everyone:

> **A job's `status` describes the session, not the work.**
> It reads `in_progress` from the moment the session opens. It becomes
> `completed` only when the session is finished off - which for many flows is a
> client action, not a platform one. A session abandoned halfway stays
> `in_progress` forever.

On a busy installation, most historical jobs sit at `in_progress` permanently.
That is normal - they are abandoned console sessions, not stuck work. So:

- To follow **the work**, watch the per-step progress feed.
- To know **the result is usable**, poll the resource itself.

`bud wait job` does the first. `bud wait resource` does the second. Real
recipes use both.

## The two representations

A job is exposed through two endpoints that return *different shapes*, and
neither is sufficient alone.

**`GET /workflows/{id}`** - session state and collected data:

```json
{
  "object": "workflow.get",
  "workflow_id": "26833c76-...",
  "status": "in_progress",
  "current_step": 4,
  "total_steps": 9,
  "reason": null,
  "workflow_steps": { "model_id": "...", "cluster_id": "...", "...": "..." }
}
```

Note `workflow_id`, not `id`. `workflow_steps` is a **dictionary of everything
the session has collected so far** - useful for recovering what a job was
given. It carries no progress feed.

**`GET /workflows`** - the recent-jobs list, and the *only* place the progress
feed appears:

```json
{"workflows": [{
  "id": "26833c76-...",
  "workflow_type": "model_deployment",
  "status": "in_progress",
  "current_step": 4,
  "total_steps": 8,
  "progress": {
    "eta": 1,
    "steps": [
      {"id": "validation", "title": "Identifying compatible clusters",
       "payload": {"content": {"status": "COMPLETED", "message": "..."}}},
      {"id": "ranking", "title": "Ranking clusters by performance",
       "payload": {"content": {"status": "COMPLETED", "message": "Found 2 suitable cluster(s)"}}}
    ]
  }
}]}
```

The list has **no per-id filter** (only `workflow_type`), so a specific job is
found by scanning recent pages. The toolkit's `fetch_job()` merges both
representations and degrades gracefully when a job is too old to appear in the
list.

Progress step status values are upper-case in the payload (`COMPLETED`,
`FAILED`); compare case-insensitively.

## Waiting, in practice

```bash
# Follow background work; fails fast with the platform's own reason
bud wait job <job-id> --timeout 1800 --interval 10

# Then confirm the thing you wanted is actually usable
bud wait resource /endpoints/<endpoint-id>/model-cluster-detail --field status --equals running
```

`bud wait job` returns when either:
- the job reports `completed`, or
- every reported step has reached a success state and no new step appeared for
  one further poll (this covers work that finished while the session stays open).

It raises immediately if any step reports a failure state, quoting the reason.
On timeout it tells you how to resume watching rather than implying failure.

`bud wait resource` is the authoritative check. It tolerates a brief 404 right
after creation (resources take a moment to become visible), and treats an
explicit failure value as fatal.

### Scripting it

```python
from budkit import BudClient, wait_workflow, wait_resource

client = BudClient()
job = client.post("/models/deploy-workflow", {...})
wait_workflow(client, job["workflow_id"], timeout=1800)
endpoint = wait_resource(client, f"/endpoints/{endpoint_id}/model-cluster-detail", equals={"running"})
```

## Job kinds and how long they take

Durations are order-of-magnitude on a warm installation; a cold cache or a
large model can be far slower.

| Kind | Typically | Notes |
|---|---|---|
| `cloud_model_onboarding` | seconds | No download; validates the provider credential |
| `local_model_onboarding` | minutes to hours | Dominated by model download size |
| `model_security_scan` | minutes | Runs as part of import |
| `cluster_onboarding` | 5-20 min | Installs platform components on the cluster |
| `model_deployment` | 5-30 min | Includes capacity planning, then rollout |
| `add_worker_to_endpoint` | minutes | Scaling an existing deployment |
| `local_model_quantization` | 20 min - hours | Depends on model size and method |
| `model_benchmark` | 10-60 min | Runs real traffic |
| `evaluation_creation` / `evaluate_model` | minutes to hours | Depends on dataset size |
| `guardrail_deployment` | 5-15 min | Deploys a policy engine |
| `prompt_creation`, `prompt_schema_creation` | seconds | Session completes on final step |
| `endpoint_deletion`, `cluster_deletion` | minutes | Cascades - confirm before starting |

Pick a timeout from this table with headroom; do not use one global value.

## Recovering an interrupted job

Nothing is lost when your process dies mid-wait - the job runs server-side.

```bash
bud jobs --limit 30                    # find it (yours only, newest first)
bud job show <job-id> --data           # what it holds, where it stopped
bud wait job <job-id>                  # resume watching
```

Jobs are **scoped to the user who created them** - the listing filters by
creator, so a job started by someone else is invisible to you even as an admin.

To continue a stepped session, re-`POST` to the same `*-workflow` endpoint with
that `workflow_id` and the next `step_number`.

## Cancelling

Some flows have explicit cancels (`POST /clusters/cancel-onboarding`,
`POST /models/cancel-deployment`, `POST /models/cancel-quantization`). Others
use `DELETE /workflows/{id}`, which removes the session record.

Deleting a session record does **not** always stop work already handed to the
platform. Prefer the purpose-built cancel endpoint, and verify afterwards that
the resource really is gone.

## Distinguishing "still working" from "wedged"

`bud wait job` reports when nothing has changed for a long stretch instead of
sitting silent. When you see that:

1. `bud job show <id>` - is the last step's message still plausible?
2. For deployments, check the target: `bud api GET /endpoints/<id>/model-cluster-detail` and the
   cluster's health.
3. Compare elapsed time against the table above. A 40-minute large-model
   download is normal; a 40-minute cloud-model onboarding is not.

Genuinely stuck work usually shows a step that never leaves its first state
while the platform reports no further events. Capture `bud job show --data`
output when reporting it.
