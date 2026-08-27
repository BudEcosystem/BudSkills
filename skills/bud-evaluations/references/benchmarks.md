# Performance benchmarks: throughput, TTFT, TPOT, latency

Measures how *fast* a model serves, not how *accurate* it is. A benchmark
deploys the model onto a cluster, drives load at a chosen concurrency, and
records per-request timings.

**This spends real compute.** It provisions a deployment before applying load,
so it can run for hours. Confirm cluster, node selection, replica count and
concurrency with the user before triggering. Requires the benchmark-manage
permission (benchmark-view for the read paths).

## The nine-step wizard

All steps POST to `/benchmark/run-workflow`. Unlike the evaluation wizard, the
step fields sit at the **top level** of the body, not under `stage_data`.

```json
{"workflow_id": "<uuid>", "step_number": 3, "trigger_workflow": false, "cluster_id": "..."}
```

| Step | Fields |
|---|---|
| 1 | `name` (<= 50 chars), `tags[]`, `description`, `concurrent_requests`, `eval_with`: `dataset`\|`configuration` |
| 2 | `datasets: ["<perf-dataset-id>"]` **or** `max_input_tokens`, `max_output_tokens` (matching the step-1 `eval_with`) |
| 3 | `cluster_id` |
| 4 | `hardware_mode`: `dedicated`\|`shared`, `nodes: [{...}]` |
| 5 | `model_id` |
| 6 | `selected_device_type`, `tp_size`, `pp_size`, `replicas`, `num_prompts` |
| 7 | `credential_id` (cloud/proprietary providers only) |
| 8 | `user_confirmation: true` |
| 9 | `run_as_simulation: false`, `trigger_workflow: true` |

Hard rules:

- **`workflow_total_steps` is required on the very first call and must NOT be
  sent together with `workflow_id`.** Sending both raises a validation error.
- **`name` is capped at 50 characters** because it becomes a cluster namespace.
- **Submitting `model_id` at step 5 synchronously fires the capacity planner**
  and requires `cluster_id`, the resolved cluster reference, `nodes`,
  `eval_with` and `concurrent_requests` to already be stored. Out of order you
  get `Missing required data for bud simulation: ...`. Always submit cluster and
  nodes *before* the model.
- On trigger, anything still missing raises
  `Missing required data for run benchmark workflow: ...` listing the keys.

`cluster_id` and node hostnames come from `bud-clusters`, `model_id` from
`bud-models`, `credential_id` from `bud-projects`.

### Getting legal sizing values for step 6

Do not guess `tp_size`/`pp_size`/`replicas`. Ask the planner:

```bash
bud api POST /benchmark/node-configurations -d '{
  "model_id": "<uuid>", "cluster_id": "<uuid>", "hostnames": ["node-1"],
  "hardware_mode": "dedicated",
  "input_tokens": 1024, "output_tokens": 512, "concurrency": 10}'
```

The response is **not** wrapped in the usual envelope:

```json
{"cluster_id": "...",
 "model_info": {"model_id": "...", "model_name": "...", "model_uri": "...",
                "estimated_weight_memory_gb": 8.2, "min_tp_for_model": 1},
 "device_configurations": [{"device_type": "cuda", "device_name": "...",
   "total_devices": 4, "nodes_count": 1, "max_devices_per_node": 4,
   "memory_per_device_gb": 48,
   "tp_pp_options": [{"tp_size": 1, "pp_size": 1, "max_replicas": 4,
                      "total_devices_needed": 1, "description": "..."}],
   "min_tp_required": 1, "supports_pipeline_parallelism": true}],
 "selected_nodes": ["node-1"], "hardware_mode": "dedicated"}
```

Pick a `(tp_size, pp_size)` pair from `tp_pp_options` and keep `replicas` at or
below its `max_replicas`. `selected_device_type` for step 6 is
`device_configurations[].device_type`.

### Traffic source

`eval_with: "dataset"` replays prompts from the performance dataset catalog:

```bash
bud api GET /dataset -q page=1 -q limit=50 | jq -r '.datasets[] | "\(.id)  \(.name)  \(.num_samples)"'
```

These are **not** eval datasets (`/experiments/datasets`) - different table,
different ids, and mixing them up yields a 400/404 or an empty run. Rows carry
`hf_hub_url`, `formatting` (`sharegpt`/`alpaca`), `split`, `columns`,
`num_samples`, `status` (`active|inactive`). Read-only.

`eval_with: "configuration"` skips the catalog and synthesises prompts at the
given `max_input_tokens`/`max_output_tokens` - better when you want a clean,
controlled shape rather than realistic traffic.

Preview what a dataset will send:

```bash
bud api POST /benchmark/dataset/input-distribution  -q num_bins=10 -d '["<dataset-id>"]'
bud api POST /benchmark/dataset/output-distribution -q num_bins=10 -d '["<dataset-id>"]'
```

Both take a **bare JSON array** as the body (not an object) and optionally
`-q benchmark_id=<id>` to scope to one run.

## Waiting

```bash
bud wait job <workflow-id> --timeout 10800        # 3h; deployment dominates the time
```

Progress lives in `GET /workflows/{workflow_id}`:
`workflow_steps.bud_simulator_events` (sizing) and
`workflow_steps.budserve_cluster_events` (deployment then load). Success is
`workflow.status == "completed"` **and**
`workflow_steps.workflow_execution_status.status == "success"`. The benchmark id
appears at `workflow_steps.benchmark_id` after the trigger.

Confirm the record itself settled rather than trusting the workflow alone:

```bash
BID=$(bud api GET /workflows/<workflow-id> | jq -r '.workflow_steps.benchmark_id')
bud wait resource "/benchmark?name=<benchmark-name>&limit=1" --field benchmarks.0.status \
  --equals success --fail-on failed,cancelled --timeout 10800

# or, once you have the id, an unambiguous per-record wait:
bud wait resource /benchmark/$BID/model-cluster-detail --field status \
  --equals success --fail-on failed,cancelled --timeout 10800
```

The second form is safer when benchmark names may collide: it addresses one
record by id, and the payload's `result` object is unwrapped for you, so the
field is a bare `status`.

`bud wait resource` takes no `-q`, so put the filter in the path and use dotted
indices (`benchmarks.0.status`, not `benchmarks[0].status`). It also cannot wait
for a numeric `0` - a zero reads as empty - so wait on a status string rather
than on a count.

`BenchmarkStatusEnum` = `success|failed|processing|cancelled`. A failure sets
`reason` on the benchmark row.

### Cancelling

```bash
bud api POST /benchmark/cancel -d '{"workflow_id":"<workflow-id>"}'
```

Takes the **workflow id, not the benchmark id**. This is the one long-running
operation in this domain that can be stopped - quality evaluations cannot.

## Reading the result

```bash
bud api GET /benchmark/result -q benchmark_id=$BID | jq '.param'
```

`benchmark_id` is a **query parameter**, and the payload is under `param` (not a
named key):

| Group | Fields |
|---|---|
| Volume | `duration`, `successful_requests`, `total_input_tokens`, `total_output_tokens` |
| Throughput | `request_throughput`, `input_throughput`, `output_throughput`, plus `p25/p75/p95/p99/min/max_throughput` |
| Time to first token | `mean/median/p25/p75/p95/p99/min/max_ttft_ms` |
| Time per output token | `..._tpot_ms` |
| Inter-token latency | `..._itl_ms` |
| End-to-end latency | `..._e2el_ms` |

For a user-facing answer, lead with `output_throughput` (tokens/s),
`median_ttft_ms` and `p95_ttft_ms` at the stated concurrency - a single mean
hides the tail that users actually feel.

> **Units differ between endpoints and there is nothing in the payload to warn
> you.** The `_ttft_ms` / `_tpot_ms` / `_itl_ms` / `_e2el_ms` fields here and the
> summary `ttft` / `tpot` on `GET /benchmark` rows are **milliseconds**; the
> `ttft`, `tpot` and `itl[]` values on per-request rows are **seconds**. A row
> reading `ttft: 0.41` and a list reading `ttft: 561.82` can be the same run.
> Normalise before you compare or chart them.

> `GET /benchmark/result` is a proxy to the cluster manager rather than a local
> read. When that component is restarting it returns a 500 quoting an internal
> invoke failure, even though the benchmark itself succeeded. Retry, and fall
> back to the summary `ttft`/`tpot` on `GET /benchmark` plus
> `GET /benchmark/request-metrics` (both served locally) if it stays down.

Model and cluster context for the run:

```bash
bud api GET /benchmark/$BID/model-cluster-detail | jq '.result'
```

### Per-request rows

```bash
bud api GET /benchmark/request-metrics -q benchmark_id=$BID -q page=1 -q limit=100
```

Returns `{items: [...], page, limit, total_items}` - a **different envelope**
from everything else in this domain (`total_items`, not `total_record`). Each
row has `latency`, `success`, `error`, `prompt_len`, `output_len`,
`req_output_throughput`, `ttft`, `tpot`, `itl[]`.

`POST /benchmark/request-metrics` is the unauthenticated internal ingest on the
same path - never call it. `POST /benchmark/add-request-metrics` is its
authenticated twin and is equally not something you should need.

### Analysis

```bash
# within one benchmark: does TTFT degrade with prompt length?
bud api POST /benchmark/$BID/analysis/field1_vs_field2 -q field1=prompt_len -q field2=ttft

# across benchmarks, for a set of models (body is a BARE array, or null for all)
bud api POST /benchmark/analysis/field1_vs_field2 -q field1=concurrency -q field2=ttft \
  -d '["<model-uuid>"]'
```

Field names for the per-benchmark form come from the request-metric columns
(`latency`, `prompt_len`, `output_len`, `ttft`, `tpot`, `req_output_throughput`,
`itl_sum`). Results land under `param.result`. The `object` label on these two
responses is copy-pasted and wrong - ignore it.

## Finding past benchmarks

```bash
bud api GET /benchmark -q page=1 -q limit=20 -q order_by=-created_at \
  -q status=success -q model_name=llama -q min_concurrency=8 -q max_ttft=500
bud api GET /benchmark/filters -q resource=model      # or cluster - distinct names
```

Note the path has **no trailing slash**. Filters: `name`, `status`,
`model_name`, `cluster_name`, `min/max_concurrency`, `min/max_tpot`,
`min/max_ttft`, `search`. `order_by` is a CSV over
`name`, `status`, `created_at`, `cluster_name`, `model_name` (prefix `-` for
descending).

Rows carry `id`, `name`, `status`, nested `model` and `cluster`, `node_type`,
`vendor_type`, `concurrency`, `tpot`, `ttft`, `eval_with`, `dataset_ids`,
`max_input_tokens`, `max_output_tokens` - enough to compare runs without
fetching each result.

## Comparing to a quality evaluation

They answer different questions and never share ids. A complete "is this
deployment good enough?" answer usually needs both: an evaluation for accuracy
on the capabilities that matter, and a benchmark at the concurrency you expect
in production. Report them as two separate tables; averaging them together is
meaningless.

For how the deployment behaves on *real* traffic rather than a fixed load
profile - live latency, error rates, cost, drift - use `bud-observability`.
