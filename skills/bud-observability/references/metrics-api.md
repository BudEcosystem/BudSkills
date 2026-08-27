# Inference analytics: metrics, filters and query paths

Four endpoints cover aggregate traffic analysis. All of them are POST with a
JSON body, all take `from_date`/`to_date` in UTC (see SKILL.md), and all of them
have an alias under `/metrics/observability/metrics/*` that is the *same
handler* - `/metrics/aggregated` and `/metrics/observability/metrics/aggregated`
behave identically, likewise for `time-series`.

## data_source: the single most important field

`data_source` appears on aggregated, time-series, analytics, geography and
latency-distribution requests.

| Value | Covers |
|---|---|
| `inference` (default) | direct calls to deployments, **excluding agent traffic** |
| `prompt` | **only** agent-originated calls |

There is no "both". A project dashboard that ignores `data_source` is reporting
direct traffic only.

## POST /metrics/aggregated

One summary object per metric, plus optional groups.

```json
{"from_date":"…","to_date":"…",
 "metrics":["total_requests","success_rate","p95_latency","total_cost"],
 "group_by":["model"],
 "filters":{"project_id":"<uuid>"},
 "data_source":"inference"}
```

Metrics: `total_requests`, `success_rate`, `avg_latency`, `p95_latency`,
`p99_latency`, `total_tokens`, `total_input_tokens`, `total_output_tokens`,
`avg_tokens`, `total_cost`, `avg_cost`, `ttft_avg`, `ttft_p95`, `ttft_p99`,
`cache_hit_rate`, `throughput_avg`, `error_rate`, `unique_users`,
`p95_inference_cost`, `max_inference_cost`, `min_inference_cost`, and the
execution-cost family below.

`group_by`: `model`, `project`, `endpoint`, `user`, `user_project`.
`filters`: `project_id`, `api_key_project_id`, `model_id`, `endpoint_id`,
`prompt_id` (uuid or list of uuids).

Response: `{groups:[{…ids and names…, metrics:{name:{value, formatted_value,
unit}}}], summary:{name:{…}}, total_groups, date_range}`. `summary` is the
ungrouped total and is present regardless of grouping.

Defaults if omitted: `from_date` = 90 days ago, `to_date` = now. Always be
explicit.

### Which query path a request takes - and why it matters

| Request contains | Path | Consequence |
|---|---|---|
| only count/rate/total metrics | pre-aggregated rollups | fast, but **`filters.prompt_id` is silently dropped** |
| any percentile metric (`p95_latency`, `p99_latency`, `ttft_p95`, `ttft_p99`, `*_inference_cost`) or `group_by:["user"]` | per-request detail | exact, honours every filter |
| any `*_execution_cost*` metric | per-run cost path | only cost metrics computed |

Verified on a live installation: `{"metrics":["total_requests","total_cost"],
"filters":{"prompt_id":X}}` returned **775 requests**, and the same call with
`p95_latency` added returned **166**. The first was the whole installation.

**Rule: whenever you filter by `prompt_id`, include `p95_latency`.** It costs
nothing and makes the filter real. Also true in reverse: a filtered number that
is not smaller than the unfiltered one is a bug, not a finding.

### Execution cost (per agent run)

Two families, both requiring `filters.prompt_id`:

- **self** - only this agent's own model calls: `avg_execution_cost`,
  `min_execution_cost`, `p95_execution_cost`, `max_execution_cost`,
  `avg_execution_input_tokens`, `avg_execution_output_tokens`.
- **cumulative** - the whole run including sub-agents it delegated to:
  the same names with `_cumulative`, plus `total_execution_cost_cumulative`.

Both land in `summary`; `groups` is always `[]` and `total_groups` 0 on this
path. **Do not mix them with count/rate metrics** - anything the path does not
compute comes back as `0`, not omitted, which reads as "zero requests today".
A sub-agent roots no trace of its own, so its cumulative figures are 0.

## POST /metrics/time-series

```json
{"from_date":"…","to_date":"…","interval":"1h",
 "metrics":["requests","error_rate","avg_latency","p95_latency","tokens","cost"],
 "filters":{"prompt_id":"<uuid>"},"group_by":["model"],
 "fill_gaps":true,"data_source":"prompt"}
```

Metrics: `requests`, `success_rate`, `avg_latency`, `p95_latency`,
`p99_latency`, `tokens`, `cost`, `ttft_avg`, `cache_hit_rate`, `throughput`,
`error_rate`, `unique_users`, `success_count`, `error_count`.
Intervals: `1m`, `5m`, `15m`, `30m`, `1h`, `6h`, `12h`, `1d`, `1w`.
`group_by`: `model`, `project`, `endpoint`, `user_project`.

Response: `{groups:[{…ids/names…, data_points:[{timestamp, values:{metric:
number|null}}]}], interval, date_range}`. With no `group_by` there is exactly
one group.

Unlike `/metrics/aggregated`, this endpoint honours `filters.prompt_id` on both
paths - but two rollup-path artefacts remain, both verified live:

- **A trailing bucket at `to_date`.** A 1d series for `[08-04, 08-05)` returned
  points for both `08-04` and `08-05`, the latter holding a full extra day.
  Discard any point whose timestamp is >= `to_date`.
- **Rollup counts can lag the exact ones** (128 vs 139 for the same day). Adding
  `p95_latency` forces the detail path and reconciles the numbers.

## POST /metrics/analytics - period comparison

The one endpoint that computes change for you, which makes it the shortest path
to "is this drifting".

```json
{"metrics":["request_count","latency"],
 "from_date":"…","to_date":"…","frequency_unit":"day","frequency_interval":1,
 "filters":{"prompt":"<uuid>"},"data_source":"prompt","return_delta":true}
```

- Metrics: `request_count`, `success_request`, `failure_request`,
  `queuing_time`, `input_token`, `output_token`, `concurrent_requests`, `ttft`,
  `latency`, `throughput`, `cache`. (No cost - use `/metrics/aggregated`.)
- `frequency_unit`: `hour|day|week|month|quarter|year`.
- **Filter keys are different here**: `model`, `project`, `endpoint`, `prompt`
  (not `*_id`), and every value must parse as a UUID or you get 422.
- `group_by`: `model`, `project`, `endpoint`, `user_project`, `api_key`.
- `topk` requires `group_by` and must not be combined with `filters` (422).

Response nests by period, newest first:

```json
{"object":"observability_metrics","items":[
  {"time_period":"2026-08-05T00:00:00","items":[
    {"model_id":null,"project_id":null,"endpoint_id":null,"api_key_id":null,
     "data":{"request_count":{"count":89,"delta":-50,"delta_percent":-35.97},
             "latency":{"avg":9395.9,"p95":18435.2,"p99":23877.9,
                        "delta":1252.13,"delta_percent":15.38}}}]}]}
```

`delta` compares against the previous period of the same length. Note this
endpoint does **not** apply the caller's project scoping - it answers exactly
what the filters ask for.

## POST /metrics/latency-distribution

```json
{"from_date":"…","to_date":"…","filters":{"endpoint_id":"<uuid>"},
 "group_by":["model"],
 "buckets":[{"min":0,"max":100,"label":"0-100ms"}]}
```

Default buckets: 0-100ms, 100-500ms, 500ms-1s, 1-2s, 2-5s, 5-10s, >10s.
Response: `{groups:[{…, buckets:[{range,count,percentage,avg_latency}],
total_requests}], overall_distribution:[…], total_requests, bucket_definitions}`.

Filters here accept only `project_id`, `api_key_project_id`, `endpoint_id`,
`model_id` - **no `prompt_id`**. And on a live installation `data_source` made no
difference to the result, so treat this as a whole-traffic histogram; use
`/metrics/time-series` percentiles when you need agent-scoped latency shape.

## POST /metrics/distribution - agent latency vs load

Buckets agent traffic along an X axis and averages a metric per bucket.

```json
{"from_date":"…","to_date":"…","bucket_by":"input_tokens",
 "metric":"response_time_ms","filters":{"prompt_id":"<uuid>"}}
```

`bucket_by`: `concurrency`, `input_tokens`, `output_tokens`.
`metric`: `total_duration_ms`, `ttft_ms`, `response_time_ms`,
`throughput_per_user`. Ten buckets are generated if you do not supply them.
Always restricted to agent traffic regardless of filters.

> Verified live: only `response_time_ms` produced non-zero averages;
> `total_duration_ms` and `ttft_ms` returned `avg_value: 0.0` with correct
> bucket counts. Check for an all-zero column before drawing conclusions.

## Cross-cutting cautions

- **Silent unfiltered retry.** If the backing query is rejected, the platform
  re-issues it without `filters` (and time-series also without `group_by`) and
  returns that instead of an error. Always verify the ids in `groups` are the
  ones you asked for, and be suspicious of a "filtered" number that matches the
  installation total.
- **Unit mismatch.** `success_rate`/`error_rate` here are percentages (88.5);
  the agent endpoints return fractions (0.885).
- **Client-scoped callers.** For client-type users the platform rewrites
  `filters.project_id` into `filters.api_key_project_id`. Same call, different
  meaning depending on who is signed in.
- **Values are `null`, not missing**, in time-series gaps when `fill_gaps` is
  true (the default).
