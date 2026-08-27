# Operating a deployment

## Workers

Workers are the processes actually serving the model.

```bash
bud api GET /endpoints/$EP/workers -q page=1 -q limit=50 -q refresh=true
bud api GET /endpoints/$EP/workers/<worker-id>
bud api GET /endpoints/$EP/workers/<worker-id>/logs
bud api GET /endpoints/$EP/workers/<worker-id>/metrics   # 12h CPU/memory history
```

A healthy worker has `status: Running` and `deployment_status: ready`. Fields
include `utilization`, `hardware`, `uptime`, `last_restart_datetime`, `cores`,
`memory` and `node_name` - restart counts and utilisation are the first things
to check when a deployment is slow.

**Add capacity by concurrency, not replica count:**

```bash
W=$(bud api POST /endpoints/add-worker -d '{
  "workflow_total_steps":3,"step_number":1,"trigger_workflow":false,
  "endpoint_id":"'"$EP"'","additional_concurrency":100}' | jq -r .workflow_id)
bud api POST /endpoints/add-worker -d '{"workflow_id":"'"$W"'","step_number":3,"trigger_workflow":true}'
bud wait job $W --timeout 900
```

**Remove one worker:**

```bash
bud api POST /endpoints/delete-worker -d '{
  "endpoint_id":"'"$EP"'","worker_id":"<id>","worker_name":"<name>"}'
```

All three fields are required. This call returns **no job id** - confirm by
re-listing workers until the count drops.

## Adapters (LoRA)

Attach a fine-tuned adapter to a running deployment without redeploying.

```bash
# the adapter must be a registry model with base_model_relation=adapter and status=active
A=$(bud api GET /models/ -q table_source=model -q base_model_relation=adapter -q limit=50 \
    | jq -r '.models[0].model.id')

W=$(bud api POST /endpoints/add-adapter -d '{
  "workflow_total_steps":3,"step_number":1,"trigger_workflow":false,
  "endpoint_id":"'"$EP"'","adapter_name":"support-tone","adapter_model_id":"'"$A"'"}' | jq -r .workflow_id)
bud api POST /endpoints/add-adapter -d '{"workflow_id":"'"$W"'","step_number":3,"trigger_workflow":true}'
bud wait job $W --timeout 600

bud api GET /endpoints/$EP/adapters
bud api POST /endpoints/delete-adapter/<adapter-id>    # returns no job id - poll the list
```

Limits: **5 adapters per deployment**; the adapter name must not collide with
any deployment name in the project. The adapter becomes callable as
`<deployment-name>-<adapter-name>`.

## Traffic policy: rate limits, retries, fallbacks

```bash
bud api GET /endpoints/$EP/deployment-settings
bud api PUT /endpoints/$EP/deployment-settings -d '{
  "rate_limits": {"algorithm":"token_bucket","requests_per_minute":600,"burst_size":50,"enabled":true},
  "retry_config": {"num_retries":2,"max_delay_s":1.5},
  "fallback_config": {"fallback_models":["<other-endpoint-uuid>"]}}'
```

- `algorithm`: `token_bucket`, `fixed_window`, `sliding_window`.
- `num_retries` 0-10; `max_delay_s` up to 60.
- Fallback targets must be in the **same project**, `running`, not itself, max 5.
- This call **merges** at the top level - omitted sections are preserved.

## Response behaviour

```bash
bud api GET /endpoints/$EP/responses-config
bud api PUT /endpoints/$EP/responses-config -d '{"responses_config":{ ... }}'
```

Controls the richer response API per deployment: `enabled`,
`background_enabled`, `store_default`, `limits` (`max_output_tokens`,
`max_tool_calls`, `chain_depth_limit`, `background_wall_clock_seconds`),
`defaults` (`reasoning_effort` of `minimal`/`low`/`medium`/`high`/`xhigh`,
`truncation` of `auto`/`disabled`, `instructions`), and `server_tools`
(`web_search`, `web_fetch`).

Unlike deployment settings this call **replaces the whole object** - read it,
modify it, write it back. Setting `enabled: false` removes that API from the
deployment's supported surface.

## Publishing, pricing, history

```bash
bud api PUT /endpoints/$EP/publish -d '{"action":"publish",
  "pricing":{"input_cost":0.0005,"output_cost":0.0015,"currency":"USD","per_tokens":1000}}'
bud api PUT /endpoints/$EP/publish -d '{"action":"unpublish"}'

bud api GET /endpoints/$EP/pricing            # 404 when never priced - normal
bud api PUT /endpoints/$EP/pricing -d '{"input_cost":0.0006,"output_cost":0.0018,
                                        "currency":"USD","per_tokens":1000}'
bud api GET /endpoints/$EP/pricing/history
bud api GET /endpoints/$EP/publication-history
```

Costs are decimals with up to 6 decimal places, below 10000, per `per_tokens`
tokens (default 1000).

Two traps: publishing an already-published deployment is a **silent no-op**, so
new pricing passed that way is ignored - use `PUT /pricing`. And `PUT /pricing`
requires the deployment to be published.

## Listing and finding

```bash
bud api GET /endpoints/ -q project_id=$BUD_PROJECT_ID --paginate
bud api GET /endpoints/ -q project_id=$P -q search=true -q name=hr
bud api GET /endpoints/$EP/model-cluster-detail     # full detail for one deployment
bud api GET /clusters/<cluster-id>/endpoints        # everything on a cluster
```

There is no `GET /endpoints/{id}` - use `model-cluster-detail` for one
deployment. With `search=false` (the default) `name` is an **exact** match;
`search=true` makes it a substring match. Deleted deployments are always
excluded.

## Deleting

```bash
bud api POST /endpoints/$EP/delete-workflow
```

Confirm with the user first. It fails while a prompt or agent version
references the deployment, naming the blocker - retire that first. Status goes
to `deleting`, then the record disappears from listings. Hosted deployments
with no cluster are removed immediately.
