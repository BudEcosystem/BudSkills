---
name: bud-observability
description: Inspect what actually happened on a Bud Foundry installation - list and open traces for an agent or deployment, read the spans of one run, drill into a single request's messages and cost, and chart latency, error rate, tokens, cost and geography over time. Use when a Bud request mentions traces, spans, a run that went wrong, errors, drift, latency, throughput, token usage, spend, usage dashboards, or "review what my agent did yesterday".
---

# Bud Foundry: observability

Two layers answer two different questions.

- **Traces** - what happened inside *this one run*: the span tree of an agent
  invocation or a model call, with its prompts, tool calls and errors.
- **Analytics** - what happened across *many runs*: requests, success rate,
  latency percentiles, tokens, cost and geography, bucketed over time.

Use traces to find a fault, analytics to prove it is a trend.

Prerequisite: connect first - see `bud-platform` (`bud login`).

## Time windows: send UTC, always

Everything here is time-windowed with ISO-8601 `from_date`/`to_date` (a few
older routes use `start_time`/`end_time`). One rule matters more than anything
else in this skill:

> **A UTC offset is honoured by some endpoints and ignored by others.**
> Verified live for one agent on 2026-08-04: sent as `+05:30`, the agent event
> endpoints converted correctly (4 runs) while the trace list and the inference
> analytics used the wall-clock digits as UTC (12 runs - a different day).
> Convert the window yourself and send `Z`; then every family agrees.

So "yesterday" in the user's timezone is not a calendar date you can paste in.
For IST (UTC+5:30) yesterday is `[prev-2 18:30Z, prev-1 18:30Z)`:

```bash
# Anchor the calendar date in the USER's zone, then convert. `date -d 'yesterday'`
# alone resolves in the HOST's zone and silently returns the wrong day whenever
# the host and the user are on different dates - i.e. 18:30-24:00 UTC for IST,
# which is exactly when a 00:00-IST routine runs.
FROM=$(date -u -d "$(TZ=Asia/Kolkata date -d yesterday +%Y-%m-%d) 00:00 +0530" +%Y-%m-%dT%H:%M:%SZ)
TO=$(date -u -d "$(TZ=Asia/Kolkata date +%Y-%m-%d) 00:00 +0530" +%Y-%m-%dT%H:%M:%SZ)
echo "$FROM .. $TO"    # sanity-check this before trusting any number derived from it
```

Getting this wrong is silent - both windows return plausible data for different
traffic. State the UTC window you used in any summary you write.

**The data has a horizon.** Raw spans are kept about 30 days, per-request and
per-agent rows about 90, and agent message bodies only 7 (the row survives, the
content nulls out). "Nothing found" for an old window usually means expired.

## Flagship: review an agent's runs for drift

The nightly routine. Find the runs that failed or degraded, read what actually
went wrong, and turn it into a change to the prompt.

### 1. Resolve the agent

```bash
bud api GET /prompts --paginate | jq -r '.[] | "\(.id)  \(.project_id)  \(.name)"'
export AGENT_ID=<prompt uuid>  PROJECT_ID=<project uuid>
```

Two identifiers are in play and they are not interchangeable:
`/metrics/observability/traces?resource_type=prompt` needs the **UUID**, while
`/prompts/{id}/traces` and the telemetry query take the agent's **name**. Mixing
them gives 404 or a confidently empty list.

### 2. List the runs

```bash
bud api GET /metrics/observability/traces \
  -q resource_type=prompt -q resource_id=$AGENT_ID \
  -q from_date=$FROM -q to_date=$TO -q page=1 -q limit=200
```

One item per trace, represented by its root span: `trace_id`, `timestamp`,
`duration`, `status_code` (`Ok`/`Error`/`Unset`), `span_name`,
`child_span_count`, `span_attributes`. Read `total_record`/`total_pages` and
walk `page=2,3,…`, or append `--paginate` to get one flat array. `limit` caps
at 1000.

With ownership validation, if you have the name and project:
`GET /prompts/<agent-name>/traces?project_id=$PROJECT_ID&from_date=…&to_date=…`.

Three traps:

- **`duration` is in NANOSECONDS** (`/1e6` for ms). Agent latency elsewhere is ms.
- For event-driven agents the **root span is the inbound webhook**, which returns
  in milliseconds while the run continues for minutes. A 6 ms root is not a 6 ms
  run - real duration comes from child spans or the event summary in step 6.
- A run appears only once its true root span has landed, which lags a minute or
  two, so **in-flight long runs are invisible by design**. For fresher data, or
  every span instead of one row per trace, add `flatten=true` - but then `limit`
  and `total_record` count *spans*, not traces.

### 3. Triage

Bucket the window by `status_code == "Error"` and by duration outliers (past the
p95 of the window). Take the union, newest first, and cap it - 10-20 traces is
plenty for a nightly review. `Unset` is the normal status for a healthy run;
only `Error` is a failure marker at the root.

> **Trace status badly understates agent failure - never count failures from
> it.** For trigger-driven agents the root span is an ingress span, not a
> gateway span, so `gateway_analytics.status_code` is absent entirely and
> `status_code` stays `Unset` even on runs that failed outright. Measured live:
> an agent whose traces showed **1 `Error` out of 15** had **0 of 15 runs
> succeed**. Get the failure *count* from
> `POST /metrics/observability/events/summary` with a `prompt_id` filter, and
> use traces to find out *why*, not *how often*.

### 4. Open the suspicious runs

```bash
bud api GET /metrics/observability/traces/<trace_id> | jq '.total_spans'
```

Every span of the trace, ordered by time; rebuild the tree with
`parent_span_id`. The spans that matter carry `gen_ai.*` attributes:

| `gen_ai.operation.name` | What it is | Read |
|---|---|---|
| `chat` | one model call | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.response.finish_reasons` |
| `execute_tool` | a tool/connector call, or a delegated sub-agent | `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |
| `invoke_agent` | the agent turn itself | `gen_ai.agent.name`, `gen_ai.conversation.id` |

Message attributes arrive already decoded, so
`.span_attributes["gen_ai.input.messages"]` is a list you can index - no second
`fromjson`.

```bash
bud api GET /metrics/observability/traces/<trace_id> | jq -r '
  .spans[] | select(.status_code=="Error")
  | "\(.span_name)  \(.status_message)  \(.span_attributes["error.type"] // "")"'
```

> **A failed tool call often does not set span status.** Tool errors are commonly
> returned as ordinary text in `gen_ai.tool.call.result` (e.g. `"Failed to
> execute prompt"`) on a span whose `status_code` is `Unset`. Scan tool results
> for failure text as well as span status, or you will report a clean run that
> produced garbage.

### 5. Get the actual request and response

Two routes to the content of a model call:

- **From the span** - `gen_ai.input.messages` / `gen_ai.output.messages` on the
  `chat` span. Best for reading the conversation as the agent saw it.
- **From the request record** - each model call also emits a gateway-side span
  (`chat completion`) carrying `model_inference.inference_id`; pass it to
  `GET /metrics/inferences/<inference_id>` for the full record: token counts,
  cost, `error_*` fields and `gateway_request`/`gateway_response` as decoded
  objects. On many installations `messages`, `output` and `raw_request`/
  `raw_response` there are empty while the `gateway_*` pair is populated - read
  those.

### 6. Quantify the drift

Traces show *what* broke; these show whether it is worse than the day before.
Run each for the review window and for the equivalent previous window, and diff.

```bash
# exact totals for the agent: events, success rate, latency
bud api POST /metrics/observability/events/summary \
  -d '{"from_date":"'$FROM'","to_date":"'$TO'","filters":{"prompt_id":"'$AGENT_ID'"}}'

# per-tool call counts, errors and p95 - the fastest drift signal
bud api POST /metrics/observability/tools/analytics \
  -d '{"from_date":"'$FROM'","to_date":"'$TO'","filters":{"prompt_id":"'$AGENT_ID'"}}'

# requests, tokens, cost and latency for the agent's model calls
bud api POST /metrics/aggregated -d '{
  "from_date":"'$FROM'","to_date":"'$TO'",
  "metrics":["total_requests","success_rate","avg_latency","p95_latency",
             "total_tokens","total_cost"],
  "filters":{"prompt_id":"'$AGENT_ID'"},"data_source":"prompt"}'
```

> **`p95_latency` in that metric list is load-bearing.** Without a percentile (or
> cost-percentile) metric the query is served from pre-aggregated rollups, and
> that path **silently discards `filters.prompt_id`** - you get whole-installation
> numbers labelled as one agent. Verified live: the same call returned 775
> requests without the percentile and 166 with it. Sanity-check that a filtered
> result is smaller than the unfiltered one.

Two unit traps: `success_rate` is a **percentage** (88.5) from
`/metrics/aggregated` but a **fraction** (0.885) from `…/events/summary`; and
`data_source` defaults to `inference`, which **excludes agent traffic** - set
`"prompt"` for anything agent-related.

`POST /metrics/analytics` computes `delta`/`delta_percent` against the previous
period for you, which saves the second call (its filter key is `prompt`, not
`prompt_id`) - see `references/metrics-api.md`.

### 7. Summarise and hand off

In this order: the UTC window and the agent; run count and how many failed; the
distinct failure signatures with counts and one example `trace_id` each; tool
error rates that moved; latency and cost deltas; then the proposed prompt change
with its evidence attached. Keep the example trace ids - they are how a human
re-opens what you saw.

- Applying the prompt change (new version, test, publish): `bud-agents`.
- Running this nightly: `bud-routines`. Schedule it in the user's local time but
  keep the window arithmetic in UTC, and have it hand this JSON to the model:

```bash
scripts/agent-trace-review --agent incident-orchestrator --day yesterday --tz +05:30 --with-content
```

It pages the window's traces, expands the failed and slowest ones, extracts error
spans and failing tool results, and adds day-over-day deltas for events, tools,
tokens and cost - one JSON document. `--help` for the full options.

## Traces for a deployment, project or model

Same endpoint, different `resource_type`: `endpoint`, `project`, `model`, or
`all` (the only value that needs no `resource_id`; the others 400 without it).

```bash
bud api GET /metrics/observability/traces -q resource_type=endpoint \
  -q resource_id=<endpoint-uuid> -q from_date=$FROM -q to_date=$TO -q limit=100
```

> `resource_type=endpoint` **deliberately excludes agent-sourced calls**, so a
> deployment an agent hammers all day can look idle. Query by `prompt`, or by
> `project`, to see that traffic.

For pod-level logs of a deployment rather than traces, use
`GET /endpoints/{endpoint_id}/workers/{worker_id}/logs` (see `bud-deployments`).

## Per-request inspection

```bash
bud api POST /metrics/inferences/list -d '{
  "from_date":"'$FROM'","to_date":"'$TO'","is_success":false,
  "offset":0,"limit":100,"sort_by":"timestamp","sort_order":"desc",
  "project_id":"'$PROJECT_ID'"}'

bud api GET /metrics/inferences/<inference_id>
```

This list is **offset-based** (`offset += limit` while `has_more`), unlike the
page-based trace endpoints.

> **Deduplicate by `inference_id`.** The list can repeat a request several times -
> a 20-row page held 6 distinct requests on a live installation - and
> `total_count` is an estimate that can exceed the unfiltered total. Count
> distinct ids, never rows.

Other sharp edges: blocked requests are absent entirely (they carry no request
id); `endpoint_type` accepts only `chat|embedding|audio_transcription|
audio_translation|text_to_speech|image_generation|moderation` and 422s on
anything else; there is no agent filter, so agent-generated calls are
interleaved with direct ones. Human ratings are at
`GET /metrics/inferences/{id}/feedback` - best-effort, it can 500.

## Aggregate KPIs and trends

```bash
# one number per metric, optionally grouped
bud api POST /metrics/aggregated -d '{"from_date":"'$FROM'","to_date":"'$TO'",
  "metrics":["total_requests","success_rate","p95_latency","total_tokens","total_cost"],
  "group_by":["model"],"filters":{"project_id":"'$PROJECT_ID'"}}'

# a chart series
bud api POST /metrics/time-series -d '{"from_date":"'$FROM'","to_date":"'$TO'",
  "interval":"1h","metrics":["requests","error_rate","avg_latency","p95_latency","cost"],
  "filters":{"project_id":"'$PROJECT_ID'"}}'
```

`summary` carries the ungrouped totals even when `groups` is populated; with no
`group_by`, the series is `groups[0].data_points[]`.

> Two failure modes to guard against. **If the backing query is rejected, the
> platform retries it without your filters** (a time series also drops
> `group_by`), so a bad filter becomes a valid-looking but unscoped answer -
> confirm the ids in `groups` match what you asked for. And on the rollup path a
> series can carry **one extra bucket starting exactly at `to_date`** holding a
> whole further interval of data; drop any point at or after `to_date`.

Never mix the `*_execution_cost*` metrics with count/rate metrics in one call -
that path computes only cost and returns everything else as `0`. Issue them
separately. Metric lists, filter routing and the cost model:
`references/metrics-api.md`.

## Fleet health, traffic and dashboards

```bash
bud api POST /metrics/observability/events/summary -d '{"from_date":"'$FROM'","to_date":"'$TO'"}'
bud api POST /metrics/observability/events/analytics \
  -d '{"from_date":"'$FROM'","to_date":"'$TO'","group_by":["agent_name","is_success"]}'
bud api GET /metrics/geography -q from_date=$FROM -q to_date=$TO -q group_by=country -q limit=100
bud api GET /metrics/count      # headline dashboard counts
```

`active_agents`/`active_conversations` from `events/analytics` are per-bucket
approximations - never sum them across buckets; take exact totals from
`events/summary`. In `/metrics/count` only `total_projects` is scoped to the
caller; model, endpoint and cluster counts are installation-wide.

Four gateway analytics routes (`/metrics/gateway/analytics`, `top-routes`,
`client-analytics`, `geographical-stats`) return 404 on current builds - use
`/metrics/geography` and `/metrics/analytics` instead.
`references/traffic-and-security.md` has the working set, plus blocked-traffic
review.

## Trace data is not project-scoped

`/metrics/observability/traces` and `…/traces/{trace_id}` apply no project
membership check: any signed-in user holding a `trace_id` can read the whole span
tree, prompts and responses included. Treat what you retrieve as sensitive, do
not paste raw message content into shared channels, and prefer the
`/prompts/{name}/traces` variants, which do validate ownership.

## Deeper reference

- `references/traces.md` - trace list and detail parameters, the span/attribute
  taxonomy, `flatten`, visibility lag, the attribute-filtered telemetry query
- `references/metrics-api.md` - every metric name, interval and filter, which
  query path each combination takes, and the cost model
- `references/agent-analytics.md` - agent event, tool and token analytics,
  groupable dimensions, window semantics, exact vs approximate counts
- `references/traffic-and-security.md` - geography, client and blocked-traffic
  review, dashboard counts, broken endpoints and their replacements

## Where to go next

`bud-prompt-optimization` to turn what you found into test cases and gate the fix on a
per-case regression check - that is what makes "no regressions" real;
`bud-agents` to ship the prompt change as a new version; `bud-routines` to run the review
nightly; `bud-deployments` for endpoint health and worker logs; `bud-clusters`
for node, GPU and cluster utilisation; `bud-evaluations` for graded quality
scores rather than live traffic.
