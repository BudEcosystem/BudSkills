# Agent governance: policies and bindings

A governance policy answers "what may this agent do?" - which tools it may
call, what it may say, when a human must approve, when a handoff is allowed.
It is evaluated inside the agent runtime at named hooks, not on model traffic.

## The policy body

```json
{
  "name": "tool-approval",
  "description": "Human approval before destructive tools",
  "scanner_type": "agentmesh_policy",
  "config": {
    "conflict_strategy": "priority_first_match",
    "policy": {
      "version": "1.0",
      "rules": [{
        "name": "approve-destructive-tools",
        "enabled": true,
        "verdict": "require_approval",
        "guard_types": ["pre_tool"],
        "checks": [{"kind":"action","patterns":["delete_*","drop_*"]}],
        "conditions": [{"key":"environment","op":"is","value":"production"}],
        "priority": 100,
        "guidance": "Destructive tool in production needs a reviewer.",
        "scope": "agent",
        "min_approvals": 1,
        "require_ack": false,
        "approval_policy": {
          "roles": [], "distinct_approvers": true,
          "ordered_tiers": [{"roles":[],"quorum":1,"ttl_secs":3600}],
          "quorum": 1, "ttl_secs": 3600,
          "on_expiry": "deny", "safe_default": "deny", "store_content": false
        }
      }]
    },
    "domain_context_schema": []
  }
}
```

### Verdicts

| Verdict | Effect | UI alias also accepted |
|---|---|---|
| `allow` | permit explicitly | |
| `deny` | block the action | |
| `transform` | modify the content (e.g. redact) | `redact` |
| `require_approval` | suspend the run, raise an escalation | `escalate` |
| `notify` | proceed, but emit a review record | `allow_with_review` |

### Hooks (`guard_types`)

`pre_request`, `post_request`, `pre_tool`, `post_tool`, `before_agent`,
`after_agent`, `outbound_handoff`.

Aliases normalized on write: `input`→`pre_request`, `output`→`post_request`,
`before_request`→`pre_request`, `after_reply`→`post_request`,
`before_tool`→`pre_tool`, `after_tool`→`post_tool`. Seeded preset policies
often report the alias form (`["input","output"]`) - that is expected.

### Checks

Each rule needs at least one check:

- `{"kind":"detector","ref":"<catalog rule or probe uuid>"}` - reuse a content
  detector from the probe catalog. The `ref` **must resolve to a live catalog
  entry** or the write (and activation) 422s.
- `{"kind":"action","patterns":["delete_*"]}` - match tool/action names.

Legacy `kind` values `scanner` and `guardrail` normalize to `detector`.

> Responses inject a display-only `ref_name` onto detector checks. It is
> stripped on write - never send it back and never use it for identity. The
> stored policy is id-only.

### Conditions

`{"key": "...", "op": "is|isnt|gt|lt|has", "value": ...}` evaluated against
ambient facts. `key` must be an allowed fact name; declare custom ones in
`domain_context_schema`. Check reserved names before authoring:

```bash
bud api GET /governance/reserved-variable-names   # {names[], namespaces[], version}
```

### Conflict strategy

When several rules match: `priority_first_match` (default mental model - higher
`priority` wins), `deny_overrides`, `allow_overrides`, `most_specific_wins`.

### Approval policy

`ttl_secs` is bounded 60..2592000 (30 days). On expiry: `on_expiry: "deny"` or
`"safe_default"` (with `safe_default` itself `allow` or `deny`).
`distinct_approvers: true` enforces maker≠checker - the actor who triggered the
gate can never approve it. `ordered_tiers` escalates through approver groups in
order, each with its own quorum and TTL.

## Lifecycle

```
draft  <->  active        (+ deleted, terminal)
```

```bash
bud api POST   /guardrails/governance-policies -d @policy.json       # -> draft
bud api PUT    /guardrails/governance-policies/<id> -d '{"config":{...}}'   # new version
bud api PUT    /guardrails/governance-policies/<id> -d '{"status":"active"}'
bud api GET    /guardrails/governance-policies -q page=1 -q limit=50
bud api GET    /guardrails/governance-policies/<id>
bud api DELETE /guardrails/governance-policies/<id>
```

- A `config` change **mints a new immutable version** (`version` increments).
  A `status` change runs the state machine. One `PUT` may do both.
- `shadow` and `paused` exist in the enum but are **not reachable** - trying to
  transition into them is a 409. (Shadow behaviour lives on the *binding*, not
  the policy.)
- Setting `status` to its current value is a no-op, not an error.
- Activation re-validates that every detector `ref` still resolves (422 if not)
  and that the hooks/verdicts used are supported.
- A version mint or status change re-pushes refs to every bound agent and
  refreshes every carrying profile's live configuration.
- Delete is implemented as `status=deleted`, so the state machine still
  applies. It frees the name for reuse.
- Duplicate policy name for the same user → 409.

**Preset policies** (`created_by: null`) are a seeded compliance catalog - GDPR,
healthcare, California AI suite, prohibited practices, AI-use transparency, and
dozens more. They always report `status: "active"` with `config: null` (the
body is owned by the safety engine) and are fully immutable. You deploy and
bind them as-is; any mutating `PUT` is rejected, except a `status=active` PUT
which is accepted as a no-op so a uniform deploy flow works.

A policy id is also a catalog rule id: `GET /guardrails/rule/{policy_id}`
returns the raw rule. Same id, two different response shapes - use the
`governance-policies` route unless you specifically want the rule view.

## Deploying a policy into a project

Governance policies are enforced through a guardrail deployment, exactly like
moderation profiles, but with the policy's **wrapping probe**. Creating a
policy silently creates that probe (`probe.name` = policy name, `probe.uri` =
`governance.{user_id}.{name_lower_with_underscores}`), and the create response
does not tell you its id.

```bash
bud api GET /guardrails/probes -q scanner_type=agentmesh_policy \
  -q include_governance=true -q search=true -q name="<policy name>" \
  | jq -r '.probes[] | "\(.id)  \(.name)  \(.uri)"'
```

Then run the normal `deploy-workflow` (see
`references/profiles-and-deployment.md`) with that probe in
`probe_selections`, `is_standalone: true`, and the built-in `Bud Sentinel`
provider. Deploying a policy that is still `draft` is a hard 409 naming it.

```bash
bud api GET /guardrails/governance-deployments -q project_id=$BUD_PROJECT_ID \
  | jq -r '.deployments[] | "\(.id)  \(.governance_policy_id)  \(.governance_policy_name)  \(.status)"'
```

This lists only **running** governance deployments in the project, and it is
the feed you need for binding: it pairs each `deployment_id` with its
`governance_policy_id`. `project_id` is required.

## Bindings

A binding attaches one policy to one agent with a per-agent overlay.

```bash
bud api POST /guardrails/governance-policies/<policy-id>/bind-agent -d '{
  "prompt_id": "<agent uuid OR agent name slug>",
  "prompt_version_id": "<optional version uuid>",
  "deployment_id": "<governance deployment id>",
  "mode": "active",
  "enabled": true,
  "applies_to": ["end_user","a2a","trigger"],
  "applies_when": [{"key":"environment","op":"is","value":"production","presence":"mandatory"}],
  "escalation_target": {"type":"users","refs":["<user-uuid>"],"include_invoker":false},
  "min_approvals": 1,
  "sticky_approval": true}'

bud api GET   /guardrails/agents/<prompt-id>/governance-policies -q limit=50
bud api PATCH /guardrails/governance-policies/bindings/<binding-id> -d '{"mode":"shadow"}'
bud api DELETE /guardrails/governance-policies/bindings/<binding-id>    # bare 204, no body
```

| Field | Values / meaning |
|---|---|
| `prompt_id` | accepts the agent UUID **or** its name slug (`prompt_<ts>_<rand>`) |
| `mode` | `active` (enforce), `shadow` (observe only), `off` (inert). If sent, it wins and `enabled` is derived from it. |
| `applies_to` | subset of `end_user`, `a2a`, `trigger`; default is all three |
| `applies_when` | extra gating on ambient facts; `presence` is `mandatory` or `optional` |
| `escalation_target` | `{"type":"self"｜"user"｜"users"｜"group", "ref"/"refs", "include_invoker":bool}` |
| `min_approvals` | ≥1, and must be ≥ the policy's derived floor |
| `sticky_approval` | whether `allow_always` may create a standing grant |

**Roll out in shadow first.** `mode: "shadow"` produces the same evaluation
records with no blocking and no suspended runs - the only safe way to see what
a new policy would have done to real traffic.

> `escalation_target.type: "group"` currently resolves to an **empty** approver
> list - there is no group model. A group-targeted escalation has no approvers
> unless `include_invoker: true`. Use `"users"` with explicit `refs`.

`PATCH` takes a free-form object merged over the six mutable fields (`mode`,
`enabled`, `applies_to`, `applies_when`, `escalation_target`, `min_approvals`,
`sticky_approval`). Sending `prompt_id`, `prompt_version_id` or
`governance_policy_id` is a 422 `FIELD_IMMUTABLE` - re-pointing a binding means
delete + create.

`DELETE` returns a bare **204 with no body** - the only endpoint in this domain
that does. Do not try to parse it.

### Binding errors, in the order they are checked

| Code | Status | Meaning |
|---|---|---|
| `GOVERNANCE_POLICY_NOT_FOUND` | 404 | unknown policy |
| (prompt authz) | 404 / 403 | 404 if you cannot even view the agent's project (no existence leak); 403 if you are a member without manage |
| `PROMPT_VERSION_MISMATCH` | 422 | `prompt_version_id` not a version of that agent |
| `GOVERNANCE_DEPLOYMENT_NOT_FOUND` | 404 | unknown `deployment_id` |
| `PROJECT_MISMATCH` / `NOT_ACTIVE` / `POLICY_MISMATCH` | 422 | deployment is in another project, not running, or serves a different policy |
| `APPLIES_TO_EMPTY` / `INVALID_VALUE` | 422 | bad `applies_to` |
| `APPLIES_WHEN_MALFORMED` / `INVALID_KEY` | 422 | bad `applies_when` |
| `ESCALATION_TARGET_MALFORMED` / `NOT_FOUND` / `SELF_UNDER_FLOOR` | 422 | bad target |
| `GOVERNANCE_BINDING_BELOW_FLOOR` | 422 | `min_approvals` below the policy floor; the response carries the floor |
| `GOVERNANCE_BINDING_EXISTS` | 409 | already bound for that (agent, version, policy); carries the existing `binding_id` |

### Sync

The binding is **committed even if the push to the agent runtime fails** - the
response then carries `sync_pending: true`. It is retried, and a background
reconcile sweeps every ~10 minutes. Confirm before telling the user the policy
is live:

```bash
bud api GET /guardrails/agents/<prompt-id>/governance-policies \
  | jq -r '.bindings[] | "\(.id)  \(.mode)  sync_pending=\(.sync_pending)"'
```

`sync_pending: true` on every row means the ref push is failing generally, not
just for one binding.

The binding response also carries `effective: {min_approvals, sticky_approval,
below_floor, policy_floor}` - the values that will actually be enforced after
the policy's floors are applied to your overlay. Read those, not the values you
sent.

## Audit

Policy create/update/delete, binding create/update/delete, escalation resolves
(including rejected attempts) and run overrides are all written to the audit
trail - `bud-projects` `references/audit.md` for how to read it.
