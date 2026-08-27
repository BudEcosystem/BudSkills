# Node inventory, hardware and metrics

Two different systems answer questions about a cluster, and telling them apart
is most of the skill here:

| | Source | Fails how |
|---|---|---|
| **Inventory** - which nodes exist, are they Ready, what hardware do they have | read from the cluster itself, refreshed on a few-minute cycle | hard error, or stale data |
| **Utilisation** - CPU/memory/disk/GPU numbers, events, time series | the observability pipeline (collector → time-series store → API) | silently zero, `metrics_available: false`, or a 500 |

`GET /clusters/{id}/node-metrics` merges both, and is the endpoint to reach for
first. Everything under `/metrics` is utilisation only.

## `GET /clusters/{id}/node-metrics`

```json
{"object": "info", "message": "...", "metrics_available": true,
 "nodes": {"10.0.4.20": {
    "hostname": "s5-x6-c9-medium-phx",
    "status": "Ready",
    "system_info": {"os": "N/A", "kernel": "N/A", "architecture": "N/A"},
    "pods": {"current": 0, "max": 0},
    "cpu": {"current": 3.61, "capacity": 22.67},
    "memory": {"current": 36.96, "capacity": 105.03},
    "network": {"bandwidth": [{"timestamp": 1785951600, "mbps": 0.0}]},
    "events_count": 0,
    "capacity": {"cpu": "22.67", "memory": "105Gi", "disk": "233189Gi"},
    "id": "89cb0e57-...",
    "devices": [...],
    "gpu": {"current": 0, "capacity": 8}}}}
```

- **The map key is the node's internal IP when it has one, otherwise its
  hostname.** Never assume it is an IP, and never pass it where a hostname is
  wanted - `/node-events/{hostname}` and the per-node time-series routes take
  `nodes[*].hostname`. (If two rows collide on IP - a replaced node leaving a
  stale row - Bud re-keys by hostname.)
- `metrics_available: false` means the observability pipeline is down. Node
  list and readiness are still trustworthy; every number will be zero.
- **Always-empty fields**, verified live: `system_info` is `N/A` throughout
  (kernel details are not retained) and `pods.current`/`pods.max` are hardcoded
  0. Do not report these as findings.
- `gpu` only appears on nodes that have accelerators.

### `devices[]`

Each entry is one *kind* of device on that node, not one physical device:

```json
{"type": "cpu_high", "total_count": 1, "available_count": 1,
 "device_config": {"name": "CPU", "type": "cpu_high", "raw_name": "Intel ... Xeon 6 (Granite Rapids, P-core)",
   "model": "Intel Family 6 Model 173 CPU", "vendor": "Intel", "family": "6", "generation": "Xeon 6 ...",
   "architecture": "amd64", "cores": 128, "physical_cores": 64, "threads": 128, "socket_count": 1,
   "memory_gb": 125.23, "utilized_cores": 30.14, "utilized_memory_gb": ..., "cache_mb": null,
   "frequency_ghz": null, "instruction_sets": [...], "mem_per_GPU_in_GB": ...,
   "inter_node_bandwidth_in_GB_per_sec": ..., "intra_node_bandwidth_in_GB_per_sec": ...}}
```

`type` values: `cpu`, `cpu_high`, `gpu`, `cuda`, `rocm`, `hpu`. `cpu_high`
means a CPU with the wide vector/matrix instruction sets that make CPU serving
viable; `gpu` is legacy and current detections report `cuda` or `rocm`.

`instruction_sets`, `mem_per_GPU_in_GB` and the bandwidth figures are what the
capacity planner sizes deployments with - if they look wrong, the deployment
sizing will be wrong too.

> GPU counting is not a simple sum. When GPU sharing is enabled the hardware
> report fans out (an 8-GPU node can produce 64 entries covering 8 distinct
> devices), so Bud counts distinct device identities and only falls back to
> summing `total_count` when no identity is present. A node mixing both styles
> under-counts. Cross-check `gpu_count` on the cluster against
> `devices[].total_count` and the GPU metrics route before trusting a number
> you are going to size against.

## Refreshing the inventory

```bash
bud api POST /clusters/<id>/sync-nodes
```

- Returns `{"object":"cluster.sync_nodes","message":"Cluster node sync started","workflow_id":...}`.
  200 means **queued**. Nothing to poll - observe the effect after ~30-60s.
- Needs `cluster:manage`, even though it only refreshes a read model.
- If nothing changes, either there was no delta or the cluster was unreachable.
  Check `status`: unreachable clusters move to `not_available`, and after 24h
  to `error`.
- A failed sync also removes the cluster from the shared inventory the capacity
  planner reads, so the cluster stops appearing in deployment recommendations
  before it ever looks unhealthy in the UI.

Without this call, inventory is as fresh as the automatic cycle (a few
minutes). A fleet-wide sync also runs continuously; clusters stuck
`not_available` for 24h are moved to `error` and retried daily.

## Metrics routes

| Route | Scope | Notes |
|---|---|---|
| `GET /clusters/{id}/metrics` | per node + cluster summary | `filter=today\|7days\|month`, `metric_type=all\|memory\|cpu\|disk\|gpu\|hpu\|network_bandwidth\|power`. Keyed by **hostname**. Any upstream failure becomes a 400. |
| `GET /clusters/clusters/{id}/metrics/summary` | one-line cluster roll-up | note the doubled path segment |
| `GET /clusters/clusters/{id}/metrics/nodes` | raw node rows | `start_time`/`end_time` ISO8601. Prefer `/node-metrics`. |
| `GET /clusters/clusters/{id}/metrics/pods` | pod-level | `namespace`, `start_time`, `end_time` |
| `GET /clusters/clusters/{id}/metrics/health` | stored-metrics health view | **not** a live cluster probe; see below |
| `POST /clusters/clusters/{id}/metrics/query` | free-form query | body is forwarded; the cluster identifier is injected for you |
| `GET /clusters/{id}/metrics/gpu` | GPU devices, sharing slices, summary | only meaningful with GPU sharing installed |
| `GET /clusters/{id}/nodes/{hostname}/metrics/gpu` | one node's GPUs | hostname, not the node-metrics key |
| `GET /clusters/{id}/nodes/{hostname}/metrics/gpu/timeseries` | utilisation, memory, temperature, power, slice activity | `hours` 1..168, default 6 |
| `GET /clusters/{id}/nodes/{hostname}/metrics/cpu/timeseries` | CPU usage and load averages | `hours` 1..168, default 6 |
| `GET /clusters/{id}/node-events/{hostname}` | Kubernetes events for a node | from stored data, not live |
| `GET /clusters/{id}/grafana-dashboard` | monitoring dashboard URL | resolves by the internal cluster identifier |

The five `/clusters/clusters/{id}/metrics/*` routes are raw pass-throughs: no
envelope, no `object` key, and no `cluster:view` check - any signed-in user can
read them. They are also the ones most likely to break when the observability
stack is reconfigured.

Observed on the installation used to write this skill:

- `/clusters/clusters/{id}/metrics/health` → **500 "Error retrieving health
  status"**.
- `/clusters/{id}/grafana-dashboard` → **500**, the dashboard URL could not be
  resolved.
- `/clusters/clusters/{id}/metrics/summary` returns plausible-looking but
  inconsistent aggregates - it sums samples over the window, so a single
  128-core node reported `node_count: 1` alongside `total_cpu_cores` in the
  thousands, drifting between calls - and `cluster_name` is set to the
  cluster's internal identifier rather than its name. Use `/node-metrics` for
  capacity you intend to act on.

A 500, or a 400 carrying internal transport detail, on any of these is an
**installation fault, not a malformed request**. Report it; do not paper over
it with retries or by switching endpoints until one answers.

## What Bud does not expose

There is no public route to a live Kubernetes health probe (node readiness, API
server, volume binding, DNS, GPU driver). `/metrics/health` is derived from
stored time-series and will look healthy or empty when the cluster is actually
unreachable. The truth available to you is `status` on the cluster plus
per-node `status` in `/node-metrics`.

Also not exposed: structured device info for external sizing tools, the stored
cluster configuration, and live per-node event counts. And there is **no
node-pool API anywhere** - nodes are discovered, never declared. Bud cannot
create, scale, drain, taint or label nodes; do that with your own Kubernetes
tooling and then `sync-nodes`.

## Diagnosing an unhealthy cluster

```bash
bud api GET /clusters/<id> | jq '.cluster | {status, total_nodes, available_nodes, cpu_count, gpu_count, hpu_count}'
bud api GET /clusters/<id>/node-metrics | jq '{metrics_available, nodes: (.nodes | map_values(.status))}'
bud api GET /clusters/<id>/node-events/<hostname> | jq '.events[:20]'
bud api GET /clusters/<id>/metrics -q filter=today -q metric_type=all
bud api GET /clusters/<id>/metrics/gpu | jq .summary
bud api POST /clusters/<id>/sync-nodes && sleep 60
```

Reading the result:

| Signal | Conclusion |
|---|---|
| a node `NotReady` | genuine cluster problem - go to the node's events |
| all nodes `Ready`, cluster `not_available` | inventory is stale or the last sync failed - `sync-nodes` and re-check |
| `metrics_available: false`, nodes listed and Ready | monitoring pipeline problem, cluster is fine |
| every metrics route empty but `/node-metrics` lists nodes | the collection pipeline stopped; the cluster itself is healthy |
| cluster `error` | it has been unreachable for over a day; it is retried daily, so fix connectivity and force a sync |
| `gpu_count: 0` shortly after onboarding | drivers were still installing during the first snapshot - `sync-nodes` |
