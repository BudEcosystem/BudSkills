# Choosing a model

Requests like "the cheapest Gemini model", "the most accurate model from this
provider", or "a model that is good for HR questions" all reduce to: filter by
capability, rank by a measurable signal, then apply judgement. Be explicit
about which signal you used, because the platform does not have an opinion on
"good for HR".

## 1. Filter by capability

> **Do not shortlist with the `supported_endpoints` query filter.** It matches a
> model's endpoint set *exactly*, so it silently drops every model that also
> supports another endpoint. Measured live: the filter returns **16** chat
> models where **25** actually have chat enabled - and the 9 it drops are the
> strong general-purpose models a serious shortlist most wants.

Filter client-side instead, on the `enabled` flag:

```bash
bud api GET /models/ -q table_source=model --paginate --page-size 100 \
  | jq -r '.[] | .model
      | select(.supported_endpoints.chat.enabled)
      | "\(.id)  \(.name)  \(.model_size // "-")B"'
```

Swap `.chat` for `.embedding`, `.responses`, `.audio_transcription` and so on.
The response field is an object keyed by capability - see
`model-metadata.md` for its full shape, and read that **before** composing
filters, not after.

Filters that *are* safe to use as query parameters:

```bash
bud api GET /models/ -q table_source=model -q modality=image_input
bud api GET /models/ -q table_source=model -q model_size_min=7 -q model_size_max=13
#  ^ filters are in BILLIONS of parameters; the `model_size` field in responses
#    is the RAW parameter count (4022468096, not 4). Filtering with a raw count
#    returns nothing.
bud api GET /models/ -q table_source=cloud_model -q source=anthropic
bud api GET /models/ -q table_source=cloud_model -q search=true -q name=gemini
```

`search=true` switches `name`/`source` to partial matching - and on the catalog
table it also flips filter combination from AND to OR, so results get broader,
not narrower.

What values exist on this installation:

```bash
bud api GET /models/tasks        # task labels   (registry only)
bud api GET /models/tags         # tags in use   (registry only)
bud api GET /models/authors      # publishers
```

Note `tasks`/`tags` read the **registry**, so their vocabulary does not cover
the hosted catalog. The catalog's own vocabulary endpoint
(`/models/cloud-models/recommended-tags`) returns **500** on current builds -
if you need catalog tags, list catalog entries and collect the tags yourself.
Both endpoints match by **prefix**, so `?name=reason` will not find
`commonsense-reasoning`.

**Context length is not exposed.** `token_limit`, `max_input_tokens` and
`context_length` are `null` on every model and catalog record on the reference
installation. If the user's inputs are long, say you cannot confirm the window
from the platform and source it from the provider - do not filter on it.

## 2. Rank by cost

**Read this before promising a user "the cheapest X".** Cost data here is not
just thin - several fields that look like a price are wrong, and they fail
silently. The section below is the authoritative recipe; prefer it over
improvising.

### The one command to use

```bash
bud api GET /playground/deployments -q limit=100 2>/dev/null | jq -r '
  .endpoints[]
  | select(.input_cost != null)
  | [ .name,
      (.input_cost.input_cost_per_token   * 1000000),
      (.output_cost.output_cost_per_token * 1000000),
      (.input_cost.input_cost_per_token_cache_hit // empty | . * 1000000)
    ] | @tsv' | sort -k2 -g
```

Output is `deployment, $/M input, $/M output, $/M cached-input`, cheapest first.

> `input_cost` and `output_cost` are **objects, not numbers**. The keys are
> `input_cost_per_token` and `output_cost_per_token`, both priced per **single**
> token - multiply by 1e6 for a per-million figure. Sorting on `.input_cost`
> directly compares JSON objects and yields a confident, wrong answer with a
> zero exit code.

Optional sibling keys, present on some deployments and worth surfacing because
they change the recommendation:

| Key | Meaning |
|---|---|
| `input_cost_per_token_cache_hit` | discounted rate for repeated prompt prefixes - often **10x** cheaper |
| `input_cost_per_token_batches` | discounted rate for asynchronous batch work - typically 50% |
| `output_cost_per_token_batches` | the same for output |

A cache-hit rate matters enormously for assistants that resend a large system
prompt every turn. Check for it before ranking.

### Fields that look like cost and are not

These will be quoted by anyone who does not know better. Two of them invert the
true ordering:

| Field | Why not |
|---|---|
| `model_cluster_recommended.cost_per_million_tokens` | **A hardware sizing artifact, not a token price.** It is populated for hosted models where it is meaningless. Measured live: it rates `moonshot-vision` at 0.224 and `gpt-4-nano` at 0.919 - but their real input costs are **$2.00/M and $0.20/M**. Ranking on it picks a model 10x more expensive and calls it cheapest. Use it only for self-hosted sizing. |
| `pricing.input_cost` on `/models/catalog` | The price **you** charge downstream consumers, set at publish. Often `0.0`, which is not the same as free-to-you - `gpt-4-nano` is published at $0.00 while costing $0.20/M. |
| observed spend from analytics | A blended figure over real traffic, not a rate. Measured live it disagrees with declared rates by orders of magnitude on low-traffic deployments. Not comparable. |

On a single deployment the platform can report **four different, mutually
inconsistent costs**. Say which one you used.

### All the cost sources

| Source | Prices | Whose number |
|---|---|---|
| `GET /playground/deployments` | running deployments | the **vendor's** cost per token - use this |
| `GET /endpoints/{id}/pricing` | one published deployment | the price you charge; 404 when unpublished, which is the cleanest published-vs-not test |
| `GET /models/catalog` | published deployments | the price you charge |
| `GET /models/?table_source=cloud_model` | hosted models available to add | **nothing - no price is returned** |

Normalisation traps:

- `pricing.per_tokens` **varies per entry** - observed at 1e3, 1e6 and 1e7 on
  one installation. Always divide by it; assuming 1000 can be off by 10,000x.
- `/playground/deployments` costs are per **single** token; catalog prices are
  per `per_tokens`. Never put the two in one sorted list.
- `pricing: null` means **unpriced**, not free. Exclude and say so - on the
  reference installation 19 of 29 deployments are unpriced.

### "Deploy the cheapest Gemini model" - answer honestly

The hosted catalog does **not** expose per-token pricing. Listing
`?table_source=cloud_model&source=gemini` returns names, capabilities and
modalities - and no cost field. You cannot rank not-yet-added hosted models by
price through the API.

What to do, in order:

1. **If one is already deployed**, price it from `GET /playground/deployments`.
2. **If not**, say plainly that the platform publishes no list price for hosted
   models, and either ask the user or rank on something you *can* measure
   (capability, context, benchmark score) while naming the substitution.
3. Only then offer to add and deploy a candidate so its cost becomes visible.

Do not silently rank on a different signal and call the winner "cheapest", and
do not quote prices from your own knowledge of a vendor's public list - the
installation may be on a reseller or negotiated rate.

For a **self-hosted** model, cost is not a property of the model: it comes from
the capacity plan at deploy time (`bud-deployments`), whose `cost_per_token`
field is - despite its name - cost per **million** tokens.

## 3. Rank by quality

```bash
bud api GET  /models/<model-id>/leaderboards -q table_source=model -q k=10
bud api POST /models/top-leaderboards -d '{"benchmarks":["mmlu","gpqa","humaneval"],"k":5}'
```

Each entry is `{model: {uri, model_size, is_selected}, benchmarks: {<name>:
{type, label, value}}}`. `top-leaderboards` gives the best registry models per
named benchmark - the closest thing to "the most accurate model available".

Benchmark names are normalised (lowercase, punctuation stripped). Recognised
ones include: `mmlu`, `mmlupro`, `gpqa`, `drop`, `humaneval`, `livecodebench`,
`bcfl`, `mathvista`, `mmbench`, `mmstar`, `mmmu`, `ocrbench`, `ai2d`,
`hallucinationbench`, `mmvet`, `truthfulqa`-style safety sets, and the
retrieval/clustering family (`retrieval`, `clustering`, `reranking`,
`semantic`, `summarization`, `classification`, `pairclassification`).

Rough capability mapping, useful when a user asks for "good at X":

| Capability | Benchmarks |
|---|---|
| Reasoning | `mmbench`, `mmstar`, `drop` |
| Knowledge | `mmlu`, `mmlupro`, `gpqa`, `mmmu` |
| Coding | `humaneval`, `livecodebench` |
| Math | `mathvista` |
| Tool use | `bcfl` |

Two distinct empty-ish outcomes, which mean different things:

| Response | Meaning | What to do |
|---|---|---|
| `{"leaderboards": []}` | the call worked; this installation has no benchmark data seeded | rank on another signal, or evaluate yourself |
| `400 Failed to fetch leaderboards` | the benchmark service is momentarily unavailable | retry; if it persists, report it |

Never report either as "this model scores poorly". When no scores exist, fall
back to your own evaluation (`bud-evaluations`) and say that is what you did.

Limits to state honestly:

- Scores exist only for models with published benchmark data. Private,
  fine-tuned and freshly imported models have none - absent is not bad.
- Benchmarks measure general capability. None of them measure "good at our HR
  policies". For that, build a small evaluation set and measure it
  (`bud-evaluations`) - that is the only defensible answer.

## 4. Task fit

For "a model good for X", there is no semantic matcher. What you have is
`tasks`, `tags`, benchmark scores, size and modality. A reasonable approach:

1. Filter to models that can do the mechanics (right endpoint, right modality,
   right size). Context length is not exposed by the platform - if it matters,
   say so rather than guessing.
2. Shortlist on the benchmark closest to the work - reasoning benchmarks for
   analysis, instruction-following for assistants, code benchmarks for code.
3. Rank the shortlist by cost.
4. **Say which two or three you shortlisted and why**, and offer to evaluate
   them against real examples rather than asserting a winner.

For a cost-sensitive assistant task, a small recent instruction-tuned model is
usually the right starting point, with a stronger model held in reserve behind
a router (`bud-routing`) for hard queries.

## 5. Compatibility before you commit

```bash
bud api GET /models/<model-id> | jq '.model | {
  is_quantizable, quantization_unsupported_reason,
  is_adapter_supported, adapter_unsupported_reason,
  supported_endpoints, modality, model_size, model_cluster_recommended}'
```

`model_cluster_recommended` carries a pre-computed sizing hint
(`cost_per_million_tokens`, `hardware_type`, `cluster`). **`null` means the
sizing pass has not run yet, not that nothing fits** - do not report it as "no
capacity". For a real answer, run a capacity plan via `bud-deployments`.
