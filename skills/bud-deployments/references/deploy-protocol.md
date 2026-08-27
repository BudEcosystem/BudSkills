# The deploy session, field by field

`POST /models/deploy-workflow` is called repeatedly. Each call upserts a step
record; the trigger reads **every** step, so a field set at step 1 still counts
at step 8. Step numbers are yours to choose - they order and label the records,
they are not a state machine you must satisfy exactly.

## Request fields

```jsonc
{
  "workflow_id":  "uuid|null",        // every call after the first
  "workflow_total_steps": 8,          // FIRST call only - mutually exclusive with workflow_id
  "step_number":  1,                  // required, > 0
  "trigger_workflow": false,          // true exactly once, on the last call

  "model_id":     "uuid",             // required
  "project_id":   "uuid",             // required
  "endpoint_name":"string",           // required; 1-100 chars, [A-Za-z0-9-]
  "deploy_config": { ... },           // required; see below. SEND ONCE ONLY
  "cluster_id":   "uuid",             // required for self-hosted; = recommendation.cluster_id
  "credential_id":"uuid",             // required for hosted models

  "hardware_mode": "dedicated|shared",
  "template_id":  "uuid|null",        // validated then discarded
  "budaiscaler_specification": { ... },
  "enable_tool_calling": true,
  "enable_reasoning": false,
  "tool_calling_parser_type": "string|null",   // copy from the chosen recommendation
  "reasoning_parser_type":    "string|null",
  "chat_template":            "string|null",
  "supports_lora": true,
  "supports_pipeline_parallelism": true,
  "callback_topic": "string|null",
  "simulator_id":  "uuid|null"        // reuse an earlier plan and skip re-planning
}
```

`deploy_config`:

| Field | Type | Range | Unit |
|---|---|---|---|
| `concurrent_requests` | int | `> 0` | simultaneous users |
| `avg_context_length` | int | `0..200000` | input tokens |
| `avg_sequence_length` | int | `>= 0` | output tokens |
| `ttft` | `[int,int]` | each `1..60000` | **milliseconds** |
| `e2e_latency` | `[int,int]` | each `0..600` | **seconds** |
| `per_session_tokens_per_sec` | `[int,int]` | each `0..1000` | tokens/s per user |

## Required at trigger time

Checked across all steps; a miss returns `400 Missing required data: <list>`.

- **Self-hosted:** `model_id`, `project_id`, `endpoint_name`, `deploy_config`,
  `cluster_id`, and the planning result (set automatically when planning ran).
- **Hosted:** `model_id`, `project_id`, `endpoint_name`, `deploy_config`,
  `credential_id`.

## Ordering rules that actually matter

1. `workflow_id` XOR `workflow_total_steps`. Both or neither is a 422.
2. `deploy_config` triggers planning **every time it appears**. Send it once.
3. Planning writes its result to `step_number + 1` and advances the session's
   `current_step`. Your `step_number` and the server's `current_step` will not
   match - do not assert on that.
4. The name is checked for global uniqueness twice: when you set it, and again
   at trigger. A race can pass the first and fail the second.
5. `hardware_mode`, `template_id`, `supports_lora` and
   `supports_pipeline_parallelism` are stored but not forwarded to the rollout.
   `hardware_mode` influences planning only; `template_id` is discarded.
6. `avg_context_length` is silently capped at the model's real maximum before
   rollout - but planning sees your uncapped value, so an unrealistic figure
   distorts the estimate without erroring.

## Recovering an interrupted session

```bash
bud jobs --limit 30 | jq '.[] | select(.kind=="model_deployment")'
bud job show <workflow-id> --data     # everything collected so far
```

Then continue: `POST /models/deploy-workflow` with the same `workflow_id`, the
next `step_number`, and only the fields still missing.

## Cancelling

```bash
bud api POST /models/cancel-deployment -d '{"workflow_id":"<id>"}'
```

Only works after the trigger fired. Before that, the session holds no running
work - just abandon it, or `DELETE /workflows/{id}` to tidy up (you may only
delete sessions you created, and only while in progress).

## Closing a finished session

```bash
bud api PATCH /workflows/<workflow-id>
```

Marks the session complete. Purely bookkeeping - the deployment works either
way - but it keeps the job list meaningful for everyone. Only the creator can.
