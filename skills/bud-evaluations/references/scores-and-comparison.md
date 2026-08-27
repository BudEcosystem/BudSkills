# Reading, ranking and comparing evaluation scores

## What a score actually is

Every number the evaluation subsystem stores is a single metric row:

```
metric_name = "accuracy"      always, literally
mode        = "gen"           always, literally
metric_value = float
```

Dataset metadata advertises richer metric sets - you will see `metrics: ["accuracy"]`
on most rows but `pass@1`, `rouge`, `bleu`, `exact_match`, `f1` on others, plus
an `evaluator` class name. **None of those are ever persisted.** If a user asks
for F1 or pass@1, the honest answer is that the platform records accuracy only,
and the alternative is to score the outputs yourself (see `custom-datasets.md`,
option 2).

Two further distortions to know before you quote a number:

- **A run's score is the mean of its metric rows**, and a run whose mean is
  exactly `0.0` is **excluded from every average** as if it were missing. A
  genuine zero therefore pulls averages *up*, not down. Cross-check `num_runs`
  against the number of runs you expect whenever a figure looks too good.
- When the engine reports several scores for one run, only the **last** entry in
  the payload is stored. A multi-subset dataset can silently reduce to one
  subset's score.

## Which endpoint to use

| Goal | Call | Scope |
|---|---|---|
| Everything I have evaluated, with scores | `GET /experiments/evaluations/all` | **yours only** |
| CSV for a spreadsheet | same, `-q export_format=csv` | yours only |
| One run's metrics + raw output preview | `GET /experiments/runs/{run_id}` | yours only |
| Per-dataset leaderboard | `GET /experiments/datasets/{id}/scores` | **yours only** |
| Ranking filtered by endpoint/model/dataset | `GET /experiments/scores` | **yours only** |
| Evaluations inside one experiment | `GET /experiments/{id}/runs` | **unscoped** |
| Radar / heatmap charts | `GET /experiments/compare/*` | **unscoped** |

The scoped and unscoped families read the same underlying rows through different
filters, so **the same deployment legitimately shows different numbers in each**.
Always say which family a figure came from.

### `GET /experiments/evaluations/all` - the workhorse

```bash
bud api GET /experiments/evaluations/all \
  -q endpoint_id=<uuid> -q search=nightly -q page=1 -q page_size=100
```

Query: `model_id`, `endpoint_id` (**wins over `model_id`** when both are sent),
`search`, `searchName` (default true), `export_format=csv`, `page`,
`page_size` 1-100 (default 20). Note `page_size`, not `limit`.

```json
{"evaluations": [{
  "evaluation_id": "...", "evaluation_name": "test evel",
  "experiment_id": "...", "experiment_name": "test qa 02",
  "model": {"id": "...", "name": "...", "deployment_name": "question"},
  "traits": [{"id": "...", "name": "Examination", "icon": "...",
              "datasets": [{"id": "...", "name": "AGIEval", "version": "1.0.0"}]}],
  "status": "running",
  "scores": {"status": "running", "overall_accuracy": null,
             "datasets": [{"dataset_id": "...", "dataset_name": "...",
                           "accuracy": 0.62, "metrics": [...]}]},
  "runs": [{"run_id": "...", "run_index": 3, "status": "running",
            "evaluation_job_id": null, "created_at": "..."}],
  "duration_in_seconds": null
}], "total_record": 4, "page": 1, "limit": 20, "total_pages": 1}
```

`overall_accuracy` is null until the evaluation completes. `evaluation_job_id`
is always null in practice - it is never written back, which is also why
`GET /experiments/{id}/evaluations` can never resolve scores.

CSV mode ignores paging and pulls up to 10 000 rows, with columns:
Deployment Name, Model Name, Dataset Name, Trait Name, Evaluation Name,
Experiment Name, Score.

### `GET /experiments/runs/{run_id}` - one run in detail

```json
{"run": {"id": "...", "experiment_id": "...", "run_index": 3,
         "endpoint_id": "...", "dataset_version_id": "...", "status": "completed",
         "config": {},
         "metrics": [{"metric_name": "accuracy", "mode": "gen", "metric_value": 0.62}],
         "raw_results": { }}}
```

`raw_results` is a preview of the engine's own output - useful for showing a
user *why* a score is what it is. 404 unless you own the parent experiment.

### Rankings

```bash
# leaderboard for one dataset
bud api GET /experiments/datasets/<dataset-id>/scores -q page=1 -q limit=50

# cross-dataset; at least ONE of endpoint_id / model_id / dataset_id is required
bud api GET /experiments/scores -q endpoint_id=<uuid> -q limit=50
```

Omitting all three filters returns
`400 At least one filter (endpoint_id, model_id, or dataset_id) must be provided`.

Both return `scores[]` with `rank`, `model_id`, `model_name`,
`model_display_name`, `endpoint_name`, `accuracy`, `metrics[]`, `num_runs`,
`created_at` (plus `dataset_id`/`dataset_name`/`endpoint_id` on the
cross-dataset variant), and the usual `page`/`limit`/`total_record`/`total_pages`.

Only **completed** runs from **your own** experiments are counted. Ranking is
computed in application code after averaging - sorted by accuracy descending
with nulls last - which means `rank` is only meaningful within the page you
fetched. Pull every page before declaring a winner.

### Comparison charts

```bash
bud api GET /experiments/compare/deployments -q limit=50
bud api GET /experiments/compare/traits  -q deployment_ids=<a>,<b>
bud api GET /experiments/compare/radar   -q deployment_ids=<a>,<b> -q trait_ids=<t1>,<t2> \
                                         -q start_date=2026-01-01 -q end_date=2026-08-01
bud api GET /experiments/compare/heatmap -q deployment_ids=<a>,<b> -q dataset_ids=<d1> -q limit=10
```

| | radar | heatmap |
|---|---|---|
| Grain | per trait | per dataset |
| Aggregate | **MAX** across completed runs | **AVERAGE** across completed runs |
| Extras | `deployments[].color` for plotting | `stats{min_score,max_score,avg_score}` for colour scaling |
| Default breadth | all matching | **only 5 most recent deployments** unless you raise `limit` (max 50) |

Because radar takes the best run and heatmap the mean, a deployment routinely
looks stronger on the radar than on the heatmap. That is not a bug and it is not
a regression - name the aggregate whenever you quote either.

`compare/deployments` only lists deployments that have at least one completed
run, and returns `experiment_count` / `run_count` per deployment. An empty list
means nothing anywhere has finished, not that your filters are wrong.

A bad uuid inside any CSV filter gives 400 - or, more often, a generic 500.

### Privacy caution

`/experiments/compare/deployments`, `/compare/traits`, `/compare/radar`,
`/compare/heatmap`, `/experiments/{id}/runs` and `/experiments/{id}/summary`
apply **no ownership filter**. On a shared installation they expose other
tenants' deployment names, model names and scores. Before pasting comparison
output anywhere, check whether the deployments in it are ones you recognise, and
prefer `/experiments/scores` + `/experiments/datasets/{id}/scores` when the
results are sensitive.

## Endpoints that return nothing trustworthy

- `GET /experiments/{id}/runs/history` - **hardcoded dummy data**. Ten
  fabricated rows: random UUIDs, `model": "model_name"`, benchmarks
  `[{"name":"Benchmark","score":"Score"}, ...]`, a fixed `2024-01-13` timestamp
  and `duration_seconds: 4920`, with `total: 10` regardless of reality. It
  paginates the fake list convincingly. Never consume it and never render it.
- `GET /experiments/{id}/evaluations` - `scores` is always null.
- `experiment.stats` and `progress.percent` from `GET /experiments/{id}` -
  hardcoded zeros.

## Not evaluation scores

`GET /models/{model_id}/leaderboards` and `POST /models/top-leaderboards` return
**curated third-party benchmark tables** imported with the model catalog. They
describe published results for a model, not anything you ran, and they exist to
help *choose* a model to deploy. They live in `bud-models`. Never merge them into
a table of your own evaluation results.

## Pagination quirks in this domain

| Endpoint | Page-size parameter | Total field |
|---|---|---|
| most `/experiments/*` | `limit` | `total_record` |
| `/experiments/evaluations/all` | `page_size` | `total_record` |
| `/experiments/{id}/runs/history` | `page_size` | `total` (fake) |
| `/benchmark/request-metrics` | `limit` | `total_items`, rows under `items` |

`GET /experiments/datasets` has no `ORDER BY`, so paging it is not
deterministic. Fetch every page (`--paginate`) and de-duplicate by id rather
than trusting page boundaries.
