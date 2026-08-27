# Onboarding protocol

Everything about `POST /clusters/clusters` - the single entry point for both
onboarding an existing cluster and provisioning a new one in a cloud account.
The cloud-specific sequence lives in `cloud-provisioning.md`.

`scripts/cluster-onboard` implements all of this. Read this file when you
need to drive it by hand, debug a rejected submission, or understand a failure.

## The request

**multipart/form-data only.** A JSON body is a 422. This is why `bud api`
cannot make the call.

| Field | Type | Notes |
|---|---|---|
| `step_number` | int > 0 | required on every call |
| `workflow_total_steps` | int | **first call only** - creates the session |
| `workflow_id` | uuid | **subsequent calls only** - advances the session |
| `name` | str 1..100 | unique case-insensitively across non-deleted clusters |
| `icon` | str 1..100 | emoji; validated |
| `ingress_url` | http(s) URL | how Bud's gateway reaches the cluster; required for `ON_PREM` |
| `configuration_file` | file | kubeconfig `.yaml`/`.yml`; required for `ON_PREM` |
| `cluster_type` | `ON_PREM` \| `CLOUD` | default `ON_PREM` |
| `credential_id`, `provider_id`, `region` | uuid, uuid, str | all three required for `CLOUD` on the triggering call |
| `trigger_workflow` | bool | default `false`. **Nothing happens until a call sets this true.** |

Hard validation rules, all returning 400:

- Supplying `workflow_id` **and** `workflow_total_steps` together.
- Supplying **neither**.
- A duplicate cluster name.
- Triggering without the accumulated data the type needs: `name`, `icon`,
  `cluster_type`, and for `ON_PREM` the kubeconfig plus `ingress_url`. The
  message names what is missing: `Missing required data: ...`.

At least one of `name` / `ingress_url` / `configuration_file` must appear in
every call.

## Response

Flat - unusually for this API, it is **not** nested under an entity key:

```json
{"object": "...", "message": "...", "workflow_id": "<the job id you track>",
 "status": "in_progress", "current_step": 1, "total_steps": 3, "reason": null,
 "workflow_steps": {"name": "...", "icon": "...", "ingress_url": "...",
                    "create_cluster_events": {"workflow_id": "...", "workflow_name": "register_cluster",
                                              "status": "PENDING", "eta": 2, "steps": [...]},
                    "cluster_id": "<appears once the cluster row exists>"}}
```

Two different workflow ids exist. The top-level `workflow_id` is the one you
use for `bud wait job`, `GET /workflows/{id}`, and `POST
/clusters/cancel-onboarding`. The one inside `create_cluster_events` belongs to
the installation engine and is not addressable from the public API.

## The step-wise (wizard) mode

Used by the console; useful when you want to validate fields before committing.

```bash
# 1. open the session - captures workflow_id
cluster-onboard --name lab-a100 --icon "🧠" --no-trigger --total-steps 3

# 2. add the connection details (do NOT resend --total-steps: 400)
cluster-onboard --workflow-id <id> --step 2 --no-trigger \
    --ingress-url http://10.0.4.20 --kubeconfig ./kubeconfig.yaml

# 3. trigger
cluster-onboard --workflow-id <id> --step 3
```

Fields accumulate across steps and merge **by key**, so re-sending a key in a
later step overwrites the earlier value. Nothing is validated for
`credential_id` / `provider_id` / `region` until the triggering call.

> The uploaded kubeconfig is held in the workflow record until the installation
> encrypts it. An abandoned onboarding therefore leaves cluster credentials in
> that record - clean up with `DELETE /workflows/{workflow_id}` (after
> cancelling) rather than leaving failed attempts lying around.

## Watching it

```bash
bud job show <workflow-id>          # normalised step list
bud wait job <workflow-id> --timeout 1800
```

Progress lives at `workflow_steps.create_cluster_events.steps[]`. Each step
gains a `payload` when it runs; `payload.content.status` is one of `STARTED`,
`RUNNING`, `COMPLETED`, `FAILED`, and `payload.content.message` carries the
text. `create_cluster_events.eta` is a minutes estimate that is refreshed as
the installation progresses (existing cluster: 3 → 2 → 1; cloud: 10 → 9 → 5 →
2 → 1).

Step order:

| Existing cluster (`ON_PREM`) | Cloud (`CLOUD`) |
|---|---|
| `determine_cluster_platform` | `create_cloud_provider_cluster` |
| `check_duplicate_config` | `verify_cluster_connection` |
| `verify_cluster_connection` | `configure_cluster` |
| `configure_cluster` | `fetch_cluster_info` |
| `fetch_cluster_info` | |

`configure_cluster` is where the minutes go: it installs hardware detection,
accelerator drivers, GPU sharing and monitoring into the cluster.
`fetch_cluster_info` then waits for the hardware inventory to settle.

**Terminal states.** Success = every step `COMPLETED`, `workflow_steps.cluster_id`
present, and the cluster reaching `status: available`. Failure = any step
`FAILED`; nothing after it runs.

> Verified on a live installation: an onboarding whose `configure_cluster`
> failed still reports top-level `status: in_progress`, `reason: null`, and
> `workflow_execution_status: null` - indefinitely. `current_step` can exceed
> `total_steps` (2 of 1) because those count wizard submissions, not
> installation steps. Never infer success or failure from them.

## Failure catalogue

| Failing step / symptom | Cause | Action |
|---|---|---|
| `determine_cluster_platform` FAILED | the kubeconfig cannot be parsed, or the API server is unreachable from Bud | fix the kubeconfig / network path; check the server URL resolves from Bud's network |
| `check_duplicate_config` FAILED: `already registering` | the same cluster is mid-onboarding | cancel that onboarding, or wait ~15 min for the stuck-registration timeout |
| `check_duplicate_config` FAILED: `already registered. Please delete it and try again` | it is already onboarded and healthy | use the existing cluster |
| `check_duplicate_config` FAILED: `currently being deleted` | a deletion is in flight | wait ~15 min |
| `verify_cluster_connection` FAILED | credentials in the kubeconfig lack permission, or the connection is refused | needs cluster-admin on the current context |
| `configure_cluster` FAILED | component installation failed inside the cluster - image pulls blocked, admission policies, no default storage, insufficient resources | read `content.message`, fix in the cluster, then onboard again (the failed attempt leaves no usable cluster) |
| `fetch_cluster_info` FAILED | hardware inventory could not be read | usually transient; retry onboarding |
| **no step fails, nothing advances** (cloud) | the cloud credential is bad or expired | the credential check reports through an event that matches no declared step, so it is silently dropped. Re-check the credential. |
| `create_cloud_provider_cluster` FAILED | provisioning in the cloud account failed - quota, permissions, region | read the message; nothing is left behind to clean up in Bud |

A submission rejected at the HTTP level (400) never started anything. A
submission that failed at a step did start something: an existing cluster may
have partially-installed components, and the safe recovery is to cancel, then
onboard again once the cause is fixed.

Existing-cluster registrations in a `not_available` state are replaced silently
on re-registration; the three blocking states above are the ones that make you
wait.

## Cancelling

```bash
bud api POST /clusters/cancel-onboarding -d '{"workflow_id":"<workflow-id>"}'
```

Terminates the installation and schedules a rollback that deletes the
partially-created cluster and the namespace Bud installed into. Not
instantaneous: the activity in flight only notices on its next checkpoint, so a
long `configure_cluster` finishes first.

- `400 Cluster onboarding process has not been initiated` - no installation was
  ever triggered. There is nothing to cancel; `DELETE /workflows/{id}` clears
  the record.
- `DELETE /workflows/{id}` alone **does not stop a running installation**. It
  only removes Bud's bookkeeping row, and 400s unless the workflow is still
  `in_progress`.

Confirm the rollback:

```bash
bud api GET /clusters/clusters -q name=<name> -q search=true | jq .total_record
```
