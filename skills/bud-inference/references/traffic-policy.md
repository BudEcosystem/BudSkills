# Traffic policy from the caller's side

How a deployment's configuration shows up in the responses you get, and what to
change when it is wrong. Setting the values is a `bud-deployments` task; this is
about behaviour.

## Rate limits

```bash
bud api GET /endpoints/$EP/deployment-settings
bud api PUT /endpoints/$EP/deployment-settings -d '{
  "rate_limits":{"algorithm":"token_bucket","requests_per_minute":600,
                 "burst_size":50,"enabled":true}}'
```

`algorithm` is `sliding_window` (the default), `fixed_window` or `token_bucket`.
Changes reach the gateway in well under a second.

Every response carries the current state, so you can pace without waiting to be
refused:

```
x-ratelimit-limit: 600
x-ratelimit-remaining: 412
x-ratelimit-reset: 1785953817
```

All three at `0` means **no limit is configured**, not that you are exhausted.

When you exceed it: `429` with `Retry-After`. Honour it; do not add concurrency.

Two things surprise people:

- **The budget is per deployment, not per alias.** A router, a LoRA adapter and
  the deployment they resolve to all draw on the same bucket.
- **`429` and `402` are different systems.** `429` is this configuration. `402
  insufficient_quota` is a spend ceiling attached to the credential's owner and
  no rate-limit change will clear it.

## Retries

```json
{"retry_config": {"num_retries": 2, "max_delay_s": 1.5}}
```

`num_retries` is 0-10, `max_delay_s` up to 60. These are retries the gateway
performs on your behalf, invisibly - a call that took three attempts still looks
like one request to you, but its latency includes all three. When latency is
inexplicably high, check whether retries are configured before blaming the
model.

Retries do not apply to the errors you can fix: a `404 Model not found` or a
capability mismatch fails immediately.

## Fallbacks

```json
{"fallback_config": {"fallback_models": ["<endpoint-uuid>", "<endpoint-uuid>"]}}
```

- Entries are **endpoint UUIDs**, never deployment names. A name gives
  `400 Invalid fallback endpoint ID`.
- They must be in the **same project**; a foreign one gives
  `400 ... not found in project`.
- The deployment's own id is rejected. Maximum five.

From the caller's side a fallback is invisible: you get a normal `200` and the
`model` field still names the alias you asked for. If you need to know which
deployment actually answered, look the inference up afterwards - see below.

The PUT **merges** at the top level, so sending only `rate_limits` leaves
`retry_config` and `fallback_config` as they were. To clear a section, send it
explicitly as null or empty rather than omitting it.

## Response caching

Off by default, and opted in per request rather than per deployment:

```json
{"model":"my-deploy","messages":[...],
 "tensorzero::cache_options":{"enabled":"on","max_age_s":3600}}
```

`enabled` is `on`, `off`, `read_only` or `write_only`. Moderation and audio
paths never cache regardless. A served-from-cache call is billed and recorded as
`cached: true` - it will not appear in `usage` as free, so measure cache benefit
from the recorded inferences, not from the response body.

Use `read_only` when you want cache hits but do not want an experimental prompt
polluting the cache for everyone else.

## Publishing and reachability

Publishing does two things: lists the deployment in the consumer catalog, and
adds it to the alias list of **end-user application credentials** across the
installation.

```bash
bud api PUT /endpoints/$EP/publish -d '{"action":"publish",
  "pricing":{"input_cost":0.0005,"output_cost":0.0015,"currency":"USD","per_tokens":1000}}'
bud api GET  /endpoints/$EP/pricing
bud api PUT  /endpoints/$EP/pricing -d '{"input_cost":0.0006,"output_cost":0.0018,
                                         "currency":"USD","per_tokens":1000}'
bud api GET  /endpoints/$EP/pricing/history
bud api GET  /endpoints/$EP/publication-history
bud api GET  /models/catalog -q page=1 -q limit=20
bud api GET  /models/catalog/$EP
```

- `pricing` is mandatory on `publish`; the deployment must be `running`.
- Re-publishing an already-published deployment is a **silent no-op** and throws
  away the new pricing. Reprice with `PUT /pricing`, which in turn requires the
  deployment to already be published.
- Costs are decimals with up to six places, per `per_tokens` tokens (default
  1000).
- Propagation to the catalog and to credentials takes a second or two. Verify
  with `GET /models/catalog` rather than assuming.
- **Your own token will not see any change.** Session tokens and internal
  workspace keys are scoped to their project regardless of publication. Verify
  with an end-user application credential, or by checking the catalog.
- Unpublishing (`{"action":"unpublish"}`, no pricing needed) removes it from the
  catalog and from those credentials. Anything already integrated against it
  starts getting `404 Model not found` - warn the user before doing it.

The catalog entry carries `endpoint_name` - that is the value callers put in
`model` - and `supported_endpoints`, which tells them which paths to use.

## Edge blocking

Separately from rate limits, an installation can refuse traffic before it
reaches a model, by IP, country, user agent or observed rate. If callers are
being refused for no apparent reason, list the rules before debugging the
deployment:

```bash
bud api GET  /metrics/gateway/blocking-rules -q page=1 -q page_size=20
```

Note `page_size` rather than `limit`, and the list nests under `data`. **A rule
is not enforced until an explicit sync call is made**, so a rule that looks
active may not be. Creating and syncing rules belongs to `bud-guardrails` -
they block real callers, so confirm with the user first.

## Confirming which deployment answered, and what it cost

```bash
bud api POST /metrics/inferences/list -d '{
  "from_date":"<ISO8601>","limit":20,"endpoint_id":"'"$EP"'",
  "sort_by":"timestamp","sort_order":"desc"}'
bud api GET /metrics/inferences/<inference_id>
```

The list returns `items[]` with `inference_id`, `timestamp`, `endpoint_name`,
`input_tokens`, `output_tokens`, `total_tokens`, `response_time_ms`, `cost`,
`is_success`, `cached`, and `error_code`/`error_message`/`status_code` when it
failed. Filters worth knowing: `is_success`, `endpoint_type`, `min_tokens`,
`max_tokens`, `max_latency_ms`; sort by `timestamp`, `tokens`, `latency` or
`cost`. `from_date` is required.

The detail call adds the raw request and response bodies - the only way to see
exactly what reached the model and what came back, including when a fallback
answered.

Aggregates (`POST /metrics/gateway/analytics`, `GET /metrics/gateway/top-routes`,
`GET /metrics/gateway/client-analytics`, `GET /metrics/gateway/geographical-stats`)
are the observability domain's territory - see `bud-observability`.
