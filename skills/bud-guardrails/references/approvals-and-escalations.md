# Approvals, escalations and the run kill switch

When a governance rule returns `require_approval`, the agent run **suspends**
and an escalation appears in the inbox. When a rule returns `notify`, the run
continues and a notification record is written instead. Both are raised by the
agent runtime, never by a call you make.

## The inbox

```bash
# what is waiting for me
bud api GET /escalations -q status=pending -q target=me -q limit=50

# everything pending in a project (project_id is REQUIRED with target=all)
bud api GET /escalations -q target=all -q project_id=$BUD_PROJECT_ID -q status=pending

# one agent's gates
bud api GET /escalations -q target=all -q project_id=$BUD_PROJECT_ID \
  -q prompt_id=<agent-uuid> -q status=pending
```

Other filters: `kind` (default `escalation`; `notify` returns empty here - use
the notifications endpoint), `user_id`, `group_id`, `prompt_version`,
`session_id`, `page`, `limit` (1..100), `order`.

Fields that decide what you do next:

| Field | Use |
|---|---|
| `can_resolve` | whether **you** may act on this item |
| `allowed_resolutions[]` | which resolutions are legal for you on this item |
| `min_approvals` vs `approvals_count` | how many more votes are needed |
| `current_tier` | which approver tier is active |
| `expires_at` | when the gate auto-resolves per `on_expiry` |
| `self_approval` | whether the invoker is allowed to vote |
| `args_preview` | the tool call being gated |
| `subject`, `reason`, `guidance`, `rule_name`, `policy_name` | what to tell a human |
| `execution_id` | the run - what you would kill |
| `hook`, `kind` | `escalate` (approval gate) or `warn_ack` (acknowledge-only) |

> **An empty list is ambiguous.** `target=all` without `project_id`, or with a
> project you cannot see, returns an empty 200 - never a 403. Same for
> notifications. Confirm project membership before reporting "nothing pending".
>
> The inbox is also served through the agent runtime. If that runtime is
> unavailable the list endpoints return an **empty 200** rather than an error.
> Two consecutive empty results where you expect items is a reason to check
> `bud health`, not a reason to conclude the queue is clear.

## Resolving

```bash
bud api POST /escalations/<escalation-id>/resolve -d '{
  "resolution":"allow_once","comment":"reviewed with the requester"}'
```

| Resolution | Effect |
|---|---|
| `deny` | reject; the run continues down the deny path |
| `allow_once` | permit this occurrence only |
| `allow_always` | permit and create a **standing grant** that auto-approves matching future gates in that session |
| `ack` | acknowledge a `warn_ack` gate (single-actor) |
| `decline` | decline a `warn_ack` gate (single-actor) |

You may also edit the pending tool call before allowing it:

```bash
bud api POST /escalations/<id>/resolve -d '{
  "resolution":"allow_once","edited_args":{"target":"staging"}}'
```

The response carries `execution_signal: "sent"` once the suspended run has
actually been signalled. Anything else means the run was not resumed.

### Rules that will trip you up

- The request body **forbids unknown fields** - a stray key is a 422. Only
  `resolution`, `comment`, `edited_args`.
- Machine-readable error codes land in the response's **`param`** field, not in
  `message`. Branch on `param`.
- `allow_always` is **not** available on a `warn_ack` gate.
- **maker ≠ checker is never bypassable.** `distinct_approvers` is checked
  first and no permission overrides it - not even `governance:escalation:
  resolve_any`. The only exception is the invoker's own vote when the binding
  set `include_invoker: true`. It also fails **closed** when the runtime did not
  record who started the run.

| `param` | Status | Meaning |
|---|---|---|
| `not_an_authorized_approver` | 403 | you are not on the target |
| `maker_checker_violation` | 403 | you triggered this gate |
| `wrong_tier` | 403 | an earlier tier must vote first |
| `escalation_not_found` | 404 | unknown id |
| `escalation_not_pending` | 409 | already resolved or expired |
| `already_recorded_for_user` | 409 | you already voted |
| `invalid_resolution` | 422 | not in `allowed_resolutions` |
| `sticky_approval_disabled` | 422 | `allow_always` on a binding with `sticky_approval: false` |
| `edit_*` | 422 | `edited_args` rejected |

## Standing grants

`allow_always` creates a grant that silently approves matching gates for the
rest of the session. That is a real widening of what the agent may do - list
and revoke them deliberately.

```bash
bud api GET /escalations/standing -q project_id=$BUD_PROJECT_ID -q prompt_id=<agent-uuid>
bud api GET /escalations/standing -q session_id=<session> -q include_expired=true
bud api DELETE /escalations/standing/<grant-id>
```

Each grant carries `escalation_key`, `policy_name`, `rule_name`, `subject`,
`granted_by_name`, `granted_at`, `expires_at`, `revoked_at`.

With **neither** `session_id` nor `project_id` the call is a 422 with
`param: "scope_required"` unless you hold `governance:escalation:read_any`.

Revoking is allowed for the granter, a current member of the originating
escalation target, or a holder of `governance:escalation:resolve_any`.
Otherwise 403 `param: "not_authorized_to_revoke"`.

## Notifications (the `notify` verdict)

Runs that proceeded but left a review record:

```bash
bud api GET /escalations/notifications -q project_id=$BUD_PROJECT_ID \
  -q prompt_id=<agent-uuid> -q acknowledged=false -q limit=50
```

Filters: `prompt_version`, `session_id`, `severity`, `hook`, `acknowledged`,
`page`, `limit` (1..100). `project_id` is required in practice - without it you
get an empty 200, never a global dump.

Each record carries `policy_name`, `rule_name`, `agent_name`, `hook`,
`severity`, `subject`, `guidance`, `reason`, `execution_id`,
`execution_status`, `acknowledged_at`, `override`, `superseded_by`. This is the
right surface for "what has this policy been flagging?" when you do not want
blocking - pair it with a binding in `mode: "shadow"`.

## Permissions

| Action | Needs |
|---|---|
| list / resolve your own items | `endpoint:view` |
| list with an explicit `user_id` or `group_id` | `governance:escalation:read_any` (else 403) |
| list standing grants unscoped | `governance:escalation:read_any` (else 422) |
| revoke someone else's grant | `governance:escalation:resolve_any` |
| kill a run | superuser, `governance:escalation:resolve_any`, or project manage |

## Killing a run

```bash
bud api POST /governance/runs/<execution-id>/override -d '{
  "action":"kill","reason":"runaway tool loop"}'
```

- `action` accepts only `"kill"`, and the body forbids extra fields.
- 202 means applied. 404 `param: "run_not_found"` means the runtime has no such
  run - it already finished, or the id is wrong.
- **Every attempt is audited, including refused ones.**

Get `execution_id` from an escalation, a notification record, or the trace view
in `bud-observability`.

This aborts work in progress and is not undoable. If the intent is "stop this
agent doing more from now on" rather than "abort this run", set the binding to
`mode: "off"` (or `shadow`) instead - see `references/agent-governance.md`.

## Resolving an escalation target to people

Occasionally useful when you want to know who *would* be asked before you
create a binding:

```bash
bud api POST /governance/escalations/resolve-target -d '{
  "escalation_target":{"type":"users","refs":["<uuid>"],"include_invoker":false},
  "prompt_id":"<agent>","project_id":"'"$BUD_PROJECT_ID"'"}'
```

Returns `user_ids[]`. `self` resolves to the agent owner (plus the run invoker
when applicable); `group` currently resolves to an empty list. **An empty
`user_ids` is a valid response, not an error** - and it means nobody would be
able to approve.
