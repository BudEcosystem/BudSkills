---
name: bud-evaluations
description: Measure quality and speed in Bud Foundry - run evaluations of a deployed model against standard eval datasets, organise them into experiments, read and rank accuracy scores, compare deployments side by side, and run performance benchmarks for throughput, time-to-first-token and latency under load. Use when a Bud request mentions evaluating, scoring, benchmarking, experiments, eval datasets or traits, accuracy, "which model is better", regression testing a deployment, TTFT/TPOT/ITL, or tokens per second at a given concurrency.
---

# Bud Foundry: evaluations and benchmarks

Two separate systems live here, sharing nothing but the word "dataset".

| | **Quality evaluation** (`/experiments/*`) | **Performance benchmark** (`/benchmark*`) |
|---|---|---|
| Answers | "how accurate is this deployment?" | "how fast is it, and at what concurrency?" |
| Unit of work | experiment -> evaluation -> one run per dataset | one benchmark run |
| Dataset catalog | `GET /experiments/datasets` (~395 rows, ~50 runnable) | `GET /dataset` (17 rows) |
| Produces | `accuracy` per dataset | throughput, TTFT, TPOT, ITL percentiles |
| Takes | minutes to hours (~30 min/dataset) | minutes to hours (it deploys first) |
| Cancellable | **no** | yes |

**Ids from one catalog are never valid in the other** - passing a `/dataset` id
into an evaluation, or the reverse, gives a 400, a 404 or an empty run set.

Prerequisite: connect first - see `bud-platform` (`bud login`).

## What this can actually measure - read before promising

Three limits shape every task here. Check them against the request first and say
up front if one bites:

1. **Evaluations target a deployed model endpoint, not an agent or a prompt
   version.** Step 2 of the wizard takes an `endpoint_id` and nothing else.
2. **You cannot upload a dataset through the API.** No create/update/delete route
   exists; the only write path is a manifest sync, and the dataset must also
   exist inside the evaluation engine image.
3. **The only metric ever stored is `accuracy`.** Dataset metadata advertises
   `pass@1`, `rouge`, `bleu`, `exact_match`/`f1` - none are persisted.

Limits 1 and 2 are what "evaluate my agent against my own cases" hits;
"Evaluating your own question/answer set" is the way through.

## Evaluate a deployed model on standard datasets

The canonical flow. Datasets are indexed by **trait** (a capability grouping),
and you must pick traits before datasets - step 4 rejects any dataset not
linked to a trait chosen in step 3.

```bash
# 1. traits - always ask for limit=100; the default page size is 10
bud api GET /experiments/traits -q limit=100 | jq -r '.traits[] | "\(.id)  \(.name)"'

# 2. datasets for that trait. has_gen_eval_type defaults to TRUE and should stay
#    so: of ~395 catalogued datasets only ~50 carry a runnable config.
bud api GET /experiments/datasets -q trait_ids=<trait-id> -q limit=50 \
  | jq -r '.datasets[] | "\(.id)  \(.name)  \(.eval_types.gen)"'

# 3. the deployment to test (must be status=running), then a folder to hold it
bud api GET /endpoints/ -q project_id=$BUD_PROJECT_ID -q limit=50
bud api POST /experiments/ -d '{"name":"nightly reasoning","description":"...","project_id":"'"$BUD_PROJECT_ID"'"}'
# -> .experiment.id
```

> `GET /experiments/datasets` has **no ORDER BY**, so page 2 may repeat or drop
> rows from page 1. Use `--paginate` and de-duplicate by id.

Experiment names are lowercased on save, must match `^[a-zA-Z0-9\s\-_]+$` (a dot
or slash gives 422) and must be unique per user, case-insensitively (400 otherwise).

Then drive the five-step wizard: same endpoint, shared `workflow_id`, increasing
`step_number`. **Nothing runs until the last call sets `trigger_workflow: true`.**

```bash
EXP=<experiment-id>
# step 1 - name it (workflow_id null; read .workflow_id from the response)
bud api POST /experiments/$EXP/evaluations/workflow -d '{
  "workflow_id": null, "step_number": 1, "workflow_total_steps": 5,
  "trigger_workflow": false,
  "stage_data": {"name":"gsm8k check","description":"baseline"}}'
WF=<workflow-id>
bud api POST /experiments/$EXP/evaluations/workflow -d '{"workflow_id":"'$WF'","step_number":2,"trigger_workflow":false,"stage_data":{"endpoint_id":"<endpoint-id>"}}'
bud api POST /experiments/$EXP/evaluations/workflow -d '{"workflow_id":"'$WF'","step_number":3,"trigger_workflow":false,"stage_data":{"trait_ids":["<trait-id>"]}}'
bud api POST /experiments/$EXP/evaluations/workflow -d '{"workflow_id":"'$WF'","step_number":4,"trigger_workflow":false,"stage_data":{"dataset_ids":["<dataset-id>"]}}'
bud api POST /experiments/$EXP/evaluations/workflow -d '{"workflow_id":"'$WF'","step_number":5,"trigger_workflow":true,"stage_data":{}}'
```

Steps cannot be skipped (`step_number > current_step + 1` -> 400 "Cannot skip
steps"), the current step may be resubmitted, and once triggered the workflow is
`completed` and **that `workflow_id` can never be reused**.

**Two lookalike endpoints never start work.** `POST /experiments/{id}/runs`
creates runs marked `running` that no engine ever picks up - they hang forever
and make the experiment look busy. `POST /experiments/workflow` (the
experiment-creation wizard) leaves its workflow `in_progress` permanently and
launches nothing.

### Waiting for it

Budget **~30 minutes per dataset** and set the timeout accordingly.

```bash
bud wait resource /experiments/$EXP --field experiment.progress_overview.0.status \
  --equals completed --fail-on failed --timeout 7200 --interval 60
```

`progress_overview` has one entry per evaluation - index `0` is right for a
fresh experiment. To watch one dataset instead, wait on its run:
`bud wait resource /experiments/runs/<run-id> --field run.status --equals completed --fail-on failed`.

**Do not use `bud wait job` here.** The job's `status` flips to `completed` the
moment step 5 is accepted, so the wait returns on its first poll while the
evaluation has barely started - it tracks the wizard, not the work. The real feed
is `workflow_steps.evaluation_events` (`verify_cluster_connection` ->
`preparing_eval_engine` -> `deploy_eval_job` -> `monitor_eval_job_progress`), and
`GET /experiments/{id}` adds `progress_overview[].eta_minutes`. Ignore its
`stats` block - budget, tokens, runtime and processing rate are hardcoded zeros.

> **There is no cancel API for a quality evaluation.** Deleting the run or the
> experiment only removes database rows; the job keeps running and keeps
> consuming the deployment. Confirm dataset choice before triggering.
>
> If result extraction fails, no status update is delivered and the runs stay
> `running` indefinitely. Anything still `running` after ~45 min per dataset
> with no event movement is stuck, not slow - say so rather than waiting on.

Two preconditions fail late, at trigger time: "No datasets with valid eval_type
configurations found" (datasets without a `gen` config - re-check step 2) and
"No active project found in the system" (the trigger mints a temporary key from
the oldest active project anywhere in the installation, ignoring the
experiment's own).

### Reading the scores

```bash
# every evaluation you own, with computed scores (note page_size, not limit)
bud api GET /experiments/evaluations/all -q endpoint_id=<id> -q page_size=100 \
  | jq '.evaluations[] | {evaluation_name, status, overall: .scores.overall_accuracy,
                          datasets: [.scores.datasets[] | {dataset_name, accuracy}]}'

bud api GET /experiments/runs/<run-id> | jq '.run | {status, metrics, raw_results}'
bud api GET /experiments/evaluations/all -q export_format=csv > scores.csv
```

> **`GET /experiments/{id}/runs/history` returns hardcoded dummy data** - ten
> fabricated rows with random UUIDs and the literal strings `"model_name"`,
> `"Benchmark"` and `"Score"`. Never read it, never show it to a user.
>
> `GET /experiments/{id}/evaluations` always reports `scores: null` (and 500s in
> the one case where it would not). Use `/experiments/evaluations/all` instead.

**Averages are biased upward.** A run whose average metric is exactly `0.0` is
dropped as if it were missing, so scoring zero on a dataset raises the mean of
the rest instead of lowering it. When a result looks suspiciously good,
cross-check `num_runs` and the per-run metrics first.

## Evaluating your own question/answer set

The most-requested task in this domain, and the platform does not support it
directly. Say so plainly rather than improvising, then offer the two real
options:

1. *If the user operates the installation* - a dataset can be added, but only as
   an installation change: a manifest entry **plus** a matching config built into
   the evaluation engine image. `references/custom-datasets.md` has the exact
   entry schema, the trait-matching rules, the version bump a sync needs and how
   to verify the result. Expect to involve whoever builds that image.
2. *Otherwise, score it yourself* - usually the faster answer, and the only one
   that can target an agent. `scripts/qa-eval` in this skill does the whole loop.

### Scoring your own cases with `scripts/qa-eval`

Write the cases as JSONL - `input` and `expected` are the only required fields:

```jsonl
{"id":"pto-01","input":"How many days of PTO do I get?","expected":"25 days per year","scorer":"contains"}
{"id":"tax-02","input":"Can I expense a taxi home after 9pm?","expected":"yes","scorer":"contains"}
```

Optional per case: `scorer` (`exact`|`contains`|`regex`|`judge`, overriding
`--score`), `instructions`, `weight`. `question`/`answer` work as synonyms for
`input`/`expected`, as does a CSV with those columns.

Targets are **gateway model names**, which is what makes prompt-version
comparison possible: `my-deployment` is a deployed endpoint, `prompt:hr-agent`
is an agent's **default** prompt version, `prompt:hr-agent:v3` is **version 3**.

```bash
export BUD_GATEWAY_URL=https://gateway.<your-domain>      # a different host from the API
export BUD_GATEWAY_TOKEN=$(bud token)                     # mints AND registers - see bud-platform

scripts/qa-eval --cases hr.jsonl \
  --target prompt:hr-agent:v1 --target prompt:hr-agent:v2 \
  --score judge --judge-target <a-strong-deployment> \
  --out results.json --csv results.csv
```

It prints one row per target sorted by accuracy, plus the case ids where the
targets disagree - the part the user actually learns from. Start with
`--max-cases 5` to check the wiring and the judge's verdicts, and keep
`--concurrency` low on a shared installation. Judge scoring is a real model call
per case: it costs tokens, it is not deterministic, and a model grading its own
output flatters itself - always pass a separate `--judge-target`, prefer
`exact`/`contains`/`regex` where the answer shape allows, and read a sample of
the reasons rather than trusting the total.

**"Which prompt version scores best" is only answerable this way**, because an
evaluation pins one `endpoint_id` and prompt versions are not endpoints. Bud has
no prompt-optimisation search either: `bud-agents` owns editing and versioning
the prompt, and systematically *generating* candidates needs an external
optimisation toolkit driven from there. This skill measures; it does not
iterate.

## Compare deployments side by side

```bash
bud api GET /experiments/compare/deployments -q limit=50          # who has completed runs
bud api GET /experiments/compare/traits -q deployment_ids=<a>,<b>
bud api GET /experiments/compare/radar   -q deployment_ids=<a>,<b> -q trait_ids=<t1>,<t2>
bud api GET /experiments/compare/heatmap -q deployment_ids=<a>,<b> -q limit=10

# rankings, computed after averaging and sorted by accuracy (nulls last)
bud api GET /experiments/datasets/<dataset-id>/scores -q limit=50
bud api GET /experiments/scores -q endpoint_id=<id>    # needs >=1 of endpoint_id/model_id/dataset_id
```

Two traps produce contradictory numbers for the same deployment:

- **Radar reports the MAX score per trait; heatmap reports the AVERAGE.** They
  disagree, and neither is wrong. State which one you used.
- **The `/experiments/compare/*` family is not scoped to you** (nor are
  `/experiments/{id}/runs` and `/experiments/{id}/summary`): they carry no
  ownership filter and return every tenant's results. `/experiments/scores` and
  `/experiments/datasets/{id}/scores` *are* scoped to your own experiments,
  which is why the same deployment shows different numbers between the two
  families. Prefer the scoped pair when results may be sensitive, and check
  whose runs are in any comparison output before pasting it anywhere.

`GET /models/{id}/leaderboards` is unrelated - curated third-party benchmark
tables for *choosing* a model to deploy. See `bud-models`.

## Run a performance benchmark

Throughput, time-to-first-token and latency under load. It **deploys the model
onto a cluster first**, so it costs real compute and can take hours - confirm
cluster, replica count and concurrency with the user before triggering. Needs the
benchmark-manage permission.

Nine steps, same wizard mechanics but on `POST /benchmark/run-workflow`, with
fields at the **top level** rather than under `stage_data`:

```bash
# step 1 - workflow_total_steps is REQUIRED here and must NOT be sent with workflow_id
bud api POST /benchmark/run-workflow -d '{
  "step_number": 1, "workflow_total_steps": 9, "trigger_workflow": false,
  "name": "llama-bench", "tags": [], "description": "p95 at c=10",
  "concurrent_requests": 10, "eval_with": "dataset"}'      # name <= 50 chars

# 2 traffic source, 3 cluster, 4 nodes, 5 model, 6 sizing, 7 credential, 8 confirm, 9 go
#   step 2: {"datasets":["<perf-dataset-id>"]}  or  {"max_input_tokens":1024,"max_output_tokens":512}
#   step 3: {"cluster_id":"<id>"}     step 4: {"hardware_mode":"dedicated","nodes":[...]}
#   step 5: {"model_id":"<id>"}       step 6: {"selected_device_type":"cuda","tp_size":1,"pp_size":1,"replicas":1,"num_prompts":1000}
#   step 7: {"credential_id":"<id>"}  (cloud providers only)   step 8: {"user_confirmation":true}
#   step 9: {"run_as_simulation": false, "trigger_workflow": true}
```

Order matters more than usual: **step 5 fires the capacity planner
synchronously** and fails with "Missing required data for bud simulation" unless
the cluster and nodes are already stored. Get legal step-6 sizing from the
planner rather than guessing, then trigger, wait and read:

```bash
bud api POST /benchmark/node-configurations -d '{
  "model_id":"<id>","cluster_id":"<id>","hostnames":["node-1"],
  "hardware_mode":"dedicated","input_tokens":1024,"output_tokens":512,"concurrency":10}' \
  | jq '.device_configurations[] | {device_type, tp_pp_options}'
```

```bash
bud wait job <workflow-id> --timeout 10800          # here the job DOES track the work
BID=$(bud api GET /workflows/<workflow-id> | jq -r '.workflow_steps.benchmark_id')
bud wait resource /benchmark/$BID/model-cluster-detail --field status \
  --equals success --fail-on failed,cancelled --timeout 10800

bud api GET /benchmark/result -q benchmark_id=$BID | jq '.param'   # query param; payload under .param
```

`.param` carries `request_throughput`, `input_throughput`, `output_throughput`
and mean/median/p25/p75/p95/p99/min/max for `ttft_ms`, `tpot_ms`, `itl_ms`,
`e2el_ms`. Lead with `output_throughput`, `median_ttft_ms` and `p95_ttft_ms` at
the stated concurrency. Per-request rows are at `GET /benchmark/request-metrics
-q benchmark_id=$BID`, in an `{items, total_items}` envelope.

> **Mind the units.** `_ttft_ms`/`_tpot_ms` here and the summary `ttft`/`tpot` on
> `GET /benchmark` rows are **milliseconds**; the `ttft`, `tpot` and `itl[]` on
> per-request rows are **seconds**. `0.41` and `561.82` can be the same run.
> `GET /benchmark/result` is also a proxy to the cluster manager, so it can 500
> while that component restarts even though the benchmark succeeded.

Unlike evaluations, this **can** be stopped:
`bud api POST /benchmark/cancel -d '{"workflow_id":"<workflow-id>"}'` - the
workflow id, not the benchmark id.

## Find an evaluation you started earlier

```bash
bud api GET /workflows -q workflow_type=evaluate_model -q limit=50  # or model_benchmark
bud api GET /experiments/ -q search=<name>                          # names are lowercased
bud api GET /experiments/<id>/summary | jq '.summary'               # completed/failed/running
bud api GET /experiments/evaluations/all -q search=<name> -q page_size=50
```

## Deeper reference

- `references/evaluation-workflow.md` - every step's payload, the ordering and
  state rules, the progress-event sequence, terminal states, and how to diagnose
  runs that never finish
- `references/scores-and-comparison.md` - each score endpoint's response shape,
  the scoping and averaging matrix, and which one to trust for what
- `references/custom-datasets.md` - the dataset and trait catalog, the manifest
  entry schema for adding your own dataset, and the sync
- `references/benchmarks.md` - the nine benchmark steps, every result field,
  per-request metrics and the analysis calls
- `scripts/qa-eval --help` - the custom question/answer harness

## Where to go next

`bud-deployments` to get an endpoint worth evaluating, `bud-agents` to iterate on
the prompt or agent behind it, `bud-inference` for the gateway details behind
`qa-eval`, `bud-observability` for how a deployment behaves on live traffic
rather than a fixed dataset, and `bud-models` for published leaderboard data
about models you have not deployed yet.
