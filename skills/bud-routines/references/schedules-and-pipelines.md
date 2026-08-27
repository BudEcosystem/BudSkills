# Schedules and pipelines: full reference

Everything a pipeline needs: the DAG schema, the complete action catalog, the
cron/interval grammar, and the three ways a pipeline can be started.

## 1. Schedule expressions

### Cron

Exactly **five fields**, `minute hour day-of-month month day-of-week`. There is
no seconds field and no year field.

| Field | Range |
|---|---|
| minute | 0-59 |
| hour | 0-23 |
| day of month | 1-31 |
| month | 1-12 |
| day of week | 0-7 (0 and 7 both mean Sunday) |

Standard `*`, `,`, `-` and `*/n` syntax. Presets are accepted in place of the
five fields:

| Preset | Equivalent |
|---|---|
| `@every_minute` | `* * * * *` |
| `@hourly` | `0 * * * *` |
| `@daily`, `@midnight` | `0 0 * * *` |
| `@weekly` | `0 0 * * 0` |
| `@monthly` | `0 0 1 * *` |
| `@yearly`, `@annually` | `0 0 1 1 *` |

An unparseable expression is rejected at create time with
`400 Invalid schedule: ...`.

### Interval

`type: "interval"` with an expression matching `@every <h><m><s>`, any
combination in that order:

```
@every 30s      @every 5m      @every 1h30m      @every 2h
```

### One-time

`type: "one_time"` with `run_at` as an ISO datetime. **A `run_at` already in the
past yields `next_run_at: null` and the schedule never fires** - no error. Always
read the schedule back and check `next_run_at`.

`type: "manual"` exists and never fires by itself; it is only reachable via
`POST /budpipeline/schedules/{id}/trigger`.

### Time zones - the important part

`schedule.timezone` is stored, echoed back by every read, and **never applied**.
Fire times are computed against a UTC clock. Convert before you write the cron:

| Wall clock | Zone | UTC cron | Note |
|---|---|---|---|
| 00:00 | Asia/Kolkata (UTC+5:30) | `30 18 * * *` | previous UTC day |
| 09:00 | Asia/Kolkata | `30 3 * * *` | |
| 00:00 | Asia/Tokyo (UTC+9) | `0 15 * * *` | previous UTC day |
| 00:00 | Europe/Berlin (winter) | `0 23 * * *` | **DST - slips in summer** |
| 09:00 | America/New_York (winter) | `0 14 * * *` | **DST - slips in summer** |
| 00:00 | UTC | `0 0 * * *` | |

`scripts/cron-utc <HH:MM> <IANA zone>` does this, shifts the day-of-week field
when the conversion crosses midnight, and exits non-zero on a DST zone.

**Day-of-week and day-of-month shift too.** "Every Monday 02:00 IST" is
`30 20 * * 0` - Sunday 20:30 UTC. Getting the hour right and leaving the day
wrong is the second most common mistake after ignoring the time zone entirely.

For a DST zone there is no correct fixed expression. Options, in order of
honesty: tell the user it will drift an hour twice a year and pick standard
time; pick a time of day where an hour's drift does not matter (03:00, not
23:30); or drive the schedule from outside Bud.

### Fire semantics

- The scheduler wakes once a minute, so actual fire time is the intended instant
  **+0 to 60 seconds**.
- Each tick processes a bounded number of due schedules, so a large fleet of
  same-minute schedules spreads over several ticks.
- `status` transitions: `active` -> `paused` (manual pause or `enabled:false`),
  `expired` (past `expires_at`), `completed` (`run_count` reached `max_runs`).
  `expired` and `completed` cannot be resumed - a `resume` returns 400 and you
  must recreate the schedule.
- `POST /{id}/trigger` runs it now with the stored `params`, and deliberately
  does **not** advance `next_run_at` or `run_count`.

### Schedule object

```json
{"id":"...","workflow_id":"<pipeline id>","name":"...",
 "schedule":{"type":"cron","expression":"30 18 * * *","timezone":"UTC","run_at":null},
 "params":{},"enabled":true,"description":"",
 "created_at":"...","updated_at":"...",
 "next_run_at":"2026-08-06T18:30:00Z","last_run_at":"...",
 "last_execution_id":"...","last_execution_status":"completed",
 "run_count":148,"max_runs":null,"expires_at":null,"status":"active"}
```

Note `last_execution_status` is lowercase here while the executions API reports
uppercase. `PUT` with `params` **replaces** the dict; `PUT` with `schedule`
recomputes `next_run_at`.

## 2. Pipeline DAG schema

```json
{
  "name": "string",                    // required
  "version": "1.0",
  "description": "string",
  "parameters": [ {"name":"...","type":"...","default":...,"required":false} ],
  "settings": {
    "timeout_seconds": 7200,
    "fail_fast": true,
    "max_parallel_steps": 4,
    "retry_policy": {...}
  },
  "steps": [
    {
      "id": "step_a",                  // referenced by depends_on and by templates
      "name": "Human label",
      "action": "http_request",        // a type from GET /budpipeline/actions
      "params": {...},                 // that action's parameters
      "depends_on": ["step_b"],        // edges; the DAG must be acyclic
      "outputs": [...],
      "condition": {...},
      "on_failure": "fail",            // fail | continue | retry
      "timeout_seconds": 1800,
      "retry": {...}
    }
  ],
  "outputs": {}
}
```

`parameters` declare what the pipeline accepts; the `params` you pass to
`execute`, a schedule, a webhook or an event trigger are merged over their
defaults. Steps run in topological batches, so independent steps run together up
to `max_parallel_steps`.

**Condition operators** available to `conditional` steps and step `condition`
blocks: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `contains`, `starts_with`,
`ends_with`, `matches`, `in`, `not_in`, `exists`, `not_exists`, plus `and`, `or`,
`not`.

Validate before creating - `POST /budpipeline/validate` with `{"dag": {...}}`
returns `{valid, errors[], warnings[], step_count, has_cycles}`. You can also
check one step's parameters in isolation with `POST /budpipeline/actions/validate`
`{"action_type": "...", "params": {...}}`.

## 3. Action catalog

Read it live - `GET /budpipeline/actions` for everything, or
`GET /budpipeline/actions/{type}` for one action's full parameter spec including
labels, defaults, options and validation ranges. As deployed there are 19
actions. `*` marks a required parameter.

### Control flow (all `sync`)

| Action | Parameters | Outputs |
|---|---|---|
| `log` | `message`(template), `level`(select) | `logged`, `message`, `level` |
| `conditional` | `branches`, `condition`, `true_result`, `false_result` | branch result |
| `transform` | `input`(json), `operation`(select: passthrough, uppercase, lowercase, keys, values, count, flatten, unique, sort, reverse) | transformed value |
| `aggregate` | `inputs`(json), `operation`(select), `separator` | `result`, `count`, `operation` |
| `set_output` | `outputs`(json) | the keys you set |
| `fail` | `message`(template), `error_code` | `error_code`, `message` |
| `wait_until` (`event_driven`) | `duration_hours`, `until_time`, `timezone` | - |

`wait_until` has its **own** `timezone` parameter, and unlike the schedule's it is
a real action parameter. Do not confuse the two.

### Integration (both `sync`)

| Action | Parameters | Outputs |
|---|---|---|
| `http_request` | `url`*, `method`(GET/POST/PUT/PATCH/DELETE), `headers`(json), `body`(json), `timeout_seconds`(1-300, default 30), `include_metadata`(bool) | `status_code`, `body`, `headers` |
| `notification` | `channel`*(email/slack/teams/webhook), `title`, `message`*(template), `severity`(info/warning/error/critical), `recipients`(json), `metadata`(json) | `sent`, `notification_id` |

`http_request` **blocks private, loopback and link-local destinations** - it can
only reach genuinely public URLs. That is what makes it usable as a bridge to the
public inference gateway and unusable as a bridge to anything internal.
`include_metadata: true` appends the execution's metadata to the request body.

### Model, cluster, deployment and simulation

| Action | Mode | Required parameters | Notable outputs |
|---|---|---|---|
| `model_add` | event_driven | `model_uri` (+ `model_source`, `credential_id`, `model_name`, `max_wait_seconds`) | `model_id`, `workflow_id`, `status` |
| `cloud_model_add` | sync | `provider_id`, `model_name` (+ `cloud_model_id`, `model_uri`) | `model_id`, `workflow_id` |
| `model_delete` | sync | `model_id` (+ `force`) | `success`, `model_id` |
| `model_benchmark` | event_driven | `model_id`, `cluster_id` (+ concurrency/token/hardware knobs) | `benchmark_id`, `results` |
| `cluster_health` | sync | `cluster_id` (+ `checks`, `timeout_seconds`) | `healthy`, `status`, `issues`, `details` |
| `deployment_create` | event_driven | `model_id`, `project_id`, `endpoint_name`, `concurrent_requests`, `avg_sequence_length`, `avg_context_length` (+ cluster, SLO ranges) | `endpoint_id`, `endpoint_url`, `workflow_id` |
| `deployment_delete` | event_driven | `endpoint_id` (+ `force`, `max_wait_seconds`) | `endpoint_id`, `status` |
| `deployment_scale` | sync | `endpoint_id`, `target_replicas` | previous replica bounds, `scaling_strategy` |
| `deployment_ratelimit` | sync | `endpoint_id` (+ `algorithm`, per-second/minute/hour, `burst_size`, `enabled`) | `config` |
| `simulation_run` | event_driven | `model_id`, `input_tokens`, `output_tokens`, `concurrency`, `target_ttft`, `target_throughput_per_user`, `target_e2e_latency` | `recommendations`, `top_recommendation`, `metrics` |

`event_driven` actions hand off to a long-running platform operation and park the
step (`awaiting_event: true`, `external_workflow_id` set) until it completes or
its `timeout_at` passes. Those steps take minutes to hours; budget the DAG's
`settings.timeout_seconds` accordingly.

**There is no agent, prompt or LLM action.** The only way a pipeline talks to a
model or agent is an `http_request` step against the public inference gateway.

## 4. Starting a pipeline

### On demand

`POST /budpipeline/{id}/execute` with `{params, callback_topics, payload_type,
notification_workflow_id}`. Runs **inline**, cut off at ~30s by the proxy - a
longer DAG returns `500 Failed to execute pipeline` while the execution
continues. Recover with
`GET /budpipeline/executions?workflow_id=<pipeline id>&page_size=5`.

`POST /budpipeline/run` with `{pipeline_definition, params, ...}` runs an
unsaved DAG. The execution is persisted but with no pipeline id, so it never
appears under a `workflow_id` filter - keep the returned `execution_id`.

### On a clock

`POST /budpipeline/schedules` - see section 1.

### On an internal platform event

`POST /budpipeline/event-triggers`:

```json
{"workflow_id":"<pipeline id>","name":"Benchmark new models",
 "config":{"event_type":"model.onboarded","filters":{"result.status":"success"}},
 "params":{},"enabled":true}
```

The allowlist is fixed - anything else is `400 Unsupported event type`:

`model.onboarded`, `model.deleted`, `benchmark.completed`, `benchmark.failed`,
`cluster.healthy`, `cluster.unhealthy`, `deployment.created`,
`deployment.failed`.

`filters` are exact-equality on dot-notation paths into the event data. No
operators, no wildcards, no ranges. Execution `params` = the trigger's `params`
merged with the event data.

`GET /budpipeline/event-triggers` returns a **bare array**. There is no
single-item GET or PUT proxied - to change one, delete and recreate.

### On an inbound HTTP call

`POST /budpipeline/webhooks`:

```json
{"workflow_id":"<pipeline id>","name":"CI hook",
 "config":{"require_secret":true,"allowed_ips":null,"headers_to_include":["X-Commit-Sha"]},
 "params":{"source":"ci"},"enabled":true}
```

The response carries `secret` **once**. `allowed_ips` is exact-string matching -
no CIDR. `headers_to_include` copies those request headers into the execution
params. Effective params are: the webhook's `params`, overlaid by the request
body's `params`, overlaid by the copied headers, plus a `_trigger` block.

`endpoint_url` in the response is derived from the engine's own view of its base
URL and is generally **not** the externally reachable address. Confirm the real
path with whoever runs the installation's ingress.

`POST /budpipeline/webhooks/{id}/rotate-secret` returns a new secret and kills
the old one immediately. `GET /budpipeline/webhooks` is a bare array; there is
no single-item GET or PUT.

### Provenance

Whatever started an execution is recorded in `params._trigger.type`:
`manual`, `scheduled`, `webhook`, `event`, `api`, `chain`. A schedule's manual
`/trigger` call additionally sets `_trigger.manual: true`.

## 5. Watching an execution

`GET /budpipeline/executions/{id}/progress`:

```json
{"execution": {"id":"...","status":"COMPLETED","progress_percentage":"100.00",
               "final_outputs":null,"error_info":null},
 "steps": [{"step_id":"step_a","status":"COMPLETED","progress_percentage":"100.00",
            "awaiting_event":false,"external_workflow_id":null,"error_message":null}],
 "recent_events": [],
 "aggregated_progress": {"overall_progress":"100.00","eta_seconds":null,
                         "completed_steps":2,"total_steps":2,"current_step":null}}
```

The event feed key is `recent_events` - there is no `events` key - and it can
legitimately be empty on a fast DAG. Progress is monotonic and weighted across
concurrent steps, so it never goes backwards.

| Enum | Values | Where |
|---|---|---|
| Execution status (list/detail, and the `status=` filter) | `PENDING` `RUNNING` `COMPLETED` `FAILED` `INTERRUPTED` | uppercase |
| Execution status (the `execute`/`run` response) | `pending` `running` `completed` `failed` `cancelled` `timeout` | lowercase |
| Step status | `PENDING` `RUNNING` `COMPLETED` `FAILED` `SKIPPED` `RETRYING` `TIMEOUT` | uppercase |
| Pipeline status | `draft` `active` `archived` | lowercase |

The id field is `execution_id` in the execute response and `id` in list and
detail rows. `error_info` on a failure carries
`{total_steps, failed_steps, failed_step_names}`.

Filters on `GET /budpipeline/executions`: `workflow_id`, `status` (uppercase),
`initiator` (a user id), `start_date`, `end_date`, `page`, `page_size` (1-100).
Pagination is reported under `.pagination`, not `total_record`.

Passing `callback_topics` at kickoff publishes `execution.started`,
`execution.progress`, `step.started`, `step.progress`, `step.completed` and
`execution.completed` to topics you name - useful only if you already consume the
installation's internal event bus.

## 6. Operational hazards

- **Executions are not scoped to the caller.** Any authenticated user can list
  every execution on the installation, including other users' `params` and full
  pipeline definitions. Never put a secret in `params`.
- **Schedules, webhooks and platform event triggers have no owner.** They are
  created without the caller's identity, so anyone can list, pause, trigger or
  delete anyone's. Pipelines themselves *are* scoped to their creator.
- **Deleting a pipeline leaves its schedules, webhooks and event triggers
  behind.** They keep firing and fail at execute time. Delete them first.
- **Schedules, webhooks and event triggers are stored separately from pipelines**,
  in a key-value store rather than the main database. If that store is reset,
  every schedule disappears silently while the pipelines survive. After any
  platform incident, re-check `GET /budpipeline/schedules` before assuming a
  routine is still armed.
- **The DELETE proxies return HTTP 204 with a two-byte `{}` body.** Strict HTTP
  clients may object; the toolkit does not.
- Several engine routes are not exposed: schedule next-run preview, single-item
  GET/PUT for webhooks and event triggers, the event-type discovery route, and
  `/executions/{id}/steps` and `/events`. The `/progress` endpoint is the only
  step-level view available.
