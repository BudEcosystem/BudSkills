# Operating agents: runs, approvals, escalations, channels

Everything here is the management API (`bud api`) and applies to **durable
runs** - the ones started with `background: true` (see `running-agents.md`) or
by a trigger or channel message. A synchronous call has no run to supervise.

Throughout, `session_id` is the `resp_*` id the run was created with.

## The run console

```bash
bud api GET /runs -q project_id=$BUD_PROJECT_ID \
  | jq -r '.items[] | "\(.session_id)  \(.status)  \(.suspend_reason // "-")  \(.expires_at)"'

bud api GET /runs -q project_id=$BUD_PROJECT_ID -q status=in_progress
bud api GET /runs -q project_id=$BUD_PROJECT_ID -q prompt_id=<agent-uuid>
```

`project_id` is required. Paging is cursor-based, not page numbers: pass the
previous response's `next_cursor` back as `cursor`; `null` means the end.

> If `/runs` and `/approvals` both return **502** complaining about a missing
> operator token, the run console is not enabled on that installation - it is a
> configuration gap, not your call. Durable runs still execute and can still be
> read from the gateway (`GET /v1/responses/{id}` with `x-model-name`); you just
> cannot supervise them. Say so rather than retrying.

```bash
bud api GET /runs -q project_id=$BUD_PROJECT_ID -q cursor=<next_cursor>
```

One run in full - read this before building any approve or steer call, because
it carries the fields those calls need:

```bash
bud api GET /runs/<session-id> | jq '.state'
```

`state` includes `status`, `suspend_reason`, `awaited_token`, `pending_call`,
`wake_at`, `expires_at`, `escalation_tier`, `escalation_policy`,
`escalation_trail`, `run_policy`, and running totals
(`cum_input_tokens`, `cum_output_tokens`, `cum_cost_micros`, `cum_tool_calls`).
It is an untyped passthrough, so new fields can appear - do not assume the list
is closed.

History:

```bash
bud api GET /runs/<session-id>/timeline \
  | jq -r '.items[] | "\(.ts)  \(.kind)"'
```

Entry kinds are `initiate`, `signal`, `resume`, `escalation_tier` and
`terminal`. This is the run-level story; for model-level detail (spans, tokens,
latency per step) use traces - `bud-observability`.

## Human-in-the-loop tool approvals

When a run reaches a tool marked `require_approval: always` it parks with
`status: awaiting_approval` and `suspend_reason: awaiting-approval` and waits
for a person.

```bash
# what is waiting for me
bud api GET /approvals -q project_id=$BUD_PROJECT_ID \
  | jq -r '.items[] | "\(.session_id)  \(.pending_call.tool)  expires=\(.expires_at)"'

# see exactly what it wants to do
bud api GET /approvals -q project_id=$BUD_PROJECT_ID | jq '.items[0].pending_call'
```

`pending_call` is `{call_id, tool, args}`. **Read `args` before approving** and
show them to the user - this is the whole point of the gate.

```bash
# approve as-is
bud api POST /approvals/<session-id>/approve -d '{"call_id":"<call_id>","message":"ok"}'

# approve with edited arguments
bud api POST /approvals/<session-id>/approve -d '{
  "call_id":"<call_id>", "override_args":{"recipient":"finance@example.com"}, "message":"narrowed scope"
}'

# refuse but let the run continue (the model is told it was refused)
bud api POST /approvals/<session-id>/reject -d '{"call_id":"<call_id>","reason":"not permitted"}'

# refuse and end the run
bud api POST /approvals/<session-id>/reject -d '{"call_id":"<call_id>","on_reject":"abort"}'
```

All four return `202` - the signal has been forwarded, the run has not resumed
yet. Confirm:

```bash
bud wait resource /runs/<session-id> --field state.status \
  --equals completed,incomplete,failed,cancelled --fail-on failed --timeout 900
```

Rules that matter:

- **Never construct `awaited_token` yourself.** The platform reads the live
  token and compares it; a stale one is a `409`. On 409, re-read
  `/runs/{id}` and retry - it means the run moved on.
- Approve and reject are **idempotent** (the signal id is derived from the call
  id), so a double-click is harmless.
- Who may approve comes from the run's `run_policy.approvers`, which accepts a
  user id, `user:<id>`, `approver:<id>`, `operator:<id>`, an email, or `*`.
  Administrators see everything as a break-glass.
- **A run with no `run_policy` is invisible in `/approvals` to non-administrators**
  even though it is genuinely parked. It will hang until its deadline and only
  an administrator can clear it. If a user reports "my run is stuck waiting for
  approval but there is nothing in my inbox", this is why - check
  `GET /runs/{id}` directly.
- If `expires_at` passes before anyone acts, the run is terminated as
  `incomplete (deadline_exceeded)`. Approvals are time-boxed; say so when you
  hand one to a human.

## Steering and stopping a run

```bash
# nudge a running agent with new context
bud api POST /runs/<session-id>/inject -d '{"text":"the customer changed the address to 4 High St"}'

# stop it: finish the current turn first
bud api POST /runs/<session-id>/abort -d '{"mode":"after_turn"}'

# stop it at the next checkpoint
bud api POST /runs/<session-id>/abort -d '{"mode":"immediate"}'
```

Both return 202. `after_turn` is the default and the polite one - the current
model turn completes, so partial work is not lost. `immediate` tears down at the
next checkpoint and leaves `status: cancelled`.

> **`inject` and `abort` are not idempotent.** Each call mints a fresh signal,
> so a retried inject applies the context delta **twice**. If a call times out,
> read `/runs/{id}/timeline` to see whether the signal landed before retrying.

Permission: `endpoint:manage` plus project membership, and if the run carries a
`run_policy` with `steerers` / `aborters` rosters, you must be on them.

## Governance escalations

The second, policy-driven human gate. Where a tool approval is configured on the
tool, an escalation is raised by a governance policy bound to the agent
(`bud-guardrails` owns the policies). The run sits at `requires_action`.

```bash
bud api GET /escalations -q project_id=$BUD_PROJECT_ID -q status=pending \
  | jq -r '.escalations[] | "\(.id)  \(.kind)  \(.hook)  \(.approvals_count)/\(.min_approvals)  can_resolve=\(.can_resolve)"'
```

Check `can_resolve` and `allowed_resolutions` on the row before acting - not
every escalation is yours to clear.

```bash
bud api POST /escalations/<escalation-id>/resolve -d '{
  "resolution": "allow_once", "comment": "verified with the requester"
}'
```

| Resolution | Meaning |
|---|---|
| `allow_once` | permit this occurrence only |
| `allow_always` | permit and mint a **standing grant** for future runs |
| `deny` | refuse |
| `ack` / `decline` | only valid on an acknowledgement-style gate (`kind: warn_ack`) |

`allow_always` is not accepted on a `warn_ack` gate. `edited_args` may be passed
alongside a resolution, but it is not schema-checked until the tool actually
runs - a malformed edit fails later, inside the run.

If `min_approvals > 1`, each approver resolves separately until
`approvals_count` reaches it; the run stays parked until then.

Standing grants outlive the run - audit and revoke them:

```bash
bud api GET    /escalations/standing -q project_id=$BUD_PROJECT_ID
bud api DELETE /escalations/standing/<grant-id>
```

An awareness feed for governed runs that need no action:

```bash
bud api GET /escalations/notifications -q project_id=$BUD_PROJECT_ID
```

**Foreground runs are different.** A synchronous call that a policy escalates
comes back as `200` with `status: "requires_action"` (or `403` if denied).
After the escalation is resolved, re-submit the call rather than waiting:

```bash
curl -sS "$BUD_GATEWAY/v1/responses" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":{"id":"hr-assistant"},"input":"...","previous_response_id":"resp_...",
       "governance_approval":{"decision":"approve"}}'
```

Killing a governed run outright:

```bash
bud api POST /governance/runs/<execution-id>/override -d '{"action":"kill","reason":"..."}'
```

Which policies apply to an agent, and binding one:

```bash
bud api GET  /guardrails/agents/<agent-uuid>/governance-policies
bud api POST /guardrails/governance-policies/<policy-id>/bind-agent -d '{"prompt_id":"<agent-uuid>"}'
bud api PATCH  /guardrails/governance-policies/bindings/<binding-id> -d '{"mode":"shadow"}'
bud api DELETE /guardrails/governance-policies/bindings/<binding-id>
```

Bindings can be pinned to a version or deployment, and run in `active`,
`shadow` (evaluate and record, do not block) or `off`. **Bind in `shadow`
first** on anything already carrying traffic - see `bud-guardrails`.

## Chat-channel bots

Give an agent a presence in Slack, Teams or Workvivo. Each thread becomes a
keyed conversation, so the agent keeps context per thread.

```bash
# which providers are conversational, and what credentials they need
bud api GET /prompts/trigger-providers \
  | jq '.providers[] | select(.conversational) | {provider, credentials}'

# optional: the event types a provider can emit
bud api GET /prompts/event-catalog -q descriptor_ref=slack

# provision
bud api POST /prompts/hr-assistant/connectors/<connector-id>/triggers -d '{
  "descriptor_ref": "slack",
  "credentials": {"signing_secret":"...","bot_token":"..."},
  "event_types": [],
  "version": 1
}' | jq '.trigger'
# -> {subscription_id, connector_id, trigger_type, event_types, gateway_id, webhook_url}

bud api GET    /prompts/hr-assistant/triggers -q version=1
bud api DELETE /prompts/hr-assistant/connectors/<connector-id>/triggers -q version=1
```

- `descriptor_ref` must be `slack`, `teams` or `workvivo`. Anything else is
  rejected with a pointer to `POST /triggers` - fire-and-forget event triggers
  are a different feature, owned by `bud-routines`.
- `event_types: []` is **correct** for conversational providers; the platform
  derives message and mention events itself.
- Missing required credential keys fail closed with a 400. Read the provider's
  `credentials` field spec first; each entry says whether it is `required` and
  whether it is a secret.
- The last step is manual: paste the returned `webhook_url` into the provider
  app's event-subscription settings. Tell the user this explicitly - until they
  do it, the bot is silent and nothing in Bud looks wrong.

Bots are bound to a **version**, so a new version needs its bot re-provisioned
(or provision against the version you promote).

## Seeing what an agent actually did

```bash
bud api GET /prompts/hr-assistant/traces -q project_id=$BUD_PROJECT_ID \
  -q from_date=2026-08-01T00:00:00Z -q to_date=2026-08-06T00:00:00Z -q page=1 -q limit=50

bud api GET /prompts/hr-assistant/traces/<trace-id> -q project_id=$BUD_PROJECT_ID
```

> The path parameter here is the agent **name**. A UUID 404s.

Add `flatten=true` to get every span rather than root spans. Bodies captured
from streamed runs can be raw event text rather than JSON, so parse defensively.
For anything beyond a quick look - cost, latency distributions, error rates,
cross-agent comparison - use `bud-observability`.

## When an agent misbehaves: what to check, in order

1. **Is it the right version?** `GET /prompts/{id}/versions` -
   `is_default_version`. Callers that omit `prompt.version` get the default.
2. **Is the config what you think?** `GET /prompts/prompt-config/{name}?version=N`
   and check `deployment_name` is non-empty. An empty config means the read
   failed, not that the agent is unconfigured.
3. **Did a recent edit land?** Right after a save, a run can still see the old
   config for a few minutes on multi-replica installations. Re-test before
   re-editing.
4. **Is the deployment healthy?** The agent list's `status` column is the
   deployment's status. A `404 Prompt not found` at the gateway is a key-map
   problem; a 5xx is usually the deployment (`bud-deployments`).
5. **Is it parked rather than broken?** `GET /runs/{session_id}` -
   `suspend_reason`. `awaiting-approval` needs a human, `awaiting-join` and
   `awaiting-callback` need whatever they are waiting on.
6. **Did a policy stop it?** `GET /escalations -q project_id=... -q status=pending`,
   and check the run's `escalation_trail`.
7. **What did the model actually see?** The trace spans carry the rendered
   prompt - the usual culprit is a Jinja variable that was never supplied, which
   renders empty rather than erroring.
