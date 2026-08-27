---
name: bud-guardrails
description: Keep Bud Foundry models and agents safe and compliant - attach a content-safety profile to a deployment, call moderation directly, author custom safety probes with your own violation taxonomy, define what an agent is allowed to do, work the approval inbox, kill a runaway agent run, and block traffic by IP or country. Use when a Bud request mentions guardrails, safety, moderation, content filtering, PII, jailbreak or prompt-injection protection, compliance policy, what an agent may or may not do, approvals or escalations, blocking traffic by IP or country, or a deployment failing with guardrail_failure.
---

# Bud Foundry: guardrails and governance

Bud enforces safety at three layers, and picking the wrong one is the usual
reason a control "does not work":

| Layer | Stops | Reach for it when |
|---|---|---|
| **Blocking rules** | requests at the edge, before any model runs | blocking by IP, country, user agent or rate |
| **Guardrail profiles** | unsafe *content*, going in or coming out | moderation, PII, jailbreak and injection detection |
| **Governance policies** | what an *agent* is allowed to do | restricting tools, requiring human approval |

Prerequisite: connect first - see `bud-platform` (`bud login`). Most calls need
a `project_id` (`bud-projects`).

> Routing traffic *between* models is a different skill: `bud-routing`.

## Start here if a deployment is broken

A misconfigured guardrail is a **total outage for the deployment it guards** -
every request fails, not only unsafe ones:

```json
{"error":{"message":"Guardrail execution failed","type":"server_error","code":"guardrail_failure"}}
```

Two things make this hard to diagnose from outside. The error is identical for
every request, so it reads like the model is down. And the guardrail deployment
**still reports `status: running`** while it happens - verified on the
reference installation, where one deployment answers `guardrail_failure` on
every call while its attachment record looks perfectly healthy. `status` is a
rollout signal, not a health signal.

Bud also indexes attachments **by profile, not by deployment**, so there is no
"which guardrail is on this endpoint?" call. During an incident that is exactly
the question, so use the helper:

```bash
python3 scripts/guardrail-attachments.py <endpoint-id>   # what guards this deployment
python3 scripts/guardrail-attachments.py --unhealthy     # every bad attachment, fleet-wide
```

**Detach first, diagnose second.** One call, instant, and it does not delete
the profile:

```bash
bud api DELETE /guardrails/deployment/<deployment-id>
```

There is no way to disable an attachment in place - `PUT` on a deployment
changes `name`, `description`, `severity_threshold` and `guard_types`, but not
`status`. Detach is the only off switch.

Once traffic flows again, the usual causes in order: a selected rule runs on a
detector model whose deployment is not `running`; a `cloud` provider credential
is missing or expired; or the profile was edited badly - and profile edits hit
every attachment of that profile at once.

## Browsing the catalog

```bash
bud api GET /guardrails/profiles --paginate \
  | jq -r '.[] | "\(.id)  \(.profile_type)  probes=\(.probe_count) attached=\(.deployment_count)  \(.name)"'

bud api GET /guardrails/probes -q limit=100 -q order_by=name \
  | jq -r '.probes[] | "\(.id)  \(.provider.id)  \(.provider_type)  \(.provider.name)/\(.name)"'

bud api GET /guardrails/probe/<probe-id>/rules -q limit=50
```

A **probe** is a detector family (toxicity, PII, prompt injection); a **rule**
is one detector inside it; a **profile** is a named bundle of probes and rules
with thresholds. You attach profiles, not probes. A profile is either
`moderation` or `governance` - mixing the two kinds of probe is a 400.

Two catalog traps:

- **Take the provider off the probe record** - `probe.provider.id` plus the
  probe's own `provider_type` (`bud`, needing no credential, or `cloud`, which
  needs one). Do **not** discover providers through `GET /models/providers`: on
  the reference installation that listing showed one of the three providers
  that actually carry moderation probes, and omitted the built-in one that most
  seeded probes belong to.
- **The probe catalog paginates with unstable ordering.** Walking it page by
  page returned 52 rows containing only 50 distinct probes - duplicates and
  silent misses. Pass `order_by=name`, or fetch one page with a large `limit`.

## Attach a profile to a deployment

Long-running: the detectors a profile needs may have to be onboarded and
deployed before anything can enforce. Reusing a profile already proven
`running` elsewhere is both the fastest and the lowest-risk path.

```bash
PROFILE=<profile-id>

# 1. is it healthy? every existing attachment must read running
bud api GET /guardrails/profile/$PROFILE | jq '.profile | {name,status,profile_type,probe_count}'
bud api GET /guardrails/profile/$PROFILE/deployments \
  | jq -r '.deployments[] | "\(.status)  \(.endpoint_name // "standalone")  \(.id)"'

# 2. attach - a stepped session, like other Bud jobs
W=$(bud api POST /guardrails/deploy-workflow -d '{
  "workflow_total_steps": 4, "step_number": 1, "trigger_workflow": false,
  "guardrail_profile_id": "'"$PROFILE"'"}' | jq -r .workflow_id)

bud api POST /guardrails/deploy-workflow -d '{
  "workflow_id":"'"$W"'","step_number":2,"trigger_workflow":true,
  "project_id":"'"$BUD_PROJECT_ID"'","endpoint_ids":["'"$EP"'"]}'

bud wait job $W --timeout 1200 --interval 15

# 3. confirm the attachment settled...
D=$(bud api GET /guardrails/profile/$PROFILE/deployments \
    | jq -r --arg ep "$EP" '.deployments[] | select(.endpoint_id==$ep) | .id')
bud wait resource /guardrails/deployment/$D \
  --field deployment.status --equals running \
  --fail-on failure,unhealthy,deleted --timeout 900

# 4. ...then prove the deployment still serves - see bud-inference
```

Field names bite here: it is `guardrail_profile_id`, not `profile_id`, and
`endpoint_ids` is an **array**. `endpoint_ids` and `is_standalone` are mutually
exclusive (422), and an endpoint that already carries a guardrail is rejected,
naming the profile that got there first. Note the `deployment.status` field
path in the wait - the entity nests under its own key.

> Do **not** poll `GET /guardrails/deployment/{id}/progress`. It is documented
> as the progress feed but returned HTTP 500 for **every** deployment on the
> reference installation, healthy ones included.

Deployment statuses: `pending`, `deploying`, `running`, `unhealthy`, `failure`,
`deleting`, `deleted`. Step 4 is not optional - see the outage warning above.

## Build a new profile

There is **no `POST /guardrails/profile`**. The same `deploy-workflow` is the
only creation path; `PUT /guardrails/profile/{id}` edits one afterwards.

```bash
W=$(bud api POST /guardrails/deploy-workflow -d '{
  "workflow_total_steps":10,"step_number":1,
  "provider_id":"'"$PROVIDER"'","provider_type":"bud"}' | jq -r .workflow_id)

bud api POST /guardrails/deploy-workflow -d '{"workflow_id":"'"$W"'","step_number":2,
  "project_id":"'"$BUD_PROJECT_ID"'","endpoint_ids":["'"$EP"'"]}'
# provider_type "cloud" needs one more call: {"step_number":3,"credential_id":"..."}

bud api POST /guardrails/deploy-workflow -d '{"workflow_id":"'"$W"'","step_number":4,
  "probe_selections":[{"id":"'"$PROBE"'","rules":[{"id":"'"$RULE"'","status":"active"}]}]}' \
  | jq '.workflow_steps | {total_models, models_reusable,
                           models_requiring_onboarding, models_requiring_deployment}'

bud api POST /guardrails/deploy-workflow -d '{"workflow_id":"'"$W"'","step_number":7,
  "name":"pii-and-injection","guard_types":["input","output"],
  "severity_threshold":0.5,"trigger_workflow":true}'

bud wait job $W --timeout 2400 --interval 30
PROFILE=$(bud job show $W --data | jq -r '.data.guardrail_profile_id')
```

**Stop at the probe-selection response and read those four counts.** If
`models_reusable == total_models` the rest takes seconds. If either
`models_requiring_*` is non-zero, a detector-model download and a cluster
deployment are about to happen - tens of minutes and real compute - and you
must supply `hardware_mode`, `deploy_config`, then a `cluster_id`. Tell the
user before triggering. Full branch in
`references/profiles-and-deployment.md`.

`name` must be unique among active profiles installation-wide ("Guardrail
profile already exists" on a clash). `guard_types` on a moderation profile are
`input` / `output`. `severity_threshold` is a 0-1 confidence floor
(installation default 0.5) - treat lower as stricter, and confirm by testing
rather than by assuming.

### Changing what a profile enforces

```bash
bud api PUT /guardrails/profile/$PROFILE -d '{
  "severity_threshold":0.7,
  "probe_selections":[{"id":"<probe>","rules":[{"id":"<rule>","status":"active"}]}]}'
```

> `probe_selections` is a **full replacement** - any probe you omit is removed.
> The change applies to every deployment of that profile at once, with no
> staged rollout, so a bad edit is a multi-deployment outage. Check
> `deployment_count` first, and never share one profile between production and
> experiments.

Reading the current selection back is awkward:
`GET /guardrails/profile/{id}/probes` returned **500 for every profile** on the
reference installation. `GET /guardrails/profile/{id}/probe/{probe_id}/rules`
does work, so the contents can be recovered by testing catalog probes against
it - which is what `python3 scripts/guardrail-attachments.py --probes` does.

## Call moderation without attaching it

A profile can be called directly - the right shape when you want to screen text
in your own flow, and the right way to prove a profile before it guards
anything. Deploy it with `"is_standalone": true` instead of `endpoint_ids`,
then:

```bash
curl -s $BUD_GATEWAY/v1/moderations -H "Authorization: Bearer $(bud token)" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<profile-name>","input":"text to screen"}'
```

Send two inputs: one obviously benign, one obviously in scope. Benign passing
and unsafe being flagged is a working guard. **Both** failing is a broken one -
and that is what would have happened to every request on the deployment you
were about to attach it to.

Naming overload worth knowing: for a **standalone** profile the deployment
`name` is the profile name, and that is what you send as `model`. For an
**attached** deployment the record's `name` is the endpoint's UUID and
`endpoint_name` holds the readable name.

## Custom probes: your own violation taxonomy

When the built-in categories do not match your policy, author a probe with your
own categories, severities and examples. Three steps, ending in
`trigger_workflow: true`:

```bash
W=$(bud api POST /guardrails/custom-probe-workflow -d '{
  "step_number":1,"workflow_total_steps":3,"probe_type_option":"llm_policy"}' | jq -r .workflow_id)
bud api POST /guardrails/custom-probe-workflow -d @policy.json     # step 2: the taxonomy
bud api POST /guardrails/custom-probe-workflow -d '{"workflow_id":"'"$W"'","step_number":3,
  "name":"house-style-policy","guard_types":["input"],"modality_types":["text"],
  "trigger_workflow":true}'
bud wait job $W --timeout 1800
PROBE=$(bud job show $W --data | jq -r '.data.probe_id')
```

The step-2 body carries `policy` with `task`, `definitions`, `safe_content` and
`violations[]`, each violation having a `severity` of `Moderate`, `High`,
`Critical` or `Maximum`. The resulting `probe_id` goes into a
`probe_selections` list like any other probe. Full walkthrough, plus the
one-shot path when the backing model already exists:
`references/profiles-and-deployment.md`.

## Agent governance: what an agent may do

Where a profile filters content, a **governance policy** constrains behaviour:
which tools an agent may call, what needs a human, what is forbidden. Each rule
yields a verdict - `allow`, `deny`, `transform`, `notify` (proceed, record it)
or `require_approval` (suspend until a human decides).

The lifecycle is author, activate, deploy into a project, bind to an agent.
**A policy that is authored but never bound does nothing**, which is the most
common reason a rule appears to be ignored.

```bash
POLICY=$(bud api POST /guardrails/governance-policies -d @policy.json | jq -r '.policy.id')
bud api PUT /guardrails/governance-policies/$POLICY -d '{"status":"active"}'

# deploy it using the policy's own (hidden) probe
bud api GET /guardrails/probes -q scanner_type=agentmesh_policy \
  -q include_governance=true -q limit=100 | jq -r '.probes[] | "\(.id)  \(.name)"'
#   ... then deploy-workflow with probe_selections=[{"id":"<that probe>"}], is_standalone=true

bud api GET /guardrails/governance-deployments -q project_id=$BUD_PROJECT_ID \
  | jq -r '.deployments[] | "\(.id)  \(.governance_policy_id)  \(.governance_policy_status)  \(.name)"'

bud api POST /guardrails/governance-policies/$POLICY/bind-agent -d '{
  "prompt_id":"<agent uuid or name>","deployment_id":"<deployment id>",
  "mode":"shadow","applies_to":["end_user","a2a","trigger"],
  "escalation_target":{"type":"users","refs":["<reviewer-uuid>"]},
  "min_approvals":1,"sticky_approval":true}'
```

Four traps:

- **Governance probes are hidden by default.** The create response does not
  tell you the probe id it made, and the probe listing filters governance
  probes out unless you pass `include_governance=true` or
  `scanner_type=agentmesh_policy`. The probe's name equals the policy's name.
- **Deploying a `draft` policy is a 409** - activate first. The reverse is not
  prevented: a `running` governance deployment can carry a policy that has
  since gone back to `draft` (seen on the reference installation), so read
  `governance_policy_status` before binding to it.
- **`mode` decides whether it bites.** `shadow` observes and records without
  blocking - the right first setting on a live agent. `active` enforces; `off`
  is inert.
- **The binding commits even if it never reaches the agent runtime.** The
  response carries `sync_pending`; while that is `true`, poll
  `GET /guardrails/agents/{prompt_id}/governance-policies` until it clears - a
  reconcile sweep rescues it within about ten minutes.

Policy body, lifecycle states, the shipped regulatory presets and how bindings
resolve: `references/agent-governance.md`.

## Approvals, escalations and killing a run

When a rule requires approval the run **suspends** and waits - it does not
fail. Someone must act, or it sits there.

```bash
bud api GET /escalations -q status=pending -q target=me -q limit=50 \
  | jq -r '.escalations[] | "\(.id)  \(.agent_name)  \(.subject)  can_resolve=\(.can_resolve)"'

bud api GET /escalations -q target=all -q project_id=<id>   # project_id REQUIRED with target=all
bud api POST /escalations/<escalation-id>/resolve -d '{"resolution":"allow_once","comment":"reviewed"}'
```

Read `can_resolve` and `allowed_resolutions` off each row rather than assuming
you may approve - it depends on the escalation target, on maker-checker rules
and on approval tiers. Standing grants (from `allow_always`) pre-approve a
repeated action so a routine agent does not stall nightly.

> An empty list is ambiguous. `target=all` answers `200` with `[]` for a
> non-member rather than 403, and listings degrade to empty when the agent
> runtime is unreachable - so "nothing pending", "not allowed to see it" and
> "the service is down" look identical. Say "nothing listed", never "there is
> nothing".

> On some installations these endpoints answer `502 ... operator token is not
> configured`. That means the agent-run plane is not wired up there - a
> configuration gap, not your request. Report it rather than retrying.

To stop a misbehaving run, kill it rather than waiting for a policy to catch
it. This **destroys in-flight work**, so confirm with the user unless they
asked for it:

```bash
bud api POST /governance/runs/<execution-id>/override -d '{"action":"kill","reason":"runaway loop"}'
```

`kill` is the only action; 202 means it was applied; every attempt including
refused ones is audited. Resolving, standing grants and notifications in full:
`references/approvals-and-escalations.md`.

## Blocking traffic at the edge

Blocking rules reject requests before any model runs - the cheapest control,
and the right one for abuse rather than content. Highest `priority` wins.

```bash
bud api GET  /metrics/gateway/blocking-rules -q page=1 -q page_size=50
bud api POST /metrics/gateway/blocking-rules -d '{
  "name":"block-embargoed","rule_type":"country_blocking",
  "rule_config":{"countries":["CN","RU"]},"priority":100,"reason":"compliance"}'
bud api PUT  /metrics/gateway/blocking-rules/<id> -d '{"status":"inactive"}'  # off, not deleted
bud api POST /metrics/gateway/blocking-rules/sync -d '{"force_sync":true}'    # if enforcement looks stale
```

Three things to know about that path:

- Blocking rules live under `/metrics/gateway/`, not `/guardrails/`, because
  they are enforced at the gateway. `/guardrails/blocking-rules` is a 404.
- **Do not pass `project_id`** - that filter returns 500. List them all and
  filter client-side on each rule's own scope.
- Pagination uses `page_size`, not `limit`, and the list key is `items` -
  unlike everything else in this domain.

`rule_config` by type: `ip_blocking` takes `ip_addresses` (plain or CIDR),
`country_blocking` takes ISO-2 `countries`, `user_agent_blocking` takes
`patterns`, `rate_based_blocking` takes `threshold` plus `window_seconds`.
`rule_type` is fixed at creation. Scope comes from what you send: nothing =
installation-wide, `model_name` = that model only. Rules take effect at the
gateway, so verify from outside rather than trusting that the rule saved.
`references/blocking-rules.md` covers the types and effectiveness stats.

## Deeper reference

- `references/profiles-and-deployment.md` - the object model, catalog, the
  deploy session field by field, attaching/detaching, editing, custom probes
- `references/agent-governance.md` - policy body, lifecycle, project
  deployment, bindings, audit
- `references/approvals-and-escalations.md` - the inbox, resolving, standing
  grants, notifications, killing a run
- `references/blocking-rules.md` - rule types, scope, CRUD, making them take
  effect, measuring effectiveness
- `scripts/guardrail-attachments.py` - the reverse index Bud does not provide:
  which profile guards which deployment, and which attachments are unhealthy

## Where to go next

`bud-inference` to see how a guardrail failure looks to a caller and to prove a
guarded deployment still answers; `bud-deployments` for the deployment a
profile attaches to; `bud-agents` for the agents policies bind to;
`bud-observability` to measure what is being blocked.
