# Traffic, clients, blocked requests and dashboards

Everything in this file is about the shape of incoming traffic rather than what
a run did.

## What works and what does not

Four routes under `/metrics/gateway/` return **404 on current builds** because
of a path defect in the analytics proxy, verified live:

| Broken | Use instead |
|---|---|
| `POST /metrics/gateway/analytics` | `POST /metrics/analytics`, `POST /metrics/time-series` |
| `GET /metrics/gateway/geographical-stats` | `GET /metrics/geography` |
| `GET /metrics/gateway/top-routes` | trace listing filtered by `span_name`, or `POST /metrics/analytics` grouped by `endpoint` |
| `GET /metrics/gateway/client-analytics` | the `gateway_analytics.*` span attributes (below) |

The failure looks like `HTTP 404 - Analytics request failed`. It is not a
permissions problem and retrying will not help; do not report it as missing data.

`GET /metrics/gateway/blocking-rules-stats` also returned 500 on a live
installation. `GET /metrics/gateway/blocking-dashboard-stats` returns the same
kind of overview and works.

## Geography

```bash
bud api GET /metrics/geography -q from_date=$FROM -q to_date=$TO \
  -q group_by=country -q limit=100 -q project_ids=<uuid>,<uuid>
```

`group_by`: `country` | `region` | `city`; `limit` 1..1000 (default 50);
`project_ids` is a comma-separated string and defaults to the caller's projects.

Response: `{locations:[{country_code, country_name, region, city, latitude,
longitude, request_count, success_rate, avg_latency_ms, unique_users,
percentage}], total_requests, total_locations, date_range, group_by}`.

`GET /metrics/observability/metrics/geography` is a reduced alias - country
grouping, limit 50, no project selection.

> Geographic attribution comes from the client IP recorded at the gateway. On
> installations where traffic arrives from inside the cluster or through a proxy
> that hides the client (loopback or private addresses), `locations` is legitimately
> empty even in a busy window. Check a span's `gateway_analytics.client_ip`
> before concluding the data is missing.

## Per-client and per-device analysis

With `client-analytics` broken, the source of truth is the gateway span
attributes on any trace (see `traces.md`):

`gateway_analytics.client_ip`, `.device_type`, `.browser_name`, `.os_name`,
`.user_agent`, `.is_bot`, `.is_blocked`, `.status_code`, `.method`, `.path`,
`.protocol_version`, `.proxy_chain`, `.total_duration_ms`,
`.gateway_processing_ms`, `.request_headers`, `.response_headers`,
`.inference_id`.

```bash
# device mix for a window, from the traces themselves
bud api GET /metrics/observability/traces -q resource_type=all \
  -q from_date=$FROM -q to_date=$TO -q limit=1000 \
  | jq -r '.items[].span_attributes["gateway_analytics.device_type"] // empty' \
  | sort | uniq -c | sort -rn
```

Page through with `page=2,3,…` for a full window; at 1000 rows per page this is
practical for a day of traffic, not for a month.

## Blocked traffic and blocking rules

Read side (works):

```bash
bud api GET /metrics/gateway/blocking-dashboard-stats           # counts, top IPs/countries, timeline
bud api GET /metrics/gateway/blocking-stats -q start_time=$FROM -q end_time=$TO
bud api GET /metrics/gateway/blocking-rules -q page=1 -q page_size=20 \
  -q status=active -q rule_type=ip_blocking
bud api GET /metrics/gateway/blocking-rules/<rule-id>            # nested under .data
```

Note these use `start_time`/`end_time`, not `from_date`/`to_date`. Blocked
requests never appear in `/metrics/inferences/list` (they have no request id) -
this is the only place they are counted.

Write side - **a real traffic-control change; confirm with the user first**:

```bash
bud api POST /metrics/gateway/blocking-rules -d '{
  "name":"block-cc","rule_type":"country_blocking",
  "rule_config":{"countries":["CN"]},"reason":"abuse","priority":10}'
bud api POST /metrics/gateway/blocking-rules/sync -d '{}'        # push to the gateway
```

`rule_type`: `ip_blocking` (`{"ip_addresses":[…]}`), `country_blocking`
(`{"countries":[…]}`), `user_agent_blocking` (`{"patterns":[…]}`),
`rate_based_blocking` (`{"threshold":100,"window_seconds":60}`). Status values:
`active`, `inactive`, `expired`. Create returns 201 with the rule under `data`;
`PUT` patches only supplied fields; `DELETE` removes it.

> A new rule is not enforced until it reaches the gateway. Create/update/delete
> sync that one rule inline, but after a batch of changes call
> `POST /metrics/gateway/blocking-rules/sync` explicitly. Both sync endpoints
> return **HTTP 200 even when they fail** - read `message` for a
> `"Sync failed: …"` prefix rather than trusting the status code.
> `POST /metrics/gateway/blocking-rules/sync-stats` refreshes each rule's
> `match_count` / `last_matched_at`.

For model-level safety policy (rather than network-level blocking), see
`bud-guardrails`.

## Dashboard counts

```bash
bud api GET /metrics/count
```

`{total_model_count, cloud_model_count, local_model_count, total_projects,
total_project_users, total_endpoints_count, running_endpoints_count,
total_clusters, inactive_clusters}`.

> Only `total_projects` is scoped to the caller. Model, endpoint and cluster
> counts are installation-wide, so on a multi-tenant installation this is a
> platform overview, not "your" numbers. If a user asks "how many deployments do
> I have", count `GET /endpoints/` within their projects instead.

## Background usage sync

```bash
bud api GET /metrics/sync-stats
```

`{success, data:{incremental_syncs, full_syncs, last_incremental_sync,
last_full_sync, is_running, incremental_interval, full_sync_interval,
last_error, total_alerts_triggered, total_alerts_failed}, message}`.

This tracks the task that refreshes API-key and per-user usage counters. If
key "last used" timestamps or usage-based alerts look stale, check that
`is_running` is true and `last_incremental_sync` is recent (the default cadence
is roughly a minute for incremental, quarter-hour for full) before investigating
anything else.

## Deployment runtime, not traffic

Per-deployment logs and worker metrics live with the deployment, not here:

```bash
bud api GET /endpoints/<endpoint_id>/workers/<worker_id>/logs
bud api GET /endpoints/<endpoint_id>/workers/<worker_id>/metrics
```

Cluster, node and GPU utilisation belong to `bud-clusters`.
