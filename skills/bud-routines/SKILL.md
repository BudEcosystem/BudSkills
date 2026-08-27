---
name: bud-routines
description: Make Bud Foundry do things without a human clicking - run a routine on a recurring schedule ("every day at 00:00 IST"), fire an agent when an external system emits an event, start a flow from a webhook or a platform event, run a multi-step pipeline on demand, and follow, pause, edit or retire what you started. Use when a Bud request mentions a schedule or cron, "every day/night/hour/Monday", automating or triggering something, a pipeline or multi-step flow, webhooks, GitHub/Slack/Jira/Linear events, or unblocking a run that is waiting for approval.
---

# Bud Foundry: routines

A **routine** is anything Bud does on its own: on a clock, on an event, or on an
inbound call. Three separate machines do this, they do not share vocabulary, and
confusing them is the most common failure in this domain.

Prerequisite: connect first - see `bud-platform` (`bud login`).

## Pick the machine before writing anything

| What starts the work | Machine | What it can start |
|---|---|---|
| A clock ("every night at midnight") | **Schedule** - `/budpipeline/schedules` | a pipeline |
| An outside system's webhook (GitHub, Jira, Slack, Linear... 60 providers) | **Event trigger** - `/triggers` | an agent |
| A Bud platform event (`model.onboarded`, `cluster.unhealthy`) | **Platform event trigger** - `/budpipeline/event-triggers` | a pipeline |
| Any HTTP caller you control | **Pipeline webhook** - `/budpipeline/webhooks` | a pipeline |
| You, right now | `POST /budpipeline/{id}/execute` | a pipeline |

Two facts shape every design. **The clock can only start pipelines, and no
pipeline action runs an agent** - there is no "run agent X" step, so a scheduled
routine reaches an agent the way any outside caller would: an `http_request` step
against the public inference gateway. And **event triggers only start agents**;
platform event triggers never see provider events. `bud schema` will not help
with pipeline bodies (they are free-form) - the live palette is
`GET /budpipeline/actions`.

## Recurring schedules

### The rule that breaks routines: schedules run on UTC

A schedule carries a `timezone` field. **It is stored, echoed back by every read,
and never applied** - fire times are computed against a UTC clock. Sending
`"expression":"0 0 * * *","timezone":"Asia/Kolkata"` gives midnight UTC, which is
05:30 IST. Convert the wall-clock time yourself:

```bash
scripts/cron-utc 00:00 Asia/Kolkata              # -> 30 18 * * *
scripts/cron-utc 00:00 Asia/Kolkata --weekdays   # -> 30 18 * * 0,1,2,3,4
```

00:00 IST is 18:30 UTC **the previous day**, so a weekdays-only routine becomes
Sun-Thu in UTC. Getting the hour right and the day wrong is the second most
common mistake here. Send `"timezone":"UTC"` so the stored record matches what
actually happens.

For a zone that observes daylight saving there is no correct fixed expression -
the routine drifts an hour twice a year, and `cron-utc` exits non-zero to make
you notice. Tell the user before creating it. Grammar, presets, intervals and a
conversion table: `references/schedules-and-pipelines.md`.

### 1. Build the pipeline the schedule will run

```bash
bud api GET /budpipeline/actions | jq -r '.actions[] | "\(.type)\t\(.category)"'
```

19 actions exist: control flow (`log`, `conditional`, `transform`, `aggregate`,
`set_output`, `wait_until`, `fail`), model / cluster / deployment operations,
`simulation_run`, `http_request`, `notification`. Validate before creating - a
bad DAG otherwise returns a 400 with the errors buried in `detail.errors`:

```bash
cat > /tmp/dag.json <<'JSON'
{"dag": {
  "name": "nightly-agent-review", "parameters": [], "outputs": {},
  "steps": [
    {"id":"review","name":"Ask the reviewer agent","action":"http_request","depends_on":[],
     "params":{"url":"https://gateway.example.bud.studio/v1/chat/completions","method":"POST",
       "headers":{"Authorization":"Bearer REPLACE_ME","Content-Type":"application/json"},
       "body":{"model":"hr-reviewer","messages":[{"role":"user","content":"Review yesterday's HR traces..."}]},
       "timeout_seconds":300}},
    {"id":"tell","name":"Notify owners","action":"notification","depends_on":["review"],
     "params":{"channel":"email","title":"Nightly HR agent review","message":"Review finished."}}
  ]}}
JSON
bud api POST /budpipeline/validate -d @/tmp/dag.json   # {"valid":true,"step_count":2,"has_cycles":false}
PIPELINE=$(bud api POST /budpipeline -d @/tmp/dag.json | jq -r .id)
```

Pipeline responses are **flat** - `.id` at the top level, the exception to the
platform-wide nesting rule (`bud-platform` §5); triggers, runs and approvals all
nest normally.

### 2. Create the schedule, then verify the instant

```bash
SCHED=$(bud api POST /budpipeline/schedules -d '{
  "workflow_id": "'"$PIPELINE"'",
  "name": "Nightly HR agent review (00:00 IST)",
  "schedule": {"type":"cron","expression":"30 18 * * *","timezone":"UTC"},
  "params": {}, "enabled": true}' | jq -r .id)

bud api GET /budpipeline/schedules/$SCHED | jq '{next_run_at, status, run_count, last_execution_status}'
```

`workflow_id` is the **pipeline** id - the field name is historical.

**Verify `next_run_at` rather than trusting the expression you sent.** It is a
UTC timestamp: check it is the instant you meant (`...T18:30:00Z`) and read it
back to the user in their own timezone. Other types are `interval`
(`"@every 1h30m"`), `one_time` (`run_at`, ISO) and `manual`; a `one_time` already
in the past gets `next_run_at: null` and silently never fires. The scheduler
wakes once a minute, so real fire time is up to 60s late.

### 3. Test it now, instead of waiting for midnight

```bash
EXEC=$(bud api POST /budpipeline/schedules/$SCHED/trigger | jq -r .execution_id)
bud api GET /budpipeline/executions/$EXEC/progress \
  | jq '{status: .execution.status, agg: .aggregated_progress,
         steps: [.steps[] | {step_name, status, awaiting_event, error_message}]}'
```

`/trigger` runs the pipeline immediately with the schedule's stored params and
does **not** advance `next_run_at` or `run_count` - a safe smoke test. Always do
it before telling the user the routine is live.

Poll the progress endpoint every 3-10s. It is terminal when `execution.status`
is `COMPLETED` or `FAILED` (uppercase); read `error_info` on failure and
`final_outputs` on success. The keys
are `execution`, `steps`, `recent_events` and `aggregated_progress` - there is no
`events` key, and no separate steps or events endpoint exists. A step reporting
`awaiting_event: true` has handed off to a long platform operation and holds the
execution `RUNNING` for minutes to hours. `bud wait job` does not apply to
pipelines - poll `/progress`.

### 4. Change, pause, retire

```bash
bud api POST /budpipeline/schedules/$SCHED/pause     # keep it, stop firing
bud api POST /budpipeline/schedules/$SCHED/resume    # recomputes next_run_at from now
bud api PUT  /budpipeline/schedules/$SCHED -d '{"schedule":{"type":"cron","expression":"0 19 * * *","timezone":"UTC"}}'
```

Supplying `schedule` recomputes `next_run_at`; supplying `params` replaces the
whole dict. `resume` is a 400 once a schedule has `expired` (past `expires_at`)
or `completed` (hit `max_runs`) - those must be recreated.

> **Deleting a pipeline does not delete its schedules.** They keep firing and
> fail at execute time. Delete schedules, webhooks and event triggers first.

> **Schedules have no owner**: any signed-in user can list, pause, trigger or
> delete any schedule, and executions are visible installation-wide. Name them
> distinctively, and never put a secret in a DAG or in schedule `params` - the
> whole definition is readable by every authenticated user through
> `GET /budpipeline/executions`.

## Flagship: "every night at 00:00 IST, review the HR agent and improve it"

This is the four steps above, wired together. Build the reviewer agent first
(`bud-agents`) with tools that can read the HR agent's traces and propose a
revision, deploy it, then point the DAG's `http_request` at it - the `model`
field takes the agent's **deployment name**, not the model or agent name
(`bud-inference`). Schedule `30 18 * * *`, confirm `next_run_at`, smoke-test with
`/trigger`. Be explicit with the user about the division of labour: Bud schedules
and observes, the judgement is the agent's.

Three things to get right, and to say out loud:

- **The credential.** The gateway needs `Authorization: Bearer <token>`
  (`bud token`; `bud-inference`). Whatever you embed sits in a world-readable
  pipeline definition and expires - plan on rotating it, and suspect it first
  when a working routine starts failing with 401.
- **The private-network block.** `http_request` refuses loopback, link-local and
  private addresses, so the gateway must be reached on its public hostname. The
  step also caps at `timeout_seconds: 300` - if the review needs longer, narrow
  its scope or split it across steps.
- **"Without regressions" is not automatic.** Bud will not stop a bad prompt
  shipping. Either have the reviewer *propose* while the actual edit parks behind
  a human approval (below), or evaluate against a held-out set before promoting
  (`bud-evaluations`). Say which one you implemented. Spotting the drift is
  `bud-observability`'s job; versioning the prompt is `bud-agents`'.

## Event triggers: fire an agent when an outside system emits an event

**Read the catalog - never guess the event string.**

```bash
bud api GET /events/descriptors -q provider=github \
  | jq '{types: [.descriptors[] | {canonical_type, actions: [.actions[].value]}],
         github: (.providers[] | select(.provider=="github"))}'
```

`providers[].sample_event.eventType` is the **exact** string for `event_types` -
`issues.opened` (GitHub), `Issue.create` (Linear), `firing` (an alert manager).
Some providers compose `type.action` and others do not, and nothing validates
what you send: a wrong string creates a trigger that looks healthy in every read
and never fires. `providers[].grouping_suggestions[]` gives the correlation key -
take the entry with `suggested: true`. `?provider=` filters only `descriptors[]`.

```bash
CONN=$(bud api POST /events/shared-connections -d '{
  "scope":"project", "project_ids":["'"$BUD_PROJECT_ID"'"], "provider":"github",
  "name":"Acme GitHub", "credentials":{"signing_secret":"<secret you set in GitHub>"}}' \
  | jq -r .connection.id)

bud api POST /triggers -d '{
  "project_id":"'"$BUD_PROJECT_ID"'", "name":"Triage new issues",
  "connection_id":"'"$CONN"'", "target":{"prompt_id":"<agent-id>","version":"1"},
  "event_types":["issues.opened"],
  "descriptor":{"correlation":{"key":"$.repository.full_name","ttl_seconds":3600,"multi_match":"newest"}}}'

bud api GET /triggers/<trigger-id> | jq '.trigger | {event_types, descriptor, sources}'
```

Point the provider at the connection's `webhook_url` (returned on create, and
re-readable from `GET /events/shared-connections`) using the same secret.
`scope:"global"` needs administrator rights, and credentials are write-once, so
rotating one means delete and recreate - which invalidates every trigger on that
connection. Trigger responses nest under `.trigger`; confirm every `sources[]`
entry has a non-empty `subscription_id`, the proof it reached the event router.

> **Write the correlation key in payload space** (`$.repository.full_name`),
> exactly as `grouping_suggestions` gives it - the platform rewrites it
> internally. A key that resolves to nothing produces no error at all: the
> trigger quietly degrades to one agent run per event, with no grouping and any
> join unreachable. If runs look duplicated, suspect the key first.

> `descriptor.hitl` is a 400 - approval gates on triggers do not exist yet, so
> gate inside the agent instead. And `join`, `debounce`, `cep` and `escalation`
> register **best-effort**: if the event router is unreachable the trigger is
> still created and plain routing works while the advanced behaviour silently
> does not. There is no read-back API - what you GET is what you authored, not
> what is enforced. Re-issuing the identical `PUT /triggers/{id}` re-registers.

**Then watch what it produced** - `GET /runs?project_id=...&prompt_id=...`, then
`/runs/<session_id>/timeline`. Expect a run within seconds. If nothing appears:
re-check `event_types`, confirm the provider's delivery log shows a 2xx, and
confirm `subscription_id` is set. There is no delivery log on the Bud side, so an
event that verified but did not match is invisible. Always pass `project_id` to
`GET /triggers`, or the list is not membership-filtered. A triggered run's
credential lifetime is fixed at dispatch, so a very long triggered run can fail
partway with an authentication error - keep triggered work bounded.

**To fire only when two systems agree** (multi-source join), add a `sources[]`
array plus a `join`; four schema rules are enforced at 422, and the correlation
keys must resolve to the *same real-world identifier* in both payloads. Worked
example, the full descriptor algebra, and the separate mechanism for a Slack
channel bot: `references/event-triggers.md`.

## Webhooks and platform events

```bash
bud api POST /budpipeline/webhooks -d '{"workflow_id":"'"$PIPELINE"'","name":"CI hook",
  "config":{"require_secret":true},"params":{"source":"ci"},"enabled":true}'   # secret returned ONCE

bud api POST /budpipeline/event-triggers -d '{"workflow_id":"'"$PIPELINE"'","name":"Benchmark new models",
  "config":{"event_type":"model.onboarded","filters":{"result.status":"success"}},"enabled":true}'
```

Store the webhook secret immediately - it is never shown again, and
`/rotate-secret` kills the old one instantly. The returned `endpoint_url` comes
from the engine's own view of its base URL and is usually **not** externally
routable; confirm the real address with whoever runs the ingress. `event_type`
must be one of eight platform events (full list in
`references/schedules-and-pipelines.md`) and `filters` are exact-equality on dot
paths only. Both listings return **bare arrays** with no single-item GET or PUT,
so changing one means delete and recreate.

## Run a pipeline on demand

```bash
bud api POST /budpipeline/$PIPELINE/execute -d '{"params":{}}'
bud api GET /budpipeline/executions -q workflow_id=$PIPELINE -q page_size=5 | jq -r '.executions[0].id'
```

> **Not fire-and-forget.** The DAG runs inline inside the request and the call is
> cut off at ~30s with `500 Failed to execute pipeline` while the execution
> carries on server-side. **Never retry that 500** - you would start a second
> run. Recover the id from the executions list instead (second line above).

The id field is `execution_id` in the execute response and `id` in list rows,
and status is lowercase in the execute response but **uppercase everywhere
else** - the `status` filter takes only
`PENDING|RUNNING|COMPLETED|FAILED|INTERRUPTED`, and a lowercase value returns a
500 rather than a 422.

## Unblocking a run that is waiting for a human

An agent run can park mid-flight waiting for a person, and **two independent
queues do this** over the same runs - check both before telling anyone nothing is
waiting. `GET /approvals?project_id=...` holds a tool call needing sign-off;
`GET /escalations?status=pending&target=me` holds a safety policy demanding a
decision (`bud-guardrails`).

```bash
bud api GET /approvals -q project_id=$BUD_PROJECT_ID \
  | jq '.items[] | {session_id, call_id: .pending_call.call_id, tool: .pending_call.tool, expires_at}'
bud api GET /runs/<session_id> | jq '.state | {status, suspend_reason, run_policy}'
bud api POST /approvals/<session_id>/approve -d '{"call_id":"<call_id>","message":"ok"}'
bud api POST /approvals/<session_id>/reject  -d '{"call_id":"<call_id>","reason":"unsafe","on_reject":"deny"}'
```

`override_args` on approve edits the tool's arguments before they run;
`on_reject:"abort"` ends the run instead of letting it continue.

> **202 means forwarded, not applied.** Re-read `GET /runs/{session_id}` until
> `suspend_reason` clears. A 409 on approve means the run moved while you were
> deciding - re-read `/approvals` and retry once. An empty `/approvals` is not
> proof nothing is stuck: a run with no approver roster is invisible to
> non-administrators, and a non-member gets an empty 200 rather than a 403.

Other operator moves: `POST /runs/{id}/inject {"text":"..."}` steers a run
mid-flight (**not idempotent** - a retry injects twice),
`POST /runs/{id}/abort {"mode":"after_turn"|"immediate"}` stops it gracefully,
and `POST /governance/runs/{execution_id}/override {"action":"kill"}` is the
blunt instrument for a rogue run. Escalations resolve with
`POST /escalations/{id}/resolve`, only on rows where `can_resolve` is true and
only with a value from that row's `allowed_resolutions`. Rosters, quorum and
standing grants: `references/runs-and-approvals.md`.

## Editing and retiring without dropping events

`PUT /triggers/{id}` is a **full replace, and the read model is not the write
model**: GET returns `prompt_id` and `version` at the top level of `.trigger`,
while PUT wants a nested `target` object. Rebuild it or the version blanks.

```bash
bud api GET /triggers/<id> | jq '.trigger | {name, connection_id, event_types,
  target: {prompt_id, version}, descriptor}'    # edit, then PUT the whole thing back
```

Bud reconciles source by source: unchanged sources keep their subscription so
**no events are dropped**, new ones subscribe before the change commits, removed
ones are torn down after. Changing `target.prompt_id` or `target.version`
re-subscribes every source - verify by diffing `sources[].subscription_id`.

To stop a trigger, `DELETE /triggers/{id}`; there is no enable/disable flag, and
a 502 keeps the row so just re-run the delete. For a reversible pause, save the
full body first and recreate from it later. Do **not** improvise a pause by
deleting the subscription (orphans a row that can never fire) or the shared
connection (cascades every trigger on it, and 409s if it is a secondary join
source).

## Deeper reference

- `references/schedules-and-pipelines.md` - cron/interval grammar, DST, the
  schedule status machine, the DAG schema, all 19 actions, execution statuses,
  the three ways to start a pipeline, operational hazards
- `references/event-triggers.md` - the provider catalog, deriving event types,
  connections, the descriptor algebra, multi-source joins, channel bots, and
  debugging a trigger that never fires
- `references/runs-and-approvals.md` - reading runs, the two approval queues,
  signals and idempotency, escalations, standing grants, and what an empty
  result really means
- `scripts/cron-utc` - wall-clock time to a UTC cron, shifting the day fields
  and flagging daylight saving (`--help`)

## Where to go next

`bud-agents` to build the agent a trigger fires or a routine calls;
`bud-observability` to analyse what those runs produced; `bud-evaluations` to
prove a change did not regress; `bud-connectors` to give an agent its tools;
`bud-inference` for the gateway token and deployment names.
