---
name: bud-deployments
description: Deploy a model in Bud Foundry and run it - size it to latency/concurrency targets, pick a cluster on cost or speed, scale it, autoscale it, attach LoRA adapters, publish it with pricing, and delete it. Use when a Bud request mentions deploying or serving a model, endpoints or deployments, replicas or workers, scaling or autoscaling, throughput, latency or SLO targets, or making a model available to callers.
---

# Bud Foundry: deployments

A **deployment** makes one model callable at an OpenAI-compatible address
inside a project. Bud sizes it for you: you state the load you expect and the
latency you need, and the capacity planner returns cluster options with
predicted cost and performance.

Prerequisite: connect first - see `bud-platform` (`bud login`).
You need a `project_id` (`bud-projects`) and a model (`bud-models`).

## First: which kind of deployment?

The two paths are genuinely different. Check `provider_type` on the model.

| | Self-hosted model | Hosted (commercial) model |
|---|---|---|
| `provider_type` | `hugging_face`, `url`, `disk` | `cloud_model` |
| Needs a cluster | yes | no |
| Needs a provider credential | no | yes |
| Capacity planning runs | yes | skipped |
| Takes | ~12-20 minutes | ~1 second |

## Deploying a hosted model (fast path)

Three calls, no waiting.

```bash
MODEL=$(bud api GET /models/ -q table_source=model -q provider_type=cloud_model -q limit=50 \
  | jq -r '.models[0].model.id')
CRED=$(bud api GET /proprietary/credentials/ -q limit=10 | jq -r '.credentials[0].id')

# 1. open the session
W=$(bud api POST /models/deploy-workflow -d '{
  "workflow_total_steps": 4, "step_number": 1, "trigger_workflow": false,
  "project_id": "'"$BUD_PROJECT_ID"'", "model_id": "'"$MODEL"'"}' | jq -r .workflow_id)

# 2. the provider credential
bud api POST /models/deploy-workflow -d '{
  "workflow_id":"'"$W"'","step_number":2,"trigger_workflow":false,
  "credential_id":"'"$CRED"'"}'

# 3. name it and go
bud api POST /models/deploy-workflow -d '{
  "workflow_id":"'"$W"'","step_number":4,"trigger_workflow":true,
  "endpoint_name":"hr-01",
  "deploy_config":{"concurrent_requests":100,"avg_context_length":2048,"avg_sequence_length":512}}'
```

The deployment is created **inside that last call** - read its id straight back
from `workflow_steps.budserve_cluster_events.endpoint_id`:

```bash
bud job show $W --data | jq -r '.data.budserve_cluster_events'
```

Latency targets are ignored for hosted models - you do not control their
serving - so leave `ttft`, `e2e_latency` and `per_session_tokens_per_sec` out.

## Before you deploy: a read-only feasibility pass

A self-hosted deployment can cost hours - a multi-gigabyte model import, then a
20-minute rollout - before anything tells you the fleet cannot host it. Every
check below is a GET. Run them first.

```bash
# 1. Is there a model of the size the user asked for?  (filters are in BILLIONS)
bud api GET /models/ -q table_source=model -q model_size_min=6 -q model_size_max=9
#    total_record: 0  ->  it must be imported first (bud-models), which is the
#    expensive step. Do the rest of this pass BEFORE starting that import.

# 2. Which clusters are actually usable?
bud api GET /clusters/clusters -q limit=50 | jq -r '.clusters[] | "\(.id)  \(.name)  \(.status)"'

# 3. For each `available` cluster: does it have a default storage class?
bud api GET /clusters/<cluster-id>/settings | jq '.settings.default_storage_class'
#    404 or null  ->  DISQUALIFIED. It will not host a deployment however good
#    the capacity plan looks. This is invisible to the planner.

# 4. What accelerators exist?
bud api GET /clusters/<cluster-id>/node-metrics | jq '.. | .gpu_total_workers? // empty'
#    all zero  ->  CPU only. Say so plainly; it caps concurrency hard.

# 5. Empirical sizing proxy: what did the closest already-running model achieve?
bud api GET /endpoints/<similar-endpoint-id>/model-cluster-detail \
  | jq '.result.deployment_config.nodes[0] | {replicas, concurrency, ttft, e2e_latency, type}'
```

Step 5 is the most useful and the least obvious: a comparable model already
deployed on the same hardware tells you what that hardware really delivers,
which no amount of planning will contradict. If a 4B model needed a whole node
to serve **one** concurrent request, a 7B at 100 concurrent is not happening on
that fleet.

**If the pass says it cannot work, the right output is a capacity report, not a
deployment.** Tell the user what is missing (accelerator class, storage class,
node count), what the fleet can realistically do instead, and let them decide.
Do not import a model you already know cannot be served.

## Deploying a self-hosted model (the full path)

This is a stepped session: you push fields in over several calls, the platform
plans capacity, you choose a cluster, then you trigger it. Fields accumulate,
so anything set in an earlier step still counts at the end.

### Step 1 - open the session

```bash
W=$(bud api POST /models/deploy-workflow -d '{
  "workflow_total_steps": 8, "step_number": 1, "trigger_workflow": false,
  "project_id": "'"$BUD_PROJECT_ID"'", "model_id": "'"$MODEL"'",
  "hardware_mode": "dedicated"}' | jq -r .workflow_id)
```

Send `workflow_total_steps` **only** on this first call, and `workflow_id` on
every call after it. Sending both, or neither, is rejected.

`hardware_mode` is `dedicated` (whole accelerators) or `shared` (time-sliced,
cheaper, slower under contention).

### Step 2 - state the workload and the SLOs

This call is what starts capacity planning, so get it right the first time.

```bash
bud api POST /models/deploy-workflow -d '{
  "workflow_id":"'"$W"'","step_number":4,"trigger_workflow":false,
  "endpoint_name":"support-7b",
  "deploy_config":{
    "concurrent_requests": 100,
    "avg_context_length": 2048,
    "avg_sequence_length": 512,
    "ttft": [200, 1000],
    "e2e_latency": [5, 10],
    "per_session_tokens_per_sec": [55, 120]},
  "enable_tool_calling": true, "enable_reasoning": false}'
```

| Field | Unit | Meaning |
|---|---|---|
| `concurrent_requests` | count | simultaneous users to support |
| `avg_context_length` | tokens | typical **input** size (max 200000) |
| `avg_sequence_length` | tokens | typical **output** size |
| `ttft` | **milliseconds** `[min,max]` | acceptable time to first token |
| `e2e_latency` | **seconds** `[min,max]` | acceptable end-to-end time |
| `per_session_tokens_per_sec` | tokens/s `[min,max]` | per-user generation speed |

The three ranges must be exactly two integers.

> **Send `deploy_config` exactly once.** Including it again in a later call
> starts a *second* planning run and discards the first one's results.

> **Always supply all three ranges for a self-hosted deployment.** Omitting one
> sends a target of zero, which collapses the ranking to pure cost and quietly
> ignores your latency requirements.

**The three targets are coupled - derive them, do not pick them.** Generation
speed, output length and end-to-end time are the same constraint stated three
ways:

```
per_session_tokens_per_sec[0]  >=  avg_sequence_length / (e2e_latency[1] - ttft[1]/1000)
```

For "max 10 seconds, 512 output tokens, first token within 1 s":
`512 / (10 - 1) = 57` tokens/sec, so the floor is ~57 and a sane range is
`[55, 120]`. Setting `[20, 60]` alongside a 10-second budget asks for 512
tokens at 20/sec - **25.6 seconds** - and the planner then arbitrates silently
between targets that cannot all hold.

So "max 10 second end-to-end latency, up to 100 concurrent requests" becomes:

```jsonc
"concurrent_requests": 100,
"avg_context_length": 2048,
"avg_sequence_length": 512,
"ttft": [200, 1000],
"e2e_latency": [5, 10],
"per_session_tokens_per_sec": [55, 120]   // derived: 512 / (10 - 1)
```

Check the arithmetic before sending it. A self-contradictory SLO is worse than
a loose one, because the recommendation will look like it passed.

### Step 3 - wait for the plan, then read the options

```bash
# poll until non-empty (typically 30-120s, allow 5 minutes)
until [ "$(bud api GET /clusters/recommended/$W | jq '.clusters | length')" != "0" ]; do sleep 10; done
bud api GET /clusters/recommended/$W | jq '.clusters[] | {
  cluster_id, name,
  cost_per_million_tokens: .cost_per_token,
  replicas: .benchmarks.replicas,
  ttft_ms: .benchmarks.ttft.value,
  e2e_s:   .benchmarks.e2e_latency.value,
  meets:   [.benchmarks.ttft.label, .benchmarks.e2e_latency.label]}'
```

> An empty `clusters` array is **ambiguous**: still planning, or no cluster can
> host this. Poll with a deadline; if it is still empty after ~5 minutes, treat
> it as "no capacity" and report the constraint rather than retrying.

Reading the results:

- `cost_per_token` is **cost per million tokens**, despite the name.
- Results are **already sorted cheapest first**, so `clusters[0]` is the
  cheapest option - no sorting needed.
- Each `benchmarks.*.label` is `Better`, `Expected` or `Worse` against your
  target. A cluster meets your SLOs when no label is `Worse`.
- You get **one option per cluster** - its cheapest viable configuration. There
  is no way to ask for a faster configuration on a specific cluster.

Choosing:

| The user asked for | Pick |
|---|---|
| cheapest | `clusters[0]` |
| lowest latency | lowest `benchmarks.e2e_latency.value` |
| fastest first token | lowest `benchmarks.ttft.value` |
| most headroom | highest `benchmarks.over_all_throughput.value` |
| "meets my SLO, cheapest" | first cluster with no `Worse` label |

State which you picked and why, with the cost and predicted latency.

### Step 4 - choose the cluster

```bash
C=$(bud api GET /clusters/recommended/$W | jq -r '.clusters[0]')
bud api POST /models/deploy-workflow -d '{
  "workflow_id":"'"$W"'","step_number":6,"trigger_workflow":false,
  "cluster_id":'"$(jq -r .cluster_id <<<"$C" | jq -R .)"',
  "tool_calling_parser_type":'"$(jq -c .tool_calling_parser_type <<<"$C")"',
  "reasoning_parser_type":'"$(jq -c .reasoning_parser_type <<<"$C")"',
  "chat_template":'"$(jq -c .chat_template <<<"$C")"'}'
```

> Send `cluster_id`, **not** `id`. Each recommendation carries both; `id` is an
> internal key and using it fails with a confusing not-found error.

Copy the parser fields from the recommendation you chose - they describe how
that configuration runs the model.

### Step 5 - trigger, and wait

```bash
bud api POST /models/deploy-workflow -d '{
  "workflow_id":"'"$W"'","step_number":8,"trigger_workflow":true,
  "budaiscaler_specification":{"enabled":false,"minReplicas":1,"maxReplicas":10,
                               "scalingStrategy":"BudScaler"}}'

bud wait job $W --timeout 2400 --interval 30
```

**Nothing happens without `"trigger_workflow": true`.** A session that never
gets it just sits holding data - the single most common reason a deployment
"silently didn't happen".

If required fields are missing you get `400 Missing required data: ...` naming
them. For a self-hosted deployment the set is `model_id`, `project_id`,
`endpoint_name`, `deploy_config`, `cluster_id`, and the planning result.

### Step 6 - confirm it is actually serving

Job completion is not the same as serving. The authoritative check:

```bash
bud api GET /endpoints/ -q project_id=$BUD_PROJECT_ID -q name=support-7b \
  | jq -r '.endpoints[0] | "\(.id) \(.status)"'
```

`status` must be exactly `running`. Other values: `deploying`, `pending`,
`unhealthy`, `failure`, `deleting`. An **empty list means still deploying or
failed** - the record only appears once the rollout succeeds.

Then verify it answers - see `bud-inference`.

## Naming rules

Deployment names are **globally unique across the whole installation**, not
just your project, and are silently rewritten: spaces become hyphens, the name
is lower-cased, and only letters, digits and hyphens survive. So `"HR 01"`
becomes `hr-01` - search for the rewritten form afterwards. A name collision
surfaces as an error at trigger time, after you have already waited.

## Scaling and autoscaling

You scale by **concurrency**, not replica count - Bud works out the pods.

```bash
# add capacity
W2=$(bud api POST /endpoints/add-worker -d '{
  "workflow_total_steps":3,"step_number":1,"trigger_workflow":false,
  "endpoint_id":"'"$EP"'","additional_concurrency":100}' | jq -r .workflow_id)
bud api POST /endpoints/add-worker -d '{"workflow_id":"'"$W2"'","step_number":3,"trigger_workflow":true}'
bud wait job $W2 --timeout 900
```

Autoscaling is a single synchronous call. To hold a latency SLO under varying
load:

```bash
bud api PUT /endpoints/$EP/autoscale -d '{"budaiscaler_specification":{
  "enabled": true, "minReplicas": 2, "maxReplicas": 8, "scalingStrategy": "BudScaler",
  "metricsSources": [{"type":"pod","protocolType":"http","port":"9090","path":"/metrics",
                      "targetMetric":"bud:time_to_first_token_seconds_average",
                      "targetValue":"0.5"}]}}'
```

`targetValue` and `port` are **strings**. Metric names are validated only when
`enabled` is true, so a typo on a disabled config fails later, on enable.

**There is no pause/resume verb.** Pause by scaling to zero
(`minReplicas: 0` plus `scaleToZeroConfig`), resume with `minReplicas: 1`.
See `references/autoscaling.md`.

## Publishing and pricing

```bash
bud api PUT /endpoints/$EP/publish -d '{"action":"publish",
  "pricing":{"input_cost":0.0005,"output_cost":0.0015,"currency":"USD","per_tokens":1000}}'
```

The deployment must be `running`, and pricing is mandatory. Publishing puts it
in the catalog so client applications can reach it.

Repricing later uses `PUT /endpoints/{id}/pricing` - re-publishing an
already-published deployment is a no-op and silently ignores new pricing.

## Deleting

```bash
bud api POST /endpoints/$EP/delete-workflow
```

**Confirm with the user first, naming the deployment.** It frees real compute
and breaks anything calling it. Deletion fails while a prompt or agent version
still references it, naming the blocker.

## Deeper reference

- `references/deploy-protocol.md` - every field of the stepped session, both
  paths end to end, and the required-field rules
- `references/recommendations.md` - how capacity planning works, what the
  numbers mean, and how to reason about cost/latency trade-offs
- `references/autoscaling.md` - the full scaling specification, metrics per
  serving engine, schedules, cost caps and scale-to-zero
- `references/operations.md` - workers, adapters/LoRA, rate limits, retries,
  fallbacks, response behaviour, publication history

## Where to go next

`bud-inference` to send traffic, `bud-observability` to watch it,
`bud-routing` to split traffic across deployments, `bud-guardrails` to add
safety policy, `bud-evaluations` to measure quality.
