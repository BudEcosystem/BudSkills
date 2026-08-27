# Cluster endpoint reference

Every publicly reachable endpoint in this domain, with its parameters, response
shape and permission. Paths were confirmed against a live installation with
`bud paths cluster`; run that yourself if something 404s.

Envelope conventions: most responses nest the entity under its own key
(`.cluster`, `.settings`) - read `bud-platform` §5. The exceptions are flagged
below.

## Cluster lifecycle

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/clusters/clusters` | `cluster:manage` | onboard / provision. **multipart only**; see `onboarding.md`. Response is **flat** (`workflow_id` at top level) |
| GET | `/clusters/clusters` | `cluster:view` | `page` (≥1), `limit` (≥0), `name`, `search` (bool), `order_by`. Response `{clusters:[...], page, limit, total_record, total_pages}` |
| GET | `/clusters/{id}` | `cluster:view` | nested under `.cluster`. **400 `Cluster not found`** for missing/deleted, never 404 |
| PATCH | `/clusters/{id}` | `cluster:manage` | `{name?, icon?, ingress_url?}`; nested under `.cluster` |
| POST | `/clusters/{id}/delete-workflow` | `cluster:manage` | no body, **no workflow id returned** |
| POST | `/clusters/cancel-onboarding` | `cluster:manage` | `{workflow_id}` - the id from the submit call |
| POST | `/clusters/{id}/sync-nodes` | `cluster:manage` | queues an inventory refresh; `{workflow_id}` is not pollable |

## Settings and storage

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/clusters/{id}/settings` | `cluster:view` | nested under `.settings`; 404 when unset |
| POST | `/clusters/{id}/settings` | `cluster:manage` | 201; no duplicate guard - prefer PUT |
| PUT | `/clusters/{id}/settings` | `cluster:manage` | upsert, **full replace** |
| DELETE | `/clusters/{id}/settings` | `cluster:manage` | |
| GET | `/clusters/{id}/storage-classes` | `cluster:view` | **bare payload**, no envelope, includes `code`. Requires the cluster to be `available` |

## Inventory, metrics and health

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/clusters/{id}/node-metrics` | `cluster:view` | the canonical node view; `metrics_available` flags the observability pipeline |
| GET | `/clusters/{id}/node-events/{hostname}` | `cluster:view` | hostname = `nodes[*].hostname`, not the map key |
| GET | `/clusters/{id}/metrics` | `cluster:view` | `filter=today\|7days\|month`, `metric_type=all\|memory\|cpu\|disk\|gpu\|hpu\|network_bandwidth\|power` |
| GET | `/clusters/{id}/metrics/gpu` | `cluster:view` | GPU devices + sharing slices |
| GET | `/clusters/{id}/nodes/{hostname}/metrics/gpu` | `cluster:view` | |
| GET | `/clusters/{id}/nodes/{hostname}/metrics/gpu/timeseries` | `cluster:view` | `hours` 1..168 (default 6) |
| GET | `/clusters/{id}/nodes/{hostname}/metrics/cpu/timeseries` | `cluster:view` | `hours` 1..168 (default 6) |
| GET | `/clusters/clusters/{id}/metrics/summary` | signed-in only | doubled path segment; raw pass-through |
| GET | `/clusters/clusters/{id}/metrics/nodes` | signed-in only | `start_time`, `end_time` |
| GET | `/clusters/clusters/{id}/metrics/pods` | signed-in only | `namespace`, `start_time`, `end_time` |
| GET | `/clusters/clusters/{id}/metrics/health` | signed-in only | stored-metrics view, not a live probe |
| POST | `/clusters/clusters/{id}/metrics/query` | signed-in only | arbitrary body; cluster identifier injected |
| GET | `/clusters/{id}/grafana-dashboard` | `cluster:view` | `{url}` |

The five doubled-path routes carry no permission check - any signed-in user
reaches them, unlike every other route here.

## Consumers and neighbours

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/clusters/{id}/endpoints` | `cluster:view` | deployments on this cluster; `page`, `limit`, `name`, `status`, `search`, `order_by` |
| GET | `/projects/{project_id}/clusters` | `project:view` | inner-joins deployments |
| GET | `/clusters/recommended/{workflow_id}` | signed-in only | takes a **deployment** workflow id; `bud-deployments` owns this |
| POST | `/clusters/recommended-scheduler` | none | background scheduler trigger; do not call |
| POST | `/benchmark/node-configurations` | `benchmark:manage` | valid parallelism options for a cluster + nodes + model |

## Cloud credentials

| Method | Path | Notes |
|---|---|---|
| GET | `/credentials/cloud-providers` | `{providers:[{id, name, unique_id, schema_definition, is_enabled, logo_url}]}` |
| POST | `/credentials/cloud-providers` | `{provider_id, credential_name, credential_values}`; 201, **id not returned** |
| GET | `/credentials/cloud-providers/credentials` | `provider_id` filter; newest first; values masked |
| GET | `/credentials/cloud-providers/credentials/{id}` | returns `credentials[0]` - a list of one; 403 if it is not yours |
| GET | `/credentials/cloud-providers/{provider_id}/regions` | `{regions:[{id, name}]}` - send `id` as `region` |

Providers seen on a live installation: `aws`, `gcp`, `azure`, `oracle`,
`lambda`, `runpod`, `kubernetes`, `fluidstack`, `vast_ai`, `cudo`,
`paperspace`, `digitalocean`, `cloudflare`, `samsung`, `ibm_cloud`, `vmware` -
all reporting `is_enabled: true`. **Only `azure` and `aws` can actually
provision a cluster.**

## Job observation

| Method | Path | Notes |
|---|---|---|
| GET | `/workflows/{id}` | full record; onboarding progress at `workflow_steps.create_cluster_events.steps[]`, deletion at `delete_cluster_events` |
| GET | `/workflows` | `workflow_type=cluster_onboarding\|cluster_deletion`, `page`, `limit`, `order_by`, `search`. **Only your own, only `in_progress`** - a finished one disappears; fetch by id |
| DELETE | `/workflows/{id}` | removes the record only; does **not** stop a running installation. 400 unless `in_progress` |

`bud job show <id>` and `bud wait job <id>` read both of these and normalise the
step states - prefer them.

## Enums

**Cluster status**: `available`, `not_available`, `error`, `deleting`,
`deleted`. Only `available` accepts deployments or answers a storage-class
query.

**Cluster type**: `ON_PREM`, `CLOUD` on input. Stored values are inconsistent
across older installations (a misspelled variant exists in some rows, and the
list response may return `null`); read it from `GET /clusters/{id}`.

**Device type**: `cpu`, `cpu_high`, `gpu`, `cuda`, `rocm`, `hpu`. `cpu_high` =
CPU with wide vector/matrix instruction sets; `gpu` is legacy, current
detections use `cuda`/`rocm`.

**Hardware type** (detail response): any of `CPU`, `GPU`, `HPU`.

**Access modes**: `ReadWriteOnce`, `ReadWriteMany`, `ReadOnlyMany`,
`ReadWriteOncePod`.

**Metric type**: `all`, `memory`, `cpu`, `disk`, `gpu`, `hpu`,
`network_bandwidth`, `power`. **Metrics window**: `today`, `7days`, `month`.

**Workflow status** (the record): `in_progress`, `completed`, `failed` - there
is no `cancelled`; cancelling deletes the record. **Step status** (inside a
step payload, uppercase): `UNKNOWN`, `STARTED`, `RUNNING`, `COMPLETED`,
`FAILED`, `TERMINATED`, `PENDING`, `SUSPENDED`.

**Onboarding step ids** - existing cluster: `determine_cluster_platform`,
`check_duplicate_config`, `verify_cluster_connection`, `configure_cluster`,
`fetch_cluster_info`. Cloud: `create_cloud_provider_cluster`,
`verify_cluster_connection`, `configure_cluster`, `fetch_cluster_info`.
Deletion: `delete_namespace`.

**Permissions**: `cluster:view`, `cluster:manage` (manage implies view).

**Response discriminators** (`object`): `cluster.list`, `cluster.get`,
`cluster.edit`, `cluster.delete`, `cluster.cancel`, `cluster.sync_nodes`,
`cluster.grafana-dashboard`, `cluster.recommended_clusters`,
`cluster.endpoints.list`, `cluster.storage-classes`,
`cluster.settings.get|create|update|delete`, `project.clusters.list`, and
`info` for the metrics and node views.

## Error shapes to expect

| Status | When |
|---|---|
| 400 | missing/deleted cluster (`Cluster not found`); duplicate name; both or neither of `workflow_id`/`workflow_total_steps`; deleting a cluster with deployments; cancelling an onboarding that never started; an upstream metrics failure on `/clusters/{id}/metrics` |
| 403 | signed in without `cluster:view`/`cluster:manage`; reading another user's cloud credential |
| 404 | settings never set; unknown cloud provider. **Not** used for missing clusters |
| 422 | JSON sent to the multipart-only submit endpoint; an invalid access mode or storage-class name |
| 500 | an unknown `order_by` column; storage classes on a non-`available` cluster; degraded observability routes |
| 502 | the cluster subsystem is unavailable during `sync-nodes` |
