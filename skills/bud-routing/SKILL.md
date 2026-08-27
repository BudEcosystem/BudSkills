---
name: bud-routing
description: Route inference between Bud Foundry models with a router - send easy prompts to a cheap deployment and hard ones to a strong one, split traffic 90/10 for a canary or A/B rollout, cascade from cheap to strong on low confidence, and expose it all as one model name clients call. Use when a Bud request mentions routers or routing, traffic splitting, canary or A/B rollouts, "make inference cheaper without losing quality", picking a model per request, or a router that looks saved but is not taking effect.
---

# Bud Foundry: routing

A **router** is a named alias that decides, per request, which deployment
answers it. Callers keep sending one `model` name; behind it the router
measures the request, matches it against your rules and picks a deployment.
That is how you get "cheap model for easy questions, strong model for hard
ones" and "10% of traffic to the new model" without touching client code.

Prerequisite: connect first - see `bud-platform`. You also need a project
(`bud-projects`) and at least two `running` deployments (`bud-deployments`) -
a router only chooses between deployments that already exist, and refers to
them by endpoint id. Routing calls need the endpoint-manage permission.

```bash
bud api GET /endpoints/ -q project_id=$BUD_PROJECT_ID --paginate \
  | jq -r '.[] | "\(.id)  \(.name)  \(.status)"'
```

## How a router is built

A router holds a small graph (`dag_config.steps`) of five kinds of node:

| Kind | `action_type` | What it does |
|---|---|---|
| Signal | `signal_complexity`, `signal_keyword`, ... | Measures something about the request |
| Decision | `decision` | Fires when its rule matches; lists candidates in `modelRefs` |
| Projection | `projection_score`, `projection_mapping`, ... | Turns signals into a score or named bands |
| Algorithm | `algorithm_weighted_random`, `algorithm_confidence`, ... | Picks among a Decision's candidates |
| Plugin | `plugin_semantic_cache`, `plugin_system_prompt`, ... | Alters the request or response on that route |

Decisions are evaluated by `priority`, highest first, first match wins.
Algorithms and Plugins attach to exactly one Decision via `depends_on`.

**Three rules cause most of the failures:**

1. A rule leaf references a step by its **`name`**, never its `step_id`, and
   the leaf's `type` is the signal's class (`complexity`, `keyword`,
   `language`, `projection`, ...), not its action type.
2. Complexity leaves use `"<step name>:<level>"`, level `easy`/`medium`/`hard`.
   A bare step name never matches at runtime.
3. `{"operator":"AND","conditions":[]}` is the catch-all. Without one Decision
   shaped like that, requests your rules do not cover are rejected with a 503.

`GET /routers/actions` is the authoritative registry of action types and their
params; `POST /routers/actions/validate` dry-runs one step's params. Prefer
both over guessing. Full catalogue: `references/dag-reference.md`.

## Make inference cheaper without losing quality

Classify each request; easy ones go to the cheap deployment, the strong one is
kept for the hard ones.

```bash
CHEAP=<cheap-endpoint-id>; STRONG=<strong-endpoint-id>

bud api POST /routers/ -d '{
  "project_id": "'"$BUD_PROJECT_ID"'",
  "name": "assistant-router",
  "status": "active",
  "dag_config": {
    "name": "assistant-router",
    "entry_step": "complexity",
    "steps": [
      {"step_id":"complexity","name":"Is Hard","action_type":"signal_complexity",
       "params":{"threshold":0.7,
         "hard_candidates":["Derive a closed-form solution for T(n)=2T(n/2)+n.",
                            "Prove that every continuous function on [0,1] attains its supremum."],
         "easy_candidates":["What is the capital of France?","Add 12 and 8."]}},
      {"step_id":"decide_hard","name":"Route Hard","action_type":"decision",
       "params":{"priority":100,
         "rules":{"operator":"AND","conditions":[{"type":"complexity","name":"Is Hard:hard"}]},
         "modelRefs":["'"$STRONG"'"]}},
      {"step_id":"decide_rest","name":"Route Default","action_type":"decision",
       "params":{"priority":0,
         "rules":{"operator":"AND","conditions":[]},
         "modelRefs":["'"$CHEAP"'"]}}
    ]}}' | jq '{id:.result.id, sync:.result.sync_state, warnings:.result.warnings}'
```

**Trailing slash required** (`POST /routers/`, not `/routers`); single-router
responses nest under `.result`, while the list endpoint uses `.routers[]`.

Read two fields back before you celebrate. `sync_state` must be `synced` - a
`201` only means the router was saved; the push to the gateway is reported
separately and never fails the call. `warnings` is advisory but rarely
ignorable - the usual entry is the missing-catch-all warning. And `status`
matters as much as the DAG: **only `active` routers reach the gateway**, so a
`draft` router is a document, not a route, and flipping one back to `draft`
tears its alias down.

The quality dial is `threshold` (0-1, higher = fewer requests judged hard) plus
the two candidate lists - four to six real examples of your own traffic on each
side beat generic ones.

**To try the cheap model first and escalate only when its answer is weak**, put
both endpoints on one Decision, cheap first, and attach a confidence cascade:

```json
{"step_id":"cascade","name":"Cascade By Confidence","action_type":"algorithm_confidence",
 "depends_on":["decide_all"],
 "params":{"confidence_method":"hybrid","threshold":0.75,
           "escalation_order":"small_to_large","on_error":"escalate"}}
```

`on_error:"escalate"` doubles as a **fallback chain**: when a candidate errors,
the request moves up the list instead of failing. Escalated requests pay for two
generations, so this beats "always use the strong model" only when most traffic
stops at the first hop - watch `x-sr-looper-models-used`.

## Roll a new model out to 10% of traffic

One catch-all Decision listing both deployments, with a weighted draw attached.

```bash
bud api POST /routers/ -d '{
  "project_id": "'"$BUD_PROJECT_ID"'",
  "name": "assistant-router-canary",
  "status": "active",
  "dag_config": {
    "name": "assistant-router-canary",
    "entry_step": "decide_all",
    "steps": [
      {"step_id":"decide_all","name":"All Traffic","action_type":"decision",
       "params":{"priority":0,"rules":{"operator":"AND","conditions":[]},
                 "modelRefs":["'"$STABLE"'","'"$CANDIDATE"'"]}},
      {"step_id":"split","name":"Canary Split","action_type":"algorithm_weighted_random",
       "depends_on":["decide_all"],
       "params":{"endpoint_weights":[{"endpoint_id":"'"$STABLE"'","weight":90},
                                     {"endpoint_id":"'"$CANDIDATE"'","weight":10}]}}
    ]}}' | jq '{id:.result.id, sync:.result.sync_state}'
```

Weights are relative - they need not sum to 100. The endpoint set in
`endpoint_weights` must **equal** the Decision's `modelRefs` set; a mismatch is
a 422 naming the missing or extra rows.

### Confirm the split is actually happening

Do not trust the config - send traffic and count where it lands. A router is
called like any model, at the inference gateway with a bearer token:

```bash
export BUD_GATEWAY_TOKEN=$(bud token)          # mints AND registers - see bud-inference
GW=https://gateway.<your-domain>

for i in $(seq 1 60); do
  curl -sS -o /dev/null -D - "$GW/v1/chat/completions" \
    -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
    -d '{"model":"assistant-router-canary","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' \
  | awk 'tolower($1)=="x-sr-selected-model:"{print $2}'
done | tr -d '\r' | sort | uniq -c
```

60 requests distinguishes 90/10 from 100/0; it is not enough to argue about 88
vs 92. For the real answer over real traffic, count served deployments
server-side over a window:

```bash
bud api POST /metrics/inferences/list -d '{
  "project_id":"'"$BUD_PROJECT_ID"'","from_date":"2026-08-05T00:00:00Z","limit":1000
}' | jq -r '.items[].endpoint_name' | sort | uniq -c
```

Ramp by editing the weights, but **`PUT` replaces the whole DAG** - read it,
change it, write it back; never send a partial `dag_config`:

```bash
bud api GET /routers/$R | jq '.result.dag_config' > dag.json   # edit weights in dag.json
jq -n --slurpfile d dag.json '{status:"active", dag_config:$d[0]}' > body.json
bud api PUT /routers/$R -d @body.json | jq '{sync:.result.sync_state, warnings:.result.warnings}'
```

To abort a canary, drop the candidate from both `endpoint_weights` and
`modelRefs` (they must stay in step). Do not delete the deployment out from
under a live router - see the silent-drop warning below.

## Seeing which branch fired

Routed responses carry `x-sr-*` headers, streaming and non-streaming alike:
`x-sr-selected-decision` (winning Decision, by `name`), `x-sr-selected-model`
(deployment that served it), `x-sr-matched-<signal>` (what each signal decided),
`x-sr-looper-*` (cascade behaviour), `x-sr-cache-hit` / `x-sr-fast-response`
(plugin effects). They are the fastest way to debug a rule that "should have
matched"; full list in `references/operations.md`. **No `x-sr-*` headers at
all** means the routing engine never ran - treat that like the sync failure
below.

## When the router does nothing

A router that exists in the console but not at the gateway is silent: requests
either fail with an unhelpful message or all land on one deployment. Check in
this order:

```bash
bud api GET /routers/$R \
  | jq '{status:.result.status, sync:.result.sync_state,
         attempts:.result.sync_attempts, error:.result.last_sync_error}'
```

- **`status:"draft"`** - never pushed. Set it to `active`.
- **`sync_state:"failed"`** - the configuration never reached the gateway.
  `last_sync_error` usually names the cause; fix that first (a deleted endpoint
  in the DAG needs a `PUT` with corrected `modelRefs`), then force a re-push
  with `bud api POST /routers/$R/retry-sync` and re-read `sync_state` on the
  response. Automatic retries back off up to half an hour and **stop after
  about ten consecutive failures**; `retry-sync` resets that counter, so a
  router left failing overnight does not recover on its own.
- **`sync_state:"pending"`** - queued for the background sweep (~5 minutes).
  Wait or call `retry-sync`:
  `bud wait resource /routers/$R --field sync_state --equals synced --timeout 600`.

Two silent failure modes survive a healthy `synced`:

> **A deployment deleted after the router was written leaves a dangling
> reference, and nothing tells you.** The router still reports `synced`, but
> that Decision has a candidate the gateway cannot resolve - and if it was the
> only one, the branch serves nothing. After deleting or recreating a
> deployment, re-read every router that named it.

> **If the routing engine itself is unreachable, requests are not failed** -
> they go to one fixed candidate instead. Traffic keeps flowing while your split
> or branching quietly stops applying: exactly the shape of "the canary got 0%
> all week". Watch for `x-sr-selected-decision` going absent, not just errors.

At request time a router returns `503` with a machine-readable `error.code` -
branch on the code, not the prose. `no_decision_matched` means no rule covered
the request (add the catch-all Decision). `signal_not_ready` means a signal's
infrastructure did not run, named in a `signal` field - platform-side, so report
it with the request id rather than rewriting the DAG. An older message you may
still meet - "Invalid inference target: router ... resolved to candidate
'bud-routed', but the API key has no access" - is the same no-match case, not an
API-key problem.

## Renaming and deleting

The **`name` is the alias clients send as `model`** and must be unique in the
project. It is used verbatim - unlike deployment names it is not slugified, so
`"QA Test"` really does mean `"model": "QA Test"`; prefer a hyphenated
lower-case name. Renaming moves the alias - the old name stops working
immediately, so update callers in the same change. `DELETE /routers/{id}` breaks every client
sending that model name at once: confirm with the user first, naming the router.
Occasionally a renamed or deleted alias lingers at the gateway; if an old name
still answers, say so rather than assuming the change failed.

## Deeper reference

- `references/dag-reference.md` - every action type and its params, rule syntax,
  projections, selection and cascade algorithms, plugins, and the validation
  rules behind the 422s
- `references/operations.md` - sync lifecycle, runtime error envelope, the full
  `x-sr-*` header list, listing and filtering routers, response-envelope quirks

## Where to go next

`bud-deployments` to create or resize the deployments a router chooses between
(and for per-deployment retries and fallback targets, configured on the
deployment rather than here), `bud-inference` to call the alias,
`bud-observability` to compare cost and latency across branches,
`bud-guardrails` for safety policy - a separate control from routing.
