# Autoscaling

One synchronous call sets the whole policy:

```bash
bud api PUT /endpoints/<endpoint-id>/autoscale -d '{"budaiscaler_specification": { ... }}'
bud api GET /endpoints/<endpoint-id>/autoscale
```

The wrapper key `budaiscaler_specification` is required and no other top-level
keys are accepted. The call performs a live upgrade of the running deployment
and blocks until it lands (2-15 seconds).

## Specification

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | |
| `minReplicas` | int `>=0` | `1` | `0` enables scale-to-zero |
| `maxReplicas` | int `>=0` | `10` | |
| `scalingStrategy` | `HPA` \| `KPA` \| `BudScaler` | `BudScaler` | |
| `metricsSources[]` | list | `[]` | what to scale on - see below |
| `gpuConfig` | object | disabled | `memoryThreshold`, `computeThreshold` (0-100), `topologyAware`, `preferredGPUType`, `vGPUSupport` |
| `costConfig` | object | disabled | `hourlyBudgetLimit`, `dailyBudgetLimit` (USD, `0` = unlimited), `spotInstancePreference` (`none`/`prefer`/`require`), `cloudProvider` |
| `predictionConfig` | object | disabled | `lookAheadMinutes` (1-60), `historyDays` (1-90), `minConfidence`, `predictionMetrics[]` |
| `scheduleHints[]` | list | `[]` | `{name, cronExpression, targetReplicas, duration}` |
| `multiCluster` | object | disabled | `federationMode` (`active-passive`/`active-active`/`weighted`), `clusterWeights`, `failoverThresholds` |
| `behavior` | object | see below | reaction speed |
| `scaleToZeroConfig` | object \| null | enabled | `{enabled, activationScale >=1, gracePeriod}` - only effective with `minReplicas: 0` |

`behavior` defaults: scale up immediately (`stabilizationWindowSeconds: 0`,
policies `Percent 100 / 15s` and `Pods 4 / 15s`, `selectPolicy: Max`); scale
down cautiously (`stabilizationWindowSeconds: 300`, `Percent 100 / 15s`,
`selectPolicy: Min`). Policy `type` is `Pods` or `Percent`; `selectPolicy` is
`Max`, `Min` or `Disabled`.

## Metrics you can scale on

Generative text deployments:

| Metric | Scales on |
|---|---|
| `bud:gpu_cache_usage_perc_average` | accelerator cache pressure (**default**) |
| `bud:time_to_first_token_seconds_average` | first-token latency |
| `bud:e2e_request_latency_seconds_average` | end-to-end latency |
| `bud:time_per_output_token_seconds_average` | generation speed |
| `bud:num_requests_waiting` | queue depth |
| `bud:num_requests_running` | in-flight requests |

Embedding deployments:

| Metric | Scales on |
|---|---|
| `bud:infinity_queue_depth` | queue depth (**default**) |
| `bud:infinity_embedding_latency_seconds_average` | embedding latency |
| `bud:infinity_classify_latency_seconds_average` | classification latency |

Leaving `metricsSources` empty applies the default metric for the deployment
type at a target of `0.8`.

```jsonc
"metricsSources": [{
  "type": "pod", "protocolType": "http", "port": "9090", "path": "/metrics",
  "targetMetric": "bud:e2e_request_latency_seconds_average",
  "targetValue": "8.0"
}]
```

**`targetValue` and `port` are strings.** Numbers are rejected.

## Holding an end-to-end latency SLO

To keep a 10-second end-to-end budget with headroom, target well inside it and
let scale-up react fast:

```jsonc
{"budaiscaler_specification": {
  "enabled": true, "minReplicas": 2, "maxReplicas": 10, "scalingStrategy": "BudScaler",
  "metricsSources": [{"type":"pod","protocolType":"http","port":"9090","path":"/metrics",
                      "targetMetric":"bud:e2e_request_latency_seconds_average",
                      "targetValue":"7.0"}],
  "behavior": {
    "scaleUp":   {"stabilizationWindowSeconds": 30,
                  "policies":[{"type":"Pods","value":2,"periodSeconds":15}],
                  "selectPolicy":"Max"},
    "scaleDown": {"stabilizationWindowSeconds": 300,
                  "policies":[{"type":"Percent","value":50,"periodSeconds":60}],
                  "selectPolicy":"Min"}}}}
```

Set `minReplicas` to the recommendation's `benchmarks.replicas` - the replica
count sized for your full `concurrent_requests` - not `1`. Otherwise the first
burst after a quiet period misses the SLO while scaling up. A reasonable
`maxReplicas` is roughly twice that, capped at what the cluster can hold. If you
deliberately want less warm capacity, set `minReplicas` to the fraction of peak
you are willing to serve immediately, and say so.

## Pause and resume

```bash
# pause - releases the compute, keeps the deployment
bud api PUT /endpoints/$EP/autoscale -d '{"budaiscaler_specification":{
  "enabled":true,"minReplicas":0,"maxReplicas":5,"scalingStrategy":"BudScaler",
  "scaleToZeroConfig":{"enabled":true,"activationScale":1,"gracePeriod":"5m"}}}'

# resume
bud api PUT /endpoints/$EP/autoscale -d '{"budaiscaler_specification":{
  "enabled":true,"minReplicas":1,"maxReplicas":5,"scalingStrategy":"BudScaler"}}'
```

Set `gracePeriod` explicitly - the effective default when scaling to zero is
much shorter than the schema suggests. The first request after a scale-to-zero
pays a cold start.

## Failure modes

- Allowed deployment states for this call are `running`, `deploying`,
  `failure`, `unhealthy`. The last two are deliberate, so a paused or broken
  deployment can be revived. Any other state is a 400.
- Hosted models have no cluster and cannot autoscale:
  `400 Cannot update autoscale for endpoint without cluster assignment`.
- `targetMetric` is validated **only when `enabled` is true**. A typo on a
  disabled policy is accepted and fails later, when you enable it.
- Some values this call accepts are rejected further down and surface as a
  500 with a message. If that happens, keep policy values at `1` or above and
  keep `lookAheadMinutes` at 60 or less.
