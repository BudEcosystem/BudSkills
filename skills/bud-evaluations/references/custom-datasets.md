# Eval datasets, traits, and your own question/answer set

## The catalog is read-only

There is **no create, update or delete endpoint for eval datasets or traits, and
no file upload**. The full surface is:

```
GET  /experiments/datasets                 list
GET  /experiments/datasets/{dataset_id}    one, with its traits
GET  /experiments/datasets/{id}/scores     leaderboard for it
GET  /experiments/traits                   list traits
POST /experiments/sync/datasets            re-import the operator-controlled manifest
```

The sync is the only write path, and it imports from a manifest the installation
owns - not from anything a caller supplies. Anyone promising "upload your CSV of
Q/A pairs" is describing a feature that does not exist.

## What is in the catalog

On the installation checked: **395 catalogued datasets**, of which **51** are
actually runnable. (The shipped manifest lists 446 entries; not all of them
import.) Treat these as indicative and count for yourself:

```bash
bud api GET /experiments/datasets -q limit=1 | jq .total_record                        # 51  runnable
bud api GET /experiments/datasets -q limit=1 -q has_gen_eval_type=false | jq .total_record  # 395 whole catalog
```

`has_gen_eval_type=false` does **not** mean "only the broken ones" - it removes
the filter and returns everything.

`has_gen_eval_type` **defaults to true**, which is the useful default: it hides
every dataset whose `eval_types` lacks a `gen` key, and those can never be
evaluated. Set it to `false` only to confirm that a name is already taken.

Other filters: `name` (matches name *and* description), `modalities`, `language`,
`domains`, `trait_ids` - all comma-separated CSV where plural.

> No `ORDER BY` is applied, so pagination is non-deterministic: the same row can
> appear on two pages or on none. Use `--paginate` and de-duplicate by id.

A dataset row (fields that matter):

```json
{"id": "44ecbd2d-...", "name": "AGIEval",
 "description": "...",
 "eval_types": {"gen": "agieval_gen"},          // the engine config name - the key field
 "metrics": ["accuracy"], "evaluator": "AccEvaluator",
 "modalities": ["text"], "language": ["English"], "domains": ["General"],
 "task_type": ["difficulty:medium"],
 "traits": [{"id": "088e69bc-...", "name": "Examination", "description": "..."}],
 "sample_questions_answers": null, "advantages_disadvantages": {...},
 "why_run_this_eval": [...], "what_to_expect": [...], "meta_links": {...}}
```

`eval_types.gen` is the name of a dataset configuration **built into the
evaluation engine image**, e.g. `agieval_gen`. That string - not the dataset
uuid, and not the manifest's data file - is what actually gets executed.

`ModalityEnum` = `text|image|video|actions|embedding` (the field docs only
advertise the first three).

## Traits

Traits are global capability groupings; they index datasets and drive step 3 of
the evaluation wizard.

```bash
bud api GET /experiments/traits -q limit=100 | jq -r '.traits[] | "\(.id)  \(.name)"'
```

The manifest defines **23**:

```
Strong Reasoning, Multimodal, Science, Reasoning, Agent, Code, Math, Instruct,
Examination, Knowledge, Medical, Understanding, Language, Safety, Long-Context,
Creation, Hallucination, Alignment, Visual-Qa, Visual-Localization,
Spatial-Understanding, Video-Understanding, Other
```

In practice only about **11** are reachable, because the rest index no runnable
dataset - a stock installation returns Examination, Strong Reasoning, Instruct,
Reasoning, Code, Safety, Language, Understanding, Other, Knowledge, Math. Always
pass `limit=100`; the default page size is 10 and will silently hide half of them.

If a user asks for a trait that is not in the returned list (Medical, Agent,
Multimodal are the common ones), say it has no runnable datasets on this
installation rather than picking a near-neighbour.

## Adding your own dataset - what it really takes

Two things must both be true before a dataset can be evaluated:

1. A row exists in the catalog with a non-empty `eval_types.gen`.
2. The string in `eval_types.gen` names a dataset configuration **that already
   exists inside the evaluation engine image**.

The API only helps with (1). (2) is an image build, owned by whoever operates
the installation. Get that confirmed before doing any of the below - otherwise
the evaluation launches, the job fails, and every run is marked `failed`.

### Step 1 - confirm the name is free

Datasets are matched **by name** on import. Reusing an existing name *updates*
that row instead of creating a new one.

```bash
bud api GET /experiments/datasets -q name="My HR Set" -q has_gen_eval_type=false
```

### Step 2 - add a manifest entry

The manifest is a JSON document with `version_info`, `traits.definitions[]` and
`datasets.<collection>.datasets[]`. Append to that last array:

```json
{
  "id": "custom_hr_qa",
  "name": "HR Policy QA",
  "version": "1.0.0",
  "description": "50 internal HR policy question/answer pairs",
  "url": "custom/hr_qa.jsonl",
  "size_mb": 0.1,
  "checksum": "sha256:...",
  "sample_count": 50,
  "traits": ["Knowledge"],
  "eval_type": {"gen": "hr_qa_gen"},
  "metadata": {"format": "jsonl", "language": "English", "domain": "HR"},
  "metrics": ["accuracy"],
  "evaluator": "AccEvaluator"
}
```

| Field | Behaviour on import |
|---|---|
| `name` | the match key. Also what `GET /experiments/datasets?name=` searches. |
| `eval_type.gen` | **mandatory in practice.** Empty `{}` imports fine and can never be run - that is why roughly seven in eight catalogued rows are dead. |
| `traits[]` | matched against the traits **already in the catalog**, exact first then case-insensitive. **An unknown trait name is silently skipped**, leaving the dataset unreachable from step 3 of the wizard. Use a name that `GET /experiments/traits` actually returns - the manifest's 23 definitions are not all guaranteed to be present. |
| `version` | a new string creates a new dataset version. Runs always bind to the **latest by creation time**. |
| `metrics`, `evaluator` | stored as metadata and displayed; they do **not** change what is computed. Only `accuracy` is ever recorded. |
| `url`, `checksum`, `size_mb`, `sample_count` | stored as metadata. The engine does not fetch `url` to run the evaluation. |
| `metadata.*` | most sub-fields are not mapped onto the row; `modalities` defaults to `["text"]` regardless. |

Bump `version_info.current_version` at the top of the manifest. A sync without
`force_sync` **no-ops** when the manifest version equals the last completed one.

### Step 3 - sync and verify

```bash
bud api POST /experiments/sync/datasets -q force_sync=true
```

Returns immediately; the workflow id is embedded in the `message` string. There
is **no status endpoint** - poll the catalog instead:

```bash
bud api GET /experiments/datasets -q name="HR Policy QA" -q has_gen_eval_type=true
bud api GET /experiments/datasets/<id> | jq '{traits: .dataset.traits, gen: .dataset.eval_types.gen}'
```

Expect seconds to a couple of minutes for the full manifest (it commits in
batches of 50). `dataset.traits` must be non-empty and `eval_types.gen` must be
set; if either is missing, fix the manifest and re-sync rather than proceeding.

Individual datasets can fail while the overall sync reports success, and the
failure is only visible in operator logs. Verify explicitly; do not assume.

Then run the normal flow from `SKILL.md`, using the trait id from step 3 of this
section and your dataset id in wizard step 4.

## The pragmatic alternative: score it yourself

For most requests - "here are 50 HR question/answer pairs, how does my agent
do?" - the manifest route is the wrong shape of effort. It needs an image build,
it only ever yields an accuracy number, and it cannot target an agent or a prompt
version at all (the wizard takes an `endpoint_id`).

Use `scripts/qa-eval` in this skill; `SKILL.md` has the worked example. In
short:

1. Keep the pairs in a local JSONL: `{"input": "...", "expected": "..."}` (one
   object per line; `question`/`answer` work as synonyms, as does a CSV with
   those columns).
2. Point it at one or more **gateway model names**. A deployment is its own
   name; an agent is `prompt:<agent-name>`, and a specific prompt version is
   `prompt:<agent-name>:v<N>` - which is how you test the version under
   consideration rather than whichever one is currently default.
3. Choose a scorer per case or globally: `exact`, `contains`, `regex`, or
   `judge` (a model call to a separate, stronger deployment).
4. Read the per-target accuracy table and, more importantly, the cases where the
   targets disagree.

Tell the user plainly that these results live in your files, not in the Bud
console: nothing here writes to `/experiments/*`, so it will not appear in the
experiment list, the leaderboards or the comparison charts.

This also answers "which prompt version scores best", which the evaluation
subsystem structurally cannot: prompt versions are not endpoints. The only
in-platform way to compare them is to deploy each variant as its own endpoint
and run one evaluation per endpoint - expensive, and worth it only for a
long-lived candidate.

Bud has no prompt-optimisation search. Systematically generating and testing
prompt candidates needs an external optimisation toolkit driven from
`bud-agents`; Bud supplies the model calls and the versioning, not the search.

## Not this catalog

`GET /dataset` (singular, no `/experiments` prefix) is the **performance**
dataset catalog - the traffic sources for load benchmarks. 17 rows on a stock
installation, with `hf_hub_url`, `formatting` (`sharegpt`/`alpaca`),
`num_samples`, `status` (`active|inactive`). Also read-only. Its ids belong in
benchmark step 2 and nowhere near an evaluation. See `benchmarks.md`.
