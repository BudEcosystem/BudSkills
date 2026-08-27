# Platform issues found while building and testing these skills

Observations about the Bud Foundry platform itself, found by driving its API.
Each entry records what was observed, the evidence, the impact on an API
consumer, and how the skills work around it today.

Environment unless stated otherwise: `bud-dev` (`app.dev.bud.studio`),
signed in as `admin@bud.studio` (`super_admin`).

Status key: **open** = still reproduces, worked around in the skills.

---

## BUG-001 - Job detail and job list return incompatible shapes

**Status:** open · **Severity:** medium · **Area:** jobs

`GET /workflows/{id}` and `GET /workflows` describe the same entity with
different field names and different content:

| | list item | detail |
|---|---|---|
| identifier | `id` | `workflow_id` |
| progress feed | `progress.steps[]` | *absent* |
| collected data | *absent* | `workflow_steps` (a dict) |

Confirmed on job `64b9d138-8be5-4937-9431-bd604335d902`: the detail response
keys are `['object','message','workflow_id','status','current_step',
'total_steps','reason','workflow_steps']`, with no `progress`.

Also confusing: `workflow_steps` on the detail response is a **dictionary of
accumulated session data**, while `progress.steps` on the list response is an
**array of progress events**. Two similarly-named fields, unrelated content.

**Impact:** the obvious "fetch the job by id and watch its progress" does not
work. A client must fetch the detail for status and scan the list for progress.

**Workaround:** `budkit.waits.fetch_job()` merges both representations.

---

## BUG-002 - No way to fetch one job's progress by id

**Status:** open · **Severity:** medium · **Area:** jobs

The progress feed exists only on the list endpoint, and `WorkflowFilter`
(`services/budapp/budapp/workflow_ops/schemas.py:225`) exposes only
`workflow_type` - there is no `id` filter.

**Impact:** following a specific job means listing recent jobs and scanning for
it. A job that ages past the scanned window loses its progress feed entirely,
so long jobs on a busy installation can become unobservable.

**Workaround:** the toolkit scans the first 50 recent jobs, optionally filtered
by type, and degrades to status-only when the job is not found.

**Suggested fix:** accept `id` in `WorkflowFilter`, or include `progress` in the
detail response.

---

## BUG-003 - Abandoned job sessions accumulate without bound

**Status:** open · **Severity:** low (hygiene; misleads API consumers) · **Area:** jobs

Every one of the 500 most recent jobs on `bud-dev` is `in_progress` - not a
single `completed` or `failed` in the sample:

```
{'in_progress': 500}
prompt_creation 227 · guardrail_deployment 97 · model_deployment 63
local_model_onboarding 32 · custom_probe_creation 42 · ...
```

A job row is created when a stepped session opens and only reaches `completed`
when the client finishes it (`PATCH /workflows/{id}` is literally
`mark_workflow_as_completed`). Sessions abandoned mid-wizard stay `in_progress`
forever, and nothing reaps them.

**Impact:** `status` is unusable as a completion signal, and the job list is
mostly noise. A naive client waiting on `status == completed` hangs forever.

**Workaround:** the skills wait on the progress feed and on resource state, and
document the trap prominently.

**Suggested fix:** expire abandoned sessions after a TTL, or distinguish
"session open" from "work running" in the schema.

---

## BUG-004 - Internal component names leak through the progress feed

**Status:** open · **Severity:** low · **Area:** jobs / API surface

Progress payloads carry internal service identifiers, e.g.
`"source": "budsim"` inside `progress.steps[].payload` on
`model_deployment` jobs.

**Impact:** anything rendering raw progress to an end user exposes internal
architecture.

**Workaround:** `budkit.waits.present()` maps component names to user-facing
capability names ("capacity planner", "cluster manager", ...) before display.

---

## BUG-005 - Inconsistent collection path shapes

**Status:** open · **Severity:** low · **Area:** API surface

Most collections sit at `/<thing>/` (`/projects/`, `/models/`, `/endpoints/`),
but clusters are at `/clusters/clusters`; `GET /clusters/` returns 404.

Separately, `/workflow/{id}/...` (singular) and `/workflows/{id}` (plural) are
different namespaces. `GET /workflow/{a-real-job-id}/status` returns
`404 "No such workflow exists"`, which reads like the job is gone rather than
like the path is wrong.

**Impact:** predictable-looking guesses 404, and the error message misdirects.

**Workaround:** `bud paths <keyword>` for discovery, plus a 404 hint in the
error diagnoser naming the nesting.

---

## BUG-006 - `GET /models/` defaults to the catalog, whose ids 404 on `GET /models/{id}`

**Status:** open · **Severity:** high · **Area:** model registry

`GET /models/` takes a `table_source` parameter, and **the default is the one
most callers do not want**:

| `table_source` | Returns | On `bud-dev` | Ids usable with `/models/{id}`? |
|---|---|---|---|
| `cloud_model` (**default**) | browsable catalog of hosted models available to add | 1197 | **No - 404** |
| `model` | models actually in this installation registry | 28 | Yes |

Reproduction:

```
GET /models/?page=1&limit=1               -> models[0].model.id = ef701893-...
GET /models/ef701893-...                  -> 404 {"message":"Model not found"}
GET /models/?table_source=model&limit=1   -> models[0].model.id = 045a89b9-...
GET /models/045a89b9-...                  -> 200
```

The two id spaces are disjoint: model ids taken from live deployments
(`5b44120a-...`, `cfd387a9-...`) resolve on `/models/{id}` but do **not** appear
anywhere in the 1197-entry default listing.

Contributing detail: `retrieve_model` filters on `status = ACTIVE`
(`services/budapp/budapp/model_ops/services.py:2256`), while the list response
omits `status` entirely, so a caller cannot tell from the listing whether an
entry is fetchable. `is_present_in_model` looks like the flag for this but is
not reliable - `claude-opus-5` is returned with `is_present_in_model: true` and
still 404s on detail.

**Impact:** high. The natural sequence "list models, take an id, deploy it"
fails with a misleading 404 that reads as a permissions or scoping problem.

**Workaround:** the skills always pass `table_source=model` when they need a
deployable model, and `table_source=cloud_model` only when browsing what could
be added. Documented prominently in `bud-models`.

**Suggested fix:** make the id spaces distinguishable, or have `/models/{id}`
fall back to the catalog table and say which table the id belongs to.

---

## BUG-007 - Password sign-in returns 400 where the code raises 410

**Status:** open · **Severity:** cosmetic · **Area:** auth

`AuthService.login_user` raises `ClientException(..., status_code=410)`
(`services/budapp/budapp/auth/services.py:118`), but the response observed on
`bud-dev` is:

```
HTTP 400 {"object":"error","code":400,"message":"Password login is disabled. ..."}
```

The 410 is downgraded to 400 somewhere in the error handler.

**Impact:** minor - a client cannot distinguish "permanently gone" from "bad
request" by status alone. The message is clear, so this is low priority.

---

## BUG-008 - `POST /credentials/` returns an unusable API key with no way to decrypt it

**Status:** open · **Severity:** high · **Area:** credentials / inference

Creating a project API key returns `result.key` as **512 hex characters** -
the key encrypted with the installation RSA public key, not the key itself.
Verified live: a key created on `bud-dev` came back as
`816c51c2cdfb1a83aad0273974a5d199b7a39dc3...` (512 chars, all hex).

`credential_ops/services.py:143` sets `key = db_credential.encrypted_key`. The
console decrypts it client-side with a private key shipped to the browser; the
API exposes **no endpoint that returns the plaintext**.

**Impact:** high for automation. Any non-browser client - CI, an SDK, an agent -
that creates a key cannot use it. The failure is silent and late: the key looks
plausible, and only the eventual `401 Invalid API key` at the gateway reveals
the problem.

**Workaround:** the skills use the session-derived bearer token instead
(`bud token`: `GET /auth/redirect/ws-token` then
`POST /playground/initialize-with-token`), which is fully automatable. Verified
working end to end against `gateway.dev.bud.studio` for chat completions and
embeddings.

**Suggested fix:** return the plaintext once at creation (standard practice for
API keys), or provide a server-side reveal endpoint.

---

## BUG-009 - The inference gateway rejects valid tokens that were never registered

**Status:** open (by design, but badly signalled) · **Severity:** medium · **Area:** inference

The gateway does not validate bearer tokens - it looks up a hash of the token in
a registry populated by `POST /playground/initialize-with-token`. A perfectly
valid, unexpired session token that skipped that call is rejected with:

```
401 {"error":{"message":"Invalid API key","type":"invalid_request_error","code":401}}
```

The same happens after a token refresh if registration is not repeated.

**Impact:** the message points at the credential when the real cause is a
missing registration step, so callers debug the wrong thing.

**Workaround:** `bud token` always performs both calls.

**Suggested fix:** distinguish "unknown token" from "invalid token" in the
error message.

---

## BUG-010 - First-token latency target is interpreted in the wrong unit

**Status:** open · **Severity:** high · **Area:** deployments / capacity planning

`deploy_config.ttft` is validated as **milliseconds** (range `1..60000`) and the
console labels it "TTFT(ms)". But on the default planning path the optimiser
multiplies the value by 1000 before comparing
(`services/budsim/simulator/direct_search.py:638`, `ttft_target_ms = self.target_ttft * 1000`),
treating the input as seconds. The alternative planning path compares in
milliseconds with no such multiplication
(`services/budsim/simulator/evolution.py:871`).

**Impact:** the first-token constraint is effectively **1000x looser than
requested** on the path that actually runs by default. A user asking for
"first token within 200 ms" gets configurations planned against 200 seconds, so
the `ttft` label is meaningless and the recommendation may badly miss the SLO
in production. End-to-end latency and per-session throughput are unaffected.

**Workaround:** the skills document `ttft` labels as indicative only and tell
the user to verify real first-token latency after deployment via observability.

**Suggested fix:** align the unit in `direct_search.py` with the schema.

---

## BUG-011 - Capacity planning returns HTTP 200 with an empty list for three different states

**Status:** open · **Severity:** medium · **Area:** deployments

`GET /clusters/recommended/{workflow_id}` answers `{"status":"success","clusters":[]}`
when planning is still running, when no cluster can host the workload, and when
candidate clusters exist but are not currently `available`. The response schema
declares `status` as `Literal["success","processing"]`, but the route hardcodes
the `"success"` default (`cluster_ops/cluster_routes.py:1175-1181`), so
`"processing"` is never emitted.

**Impact:** a client cannot distinguish "wait longer" from "this will never
succeed", so it must poll to a timeout and then guess. The distinction matters:
one warrants waiting, the other warrants relaxing the request.

**Workaround:** the skills poll with a 5-minute deadline and then report it as a
capacity constraint with concrete relaxations to offer the user.

**Suggested fix:** emit `"processing"` while the plan is running.

---

## BUG-012 - `cost_per_token` is actually cost per million tokens

**Status:** open · **Severity:** medium · **Area:** deployments

The field named `cost_per_token` on each cluster recommendation is populated
directly from `cost_per_million_tokens` (`cluster_ops/services.py:2298, 2341`).

**Impact:** any consumer taking the name at face value is off by a factor of
1,000,000 when estimating spend - the kind of error that only surfaces on an
invoice.

**Suggested fix:** rename, or add a correctly-named field alongside it.

---

## BUG-013 - Two async operations return no job id

**Status:** open · **Severity:** medium · **Area:** deployments

`POST /endpoints/delete-worker` and `POST /endpoints/delete-adapter/{id}` both
start background work but return only a plain success envelope. Worse,
`delete-adapter` **declares** `RetrieveWorkflowDataResponse` in its OpenAPI
responses (`endpoint_routes.py:630-651`), so a generated client expects a
`workflow_id` that never arrives.

**Impact:** callers cannot follow or verify these operations except by polling
the worker/adapter list until the count changes, and a schema-driven client
breaks on the missing field.

**Workaround:** the skills poll the collection instead and say so explicitly.

---

## BUG-014 - Re-publishing silently discards new pricing

**Status:** open · **Severity:** medium · **Area:** deployments

`PUT /endpoints/{id}/publish` with `action: "publish"` on an **already
published** deployment is treated as a no-op: it returns 200 with the unchanged
record and creates no pricing entry. Since `pricing` is a required field of that
request, a caller updating a price this way gets a success response and no
change.

**Impact:** silent data loss on a mutation that reports success. Prices appear
to update and do not.

**Workaround:** the skills use `PUT /endpoints/{id}/pricing` for repricing and
warn about this explicitly.

**Suggested fix:** either apply the pricing on re-publish, or reject it as a
conflict.

---

## BUG-015 - `GET /models/cloud-models/recommended-tags` always returns 500

**Status:** open · **Severity:** high · **Area:** model registry · **Reproducible:** 100%

The endpoint is documented as the way to discover the tag/task vocabulary for
filtering the hosted-model catalog. It never works.

```
GET /models/cloud-models/recommended-tags?limit=3
-> 500 {"message": "Failed to get all recommended tags"}
```

Server log gives the real cause:

```
psycopg.errors.GroupingError: column "cloud_model.tags" must appear in the
GROUP BY clause or be used in an aggregate function
LINE 2: FROM (SELECT jsonb_array_elements(cloud_model.tags) ->> $1::...
```

`get_all_recommended_tags` (`model_ops/crud.py:784-828`) places the
set-returning function `jsonb_array_elements(CloudModel.tags)` in both the
select list **and** the `GROUP BY`. PostgreSQL expands set-returning functions
in the select list *after* grouping, so the bare column reference is
ungrouped. No input makes this succeed.

**Impact:** the only frequency-ranked vocabulary source for the ~1197-entry
hosted catalog is unavailable, so catalog filtering by tag becomes guesswork.
(`GET /models/tags` and `/models/tasks` read the *registry* table, a different
and much smaller vocabulary.)

**Workaround:** the skills derive vocabulary client-side by paging
`GET /models/?table_source=cloud_model` and counting tags.

**Suggested fix:** compute the elements in a lateral subquery / CTE and group
over its output column.

---

## BUG-016 - Internal transport failures surface as HTTP 400 and leak internal topology

**Status:** open · **Severity:** medium (security-adjacent) · **Area:** API surface

When a backing component is unreachable, the failure is returned to the API
client as **400 Bad Request** with the raw internal error:

```
GET /endpoints/<id>/workers
-> 400 failed to invoke, id: budcluster, err: failed to invoke target budcluster
   after 3 retries. Error: rpc error: code = Unavailable desc = connection error:
   desc = "transport: Error while dialing: dial tcp 10.42.1.7:50002: connect:
   connection refused"
```

Two problems:

1. **Wrong status class.** A dependency being down is a server-side condition
   (502/503), not a client error. Clients that retry only on 5xx will not
   retry, and clients that treat 4xx as "my request was malformed" will
   misreport it to users.
2. **Information disclosure.** The response names an internal component and
   discloses an internal pod IP and port to any authenticated caller.

Observed when the target component was running without its service-mesh
sidecar; it resolved once the pod was whole, so the *trigger* was environmental
- but the status code and the disclosure are the API's own behaviour and remain
wrong whenever a dependency is genuinely down.

**Workaround:** the skills' error handling treats a 400 whose message contains
transport wording as a transient dependency failure, and never surfaces the raw
text to end users.

**Suggested fix:** map dependency failures to 503 with a generic message and log
the detail server-side.

---

## BUG-017 - Evaluation comparison endpoints are not scoped to the caller

**Status:** open · **Severity:** high (data exposure) · **Area:** evaluations

`eval_routes.py` carries **no permission decorators at all** - every route's
only guard is "is an active user". Scoping is then applied inconsistently in
the service layer:

| Endpoint | Scoping |
|---|---|
| `/experiments/scores`, `/datasets/{id}/scores`, `/evaluations/all`, `/models` | scoped to `created_by == caller` |
| `/experiments/compare/deployments`, `/compare/traits`, `/compare/radar`, `/compare/heatmap` | **global - no user filter** |
| `/experiments/{id}/summary` | docstring claims an access check; the implementation only tests existence and not-deleted |

So any authenticated user can enumerate **every** tenant's deployment names,
model names, trait scores and evaluation counts, and read any experiment's
summary by id.

**Impact:** cross-tenant disclosure of which models an organisation is
evaluating and how well they score.

**Suggested fix:** filter the comparison queries by the caller's projects, and
add the missing ownership check on the summary route.

---

## BUG-018 - `GET /experiments/{id}/runs/history` returns fabricated data

**Status:** open · **Severity:** high · **Area:** evaluations

The handler builds ten synthetic rows with random UUIDs and literal placeholder
strings (`"model_name"`, `"Benchmark"`, `"Score"`) behind a
`# Return dummy data for now` comment (`eval_ops/services.py:2163-2181`).

**Impact:** the response is indistinguishable from real data to a caller. An
agent summarising run history would report entirely fictional results with full
confidence. This is worse than an error.

**Workaround:** the skills do not use this endpoint and warn against it.

**Suggested fix:** return 501, or implement it.

---

## BUG-019 - Evaluation scoring loses data in two ways

**Status:** open · **Severity:** medium · **Area:** evaluations

1. **Multi-score payloads collapse to the last value.** The ingest loop assigns
   rather than accumulates (`services.py:5779-5792`):
   `for acc in accuracy_score_ar: final_acc = float(acc.get("score", 0.0))` -
   so only the final element survives.
2. **Genuine zero scores are treated as missing.** Averaging guards with
   `if metric.metric_value and metric.metric_value > 0`
   (`services.py:3131`, `:3320`), so a real 0.0 is skipped, biasing every
   average upward.

Additionally only `metric_name="accuracy"` is ever persisted, though the
dataset manifest advertises richer metric sets (`pass@k`, `rouge`, `bleu`,
`exact_match`/`f1`); radar and heatmap hard-filter on `accuracy`.

**Impact:** reported accuracy is optimistically biased and multi-metric
evaluations silently lose all but one number.

---

## BUG-020 - `POST /benchmark/request-metrics` is unauthenticated

**Status:** open · **Severity:** high (security) · **Area:** benchmarks

The route has no permission decorator and no user dependency
(`benchmark_routes.py:415-418`), so anyone who can reach the API can inject
arbitrary benchmark request metrics.

**Impact:** unauthenticated write to performance data that informs capacity
planning and model comparison. If the API is reachable beyond the cluster this
is a straightforward data-integrity hole.

**Suggested fix:** require the internal-caller dependency used by the other
ingest routes.

---

## BUG-021 - `ModelService.get_leaderboard()` returns hardcoded scores

**Status:** open · **Severity:** medium · **Area:** model registry

`model_ops/services.py:2068-2084` returns literal fixed values
(`IFEval 65.1`, `BBH 46.9`, ...) regardless of the model.

**Impact:** any surface fed by it presents invented benchmark numbers as real.

**Correction on re-test:** the `400 Failed to fetch leaderboards` responses
first observed were caused by the outage below (the benchmark service was
running without its service-mesh sidecar), not by a defect. Once the pods were
whole, both endpoints returned 200 - `POST /models/top-leaderboards` answering
`{"leaderboards": []}`, i.e. working but with no benchmark data seeded on this
installation. The hardcoded-scores defect above is unaffected and stands.

---

## BUG-022 - Schedule `timezone` is accepted and then ignored

**Status:** open · **Severity:** high (silent wrong behaviour) · **Area:** routines

Schedules accept a `timezone` field and echo it back on read, but the evaluator
computes the next firing against a UTC clock and never applies it.
`next_run_at` is always emitted as a UTC instant.

**Impact:** high, and silent. A user asking for "00:00 IST every day" gets a
schedule that says `timezone: "Asia/Kolkata"` and fires at **05:30 IST**. The
API reports success, the record looks right, and the job runs at the wrong time
every day. Nothing surfaces the discrepancy.

**Workaround:** the skills convert to UTC explicitly - 00:00 IST becomes
`30 18 * * *` - and ship `scripts/cron-utc` for the conversion, because the
day-of-week field shifts too (Monday 02:00 IST is `30 20 * * 0`, a Sunday in
UTC). The script refuses to emit a fixed expression for zones with daylight
saving, where no single cron line is correct year-round.

**Suggested fix:** honour the field, or reject it so callers cannot believe it
works.

---

## BUG-023 - Pipelines cannot invoke an agent

**Status:** open · **Severity:** high (capability gap) · **Area:** routines / agents

`GET /budpipeline/actions` returns 19 actions on the reference installation and
**none of them run an agent, prompt, or model**. Verified live.

**Impact:** the natural automation - "every night, have the agent review
yesterday's traces and improve its prompt" - cannot be expressed as a scheduled
pipeline, even though scheduling and agents both exist. The available bridge is
a generic HTTP action pointed at the public inference gateway, which brings its
own problems (the token expires, private addresses are blocked).

**Workaround:** the skills document the gap plainly and give two honest routes,
recommending an externally-driven schedule for anything needing judgement or
rollback.

---

## BUG-024 - Run and approval queues are unavailable: operator token not configured

**Status:** open · **Severity:** medium · **Area:** routines / agents

`/runs` and `/approvals` return **502** on the reference installation:

```
budprompt operator token (BUDPROMPT_APP_API_TOKEN) is not configured
```

**Impact:** the human-in-the-loop queue - listing runs parked on an approval and
unblocking them - is entirely unreachable, and the error names an internal
environment variable to the caller rather than reporting a configuration fault
in user terms. Response shapes for these endpoints could not be verified.

**Suggested fix:** configure the token in the deployment, and return a 503 with
a user-facing message rather than a 502 quoting an env var name.

---

## BUG-025 - `GET /prompts/{id}/native-tools` returns 500

**Status:** open · **Severity:** medium · **Area:** agents / connectors

Both the list and detail routes for an agent's built-in tools returned HTTP 500
on a healthy-looking installation (the agent runtime was unreachable).

**Impact:** built-in tools such as web search and fetch cannot be listed or
configured through the API, so that part of agent setup is undocumentable from
a live system and unusable while the fault persists.

---

## BUG-026 - Clusters carry two different ids and the deploy path needs the non-obvious one

**Status:** open · **Severity:** high · **Area:** clusters / deployments

Every cluster has both `id` (primary key) and `cluster_id`, and they are always
different values. Verified live:

```
name=bud-default  id=9f0adddc-bdae-4fca-a127-57d9774542f7  cluster_id=c1b8dfde-3504-4156-8709-624f389330c0
name=PNAP         id=454a5f70-140a-4b1e-83bb-c78c83ba9d33  cluster_id=f13f4d80-06e5-468a-a68a-5ef4d9494527
```

They are required in different places:

- every `/clusters/...` REST path takes **`id`**;
- the deploy path resolves by the **`cluster_id` column** -
  `retrieve_by_fields(ClusterModel, {"cluster_id": cluster_id})`
  (`model_ops/services.py:3384`) - as does quantization.

**Impact:** high, and confusing to diagnose. Passing `id` to a deploy makes a
healthy, available cluster look nonexistent; passing `cluster_id` to a REST path
tends to return an empty result rather than an error. Two of the domain analyses
reached opposite conclusions about which to use, which is a fair measure of how
unclear this is from the outside.

**Workaround:** documented explicitly in `bud-clusters` with a table, and
`bud-deployments` says to carry the recommendation's `cluster_id` forward.

**Suggested fix:** name them distinguishably, or accept either on both surfaces.

---

## BUG-027 - Failed cluster onboarding reports success-shaped state forever

**Status:** open · **Severity:** medium · **Area:** clusters

Three real failed onboardings on the reference installation report
`status: in_progress`, `reason: null` and `workflow_execution_status: null`
indefinitely, while their step feed contains a `FAILED` step. On the same
records `current_step` (2) **exceeds** `total_steps` (1).

**Impact:** a client watching the top-level status waits forever on work that
has already failed, with no reason recorded. The step-count inversion also
breaks any progress display that assumes `current_step <= total_steps`.

**Workaround:** the skills judge onboarding from the step feed and the cluster's
own `status`, never from the job's top-level status.

---

## BUG-028 - Cluster metrics endpoints return 500 or inconsistent aggregates

**Status:** open · **Severity:** medium · **Area:** clusters / observability

On the reference installation:

- `GET /clusters/clusters/{id}/metrics/health` -> 500 "Error retrieving health status"
- `GET /clusters/{id}/grafana-dashboard` -> 500
- `GET /clusters/{id}/storage-classes` on a non-available cluster -> **500**
  wrapping a 404: `"Failed to fetch storage classes: 404: Cluster not found"`
- `GET /clusters/{id}/metrics/summary` returns internally inconsistent figures:
  `node_count: 1` alongside `total_cpu_cores: 703` on a 128-core node, and sets
  `cluster_name` to the internal id rather than the name.

**Impact:** cluster health cannot be read through the API, and the summary
figures cannot be shown to a user without being wrong. The wrapped 404-inside-500
also defeats normal status-based error handling.

---

## BUG-029 - All 16 cloud providers report as enabled; only two can provision

**Status:** open · **Severity:** medium · **Area:** clusters

`GET /credentials/cloud-providers` lists 16 providers, every one with
`is_enabled: true`. In practice only Azure and AWS can actually provision a
cluster.

**Impact:** an agent choosing a provider from this list will pick one that
cannot work, and only discover it partway through a provisioning attempt. The
flag that exists to answer "can I use this?" answers it wrongly for 14 of 16.

---

## BUG-030 - `POST /metrics/aggregated` silently ignores the agent filter for some metric sets

**Status:** open · **Severity:** critical (wrong numbers, reported as correct) · **Area:** observability

The same request with the same `filters.prompt_id` returns either the agent's
figures or the **entire installation's**, depending only on which metrics are
requested. Measured live over an identical window:

| `metrics` requested | requests | cost |
|---|---|---|
| `["total_requests","total_cost"]` | **283** | **$69.46** (whole fleet) |
| `["total_requests","total_cost","p95_latency"]` | **5** | **$1.78** (the agent) |

Adding a third metric is what makes the filter take effect.

**Impact:** critical. Nothing about the response signals which interpretation
you got - no warning, no echoed filter, no error. Cost attribution, chargeback,
and any "how much did this agent cost" answer can be wrong by ~40x while
looking entirely plausible. An automated report would publish fleet spend as one
team's.

**Workaround:** the skills pin the exact metric combination that honours the
filter and tell readers to sanity-check any per-agent total against the fleet
total before believing it.

---

Independently reproduced on a second occasion: 775 requests unfiltered versus 166 when a `p95_latency` field forced the exact path - i.e. a per-agent query silently returning whole-installation figures.
## BUG-031 - Timezone offsets are honoured by some analytics endpoints and ignored by others

**Status:** open · **Severity:** high · **Area:** observability

Sending `2026-08-05T11:00:00+05:30` produces two different five-hour windows
depending on the endpoint. Measured live:

| Endpoint | Offset | Result |
|---|---|---|
| trace list, time-series, `/metrics/aggregated`, inference list | **ignored** | 13 traces |
| `/events/summary` | **honoured** | 2 traces / 7 events |

**Impact:** high for exactly the use case this matters in - a nightly review
scheduled in a non-UTC timezone. Half the report covers one window and half
covers another, with no indication anything is inconsistent. A user asking about
"yesterday" in IST gets a five-hour skew on some panels and not others.

**Workaround:** the skills convert every timestamp to UTC and send `Z`, and say
why. This compounds with the inert schedule timezone (BUG-022) - both push the
same direction, so a naive IST setup is wrong twice over.

**Suggested fix:** honour the offset everywhere, or reject offsets and require `Z`.

---

## BUG-032 - Guardrail deployment progress endpoint returns 500

**Status:** open · **Severity:** medium · **Area:** guardrails

`GET /guardrails/deployment/{id}/progress` returns
`500 Failed to get deployment progress` for a healthy `running` deployment, at
both `detail=summary` and `detail=full`.

**Impact:** the documented way to follow a guardrail deployment is unusable, so
callers must fall back to polling the deployment record's `status`.

Two related gaps found alongside it:

- **There is no endpoint→guardrail lookup.** Attachments are indexed by profile
  only, so answering "which guardrail is on this deployment?" means scanning
  every profile with `deployment_count > 0`. During an incident - when a failing
  guardrail is returning 500 on every request - that is precisely the question
  you need answered fastest. The skill ships a script for the scan.
- **`GET /models/providers?capabilities=moderation` omits the built-in
  moderation provider**, returning only the third-party one, even though seeded
  probes reference it on `probe.provider`. Selecting a provider from that filter
  silently excludes the platform's own.

---

## BUG-033 - Trace contents are readable by any signed-in user

**Status:** open · **Severity:** high (data exposure) · **Area:** observability

`GET /metrics/observability/traces` and `/metrics/observability/traces/{trace_id}`
apply **no project membership check**. Any authenticated user holding a
`trace_id` can read the entire span tree - including the prompts sent and the
responses returned.

**Impact:** high on a multi-tenant installation. Traces routinely contain the
most sensitive text on the platform: user questions, retrieved documents, model
answers. Project isolation applies everywhere else and does not apply here.

Related: an unknown `trace_id` returns **200 with an empty `spans` array**
rather than 404, so a caller cannot distinguish "no such trace" from "a trace
with nothing in it", and the endpoint is quietly enumerable.

**Workaround:** the skills flag retrieved trace content as sensitive and warn
against pasting it into shared channels.

**Suggested fix:** scope trace reads by project membership, and return 404 for
unknown ids.

---

## BUG-034 - `cost_per_million_tokens` inverts the true cost ranking for hosted models

**Status:** open · **Severity:** high · **Area:** model registry

`model_cluster_recommended.cost_per_million_tokens` is populated for **hosted**
models, where it is a hardware-sizing artifact rather than a token price. It
does not merely differ from the real cost - it reverses the order. Measured live:

| Deployment | `cost_per_million_tokens` | Real input cost |
|---|---|---|
| moonshot-vision | **0.224** (looks cheapest) | **$2.00 / M** |
| gpt-4o-mini | 0.790 | $0.30 / M |
| gpt-4-nano-2025-04-14 | **0.919** (looks dearest) | **$0.20 / M** |
| away-hr-v1 | 0.815 | $5.00 / M |

Ranking on it selects a deployment **10x more expensive** and calls it the
cheapest. Every hosted model lands in a narrow 0.22-0.92 band and is even
assigned a cluster name, despite hosted deployments skipping capacity planning
entirely.

**Impact:** high. The field name is exactly the question a user asks ("cost per
million tokens"), so it will be quoted. It is also inconsistent across routes -
`/models/{id}` returned `model_cluster_recommended: null` for a model that
`/endpoints/` returned it populated for.

**Workaround:** the skills name it explicitly in an anti-list of "fields that
look like cost and are not", with this inversion as the worked example.

**Suggested fix:** do not populate it for `provider_type: cloud_model`, or
rename it to reflect that it is a sizing estimate.

---

## BUG-035 - `supported_endpoints` query filter matches exactly, silently dropping models

**Status:** open · **Severity:** medium · **Area:** model registry

`GET /models/?table_source=model&supported_endpoints=/v1/chat/completions`
returns **16** models, while **25** registry models have
`supported_endpoints.chat.enabled == true`. The filter compares the model's
whole endpoint set rather than testing containment, so any model supporting
chat *and* something else is excluded.

**Impact:** the 9 dropped models are disproportionately the strong,
multi-capability ones - precisely what a shortlist wants. There is no error and
no indication of exclusion; the caller simply sees a shorter list and believes
it.

**Workaround:** the skills tell readers not to use this filter for shortlisting
and give a client-side `jq` filter on the `enabled` flag instead.

---

## BUG-036 - Per-token cost fields are objects, and `per_tokens` magnitude varies wildly

**Status:** open · **Severity:** medium · **Area:** inference / pricing

Two shapes that break naive consumers:

- On `/playground/deployments`, `input_cost` and `output_cost` are **objects**
  (`{"input_cost_per_token": 6e-07, "input_cost_per_token_cache_hit": 2.8e-08}`),
  not numbers. Sorting on the field directly compares JSON objects and returns
  a confidently wrong "cheapest" with a zero exit code - a silent failure.
- On `/models/catalog`, `pricing.per_tokens` varies **per entry** - observed at
  1e3, 1e6 and 1e7 on one installation. Assuming a fixed denominator can be off
  by four orders of magnitude.

Related: a published deployment can carry `input_cost: 0.0`, which is
indistinguishable from "free" but means the downstream price was never set - the
same deployment costs $0.20/M from the vendor.

**Workaround:** the skills give one authoritative, normalised command and
document the sibling keys (`_cache_hit`, `_batches`), which are worth up to a
10x discount and can change which model is cheapest.

---

## BUG-037 - Two agent-enumeration endpoints disagree by half the fleet

**Status:** open · **Severity:** high · **Area:** observability

Over an identical window, `POST /metrics/observability/events/summary` reports
**122 events across 12 active agents**, while
`POST /metrics/observability/events/analytics` grouped by `prompt_id` reports
**72 events across 6 groups** - regardless of whether the grouping key is
`prompt_id`, `agent_name`, or absent. Per agent they also disagree: one agent is
**26** runs by summary and **17** by analytics.

**Impact:** high for any fleet sweep. Enumerating agents from `analytics` -
which is the natural "rank every agent" call - silently drops **half the agents
and 41% of the runs**. On the reference installation the dropped set included
both agents with a **0% success rate**, i.e. exactly the ones a review exists to
find. Nothing indicates the omission.

**Workaround:** the skills enumerate from `events/summary` and then query each
candidate with a `prompt_id` filter; `analytics` is documented as unreliable for
enumeration.

---

## BUG-038 - `prompt_id` is not always a resolvable agent

**Status:** open · **Severity:** medium · **Area:** observability / agents

Trigger-driven agents carry generated ids such as
`prompt_1785508701957_5jnrap5as` that never appear in `GET /prompts`, yet have
fully queryable traces under `resource_id`. Anything that resolves an agent by
looking it up in `/prompts` therefore refuses precisely the traffic most worth
reviewing.

**Impact:** on the reference installation the two worst agents on the platform -
15 runs and 11 runs, **both 0% success** - are invisible to any name-based
lookup. Their traces also carry `error_type: null` on 24 of 26 runs, so no
public route explains why they failed.

**Workaround:** the skill's review script now falls back to treating an
unresolvable name as a raw `prompt_id` after confirming it has traces.

---

## BUG-039 - `tokens/usage` returns zeros for runs that demonstrably used tokens

**Status:** open · **Severity:** medium · **Area:** observability

`/metrics/observability/tokens/usage` returned 11 buckets of
`input_tokens: 0, output_tokens: 0` for an agent with **11 successful runs**,
while `/metrics/aggregated` reported `avg_execution_input_tokens: 164,437` for
the same agent and window. Fleet-wide with no filter: 17 rows, all zero.

**Impact:** a report built on this endpoint states that an agent which spent
$27.50 produced no tokens. The zeros are indistinguishable from a genuinely idle
agent.

---

## BUG-040 - `avg_execution_cost` is per execution, not per run, and the denominators differ

**Status:** open · **Severity:** medium · **Area:** observability

`avg_execution_cost` is averaged over **executions**, a different and
undocumented denominator from `total_requests`. Measured: an agent with 12 runs
had `avg_execution_cost` $0.891635 and `total_cost` $1.78327 - because the
divisor was **2**, not 12. Reporting it as cost-per-run overstates the true
figure ($0.149) by **6x**. For another agent the divisor was 13 and
13 x $1.8364 still did not reconcile with `total_cost` $27.50, the gap being
sub-agent spend.

**Impact:** per-run cost figures derived from this field are wrong by a large,
variable factor, in the direction that overstates spend.

**Workaround:** the skills derive per-run cost as `total_cost / total_requests`
and treat `avg_execution_cost` as a per-execution figure only.

---

## BUG-041 - Capability mismatch returns an empty-bodied 502 in one direction and a clear 400 in the other

**Status:** open · **Severity:** high · **Area:** inference

Calling a path a deployment does not serve gives two completely different
errors depending on which way round the mismatch is:

```
POST /v1/embeddings        on a chat deployment      -> 400 {"error":"Model `...` is not configured
                                                             to support capability `embedding`..."}
POST /v1/responses         on a chat deployment      -> 502 {"error":{"message":""},"provider_error":""}
POST /v1/chat/completions  on an embedding deployment-> 502 {"error":{"message":"{\"detail\":\"Not Found\"}"}}
```

The 400 is actionable. The 502s carry an **empty message** and are
indistinguishable from a restarting component, so the correct response
(reconfigure) looks like the wrong one (retry).

**Impact:** high, because it lands on the most common path. Agents run on
`/v1/responses`, and on the reference installation **28 of 29 deployments serve
chat while only 2 serve Responses** - so an agent built on a plausible-looking
deployment fails every run with a blank 502, and standard 502 guidance says to
retry a permanent misconfiguration forever.

**Workaround:** the skills require `supported_endpoints.responses.enabled` to be
checked before building an agent, and document the empty-bodied 502 as a
capability mismatch that must not be retried.

**Suggested fix:** return the same 400-with-capability-name in both directions.

---

## BUG-042 - Embeddings fail with the OpenAI SDK's default settings

**Status:** open · **Severity:** high · **Area:** inference

`encoding_format: "base64"` on `/v1/embeddings` returns **502 with a
zero-byte body**. Verified against `cbre-emb-lfm25-350m`:

```
{"model":"...","input":"hello","encoding_format":"base64"}  -> HTTP 502, 0 bytes
{"model":"...","input":"hello","encoding_format":"float"}   -> HTTP 200
```

**Impact:** high, because **the OpenAI Python SDK requests base64 by default**.
`client.embeddings.create(model=..., input=...)` therefore fails out of the box
against Bud, with an empty 502 that suggests the gateway is down rather than
that one parameter is unsupported. Anyone wiring an existing application to Bud
hits this immediately.

**Workaround:** `bud-inference` documents passing `encoding_format="float"`
explicitly.

**Suggested fix:** support base64, or reject it with a 400 naming the parameter.

---

## BUG-043 - The inference list returns duplicate rows

**Status:** open · **Severity:** medium · **Area:** observability

A 20-row page contained only 6 distinct `inference_id` values; a 50-row page
contained 25. Duplicates are substantial, not marginal.

**Impact:** any count, sum or average taken over the raw list over-counts.
Deduplication by `inference_id` is mandatory before using it for anything
quantitative.

---

## BUG-044 - Pipeline definitions expose embedded credentials to every authenticated user

**Status:** open · **Severity:** high (security) · **Area:** routines

There is no agent/prompt action in the pipeline action registry (confirmed
across all 19 actions), so scheduling an agent has to be expressed as an
`http_request` step calling the public gateway. That step must carry a gateway
credential - and `GET /budpipeline/executions` exposes pipeline definitions to
**every authenticated user**.

So the only way to schedule an agent requires writing a working credential into
an object other tenants' users can read. The credential also expires, so the
routine breaks silently later.

**Impact:** a security hazard created by a functional gap, with no safe
alternative available today.

**Workaround:** `bud-routines` documents the exposure and the rotation burden
explicitly rather than presenting the pattern as routine.

**Suggested fix:** add a first-class agent-invocation action, or a secret
reference that is resolved at execution time and never returned by the API.

---

## BUG-045 - An agent cannot be addressed by model name on the responses API

**Status:** open · **Severity:** medium · **Area:** agents / inference

Deployments are callable as `{"model": "<deployment-name>"}`, but an **agent**
is not callable by any model name. Verified against a real agent:

```
{"model":"prompt:latest-ai-news-summarizer"}
  -> 404 "Model 'prompt:latest-ai-news-summarizer' not found or does not support responses"
{"model":"latest-ai-news-summarizer"}
  -> 404 Model not found
{"prompt":{"id":"latest-ai-news-summarizer"}}
  -> reaches the agent (asks for its template variables)
```

**Impact:** medium, but it bites the highest-value workflow. Any tool that
treats an agent as "just another model name" - which every OpenAI-compatible
client does - cannot call agents at all. It also means prompt-version A/B
testing must use a different request shape from ordinary model testing, which
is easy to get wrong silently: a harness that sends the model-name form will
report failures that look like the *agent* being broken.

We hit exactly this: our own `qa-eval` was built on the model-name form and had
to be corrected.

**Suggested fix:** accept a reserved model-name form for agents, or document the
object form prominently in the responses API description.

---

## BUG-046 - Filtering blocking rules by project returns 500

**Status:** open · **Severity:** medium · **Area:** guardrails

`GET /metrics/gateway/blocking-rules` works, but adding the documented
`project_id` filter fails:

```
GET /metrics/gateway/blocking-rules                     -> 200
GET /metrics/gateway/blocking-rules?project_id=<uuid>   -> 500 Internal Server Error
```

**Impact:** the natural "show me this project's blocking rules" call fails, and
the 500 gives no hint that the parameter is the cause - it reads as the
subsystem being down.

**Workaround:** the skill lists all rules and filters client-side on each
rule's own scope.

---

## BUG-047 - A guardrail deployment reports `running` while it fails every request

**Status:** open · **Severity:** high · **Area:** guardrails

On `bud-dev` the `question` deployment answers
`500 {"error":{"code":"guardrail_failure"}}` for every request, benign ones
included - a total outage for that deployment. Its guardrail attachment reads:

```
GET /guardrails/profile/6ee1e367-.../deployments
  -> {"id":"2a7f5254-...","status":"running","endpoint_name":"question"}
```

`status: running` on a guardrail deployment means the attachment rolled out. It
carries no signal about whether the guardrail can actually evaluate.

**Impact:** high. Every automated health check that reads `status` reports the
guardrail as healthy while it is blocking 100% of traffic. Combined with
BUG-032 (no endpoint→guardrail lookup, no working progress endpoint) there is
no read-only way at all to tell a working guardrail from a broken one - the
only test is to send a request and inspect the answer.

**Workaround:** `bud-guardrails` states plainly that `running` is a rollout
signal rather than a health signal, tells the reader to prove a profile
standalone on `/v1/moderations` before attaching it, and leads the incident
section with the detach command
(`DELETE /guardrails/deployment/{id}` - the only off switch, since deployment
`status` is not settable via `PUT`).

**Suggested fix:** surface an evaluated-recently / last-error field on the
guardrail deployment record, or mark the attachment `unhealthy` when the
enforcement path is erroring.

---

## BUG-048 - `GET /guardrails/profile/{id}/probes` returns 500 for every profile

**Status:** open · **Severity:** medium · **Area:** guardrails

The route that lists what a profile actually enforces fails for every profile
tried on `bud-dev` (7 of 7 sampled, moderation and governance alike, with and
without filter parameters):

```
GET /guardrails/profile/<any-id>/probes -> 500 Failed to get all probes for profile <id>
```

The sibling route works:

```
GET /guardrails/profile/<id>/probe/<probe-id>/rules -> 200
```

**Impact:** there is no direct way to read a profile's contents. That matters
because `PUT /guardrails/profile/{id}` takes `probe_selections` as a **full
replacement** - omitted probes are deleted from the profile - so editing safely
requires reading the current set first, which is exactly what is broken.

**Workaround:** the per-probe rules route returns 0 records for probes a profile
does not carry, so the catalog can be sieved against it.
`skills/bud-guardrails/scripts/guardrail-attachments.py --probes <profile-id>`
does that (one call per catalog probe, ~120 calls, about two minutes).

---

## BUG-049 - The probe catalog paginates with unstable ordering

**Status:** open · **Severity:** low · **Area:** guardrails

`GET /guardrails/probes` reports `total_record: 52`, but walking it page by page
at `limit=10` yields **52 rows containing only 50 distinct probe ids** - two
duplicates and two probes never returned. Passing `order_by=name`, or fetching
one page with `limit=100`, returns all 52 distinct.

**Impact:** any client that enumerates the catalog by paging silently misses
detectors and double-counts others. The failure is invisible - no error, just a
short list.

**Workaround:** the skill always paginates the catalog with `order_by=name` or a
single large `limit`.

**Suggested fix:** apply a deterministic tiebreak (e.g. `id`) to the default
ordering.

---

## Capability gaps found while writing the skills

Not defects, but limits worth stating plainly, because they change what can be
promised to a user:

- **No Microsoft Teams, Google Workspace, Salesforce, ServiceNow, or any HR
  system in the connector registry.** 90 connectors are available (Slack,
  GitHub, Linear, Notion, Stripe and similar). An agent that must "reply in
  Teams" or "look up employee records" needs a custom connection or generated
  tools; it is not available off the shelf.
- **Evaluations target a deployment (`endpoint_id`) only.** There is no way to
  evaluate an agent or a specific prompt version in-platform, so "which prompt
  version scores best" cannot be answered by the evaluation feature without
  creating one deployment per variant. This is why the skill set ships its own
  prompt-testing loop.
- **Custom evaluation datasets cannot be uploaded through the API.** The
  executed artefact is a config name that must already exist in the evaluation
  engine image; the dataset manifest's data URL is never fetched at run time.
  "Upload my 50 question/answer pairs" is an installation change, not an API
  call.
- **Connector visibility fails closed on project scope.** On the reference
  installation `/connectors/available` returns 10 connections with a
  `project_id` and 7 without; the 5 that never appear carry no scope tag at all.
  This is the usual cause of "the connection exists but my agent cannot see it".

---

## LIMITATION - Evaluations cannot target an agent or a prompt version

**Status:** by design (documented so it is not mistaken for a bug) · **Area:** evaluations

Two gaps matter for the "iterate on a prompt until it is accurate" journey:

1. **Evaluations bind an `endpoint_id`** - a deployment. There is no way to
   evaluate an agent, or one prompt version against another, through the
   evaluation subsystem.
2. **There is no dataset-upload API.** Adding a custom dataset means editing
   the dataset manifest *and* rebuilding the evaluation engine image, because
   the engine only runs configurations compiled into it. The manifest's own
   data file is never executed.

So "score my 50 HR question/answer pairs against prompt v1 vs v2" is not
achievable with the built-in evaluation subsystem.

**How the skills handle it:** `bud-prompt-optimization` ships harnesses that run
a case file against any number of targets and score them, with significance
testing so a small sample cannot masquerade as an improvement.

**Correction (verified live).** An earlier version of this note claimed the
gateway addresses an agent as the model name `prompt:<agent-name>`. **It does
not.** That form is rejected:

```
POST /v1/responses {"model":"prompt:latest-ai-news-summarizer", ...}
  -> 404 "Model 'prompt:latest-ai-news-summarizer' not found or does not
          support responses"
```

The working form is the object reference, which resolves the agent and applies
its prompt (here answering with the template's own variable requirement, which
proves it was applied):

```
POST /v1/responses {"prompt":{"id":"latest-ai-news-summarizer"}, ...}
  -> {"message": "Missing prompt variables: query"}
```

`{"prompt": {"id": "<agent-name>", "version": <N>}}` is therefore how a specific
prompt version is targeted. `qa-eval` was built on the wrong form and has been
fixed; it now surfaces an error instead of silently reporting a pass.

---

## Observations from the dev installation (environment, not platform defects)

### 2026-08-05 outage during skill testing (infrastructure, not caused by API use)

Partway through authoring, `app.dev.bud.studio` began returning `503 no
available server` at the ingress. Confirmed **not** caused by the read-only API
traffic from this work - the failures are scheduling and image-pull problems,
which HTTP requests cannot cause:

- **One of three nodes is down.** `bud-hetzner-ax52-1` is `NotReady,SchedulingDisabled`.
  Scheduling then fails with `0/3 nodes are available: 1 Insufficient memory,
  1 Too many pods, 1 node(s) didn't match PersistentVolume's node affinity,
  1 node(s) were unschedulable`.
- **The container registry is down.** `https://registry.bud.studio/v2/` returns
  503, so every image pull fails:
  `ImagePullBackOff ... unexpected status from HEAD request to
  https://registry.bud.studio/v2/runtime/budadmin/manifests/nightly: 503`.
- Net effect: `bud-budapp` sits `Pending` with 1 desired / 0 available, so the
  API is entirely unavailable. `budcluster`, `budmodel`, `budsim`, `budprompt`,
  `mcpgateway`, `budplayground` and `prometheus-adapter` are also Pending;
  `askbud`, `budadmin`, `budcustomer`, `budmetrics` are in image-pull failure.

Two nodes were healthy but loaded (72% and 49% memory), so the cordoned third
node is the proximate capacity cause.

This blocks live verification of the skills. It does not affect the correctness
of skills written from source, but the end-to-end scenario runs need the
installation back.

These are worth knowing when testing, but are configuration of `bud-dev` rather
than product bugs:

- Most hosted deployments are not callable: the OpenAI provider key is the
  placeholder `test_123`, and the Together-backed deployments (`qwen3-6`,
  `kimi-k2-6`) report an exhausted credit balance.
- `mock-inference-deploy` and the self-hosted `cbre-emb-lfm25-350m` (embeddings)
  do work, and are the reliable targets for end-to-end tests.
- The `question` deployment returns `500 {"code":"guardrail_failure"}` on every
  request - a guardrail attached to it is failing. Worth flagging to whoever
  owns that deployment, since a failing guardrail blocks all traffic.
- `qwen-4b` is a reasoning model: with a small `max_tokens` it returns
  `content: null` and `finish_reason: "length"` rather than an error. Allow
  enough tokens or the response looks empty.

---

## Notes on things that are *not* bugs

- **`POST /auth/login` failing** is intentional: the installation is
  single-sign-on only. Headless clients complete the redirect flow instead.
- **Jobs invisible to other users** is by design - the job list filters by
  creator (`workflow_routes.py`), including for administrators.
