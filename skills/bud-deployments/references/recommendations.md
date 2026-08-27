# Capacity planning: what the numbers mean

When you supply `deploy_config` for a self-hosted model, Bud's capacity planner
simulates configurations across every eligible cluster and returns the cheapest
viable configuration **per cluster**, sorted cheapest first.

```bash
bud api GET /clusters/recommended/<workflow-id>
```

Note the path takes the **deploy session id**, not the planning id.

## Response

```jsonc
{
  "status": "success",              // always "success" - not a progress signal
  "workflow_id": "<session id>",
  "clusters": [{
    "id": "uuid",                   // internal key - do NOT send this back
    "cluster_id": "uuid",           // <- this is what deploy-workflow wants
    "name": "prod-a40",
    "cost_per_token": 4.31,         // actually USD per MILLION tokens
    "total_resources": 8, "resources_used": 2,
    "resource_details": [{"type":"GPU","available":6,"total":8}],
    "required_devices": [{"device_type":"cuda","num_replicas":2,
                          "concurrency":2,"cost_per_million_tokens":4.31}],
    "benchmarks": {
      "replicas": 2,
      "concurrency":                {"label":"Expected","value":100},
      "ttft":                       {"label":"Better","value":180.0},   // ms
      "e2e_latency":                {"label":"Expected","value":4.2},   // s
      "per_session_tokens_per_sec": {"label":"Better","value":62.0},
      "over_all_throughput":        {"label":"Better","value":6200.0}
    },
    "tool_calling_parser_type": "hermes",
    "reasoning_parser_type": null,
    "chat_template": null,
    "supports_lora": true,
    "supports_pipeline_parallelism": true
  }]
}
```

## Reading it correctly

- **`cost_per_token` is per million tokens.** Multiply by expected monthly
  tokens / 1e6 for a monthly figure.
- **`label`** compares the predicted value against your target:
  `Better` (beats it), `Expected` (meets it), `Worse` (misses it). The label is
  `null` when you did not set that target - another reason to always set all
  three ranges.
- **`replicas`** is how many copies that configuration needs **to serve your
  full `concurrent_requests`**, not one request. It drives cost, and it is the
  number to use as `minReplicas` when configuring autoscaling.
- **`over_all_throughput`** is concurrency multiplied by per-session speed -
  useful for "how much headroom does this buy".
- Latency fields are **`null` for hosted models** - their serving is not yours
  to predict.

## What you cannot ask for

- **One configuration per cluster.** Always that cluster's cheapest viable one.
  You cannot request "the fastest setup on cluster X" - to go faster you must
  pick a different cluster or relax cost.
- The candidate pool is capped internally and clusters that are not currently
  `available` are dropped silently. A cluster you expect to see may be missing
  because it is unhealthy, not because it is unsuitable - check
  `bud api GET /clusters/clusters`.

## When every option misses the target

More common than an empty list, and easier to miss: the planner returns
candidates and **all of them carry a `Worse` label**. There is no error - the
response looks like a normal success.

```bash
bud api GET /clusters/recommended/$W | jq '[.clusters[]
  | select([.benchmarks[]?.label] | index("Worse") | not)] | length'
# 0  ->  nothing meets the SLO
```

**Do not deploy the least-bad option and hope.** Report the gap concretely -
target versus predicted, per metric - and offer the same relaxations listed
under "Empty results" below. A deployment shipped against a `Worse` plan will
miss its SLO in production exactly as predicted, and the prediction was right
there in the response.

Pick with the constraint the user actually stated:

```bash
# cheapest overall (may miss the SLO)
jq '.clusters[0]'
# cheapest that HOLDS the SLO  <- usually what "cheapest" means in a request with targets
jq '[.clusters[] | select([.benchmarks[]?.label] | index("Worse") | not)][0]'
```

## Empty results

`clusters: []` with HTTP 200 means either "still planning" or "nothing can host
this". There is no flag distinguishing them. Poll every 10 seconds for up to
5 minutes; if still empty, report it as a capacity problem and offer the
realistic relaxations:

- lower `concurrent_requests`
- widen `e2e_latency` / `ttft`
- reduce `avg_context_length`
- switch `hardware_mode` to `shared`
- pick a smaller model

## A caveat on time-to-first-token targets

The first-token target is not applied as strictly as the other two on the
default planning path - treat a `ttft` label as indicative and confirm real
first-token latency after deployment with `bud-observability`. The end-to-end
latency and per-session speed targets behave as documented.

## Reusing a plan

Passing a previous `simulator_id` alongside `deploy_config` skips re-planning
and reuses that result. Useful for redeploying a known-good configuration
without waiting again.
