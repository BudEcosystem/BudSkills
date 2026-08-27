---
name: bud-clusters
description: Put compute under Bud Foundry's control and keep it healthy - onboard an existing Kubernetes or OpenShift cluster from a kubeconfig, provision a managed cloud cluster, set the storage defaults deployments need, read node and GPU inventory, check capacity and metrics, diagnose a cluster that shows as unavailable, and delete one. Use when a Bud request mentions clusters, kubeconfig, onboarding or adding compute, nodes, GPUs or accelerators, cluster capacity, health or storage classes, or asks where a model could run.
---

# Bud Foundry: clusters

A **cluster** is a Kubernetes or OpenShift installation Bud can schedule model
deployments onto. You either *onboard* one you already run (Bud installs its
own components into it) or *provision* a managed one in a cloud account. Once a
cluster is `available` **and** has a storage class it becomes a deployment
target; until then it is invisible to everything downstream.

Prerequisite: connect first - see `bud-platform` (`bud login`).

**The collection is nested**: clusters live at `/clusters/clusters`; plain
`GET /clusters/` is a 404. Single-cluster paths are not nested -
`/clusters/{id}`, `/clusters/{id}/settings`, and so on.

## See what compute exists

```bash
bud api GET /clusters/clusters -q page=1 -q limit=50 -q order_by=-created_at \
  | jq -r '.clusters[] | "\(.id)  \(.status)  \(.name)  nodes \(.available_nodes)/\(.total_nodes)  gpu \(.gpu_available_workers)/\(.gpu_total_workers)  cpu \(.cpu_available_workers)/\(.cpu_total_workers)"'
```

`status` is `available` | `not_available` | `error` | `deleting` | `deleted`;
`*_total_workers` / `*_available_workers` are schedulable capacity per kind
(`cpu`, `gpu`, `hpu`); `is_default: true` marks the cluster Bud itself runs on.
Name filtering is **exact match** unless you add `search=true`.

> A cluster can show `not_available` while still listing a full node inventory
> with every node `Ready`. The counts are the last snapshot that succeeded; the
> status is the result of the most recent sync attempt. For "can I deploy
> here?", trust `status`.

**Two ids, and each is required somewhere different.** Every cluster carries
both `id` and `cluster_id`, always different values. **`id` is what every
`/clusters/...` REST path takes.** `cluster_id` is the key the rest of the
platform uses - notably the deployment call, which wants `cluster_id`, not `id`
(see `bud-deployments`). Passing the wrong one is usually a silent empty result
rather than an error, which is why this is easy to miss.

```bash
bud api GET /clusters/$C | jq '.cluster | {id, cluster_id, status, cluster_type, hardware_type, total_nodes}'
```

> Detail nests under `.cluster`. A missing or deleted cluster answers
> **400 "Cluster not found"**, not 404 - do not test for 404 here. And
> `cluster_type` is `null` in the *list* response but populated (`ON_PREM` /
> `CLOUD`) in *detail*.

## Onboard an existing cluster

The common case. You supply a kubeconfig; Bud verifies the connection, then
installs its own components (hardware detection, accelerator drivers, GPU
time-slicing, metric collection) into a namespace it owns - that install is the
slow part. Collect first: a **kubeconfig** whose current context has
cluster-admin rights, an **ingress URL** Bud's inference gateway can reach
(`http(s)://host`), and a **name** unique across the installation
(case-insensitive).

### 1. Submit it

`POST /clusters/clusters` is the only Bud endpoint that takes
`multipart/form-data` (it carries the file), so `bud api` cannot send it. Use
the helper in this skill:

```bash
scripts/cluster-onboard \
  --name "edge-uk" --icon "🌐" \
  --ingress-url "http://10.0.4.11" \
  --kubeconfig ./kubeconfig.yaml \
  --wait --timeout 1800
```

Progress goes to stderr, the response JSON to stdout. `--dry-run` shows exactly
what would be sent, `--watch <job-id>` re-attaches to a run already in flight,
and `--help` lists every flag including the stepwise wizard mode
(`--workflow-id` / `--step`).

> Onboarding **changes infrastructure the user owns**. Confirm the target
> cluster and the ingress URL before submitting; never onboard something the
> user has not explicitly named.

### 2. Wait, and read progress properly

```bash
bud wait job $W --timeout 1800 --interval 20
```

Realistic duration: **5-15 minutes** for a small cluster, longer over slow
networks or when accelerator drivers must be installed. The platform's own ETA
starts around 3 minutes and is optimistic; overrunning it is not a failure.

| Step | What is happening | Typical |
|---|---|---|
| `determine_cluster_platform` | Kubernetes vs OpenShift | seconds |
| `check_duplicate_config` | already registered? | seconds |
| `verify_cluster_connection` | can Bud reach the API server? | seconds |
| `configure_cluster` | **installs Bud's components** - the long one | minutes |
| `fetch_cluster_info` | node and hardware discovery, then settles | ~1 min |

**Telling "still installing" from "failed" is the whole game**, because the
job's top-level `status` does not help: it stays `in_progress` after a
definitive failure and never settles on its own. Read the steps.

```bash
bud job show $W | jq -r '.summary, (.steps[] | "\(.status)\t\(.id)\t\(.message)")'
```

| What you see | What it means |
|---|---|
| Last step `configure_cluster`, `running`/`started`, nothing failed | Still installing. Keep waiting. |
| Any step `failed` | **Over.** No later step runs - they stay blank forever. Read that step's `message`. |
| All steps `completed`, job `status: completed` | Done - go to step 3. |
| Nothing advances for minutes and nothing is failed | Stalled. See the cloud-credential note below, then treat as failed. |

`bud wait job` implements exactly this: it raises on the first failed step with
the platform's reason and returns when the work settles.

**On failure, stop - do not resubmit the same kubeconfig.** Re-registering is
refused with a specific message: *"already registering"* (a previous attempt
still holds it - wait ~15 minutes), *"already registered. Please delete it and
try again"* (find it in the list instead), *"currently being deleted"* (wait
~15 minutes). Full failure catalogue: `references/onboarding.md`.

### 3. Confirm the cluster is usable

Job completion is not usability. Find the cluster, then wait for `available`:

```bash
C=$(bud api GET /clusters/clusters -q name="edge-uk" | jq -r '.clusters[0].id')
bud wait resource /clusters/$C --field status --equals available --timeout 900
```

(It also lands in the job data once the row exists - `bud job show $W --data` -
but lookup by name is more robust.) Then check what hardware Bud actually saw:

```bash
bud api GET /clusters/$C/node-metrics \
  | jq -r '.nodes | to_entries[] | "\(.value.hostname)\t\(.value.status)\t\([.value.devices[] | "\(.type)x\(.total_count)"] | join(","))"'
```

> **A brand-new GPU cluster often reports `gpu_count: 0` on its first
> snapshot** - the accelerator components were only just installed and the
> hardware has not been re-scanned. Run `sync-nodes`, wait ~60s, look again
> before concluding the GPUs are missing.

### 4. Give it a storage class - or deployments fail later

The step people skip. Deployments need a persistent volume and take the storage
class from cluster settings. Onboarding tries to seed this but gives up (within
~10 seconds) when there is no single obviously-default class - so **a 404 here
is normal, not a bug**.

```bash
bud api GET /clusters/$C/settings | jq '.settings'          # 404 => not seeded
bud api GET /clusters/$C/storage-classes | jq '.storage_classes'
bud api PUT /clusters/$C/settings \
  -d '{"default_storage_class":"local-path","default_access_mode":"ReadWriteMany"}'
```

- Use **PUT** (upsert). `POST` has no duplicate guard; a second call attempts a
  second row.
- PUT is a **full replace, not a merge** - omitting `default_storage_class`
  writes `null` over a working value. Send both fields every time.
- Access mode is exactly one of `ReadWriteOnce`, `ReadWriteMany`,
  `ReadOnlyMany`, `ReadWriteOncePod`; omit it and Bud takes the class's
  recommended mode, or stores `null` if it cannot work one out.
- Which class? The one flagged `default` if exactly one is; else the only class
  if there is only one; otherwise **ask the user** - guessing wrong produces
  deployments that never bind a volume. More:
  `references/storage-and-settings.md`.

## Provision a managed cloud cluster

> `GET /credentials/cloud-providers` lists 16 providers, but only **Azure**
> (`unique_id: "azure"`) and **AWS** (`unique_id: "aws"`) can actually be
> provisioned. Anything else fails inside the job with "Unsupported provider".
> Filter on `unique_id` before offering the user a choice.

```bash
PROV=$(bud api GET /credentials/cloud-providers | jq -r '.providers[]|select(.unique_id=="azure")|.id')
# that provider's .schema_definition names the exact credential fields it needs
bud api POST /credentials/cloud-providers -d '{
  "provider_id":"'"$PROV"'", "credential_name":"azure-prod",
  "credential_values":{"subscription_id":"...","tenant_id":"...","client_id":"...","client_secret":"..."}}'

# the create response does NOT return the id - fetch it back (newest first)
CRED=$(bud api GET /credentials/cloud-providers/credentials -q provider_id=$PROV | jq -r '.credentials[0].id')
REGION=$(bud api GET /credentials/cloud-providers/$PROV/regions | jq -r '.regions[0].id')

scripts/cluster-onboard --cluster-type CLOUD --name "aks-eu" --icon "☁️" \
  --provider-id "$PROV" --credential-id "$CRED" --region "$REGION" \
  --wait --timeout 3600
```

Expect **10-25 minutes**: the cloud cluster is built first, then the same
install path as on-prem runs on top. No kubeconfig and no ingress URL - Bud
fills the ingress URL in from the provisioning output.

> **The cloud path has one failure that is invisible in the step list.** A bad
> or expired credential is reported against a stage with no matching step, so
> the list simply never advances and nothing is marked failed. A cloud job
> showing no progress for several minutes at the start means the credential,
> not the cloud.

Deleting a cloud cluster re-uses that credential to tear the cluster down, so
rotating or deleting it strands the cluster. See
`references/cloud-provisioning.md`.

## Cancel an onboarding

```bash
bud api POST /clusters/cancel-onboarding -d '{"workflow_id":"'"$W"'"}'
```

That is the **job id**, not the cluster id. Bud stops the work and rolls back
the partially-created cluster and namespace.

- `400 "Cluster onboarding process has not been initiated"` means the job was
  never triggered, so there is nothing to stop - just
  `bud api DELETE /workflows/$W` to clear the record.
- Cancellation is not instant: a long `configure_cluster` finishes its current
  activity first. Poll `bud api GET /clusters/clusters -q name=<name>` until the
  cluster is gone.
- **`DELETE /workflows/{id}` alone does not stop the work** - it only clears the
  bookkeeping record, and is rejected once the job is not `in_progress`.

## Choose a cluster for a workload

Do not pick by hand for a real deployment: run the deployment session and read
`GET /clusters/recommended/{deployment_workflow_id}` for per-cluster cost and
predicted latency (that flow belongs to `bud-deployments`). What this skill
adds is the sanity check before committing:

```bash
bud api GET /clusters/$C | jq '.cluster | {status, hardware_type, available_nodes,
   gpu_free: .gpu_available_workers, cpu_free: .cpu_available_workers}'
bud api GET /clusters/$C/settings | jq '.settings.default_storage_class'   # must not be null
```

Three things disqualify a cluster whatever a recommendation says: `status` is
not `available`; `default_storage_class` is missing; or the accelerator kind you
need is absent from `hardware_type` or has 0 `*_available_workers`.

> An **empty** recommendation list is ambiguous - recommendations only include
> clusters that are `available` right now, so "no options" can mean "every
> candidate is unhealthy" rather than "nothing fits". Check fleet statuses
> before reporting "no capacity".

## Keep the inventory fresh

Bud re-scans every cluster on a few-minute cycle. Force it after adding nodes,
installing drivers, or whenever hardware looks wrong:

```bash
bud api POST /clusters/$C/sync-nodes   # 200 means QUEUED, not done
sleep 60 && bud api GET /clusters/$C | jq '.cluster | {status, gpu_count, cpu_count, total_nodes}'
```

Re-reading immediately returns pre-sync data and there is no job to follow -
**observe the effect, not the call**. If nothing changed after a minute, either
there was no delta or the cluster was unreachable; `status` says which.

There is **no node-pool API**. Nodes are discovered, never declared - Bud cannot
add, scale, drain or taint them. Use your own tooling, then sync.

## Diagnose a cluster that shows `not_available`

```bash
# 1. unhealthy nodes - and whether monitoring itself is down
bud api GET /clusters/$C/node-metrics \
  | jq -r '.metrics_available, (.nodes|to_entries[]|"\(.value.hostname)\t\(.value.status)\tevents \(.value.events_count)")'
# 2. what happened on a suspect node - pass the HOSTNAME, not the map key
bud api GET /clusters/$C/node-events/<hostname>
# 3. utilisation and accelerator pressure
bud api GET /clusters/$C/metrics -q filter=today | jq '.cluster_summary'
bud api GET /clusters/$C/metrics/gpu | jq '.summary'
# 4. stale inventory, or genuinely broken?
bud api POST /clusters/$C/sync-nodes && sleep 60 && bud api GET /clusters/$C | jq '.cluster.status'
```

- `metrics_available: false`, or every number zero while nodes read `Ready`, is
  a **monitoring** problem, not a cluster problem. Say so rather than reporting
  the cluster broken.
- Node-metrics keys are internal IPs *when known*, else hostnames - never assume
  the key is an IP; always pass `nodes[*].hostname` to `/node-events/`.
- `system_info` is effectively always `N/A` and `pods.current`/`pods.max` always
  0; they are not evidence of anything.
- `/storage-classes` failing is itself a signal - it reads the live cluster and
  needs it reachable and `available`.
- Unreachable for 24 hours moves a cluster to `error`; it is retried daily.

Renaming and re-pointing (`PATCH /clusters/{id}`) are in
`references/storage-and-settings.md`.

## Delete a cluster

```bash
bud api GET /clusters/$C/endpoints -q limit=1 | jq .total_record   # must be 0
bud api POST /clusters/$C/delete-workflow
```

**Confirm with the user first, naming the cluster.** This uninstalls Bud's
components from their infrastructure; for a cloud-provisioned cluster it
destroys the cluster itself.

- Refused (400) while any deployment still exists - delete those first
  (`bud-deployments`).
- **The call does not return a job id.** Recover it with
  `bud api GET /workflows -q workflow_type=cluster_deletion -q limit=5`,
  matching `title` against the cluster name, then `bud wait job <id>`.
- On-prem teardown is ~30 seconds; a cloud cluster is 10-20 minutes. Confirm by
  re-fetching: it answers 400 "Cluster not found" and leaves the list. Stuck in
  `deleting` for over a day is force-moved to `error` - for a cloud cluster the
  usual cause is a rotated credential the teardown cannot use.
- **Never delete the `is_default: true` cluster.** Nothing blocks it, and it is
  where the platform itself runs.

## Deeper reference

- `references/onboarding.md` - form fields, the stepwise wizard, raw progress
  payloads, every re-registration refusal, cancellation semantics
- `references/cloud-provisioning.md` - providers, credential schemas, regions,
  the cloud step sequence and its distinct failure modes
- `references/inventory-and-metrics.md` - node/device shapes, accelerator
  counting, every metrics route, and what is *not* exposed
- `references/storage-and-settings.md` - storage classes, access modes, the
  auto-seed rules, the PUT-replaces-everything trap, plus renaming, deletion
  states and the fleet/per-project views
- `references/endpoint-reference.md` - every endpoint with its parameters,
  response shape and permission, plus all the enum values

## Where to go next

`bud-deployments` to put a model on the cluster and compare clusters on cost
and latency, `bud-models` to get a model first, `bud-observability` for traffic
and cost once things run, `bud-projects` for the per-project view
(`GET /projects/{project_id}/clusters`).
