# The evaluation workflow, end to end

Everything here is the quality-evaluation subsystem (`/experiments/*`). For the
performance side see `benchmarks.md`.

## Object model

```
Experiment            a named folder you own. Holds evaluations. Soft-deleted.
  Evaluation          one endpoint x N datasets, created by the 5-step wizard
    Run               one per dataset. Carries the metrics and raw results.
```

`GET /experiments/{id}/runs` is misnamed - it returns **evaluations**, each with
a nested `runs[]`. Actual `Run` objects are only at `GET /experiments/runs/{run_id}`.

Name collision worth knowing: `/runs`, `/runs/{session_id}`,
`/runs/{session_id}/abort` and `/governance/runs/*` belong to agent execution
(`bud-agents`), not to evaluations. Evaluation runs live only under
`/experiments/runs/{run_id}` and `/experiments/{experiment_id}/runs`.

## Creating the experiment

```bash
bud api POST /experiments/ -d '{
  "name": "nightly reasoning",
  "description": "regression suite",
  "project_id": "<uuid>",
  "tag_ids": ["<eval-tag-uuid>"]
}'
```

| Field | Rules |
|---|---|
| `name` | 1-255, `^[a-zA-Z0-9\s\-_]+$`. **Stored lowercased.** Duplicate for the same user (case-insensitive) -> 400. Dots, slashes, colons -> 422. |
| `description` | <= 500 |
| `project_id` | optional; used for filtering only |
| `tag_ids` | uuid[] of eval tags. **Write-once** - `PATCH` cannot change tags, only `name`/`description`. |
| `tags` | deprecated string[] that auto-creates tags; prefer `tag_ids` |

Read the id at `.experiment.id`. Tags come from `GET /experiments/tags`,
`GET /experiments/tags/search?q=<prefix>`, or `POST /experiments/tags`
(idempotent by case-insensitive name; 1-20 chars, `[A-Za-z0-9_-]` only).

`GET /experiments/` filters on `project_id`, `id`, `search` (name substring),
`experiment_status`, `model_id`, `created_after`, `created_before`.
`experiment_status` is **computed from runs, not stored**: `running` if any
non-deleted run is running, `no_runs` if there are none, otherwise `completed` -
**including when every run failed**. Filtering by it loads all rows and paginates
afterwards, so it is slow on large accounts.

## The five steps

All five POST to `/experiments/{experiment_id}/evaluations/workflow` with:

```json
{"workflow_id": "<uuid|null>", "step_number": 1, "workflow_total_steps": 5,
 "trigger_workflow": false, "stage_data": { }}
```

| Step | `stage_data` | Notes |
|---|---|---|
| 1 | `{"name": "...", "description": "..."}` | `workflow_id` is `null`; read the new id from the response. Evaluation names are lowercased and must be unique inside the experiment. |
| 2 | `{"endpoint_id": "<uuid>"}` | A deployed endpoint, `status=running`, with a usable url. Not an agent, not a prompt. |
| 3 | `{"trait_ids": ["<uuid>", ...]}` | From `GET /experiments/traits`. |
| 4 | `{"dataset_ids": ["<uuid>", ...]}` | **Each dataset must be linked to one of the step-3 traits** or the call 400s. |
| 5 | `{}` with `"trigger_workflow": true` | Creates the Evaluation + one Run per dataset and launches the engine. |

State rules, all enforced server-side:

- `step_number > current_step + 1` -> 400 `Cannot skip steps`.
- Resubmitting the **current** step is allowed and overwrites its data - this is
  how you correct a mistake before triggering.
- The workflow must be `in_progress`; otherwise 409 `Workflow is not in progress`.
- After a successful trigger the workflow is `completed` and the id is spent.
  A second evaluation needs a fresh step-1 call.
- Re-read an in-flight wizard with
  `GET /experiments/{experiment_id}/evaluations/workflow/{workflow_id}`.

The response is the generic workflow envelope
(`workflow_id`, `status`, `current_step`, `total_steps`, `reason`,
`workflow_steps`), not a bespoke evaluation object. After the trigger,
`workflow_steps.evaluation_events` appears and `current_step` becomes 6.

### Failures that only surface at step 5

| Message | Cause | Fix |
|---|---|---|
| `No datasets with valid eval_type configurations found` | none of the chosen datasets has `eval_types.gen` | re-list with `has_gen_eval_type=true` (the default) and pick from those |
| `No active project found in the system` | the trigger mints a temporary 24h key from the **oldest active project anywhere in the installation**, ignoring the experiment's own `project_id` | an operator must have at least one active project |
| 400 on step 4 | a dataset is not linked to a step-3 trait | filter datasets by `trait_ids=<the trait you chose>` |

## Watching it run

Poll every 30-60s. Budget **30 minutes per dataset** (that is the engine's own
ETA formula) and set `--timeout` to at least `1800 * dataset_count`.

```bash
bud api GET /workflows/$WF | jq '.workflow_steps.evaluation_events'
bud api GET /experiments/$EXP | jq '.experiment.progress_overview'
bud api GET /experiments/$EXP/summary | jq '.summary'

bud wait resource /experiments/$EXP --field experiment.progress_overview.0.status \
  --equals completed --fail-on failed --timeout 7200
```

`bud wait resource` uses dotted paths with numeric list indices
(`progress_overview.0.status`), takes no `-q` - put filters in the path - and
cannot wait for a numeric `0`, because a zero reads as empty. Wait on a status
string, never on a count reaching zero.

Step sequence inside `evaluation_events`, each with status
`STARTED|RUNNING|COMPLETED|FAILED`:

```
verify_cluster_connection -> preparing_eval_engine -> deploy_eval_job -> monitor_eval_job_progress
```

`progress_overview[]` gives `status`, `eta_minutes`, `average_score_pct`,
`duration_in_seconds`. Caveats:

- `progress_overview[].run_id` is actually an **evaluation** id, not a run id.
- `experiment.stats` (budget, tokens, runtime, processing_rate) and
  `progress.percent` / `processing_rate_per_min` are **hardcoded zeros**. Do not
  report them.
- `GET /experiments/{id}/summary` performs **no ownership check** - it will
  happily summarise another user's experiment if you pass their id.

Lost the workflow id? `GET /workflows -q workflow_type=evaluate_model -q limit=50`
and match on `title` (the evaluation name from step 1). Note the same
`workflow_type` is used by the experiment-creation wizard, so titles can collide.

## Terminal states

| Outcome | What you see |
|---|---|
| Success | every Run `completed`, Evaluation `completed`, `duration_in_seconds` set, metrics written, a success notification raised |
| Partial | any run reported unsuccessful sets that Run `failed` **and the whole Evaluation `failed`** |
| Hard failure | `monitor_eval_job_progress` = FAILED sets *all* runs of the evaluation `failed` |
| Silent hang | extraction failed. An internal status event is published that nothing acts on, so runs stay `running` **forever** |

`RunStatusEnum` = `pending|running|completed|failed|cancelled|skipped|deleted`.
`EvaluationStatusEnum` = `pending|running|completed|failed`.

### Diagnosing a stuck evaluation

There is no reconcile endpoint and no cancel. Work through:

1. `GET /workflows/$WF` - did `evaluation_events` reach
   `monitor_eval_job_progress`, and how long ago did its status last change?
2. `GET /experiments/$EXP/summary` - `running_evaluations` vs
   `completed_evaluations`.
3. `GET /experiments/runs/<run_id>` - `metrics` empty and `raw_results` null
   means nothing was ever extracted.
4. If nothing has moved for well over 30 min per dataset, call it stuck. Report
   it; do not keep polling.

The only manual escape is `PATCH /experiments/runs/{run_id}`, and it has a shape
trap: **`status` is a query parameter and the JSON body is the bare config
dict**, not `{"status": ..., "config": ...}`. Sending
`{"status":"completed"}` as the body silently does nothing.

```bash
bud api PATCH /experiments/runs/<run-id> -q status=failed -d '{}'
```

This only edits bookkeeping. It does not stop the underlying job. Do it only
with the user's agreement, since it destroys the evidence that something hung.

## Deleting

```bash
bud api DELETE /experiments/runs/<run-id>          # removes one run row
bud api DELETE /experiments/<experiment-id>        # soft delete, status -> deleted
```

Neither stops in-flight work. Confirm with the user, naming the experiment,
before deleting anything shared.

## Endpoints that look useful and are not

| Endpoint | Why to avoid |
|---|---|
| `POST /experiments/{id}/runs` | creates runs marked `running` with no evaluation and no engine call; they never complete and the experiment reports `running` forever |
| `POST /experiments/workflow` | the experiment-creation wizard; it creates rows but never launches an evaluation and its workflow stays `in_progress` permanently |
| `GET /experiments/{id}/evaluations` | `scores` is unconditionally `null`; in the one path where it would compute them it raises a 500 |
| `GET /experiments/{id}/runs/history` | returns **fabricated data**: ten rows of random UUIDs with the literal strings `"model_name"`, `"Benchmark"`, `"Score"` and a fixed 2024 timestamp |
| `PATCH /experiments/internal/evaluations/eta` | an internal callback with **no authentication at all**; never call it, and treat reported ETAs as advisory |

## Permissions and error shapes

`/experiments/*` routes carry **no permission decorators** - any authenticated
active user can create experiments and trigger evaluations. Combined with the
unscoped read paths above, treat evaluation results on a shared installation as
visible to every user.

Most `/experiments/*` handlers wrap failures in a blanket `except -> 500` with a
generic "Failed to ..." message. A malformed uuid in a CSV filter or a missing
dataset version commonly surfaces as a 500 rather than a 400 - re-check your
inputs before assuming the platform is broken.
