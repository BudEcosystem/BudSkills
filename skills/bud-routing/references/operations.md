# Router operations

Lifecycle, sync, runtime errors and verification. `references/dag-reference.md`
covers what goes *inside* a router.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/routers/actions` | Registry of action types with full param metadata |
| GET | `/routers/actions/{action_type}` | One action's metadata (404 if unknown or disabled) |
| POST | `/routers/actions/validate` | Dry-run one step's params |
| POST | `/routers/` | Create (trailing slash) |
| GET | `/routers/` | List (trailing slash) |
| GET | `/routers/{id}` | Read one, with DAG and sync state |
| PUT | `/routers/{id}` | Update - `dag_config` replaces the whole DAG |
| DELETE | `/routers/{id}` | Delete |
| POST | `/routers/{id}/retry-sync` | Reset the failure counter and re-push now |

All of them require the endpoint-manage permission. Without it you get 403
even on the read-only registry call.

**Response envelopes differ within the resource.** Single-router calls
(`POST /routers/`, `GET /routers/{id}`, `PUT`, `retry-sync`) put the router
under `.result`. `GET /routers/` is paginated and puts them under `.routers[]`
with `total_record`. Errors arrive in two shapes too - `{"success":false,
"message":...}` from create/read/update, `{"object":"error","message":...}`
from delete and retry-sync - so match on the HTTP status and read whichever
message field is present.

## Listing and finding

```bash
bud api GET /routers/ -q project_id=$BUD_PROJECT_ID --paginate
bud api GET /routers/ -q project_id=$P -q name=assistant -q search=true
bud api GET /routers/ -q project_id=$P -q order_by=-created_at

# every router in the project with its health at a glance
bud api GET /routers/ -q project_id=$P --paginate \
  | jq -r '.[] | "\(.name)\t\(.status)\t\(.sync_state)\t\(.step_count) steps"'
```

`warnings` is empty on reads - it is derived only on create and update, so
re-save a router if you want the advisories again.

## Status, sync state, and what actually reaches the gateway

Two independent fields decide whether a router routes:

| Field | Values | Meaning |
|---|---|---|
| `status` | `draft`, `active` | Intent. Only `active` routers are published; setting `draft` removes the alias |
| `sync_state` | `pending`, `synced`, `failed` | Whether the published configuration matches what you saved |

They are independent, and the combination that catches people is
`status:"draft"` with `sync_state:"synced"` - which is what a correctly
*unpublished* router looks like. `synced` means "the gateway matches your
intent", and for a draft the intent is "no alias". Check `status` first.

The publish step runs inline, right after the save, and takes milliseconds -
but it **cannot fail the API call**. On failure the router is still created or
updated, with `sync_state: "failed"`, `last_sync_error` (truncated to 1000
characters), `last_sync_attempt_at` and an incremented `sync_attempts`.

A background sweep retries `pending` and `failed` routers roughly every five
minutes, with exponential backoff per router that grows from 30 seconds to a
30-minute ceiling, and gives up after about ten consecutive failures. Only
`retry-sync` resets the counter - so a router that failed repeatedly overnight
stays broken until someone calls it.

```bash
# diagnose
bud api GET /routers/$R | jq '{status:.result.status, sync:.result.sync_state,
  attempts:.result.sync_attempts, at:.result.last_sync_attempt_at, err:.result.last_sync_error}'

# most common cause: a referenced deployment is gone
bud api GET /routers/$R | jq -r '.result.dag_config.steps[].params.modelRefs? // empty | .[]' | sort -u
bud api GET /endpoints/ -q project_id=$BUD_PROJECT_ID --paginate | jq -r '.[] | "\(.id) \(.status)"'

# fix the DAG if needed, then
bud api POST /routers/$R/retry-sync | jq '{sync:.result.sync_state, err:.result.last_sync_error}'
bud wait resource /routers/$R --field sync_state --equals synced --timeout 600
```

Update and rename mechanics worth knowing:

- `PUT` forces `sync_state` back to `pending` before re-publishing, so a
  process that dies mid-update is rescued by the sweep.
- Renaming removes the old alias first, then publishes the new one. If that
  removal fails it is only logged - an old alias can keep answering.
- Delete removes the record first; tearing the alias down is best-effort with
  no orphan sweep behind it. If a deleted router's name still resolves, report
  it rather than assuming the delete failed.

## Runtime errors

Every routing failure is `503` with the same envelope:

```json
{"error":{"type":"router_resolution_failed","code":"no_decision_matched",
  "router":"assistant-router","router_id":"...","request_id":"...","message":"..."}}
```

| `code` | Cause | Action |
|---|---|---|
| `no_decision_matched` | No rule covered the request | Add a `{"operator":"AND","conditions":[]}` Decision at low priority |
| `signal_not_ready` | A signal could not be evaluated; the extra `signal` field names it | Platform-side. Report with `request_id`; do not rewrite the DAG |

Branch on `code`, never on the message text. If the routing engine is
unreachable entirely the request does **not** 503 - it is served by one fixed
candidate endpoint, so routing silently stops applying while traffic looks
healthy.

Routers are published as chat-capable. Send chat completions through the alias;
address a deployment directly for other request types.

## `x-sr-*` response headers

Set on routed responses (streaming and not), capped at 32 headers and 1 KB per
value. Only headers with this prefix are forwarded.

| Header | Meaning |
|---|---|
| `x-sr-selected-decision` | The winning Decision's `name` |
| `x-sr-selected-model` | The deployment that served the request |
| `x-sr-selected-confidence`, `x-sr-selected-reasoning`, `x-sr-selected-category` | Why it was chosen |
| `x-sr-destination-endpoint` | Resolved destination |
| `x-sr-matched-complexity`, `-keywords`, `-embeddings`, `-domains`, `-language`, `-context`, `-structure`, `-conversation`, `-preference`, `-reask`, `-user-feedback`, `-kb`, `-projection` | Per-signal outcomes |
| `x-sr-looper-algorithm`, `-iterations`, `-models-used`, `-model`, `-decision` | Cascade / multi-call behaviour |
| `x-sr-orchestrating` | The matched route uses an orchestrating algorithm |
| `x-sr-cache-hit`, `x-sr-cache-similarity` | Semantic cache plugin |
| `x-sr-fast-response` | A canned response was returned without calling a model |
| `x-sr-injected-system-prompt` | System-prompt plugin fired |
| `x-sr-context-token-count` | Measured request size |

Some `x-sr-*` names exist for capabilities that are not enabled on every
installation - the absence of one is not evidence of a fault. The two that
always appear on a healthy routed response are `x-sr-selected-decision` and
`x-sr-selected-model`.

## Verifying behaviour

Per request, while iterating on rules:

```bash
curl -sS -D - -o /tmp/body.json "$GW/v1/chat/completions" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"assistant-router","messages":[{"role":"user","content":"Prove that sqrt(2) is irrational."}]}' \
  | grep -i '^x-sr-'
```

Aggregate, once traffic is flowing - counts by deployment over a window, and
the same list filtered to one deployment for a spot check:

```bash
bud api POST /metrics/inferences/list -d '{
  "project_id":"'"$BUD_PROJECT_ID"'","from_date":"2026-08-05T00:00:00Z",
  "limit":1000,"sort_by":"timestamp","sort_order":"desc"
}' | jq -r '.items[].endpoint_name' | sort | uniq -c | sort -rn

bud api POST /metrics/inferences/list -d '{
  "project_id":"'"$BUD_PROJECT_ID"'","endpoint_id":"'"$CANDIDATE"'",
  "from_date":"2026-08-05T00:00:00Z","limit":50
}' | jq '{total:.total_count}'
```

`from_date` is required. `total_count` against the same window without the
`endpoint_id` filter gives the canary share directly. Cost and latency
comparisons between branches belong to `bud-observability`.

A useful sanity rule when a split looks wrong: compare the count of requests
naming the router (from the client side) with the sum across its candidate
deployments. A large shortfall means requests are being answered somewhere
outside the router - usually a client still sending a deployment name directly.
