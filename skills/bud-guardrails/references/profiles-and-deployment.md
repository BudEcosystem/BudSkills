# Guardrail profiles: catalog, creation, deployment, editing

## The object model

```
provider        a vendor of detectors, must carry the "moderation" capability
  probe         a detector family (e.g. "Jailbreak Detection")
    rule        one concrete detector inside it (e.g. "Jailbreak (Arch-Guard)")
profile         a named selection of probes+rules with thresholds
  deployment    one attachment of that profile - to an endpoint, or standalone
```

A profile's `profile_type` is **derived** from the probes you select:
`moderation` (answers on `/v1/moderations`, attachable to endpoints) or
`governance` (answers on `/v1/governance`, bindable to agents). A profile may
not mix the two - the create call returns 400.

## Browsing the catalog

```bash
bud api GET /guardrails/probes -q limit=50 -q page=1
bud api GET /guardrails/probes -q search=true -q name=jailbreak
bud api GET /guardrails/probes -q provider_id=<id> -q scanner_type=classifier
bud api GET /guardrails/probes/tags -q name=pii            # tag type-ahead
bud api GET /guardrails/probe/<probe-id>/rules -q limit=50
```

Filters on both probe and rule listings: `provider_id`, `name`, `status`
(`active|disabled|deleted`), `probe_type` (`provider|model_scanner|custom`),
`scanner_type` (`classifier|llm|pattern|bud_raa_classifier|agentmesh_policy`),
`include_governance` (default **false**), `search` (substring on name),
`order_by`.

`order_by` is a single comma-separated string - `-created_at,name:asc`.
Repeating the query parameter does not work.

> **The probe catalog paginates with unstable ordering.** Walking it page by
> page returned 52 rows containing only 50 distinct probes - duplicates and
> silent misses. Always pass `order_by=name`, or fetch a single page with a
> large `limit`.

**Governance probes are hidden by default.** Pass `include_governance=true` or
an explicit `scanner_type=agentmesh_policy` to see them.

Fields worth reading on a rule:

| Field | Why it matters |
|---|---|
| `model_uri` | the detector model the rule runs on. A rule with one needs that model onboarded **and** deployed before the profile can evaluate - this is what turns a 30-second attach into a 30-minute one. |
| `is_gated` | intended as the "needs its own model" flag, but on the reference installation **every** catalog rule reported `false` while still carrying a `model_uri`. Do not trust it; read `model_uri`, and let the workflow's `models_requiring_*` counts be authoritative. |
| `guard_types` | `input`, `output` - which side of a request the rule inspects |
| `modality_types` | `text`, `image`, ... |
| `scanner_type` | `classifier` (fast, fixed labels), `llm` (policy judgement), `pattern` |

### Finding a usable `provider_id`

`GET /models/providers?capabilities=moderation` is **unreliable** - on the
reference installation it returned only the commercial provider and omitted the
built-in `Bud Sentinel` provider that most seeded probes belong to. Read the
provider off the probe instead, which is always correct:

```bash
bud api GET /guardrails/probes -q limit=5 \
  | jq -r '.probes[] | "\(.name)  provider=\(.provider.id)  \(.provider.name)  type=\(.provider_type)"'
```

`provider_type` is `bud` (built-in, no credential) or `cloud` (commercial,
needs a `credential_id` from `GET /proprietary/credentials/`).

## Creating a profile: the deploy-workflow

`POST /guardrails/deploy-workflow` is the **only** way to create a profile.
It is a stepped session (see `bud-platform` for the shared protocol).

Every field the session accepts, and which stage it belongs to:

| Field | Stage | Notes |
|---|---|---|
| `workflow_total_steps` | first call only | the console uses 10; any number works |
| `workflow_id` | every call after the first | never together with `workflow_total_steps` (422) |
| `step_number` | every call | just a slot label; data accumulates across calls |
| `trigger_workflow` | last call | **nothing runs without `true`** |
| `provider_id`, `provider_type` | provider | `bud` or `cloud` |
| `credential_id` | provider | required when `provider_type == "cloud"` |
| `project_id` | scope | required |
| `endpoint_ids[]` **or** `is_standalone` | scope | mutually exclusive, one is required |
| `guardrail_profile_id` | scope | deploy an **existing** profile instead of defining a new one |
| `probe_selections[]` | detectors | see shape below |
| `name`, `description`, `tags[]` | identity | new profiles only; `name` unique among active profiles |
| `guard_types[]`, `severity_threshold` | tuning | e.g. `["input","output"]`, `0.6` |
| `hardware_mode`, `deploy_config`, `cluster_id`, `budaiscaler_specification` | detector-model branch | only when a gated model must be deployed |

`probe_selections` shape (also used by `PUT /guardrails/profile/{id}`):

```json
[{"id": "<probe-uuid>",
  "severity_threshold": 0.6,
  "guard_types": ["input","output"],
  "rules": [{"id": "<rule-uuid>", "status": "active",
             "severity_threshold": 0.7, "guard_types": ["input"]}]}]
```

Omitting `rules` selects the probe with its defaults. Per-rule
`severity_threshold`/`guard_types` override the probe's, which override the
profile's.

### The detector-model branch

After you submit `probe_selections`, the response's `workflow_steps` carries:

| Key | Meaning |
|---|---|
| `total_models` | detector models the selection needs |
| `models_reusable` | already deployed and usable - free |
| `models_requiring_onboarding` | must be imported first (auto-triggered when a credential is present) |
| `models_requiring_deployment` | must be deployed onto a cluster |
| `model_statuses[]` | per-model: `not_onboarded｜onboarded｜running｜unhealthy｜deploying｜pending｜failure｜deleting` |
| `skip_to_step` | the platform's hint for where to continue |
| `recommended_clusters[]` | produced once capacity planning has run |

If `models_reusable == total_models`, the rest is seconds. Otherwise you must
supply `hardware_mode` (`shared`|`dedicated`) plus a `deploy_config`, wait for
capacity planning, then pick a `cluster_id` from `recommended_clusters` - the
same shape `bud-deployments` uses, and the same 12-20 minute cost.

Watch that phase through the job feed:

```bash
bud wait job $W --timeout 2400 --interval 30
bud job show $W --data | jq '{
  onboarding: .data.guardrail_onboarding_events,
  simulation: .data.guardrail_simulation_events,
  deployment: .data.guardrail_deployment_events,
  result:     .data.workflow_execution_status,
  profile:    .data.guardrail_profile_id}'
```

Each `*_events` object carries a `status` of `PENDING｜RUNNING｜COMPLETED｜FAILED`.
Success is `workflow_execution_status.status == "success"` **and**
`guardrail_profile_id` present. Failure is workflow `status == "failed"` (read
`reason`) or `workflow_execution_status.status == "error"` (the message names
the phase).

### Errors you will actually hit

| Message / code | Cause |
|---|---|
| `workflow_total_steps and workflow_id cannot be provided together` | resent `workflow_total_steps` on a continuation call |
| `Missing required data: endpoint_ids/is_standalone` | triggered without a scope |
| `Missing required data: credential_id` | `provider_type: cloud` without a credential |
| `Guardrail profile already exists` | name collision among active profiles |
| endpoint already guarded (names the conflicting profile) | detach the existing attachment first |
| 400 mixed profile | governance + moderation probes in one profile |
| 409 naming policies | deploying a governance policy that is still `draft` |

## Deploying an existing profile to another endpoint

Same session, but pass `guardrail_profile_id` in place of
`name`/`guard_types`/`severity_threshold`/`probe_selections`. This is the
lowest-risk way to add a guardrail: you are reusing a selection already proven
`running` somewhere else.

## Editing a profile

```bash
bud api PUT /guardrails/profile/<id> -d '{
  "name":"...","description":"...","severity_threshold":0.7,
  "guard_types":["input","output"],
  "probe_selections":[ ...FULL desired set... ]}'
```

`probe_selections` is a **full replacement** - omitted probes are removed, so
you must send the whole desired set. Reading the current one back is awkward:

```bash
bud api GET /guardrails/profile/<id>/probes            # returns HTTP 500 - see below
bud api GET /guardrails/profile/<id>/probe/<probe-id>/rules   # works
```

> `GET /guardrails/profile/{id}/probes` returned **500 for every profile** on
> the reference installation, so there is no direct way to list what a profile
> contains. The per-probe rules route under a profile does work and returns 0
> records for probes the profile does not carry, so the contents can be sieved
> out of the catalog. `python3 scripts/guardrail-attachments.py --probes <id>`
> does exactly that (~50 calls, about a minute).

The edit re-syncs the profile's live gateway configuration, so it applies to
**every** deployment of that profile simultaneously. Check `deployment_count`
on the profile before editing.

## Deployment records

```bash
bud api GET  /guardrails/profile/<profile-id>/deployments
bud api GET  /guardrails/deployment/<deployment-id>
bud api PUT  /guardrails/deployment/<deployment-id> -d '{"severity_threshold":0.8}'
bud api DELETE /guardrails/deployment/<deployment-id>
```

Only `name`, `description`, `severity_threshold` and `guard_types` are editable
per deployment. **`status` is not settable** - you cannot "disable" an
attachment, you detach it (DELETE) and re-attach later.

For an attached deployment, `name` is the endpoint's id and `endpoint_name` is
the deployment name. For a standalone deployment, `endpoint_id` is `null` and
`name` is the profile name - and that name is what you send as `model` to
`POST /v1/moderations`.

`GET /guardrails/deployment/{id}/progress?detail=full` is documented to return
`{status, progress_percentage, steps[]}` but returned HTTP 500 on the reference
installation for a healthy deployment. Treat it as unreliable; use the
deployment record's `status`.

## Custom probes

Two paths.

**One-shot**, when the backing model is already onboarded:

```bash
bud api POST /guardrails/custom-probe -d '{
  "name":"disclosure-policy","description":"...",
  "scanner_type":"llm","model_id":"<onboarded-model-id>",
  "model_config_data":{"handler":"gpt_safeguard","policy":{ ...PolicyConfig... }}}'
```

`scanner_type` must match the config union member: `classifier` takes
`{"head_mappings":[{"head_name":"...","target_labels":["..."]}],"post_processing":...}`,
`llm` takes the policy body below. A mismatch is rejected by validation.

**Wizard**, when the model still has to be resolved - `POST
/guardrails/custom-probe-workflow`, three steps, described in SKILL.md. Step 1
sends `probe_type_option: "llm_policy"` and the platform derives the safety
model, scanner type and handler for you. `llm_policy` is the only option today.

`PolicyConfig` body:

```json
{"task": "what the judge is deciding",
 "definitions": [{"term": "...", "definition": "..."}],
 "safe_content": {"category":"safe","description":"...",
                  "items":[{"name":"...","description":"...","example":"..."}],
                  "examples":[{"input":"...","rationale":"...","confidence":"high"}]},
 "violations": [{"category":"...","severity":"High","description":"...",
                 "escalate": false, "items":[], "examples":[]}],
 "interpretation": "...", "evaluation": "...", "ambiguity": "..."}
```

`severity` is `Moderate｜High｜Critical｜Maximum`. Invest in `examples` on both
`safe_content` and each violation - that is what makes a policy probe accurate.

Custom probes are **owner-scoped**: `GET /guardrails/custom-probes` lists only
yours, and reading someone else's is a 403. The shared probe catalog
(`GET /guardrails/probes`) is not owner-scoped.

## Probes and rules you author by hand

`POST /guardrails/probe` and `POST /guardrails/rule` exist for building a probe
family manually. One trap: **`POST /guardrails/rule` silently drops
`scanner_type`, `model_uri`, `model_id`, `is_gated` and `model_config_json`** -
the schema accepts them, the handler does not persist them. Use
`POST /guardrails/custom-probe` when you need a model-backed rule.

Seeded catalog probes are read-only; only probes you created can be edited or
deleted. `icon` values are validated against the console's icon registry - an
unknown value is a 422.

## Permissions

| Surface | Needs |
|---|---|
| probes, rules, custom probes, deploy-workflow | `model:view` / `model:manage` |
| profiles, deployments | `endpoint:view` / `endpoint:manage` |

See `bud-projects` `references/permissions.md` for how to debug a 403.
