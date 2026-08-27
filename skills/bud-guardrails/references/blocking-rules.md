# Gateway blocking rules

Edge rules evaluated at the inference gateway before a request reaches any
model. Independent of guardrail profiles: no probes, no detector models, no
deployment - a rule takes effect as soon as it is synced.

Use these for "stop this IP range", "we may not serve these countries", "block
this scraper", "throttle abusive callers". Use a guardrail profile for anything
that needs to look at the *content* of a request.

## Rule types

| `rule_type` | `rule_config` |
|---|---|
| `ip_blocking` | `{"ip_addresses":["203.0.113.7","10.0.0.0/24"]}` - single addresses and CIDR |
| `country_blocking` | `{"countries":["CN","RU"]}` - ISO country codes |
| `user_agent_blocking` | `{"patterns":["bot","curl"]}` |
| `rate_based_blocking` | `{"threshold":100,"window_seconds":60}` |

## Scope

Scope is implied by what you send, not by an explicit field:

| You send | Scope |
|---|---|
| neither `project_id` nor `model_name` | **global** - the whole installation |
| `model_name` | that deployment only |
| `project_id` (query parameter) | that project (legacy scoping) |

`endpoint_id` exists but is deprecated - use `model_name`.

> A rule with no scope applies installation-wide. Say so explicitly before
> creating one, and default to `model_name` or `project_id` scoping unless the
> user asked for a global block.

## CRUD

```bash
bud api POST /metrics/gateway/blocking-rules -d '{
  "name":"block-scrapers",
  "rule_type":"user_agent_blocking",
  "rule_config":{"patterns":["bot","spider"]},
  "description":"known scrapers",
  "reason":"abuse",
  "priority":50,
  "model_name":"support-7b"}'

bud api GET    /metrics/gateway/blocking-rules -q page=1 -q page_size=50
bud api GET    /metrics/gateway/blocking-rules -q rule_type=country_blocking -q status=active
bud api GET    /metrics/gateway/blocking-rules/<rule-id>
bud api PUT    /metrics/gateway/blocking-rules/<rule-id> -d '{"status":"inactive"}'
bud api DELETE /metrics/gateway/blocking-rules/<rule-id>
```

- **Pagination is `page` + `page_size`** (1..100, default 20), and the
  collection comes back under **`items`** with a `total` - both different from
  every other endpoint in this domain. `--paginate` conventions from
  `bud-platform` do not apply here.
- **Do not pass `project_id` when listing.** On the reference installation
  `GET /metrics/gateway/blocking-rules?project_id=<uuid>` returns HTTP 500,
  while the same call without it returns 200. List everything and filter
  client-side on each rule's own scope. (Creation still accepts `project_id` as
  a query parameter.)
- `rule_type` is **immutable** after creation. Changing the type means delete
  and recreate.
- `PUT` is a partial update - only the fields you send change.
- Higher `priority` is evaluated first (default 0).
- `status` is `active`, `inactive` or `expired`. Prefer
  `PUT {"status":"inactive"}` over deleting - it keeps the rule and its stats
  for when the block needs to come back.

## Making rules take effect

Create/update/delete push to the gateway implicitly. If enforcement looks
stale - a rule shows `active` but traffic still gets through - force a push:

```bash
bud api POST /metrics/gateway/blocking-rules/sync -d '{"force_sync":true}'
# -> "Successfully synced N rules ..."

# or just some projects
bud api POST /metrics/gateway/blocking-rules/sync -d '{"project_ids":["<uuid>"]}'
```

This is the analogue of a router's `retry-sync` (see `bud-routing`): the record
existing in the console does not by itself mean the gateway is enforcing it.

## Effectiveness

```bash
bud api POST /metrics/gateway/blocking-rules/sync-stats            # backfill match counts
bud api GET  /metrics/gateway/blocking-rules-stats \
  -q start_time=2026-08-01T00:00:00Z -q end_time=2026-08-06T00:00:00Z

bud api GET  /metrics/gateway/blocking-stats \
  -q start_time=2026-08-01T00:00:00Z -q end_time=2026-08-06T00:00:00Z
bud api GET  /metrics/gateway/blocking-dashboard-stats
```

The last two are aggregate views of what was actually blocked (`blocking-stats`
additionally accepts `project_ids`); `blocking-rules-stats` is the per-rule
overview.

Match counts and last-matched timestamps are computed from request analytics
and are only as fresh as the last `sync-stats` run - trigger it before
reporting numbers. For what actually happened to blocked requests, use
`bud-observability`.

## Permissions

These endpoints carry no explicit permission decorator; visibility follows your
project access. A global rule (no scope) is therefore something any
project-capable user can create - one more reason to scope deliberately and to
confirm with the user before creating an unscoped block.
