# Agent runs, approvals and escalations: full reference

What a trigger (or any other agent invocation) actually produces, how to watch
it, and how a human unblocks or stops it.

**Two different queues sit over the same runs.** A blocked agent may be in
either, both, or neither - always check both before telling a user nothing is
waiting:

| Queue | Raised by | Parks the run because | Resolved at |
|---|---|---|---|
| `/approvals` | the execution engine | a tool call needs sign-off (`suspend_reason: "awaiting-approval"`) | `/approvals/{sid}/approve` \| `/reject` |
| `/escalations` | a safety policy (see `bud-guardrails`) | a policy rule demanded a decision | `/escalations/{id}/resolve` |

## 1. Reading runs

```bash
bud api GET /runs -q project_id=<PID> -q prompt_id=<AGENT_ID> -q status=running
bud api GET /runs/<SESSION_ID>
bud api GET /runs/<SESSION_ID>/timeline
```

- `project_id` is **required**; `prompt_id` and `status` are optional filters.
- Pagination is keyset: pass `cursor=<the previous page's created_at>` and stop
  when `next_cursor` is null.
- List items are pass-through dictionaries - `session_id`, `prompt_id`,
  `project_id`, `status`, `suspend_reason`, `created_at`, `wake_at`,
  `expires_at` and whatever the engine adds. Do not assume a fixed set.
- `GET /runs/{id}` nests under `.state`: status, `suspend_reason`,
  `pending_call`, `run_policy`, `escalation_tier`, counters and an
  `awaited_token`. You never send `awaited_token` yourself - the platform
  re-reads it when you approve.
- `/runs/{id}/timeline` returns `.items[]`, the step-by-step record of what the
  agent did. For cost, latency and trace analysis across many runs, use
  `bud-observability` instead - this is a single run's audit.

Membership of the run's project is enforced on every one of these, so guessing
another tenant's `session_id` fails.

> **502 "operator token is not configured"** on `/runs` or `/approvals` means the
> durable-run plane is not wired on that installation. Nothing is broken about
> your call and retrying will not help - report it rather than concluding there
> are no runs.

## 2. Approving a parked tool call

```bash
bud api GET /approvals -q project_id=<PID> \
  | jq '.items[] | {session_id, call_id: .pending_call.call_id,
                    tool: .pending_call.tool, args: .pending_call.args,
                    expires_at, escalation_tier}'
```

Then inspect before deciding (`GET /runs/{sid}` and the timeline), and act:

```bash
# approve as-is
bud api POST /approvals/<SID>/approve -d '{"call_id":"<call_id>","message":"ok"}'

# approve but edit the tool's arguments first
bud api POST /approvals/<SID>/approve -d '{"call_id":"<call_id>",
      "override_args":{"limit":10},"message":"reduced scope"}'

# deny this call, let the run continue (the model is told it was refused)
bud api POST /approvals/<SID>/reject -d '{"call_id":"<call_id>","reason":"unsafe","on_reject":"deny"}'

# deny and end the run
bud api POST /approvals/<SID>/reject -d '{"call_id":"<call_id>","reason":"unsafe","on_reject":"abort"}'
```

`call_id` is required and must come from that run's `pending_call`.

### Confirming it worked

All of these return **202, which means the signal was forwarded, not applied**.
Always re-read:

```bash
bud api GET /runs/<SID> | jq '.state | {status, suspend_reason}'
```

A **409** on approve means the run moved underneath you (the guard token
changed). Re-read `/approvals` and retry once with the fresh state - do not
retry blindly.

Approve and reject are **idempotent**: their signal id is derived from
(session, kind, call id), so a double submission collapses into one.

### Who may approve

Eligibility comes from the run's `run_policy.approvers`, and the accepted forms
are loose - a bare user id, `approver:<id>`, `operator:<id>`, `user:<id>`, the
caller's email address, or `*` as a wildcard. A roster written in an unexpected
format makes the gate unapprovable by anyone but an administrator.

Two visibility consequences worth stating to a user:

- **A run with no `run_policy` is skipped entirely for non-administrators** - the
  gate exists but nobody except an administrator can see it. Administrators
  always see everything as break-glass.
- A non-member gets an **empty 200**, not a 403. An empty queue is not proof that
  nothing is waiting; confirm your project membership first.

`GET /approvals` fetches full run state for every parked run in the project, so
it is slow and brittle on a large backlog, and a single failure fails the whole
listing. Filter with `namespace` when the project has many.

## 3. Steering and stopping a run

```bash
bud api POST /runs/<SID>/inject -d '{"text":"the customer already refunded this"}'
bud api POST /runs/<SID>/abort  -d '{"mode":"after_turn"}'    # or "immediate"
```

- `inject` adds context mid-run. It is **not idempotent** - each call gets a
  fresh signal id, so a retry injects the text twice. If it appears to fail,
  re-read the run before retrying.
- `abort` with `after_turn` (default) lets the current turn finish; `immediate`
  tears down at the next checkpoint.
- Both need endpoint-manage permission, project membership, **and** eligibility
  under the run's `run_policy` (`steerers` for inject, `aborters` for abort). An
  absent roster means any project member qualifies.

Signal kinds are fixed: `resume_awaited_result` (approve / deny),
`inject_context`, `control_abort`. The `signal_id` you get back is an
idempotency key, not a handle you can query.

## 4. The governance escalation inbox

Policy-driven decisions, distinct from engine-level tool gating.

```bash
# your personal queue
bud api GET /escalations -q status=pending -q target=me -q page=1 -q limit=20

# everything for one agent (project_id is mandatory when target=all)
bud api GET /escalations -q target=all -q project_id=<PID> -q prompt_id=<AGENT_ID>
```

Each row carries `policy_name`, `rule_name`, `hook`, `kind`, `subject`,
`reason`, `guidance`, `args_preview`, `min_approvals`, `approvals_count`,
`expires_at`, plus **`can_resolve`** and **`allowed_resolutions[]`**.

> Only act on rows where `can_resolve` is true, and only with a value taken from
> that row's own `allowed_resolutions`.

```bash
bud api POST /escalations/<ESCALATION_ID>/resolve -d '{
  "resolution":"allow_once", "comment":"verified with the requester"}'
```

| `resolution` | Meaning |
|---|---|
| `deny` | refuse this action |
| `allow_once` | permit this occurrence only |
| `allow_always` | permit and record a standing grant |
| `ack` / `decline` | the single-actor answers for a `warn_ack` row |

`edited_args` may accompany a resolution to change the tool's arguments; it is
validated at execution time, not at resolve time.

Rules that bite:

- The body is **strict** - any key other than `resolution`, `comment` and
  `edited_args` is a 422, not a silent ignore.
- `allow_always` cannot be used on a `kind: "warn_ack"` row; use `ack` or
  `decline`.
- **Maker is not checker.** A row where `self_approval` is true may be refused
  with 403 `maker_checker_violation`.
- **Quorum**: nothing happens until `approvals_count` reaches `min_approvals`.
  Until then the row stays `pending` with your vote recorded, and a second
  attempt by the same user is 409 `already_recorded_for_user`.
- Other relayed errors: 403 `not_an_authorized_approver` / `wrong_tier`; 409
  `escalation_not_pending`; 422 `invalid_resolution` / `sticky_approval_disabled`
  / edit errors.
- **Every attempt, including refused ones, is written to the audit trail**
  (`bud-projects` covers reading it).

The response carries the updated escalation plus `execution_signal: "sent"` -
again, confirm by re-listing `/escalations?status=pending` and by re-reading the
run.

### Notifications (non-blocking notices)

`GET /escalations?kind=notify` **always returns an empty list**. Use:

```bash
bud api GET /escalations/notifications -q project_id=<PID> -q prompt_id=<AGENT_ID> \
  -q acknowledged=false
```

Filters: `prompt_version`, `session_id`, `severity`, `hook`, `acknowledged`,
`page`, `limit`. A non-null `superseded_by` means the notice was discarded by a
co-firing veto and is audit-only.

### Standing grants

An `allow_always` leaves a grant that keeps permitting the same action.

```bash
bud api GET    /escalations/standing -q session_id=<SID>
bud api GET    /escalations/standing -q project_id=<PID> -q prompt_id=<AGENT_ID> -q include_expired=false
bud api DELETE /escalations/standing/<GRANT_ID>
```

Listing with neither `session_id` nor `project_id` is a 422 (`scope_required`)
unless you hold the read-any permission. Revocation is allowed for the granter,
a current member of the source escalation's target, or a holder of the resolve-any
permission - anyone else gets 403 `not_authorized_to_revoke`, and the attempt is
audited either way.

### Killing a run outright

```bash
bud api POST /governance/runs/<EXECUTION_ID>/override -d '{"action":"kill","reason":"rogue loop"}'
```

Note the prefix is `/governance/runs/...`, **not** `/escalations/...`, and the
path takes an **execution id**, not a session id. `kill` is the only action
today. This is the blunt instrument - prefer `/runs/{sid}/abort` with
`after_turn` when the run is merely wrong rather than dangerous.

## 5. Empty results do not mean "nothing is wrong"

Collected in one place, because each of these silently returns success:

| You see | It may actually mean |
|---|---|
| `/approvals` returns `[]` | you are not on the roster, or the run has no roster at all |
| `/escalations` returns `[]` | you are not a member of the project (a non-member gets 200, not 403) |
| `/escalations?kind=notify` returns `[]` | always - that filter is not wired; use `/escalations/notifications` |
| `/runs` returns `[]` | the trigger never matched, or the event-type string is wrong (see `references/event-triggers.md`) |
| a 202 from any signal | forwarded, not applied - re-read the run |
