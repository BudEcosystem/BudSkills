# Agent analytics

These four endpoints summarise **agent invocations** - one row per inbound event
(a chat turn, a webhook, a trigger firing) - rather than per model call. They are
the right source for "how is this agent doing", and they are scoped differently
from the inference analytics in `metrics-api.md`: no `data_source`, no
`endpoint_id`, a different filter vocabulary.

All four are POST, all take `from_date`/`to_date`, and all nest their result
under `param`. Unlike the trace and inference endpoints, these **do** convert a
UTC offset correctly - which is precisely why you should send `Z` everywhere,
so the two families describe the same window.

## The dimension vocabulary

`group_by` and `filters` both draw on the same list:

`event_kind`, `messaging_system`, `surface`, `agent_name`, `project_id`,
`prompt_id`, `conversation_id`, `reply_source`, `is_success`, `error_type`,
`channel`.

> **Filters are exact string equality, and anything outside that list is
> silently ignored.** `{"filters":{"endpoint_id":"…"}}` is not an error - it
> simply does nothing, and you get whole-installation numbers. Lists are not
> supported; one value per key.

`agent_name` holds whatever the caller registered: sometimes a readable name
(`incident-orchestrator`), sometimes the agent's UUID. When ranking agents,
resolve both forms against `GET /prompts` before presenting them to a human.

## POST /metrics/observability/events/summary - exact totals

```json
{"from_date":"…","to_date":"…","filters":{"prompt_id":"<uuid>"}}
```

```json
{"object":"info","message":"Event summary","param":{"totals":{
  "event_count":15,"success_count":13,"success_rate":0.8667,
  "total_duration_ms":3921007,"avg_latency_ms":261400.5,
  "success_avg_latency_ms":260904.8,"success_latency_samples":13,
  "active_conversations":0,"active_agents":1,"active_installs":0,
  "total_agents":38}}}
```

This is the endpoint to quote. Counts are exact, latency covers the whole window,
and `success_avg_latency_ms` excludes failed runs (usually the number you want -
failures are often fast).

`success_rate` is a **fraction** here (0.87), unlike the inference analytics
which return percentages. `total_agents` is an all-time count, not
window-scoped - do not present it as activity.

## POST /metrics/observability/events/analytics - bucketed series

```json
{"from_date":"…","to_date":"…","group_by":["agent_name","is_success"],
 "filters":{"project_id":"<uuid>"}}
```

Returns `param.items[]` with one row per (bucket x dimension combination):
`time_bucket`, the grouped dimensions, `event_count`, `success_count`,
`success_rate`, `total_duration_ms`, `latency_p50_p95_p99` (a 3-element array),
`active_conversations`, `active_agents`, `active_installs`.

Two things to know:

- **Bucket size is chosen for you** from the window width - under 6 hours gives
  5-minute buckets, under 7 days hourly, otherwise daily. A `tier` field in the
  body is accepted and ignored.
- **`active_*` are approximate distinct counts, per bucket.** Summing or maxing
  them across buckets is wrong. Take window totals from `events/summary`.

`to_date` is **exclusive** on the agent endpoints (`time_bucket < to`), while the
inference analytics use `<= to_date`. Expect an off-by-one-bucket difference
when comparing the two families over the same window.

## POST /metrics/observability/tools/analytics - per-tool health

```json
{"from_date":"…","to_date":"…","filters":{"prompt_id":"<uuid>"}}
```

```json
{"param":{"items":[
 {"tool_name":"search-tickets","connector":"helpdesk","call_count":285,
  "error_count":2,"avg_ms":4553.9,"p50_ms":273.5,"p95_ms":589.7}, …]}}
```

Ordered by `call_count` descending. `connector` names the external system the
tool came from and is empty for a delegated sub-agent - which is how you spot
sub-agent failures: a sub-agent appears here as a tool named after itself, with
no connector.

This is the highest-signal drift check there is: run it for two consecutive
windows and compare `error_count / call_count` and `p95_ms` per tool. Note
`error_count` counts spans marked failed - tool calls that returned an error
*string* are not counted, so cross-check against the traces (see
`traces.md`). `group_by` in the body is ignored.

## POST /metrics/observability/tokens/usage - token trend

```json
{"from_date":"…","to_date":"…","filters":{"prompt_id":"<uuid>"}}
```

Returns `param.items[]` of `{time_bucket, input_tokens, output_tokens,
total_tokens}`, with the same automatic bucket sizing. Usage is de-duplicated
per trace before summing, because a run reports cumulative usage several times.

Zeros are common: this reads the agent-run spans, and agents whose model calls
are attributed only at the gateway report 0 here while
`/metrics/aggregated` (with `data_source: prompt` and a `prompt_id` filter plus
`p95_latency`) shows the real token counts. If tokens look empty, cross-check
there before reporting "no usage".

## Cost accounting for agents

There are two different questions and two different answers:

| Question | Call |
|---|---|
| What did this agent's own model calls cost? | `/metrics/aggregated` with the `*_execution_cost` metrics + `filters.prompt_id` |
| What did the whole run cost, including sub-agents? | the same with `*_execution_cost_cumulative` |

Cumulative figures are attributed to the trace root, so **summing cumulative
cost across agents double-counts** an orchestrator and its sub-agents. Pick one
grain per report. Details in `metrics-api.md`.

## A fleet sweep in three calls

```bash
# 1. exact window totals for everything
bud api POST /metrics/observability/events/summary -d '{"from_date":"'$FROM'","to_date":"'$TO'"}'

# 2. rank agents by success rate
bud api POST /metrics/observability/events/analytics \
  -d '{"from_date":"'$FROM'","to_date":"'$TO'","group_by":["agent_name","is_success"]}' \
  | jq -r '.param.items[] | [.agent_name, .is_success, .event_count, .success_rate] | @tsv' \
  | sort

# 3. for the worst agent, drop into the flagship review in SKILL.md
```

Aggregate step 2 yourself across buckets for `event_count`/`success_count` (those
are exact and additive); never for the `active_*` columns.

## Not available through the public API

Some capabilities exist in the analytics layer but have no public route:
per-conversation rollups and conversation transcripts, the per-event cost
endpoint, guardrail decision analytics, and billing usage summaries. Do not
construct paths for them. The closest supported substitutes: `conversation_id`
as a group/filter dimension on `events/analytics`, the execution-cost metrics for
cost, `bud-guardrails` for policy outcomes, and `bud-projects` for spend controls.
