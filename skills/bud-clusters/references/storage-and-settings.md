# Storage settings, editing, deletion and the fleet view

## Storage settings

Model deployments claim volumes for weights and cache. The class they use comes
from the cluster's settings, so **a cluster with no settings is available but
not deployable** - and the failure surfaces late, during the deployment, not
when you check the cluster.

| Call | Behaviour |
|---|---|
| `GET /clusters/{id}/settings` | `{object:"cluster.settings.get", settings:{...}}`; 404 `Cluster settings not found` when never set |
| `POST /clusters/{id}/settings` | creates. 201. **No duplicate guard** - a second call tries to create a second row |
| `PUT /clusters/{id}/settings` | upsert. **Full replace** - an omitted field is written as null |
| `DELETE /clusters/{id}/settings` | removes them; 404 body when there is nothing to delete |

Use `PUT`, always with both fields:

```bash
bud api PUT /clusters/<id>/settings -d '{
  "default_storage_class": "local-path",
  "default_access_mode": "ReadWriteOnce"
}'
```

A live settings record looks like:

```json
{"object": "cluster.settings.get", "message": "...",
 "settings": {"default_storage_class": "local-path", "default_access_mode": "ReadWriteOnce",
              "id": "...", "cluster_id": "...", "created_by": "...",
              "created_at": "...", "modified_at": "..."}}
```

### Choosing the class

```bash
bud api GET /clusters/<id>/storage-classes
```

Response is the one bare payload in this domain - no envelope wrapper, and the
only body that carries `code`:

```json
{"storage_classes": [{"name": "local-path", "default": true, "provisioner": "...",
                      "recommended_access_mode": "ReadWriteOnce"}],
 "message": "...", "object": "cluster.storage-classes", "code": 200}
```

Bud's own rule, worth copying: take the class flagged `default` when exactly one
is; if there is exactly one class at all, take it; otherwise **ask**. The
decision matters because it determines whether replicas can share weights:

| Access mode | Effect |
|---|---|
| `ReadWriteOnce` | one node may mount the volume - fine for single-node deployments, blocks multi-node sharing |
| `ReadWriteMany` | many nodes may mount it - what multi-replica and multi-node deployments want |
| `ReadOnlyMany` | many readers, no writer |
| `ReadWriteOncePod` | one pod only - the most restrictive |

Anything else is a 422. Storage class names must match `^[a-zA-Z0-9.-]*$` and
may not begin or end with `-`.

Omitting `default_access_mode` on a create makes Bud resolve it from the
class's `recommended_access_mode`; if that lookup fails it stores null, which
is the same problem as not setting it.

**When `/storage-classes` errors, look at the cluster's `status` first.** The
lookup reads the live cluster and is refused for any status other than
`available`. The error is not tidy: it surfaces as a 500 carrying the internal
reason (for example `Failed to fetch storage classes: 404: Cluster not found`,
or a transport error when the cluster subsystem itself is down) even though the
cluster plainly exists. Check `status` before believing the message.

### Auto-seeding at onboarding

Bud tries to seed these settings right after the cluster row is created, with a
hard 10-second budget, and refuses to guess when zero or several classes are
flagged default. It never fails onboarding. So a 404 from `GET .../settings`
immediately after onboarding is the expected outcome of an ambiguous cluster,
not an error - just set it.

## Editing a cluster

```bash
bud api PATCH /clusters/<id> -d '{"name":"lab-a100","icon":"🧠","ingress_url":"http://10.0.4.21"}'
```

Send only the fields you are changing; omitted fields are left alone (unlike
settings). Returns the cluster nested under `.cluster`.

- Changing `ingress_url` is pushed into the cluster's own record first and the
  whole call fails with 400 if that does not take - so a 400 here can mean "the
  cluster is unreachable", not "your request was malformed".
- A duplicate name (case-insensitive, excluding this cluster) is a 400.

There is no API to change a cluster's kubeconfig. Replacing credentials means
deleting and re-onboarding.

## Deletion

```bash
bud api GET /clusters/<id>/endpoints -q limit=1 | jq .total_record   # must be 0
bud api POST /clusters/<id>/delete-workflow
```

Refused with 400 when any deployment still exists (`Cannot delete cluster with
active deployments`) or the cluster is already `deleting`.

The response carries no workflow id. Recover it:

```bash
bud api GET /workflows -q workflow_type=cluster_deletion -q limit=5 \
  | jq -r '.workflows[] | "\(.id)  \(.title)  \(.status)"'
bud job show <id>
```

One step: `delete_namespace`. State transitions:

| State | Meaning |
|---|---|
| `deleting` (immediately) | Bud has accepted it; the uninstall is running |
| `deleted` | done - `GET /clusters/{id}` now answers `400 Cluster not found` and it is gone from the list |
| stuck in `deleting` | the uninstall failed; after 24h it is force-moved to `error` |

For a cloud-provisioned cluster this also destroys the infrastructure using the
stored cloud credential, and takes 10-20 minutes rather than ~30 seconds. A
rotated or deleted credential leaves the cluster stuck in `deleting`.

Re-onboarding the same cluster is blocked for ~15 minutes after a deletion
starts (`This cluster is currently being deleted`).

> `is_default: true` marks the cluster Bud itself runs on. Nothing blocks
> deleting it, and doing so breaks the platform's own inference path. Never
> delete it, and call it out if a user asks.

## Fleet and per-project views

```bash
# whole fleet, newest first
bud api GET /clusters/clusters -q page=1 -q limit=50 -q order_by=-created_at

# cheap count
bud api GET /clusters/clusters -q limit=0 | jq .total_record

# fuzzy name search (exact match without search=true)
bud api GET /clusters/clusters -q name=lab -q search=true
```

`order_by` takes a comma list over cluster columns, e.g.
`-created_at,name:asc`. An unknown column raises a 500, not a 422 - stick to
fields you have seen in the response.

Capacity roll-up per cluster: `cpu_count` / `gpu_count` / `hpu_count` (device
counts), `*_total_workers` and `*_available_workers` (schedulable capacity), and
`total_nodes` / `available_nodes`. Sum across the fleet for headroom; deleted
clusters are always excluded server-side.

The detail response adds `total_workers_count`, `active_workers_count`,
`total_endpoints_count`, `running_endpoints_count` and `hardware_type` (any of
`CPU`, `GPU`, `HPU`, derived from the device counts).

Per project:

```bash
bud api GET /projects/<project-id>/clusters -q page=1 -q limit=50
```

This needs `project:view` rather than `cluster:view`, and **inner-joins
deployments** - a cluster the project has no deployments on never appears, so
an empty result does not mean the project has no compute available to it.

To see what is running on a cluster:

```bash
bud api GET /clusters/<id>/endpoints -q limit=50 -q order_by=status \
  | jq -r '.endpoints[] | "\(.status)  \(.name)  \(.project.name)  \(.model.name)  workers \(.active_workers)/\(.total_workers)"'
```

Filters: `name`, `status`, `search`, `order_by` over
`name|status|created_at|project_name|model_name`. Ignore `roi` - it is a
placeholder that always reads 12.
