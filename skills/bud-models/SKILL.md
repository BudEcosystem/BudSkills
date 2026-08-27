---
name: bud-models
description: Find, add and manage models in Bud Foundry - browse the hosted-model catalog, import a model from Hugging Face, register a provider key, compare models on cost and benchmark scores, run a security scan, and quantize a model. Use when a Bud request mentions models, the model registry or catalog, Hugging Face, adding or importing a model, provider keys, quantization, or choosing which model to use.
---

# Bud Foundry: models

The registry holds every model this installation can serve. Models arrive two
ways: **hosted** models from a commercial provider (you supply a provider key),
and **self-hosted** models imported from Hugging Face or a URL and stored on
your own infrastructure.

Prerequisite: connect first - see `bud-platform` (`bud login`).

## The one thing that catches everyone

`GET /models/` has a `table_source` parameter with two values, and **the
default is the browse catalog, not your registry**:

| | `table_source=model` | `table_source=cloud_model` (**default**) |
|---|---|---|
| Contains | models actually in your registry | hosted models available to add |
| Deployable | yes | not until added |
| Ids work with `GET /models/{id}` | yes | **no - they 404** |

```bash
bud api GET /models/ -q table_source=model -q page=1 -q limit=50    # yours
bud api GET /models/ -q table_source=cloud_model -q source=anthropic  # available to add
```

So "list models and deploy one" fails with a misleading `404 Model not found`
unless you pass `table_source=model`. Whenever you need an id to *do* something
with, use `table_source=model`.

## Choosing a model

```bash
# what is in the registry, newest first
bud api GET /models/ -q table_source=model -q order_by=-created_at --paginate

# search by name (search=true makes name a partial match)
bud api GET /models/ -q table_source=model -q search=true -q name=llama

# filter by capability
bud api GET /models/ -q table_source=model -q supported_endpoints=/v1/chat/completions
bud api GET /models/ -q table_source=model -q modality=text_input -q modality=image_input
bud api GET /models/ -q table_source=model -q model_size_min=7 -q model_size_max=14

# what values exist to filter on
bud api GET /models/tasks
bud api GET /models/tags
bud api GET /models/authors
```

**Quality.** Where benchmark data is available, it lets you rank by measured
accuracy rather than guesswork:

```bash
bud api GET  /models/<model-id>/leaderboards -q table_source=model -q k=10
bud api POST /models/top-leaderboards -d '{"benchmarks":["mmlu","gpqa","humaneval"],"k":5}'
```

> **An empty result is common and means "no data here", not "these models are
> unproven".** Benchmark data is populated per installation; on one with none
> seeded, `top-leaderboards` returns `{"leaderboards": []}` for any valid
> request. If the benchmark service is temporarily unavailable you get
> `400 Failed to fetch leaderboards` instead - retry before concluding
> anything.

Scores also only exist for models with published benchmark data - a private or
freshly imported model will have none. When scores are unavailable, say so and
rank on something you can measure; never present an absent score as a bad one.

**Cost.** Pricing lives with *deployments*, never with registry or catalog
entries - a model has no price until it is served. To compare what is running:

```bash
bud api GET /playground/deployments -q limit=100 | jq -r '
  .endpoints[] | select(.input_cost != null)
  | [.name, (.input_cost.input_cost_per_token*1000000),
             (.output_cost.output_cost_per_token*1000000)] | @tsv' | sort -k2 -g
```

Two traps that produce confidently wrong answers, both covered in
`references/choosing-a-model.md`: `input_cost` is an **object**, not a number
(sorting on it directly compares JSON); and
`model_cluster_recommended.cost_per_million_tokens` is a hardware sizing
artifact that **inverts the true ranking** for hosted models. For hosted models
not yet added, no price is exposed at all - say so rather than substituting
another signal.

**Matching a model to a job** ("something good for HR questions") is *not*
something the platform decides for you. Filter on capability, size and
benchmark scores, then apply your own judgement about the task - and say which
signals you used. See `references/choosing-a-model.md`.

## Adding a hosted model

Two stages: register the provider key once, then add models against it.

```bash
# 1. what providers exist, and what fields does this one need?
bud api GET /models/providers -q limit=100
bud api GET /proprietary/credentials/provider-info -q provider_name=openai

# 2. store the key (once per provider)
CRED=$(bud api POST /proprietary/credentials/ -d '{
  "name":"prod-openai","type":"openai","provider_id":"<provider-uuid>",
  "other_provider_creds":{"api_key":"sk-..."}}' | jq -r .result.id)

# 3. pick a catalog entry
bud api GET /models/ -q table_source=cloud_model -q source=openai -q search=true -q name=gpt-4o

# 4. add it - a short stepped session
W=$(bud api POST /models/cloud-model-workflow -d '{
  "workflow_total_steps":5,"step_number":2,
  "provider_type":"cloud_model","add_model_modality":["llm"]}' | jq -r .workflow_id)
bud api POST /models/cloud-model-workflow -d '{"workflow_id":"'"$W"'","step_number":3,"provider_id":"<provider-uuid>"}'
bud api POST /models/cloud-model-workflow -d '{"workflow_id":"'"$W"'","step_number":4,"cloud_model_id":"<catalog-model-id>"}'
bud api POST /models/cloud-model-workflow -d '{"workflow_id":"'"$W"'","step_number":5,"trigger_workflow":true,
  "name":"GPT-4o (prod)","modality":["text_input","text_output"],
  "tags":[{"name":"chat","color":"#4A9DFF"}]}'
```

This is **synchronous** - the final call returns the new model id at
`workflow_steps.model_id`. No waiting.

The model `name` must be globally unique (case-insensitive), as must the
combination of source and identifier. Both come back as 400s.

## Importing a model from Hugging Face

This one is long-running - minutes for a small model, hours for a large one.

```bash
# optional: a Hugging Face token, needed only for gated repositories
HF=$(bud api POST /proprietary/credentials/ -d '{
  "name":"hf","type":"huggingface","other_provider_creds":{"api_key":"hf_..."}}' | jq -r .result.id)

W=$(bud api POST /models/local-model-workflow -d '{
  "workflow_total_steps":5,"step_number":1,
  "provider_type":"hugging_face","add_model_modality":["llm"]}' | jq -r .workflow_id)

bud api POST /models/local-model-workflow -d '{
  "workflow_id":"'"$W"'","step_number":2,"trigger_workflow":false,
  "name":"Qwen3-8B","uri":"Qwen/Qwen3-8B","author":"Qwen",
  "tags":[{"name":"chat","color":"#4A9DFF"}]}'

bud api POST /models/local-model-workflow -d '{
  "workflow_id":"'"$W"'","step_number":3,"trigger_workflow":true,
  "proprietary_credential_id":"'"$HF"'"}'

# minutes to hours - size it to the model
bud wait job $W --timeout 7200 --interval 30
MODEL=$(bud job show $W --data | jq -r '.data.model_id')
```

Notes that matter:

- **Keep the job id.** There is no way to poll by model - the model does not
  exist until the import finishes. If you lose it:
  `bud jobs --limit 30 | jq '.[] | select(.kind=="local_model_onboarding")'`.
- **Do not re-send the trigger step while it is running** - you will start a
  second import.
- A rejection for **insufficient storage** appears as a failed capacity step
  before any download starts. That is a real answer: report the shortfall
  rather than retrying.
- Duration tracks repository size. A 1B model is minutes; 70B+ can take hours.

Then confirm what you got, since capabilities are derived during import:

```bash
bud api GET /models/$MODEL | jq '.model | {name, modality, supported_endpoints,
  is_quantizable, quantization_unsupported_reason,
  is_adapter_supported, adapter_unsupported_reason}'
```

## Security scanning

Recommended for anything imported from a public source.

```bash
S=$(bud api POST /models/security-scan -d '{
  "workflow_total_steps":3,"step_number":1,"trigger_workflow":true,
  "model_id":"'"$MODEL"'"}' | jq -r .workflow_id)
bud wait job $S --timeout 1800
bud api GET /models/$MODEL | jq '.scan_result'
bud api GET /models/$MODEL/security-scan/export      # full report
```

Report the findings to the user before deploying a model that scanned dirty -
do not silently proceed.

## Quantizing

Quantization shrinks a self-hosted model so it serves on cheaper hardware, at
some cost in quality.

```bash
bud api GET /models/$MODEL | jq '.model | {is_quantizable, quantization_unsupported_reason}'
bud api GET /models/quantization-methods -q limit=1000
```

Check `is_quantizable` first and surface the reason when it is false. Choose a
method whose `runtime_hardware_support` includes the hardware you intend to
serve on - a model quantized into a format your target cannot run is a wasted
job. Full walkthrough: `references/quantization.md`.

> The quality figures reported after quantization are not a reliable measure of
> real-world accuracy. Treat them as a smoke test, and evaluate properly with
> `bud-evaluations` before trusting a quantized model.

## Deeper reference

- `references/choosing-a-model.md` - filtering, benchmark scores, cost, and
  how to answer "the cheapest model that can do X" honestly
- `references/adding-models.md` - every field of both add flows, provider
  credentials, and the failure modes
- `references/quantization.md` - methods, hardware support, the stepped flow
- `references/model-metadata.md` - the model record field by field, modalities,
  supported endpoints, adapters and model relations

## Where to go next

`bud-deployments` to serve a model, `bud-clusters` to check where it can run,
`bud-evaluations` to measure it.
