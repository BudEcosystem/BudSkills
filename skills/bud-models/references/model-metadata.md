# The model record

`GET /models/{model_id}` (registry ids only - see the catalog-vs-registry note
in the skill) returns the model under `.model`, plus `scan_result`,
`model_tree` and `endpoints_count` alongside it.

## Fields worth knowing

| Field | Meaning |
|---|---|
| `id`, `name`, `description`, `author`, `uri` | identity; `uri` is the source path such as `Qwen/Qwen3-8B` |
| `provider_type` | `cloud_model` (hosted) or `hugging_face` / `url` / `disk` (self-hosted) |
| `source` | the provider or origin (`openai`, `anthropic`, ...) |
| `model_size` | parameters in billions; `null` for hosted models |
| `modality` | which input/output kinds it handles |
| `supported_endpoints` | which API surfaces it can serve |
| `tasks`, `tags` | labels for filtering |
| `is_quantizable` + `quantization_unsupported_reason` | computed - cannot be filtered on |
| `is_adapter_supported` + `adapter_unsupported_reason` | whether LoRA adapters can attach |
| `model_cluster_recommended` | pre-computed sizing hint, or `null` if not yet run |
| `is_present_in_model` | catalog listings only - and **not** a reliable indicator that the id resolves |
| `local_path` | where a self-hosted model is stored |

`is_quantizable` and `is_adapter_supported` are computed on read. They appear
in responses but are **not filter parameters** - filter client-side.

## Modality values

`text_input`, `text_output`, `image_input`, `image_output`, `audio_input`,
`audio_output`. A chat model is `text_input` + `text_output`; a vision model
adds `image_input`.

## Supported endpoints

This is what decides whether a model can do the job at all. An embedding model
will not serve chat however good its benchmark scores.

**The response field and the query filter use different shapes** - this catches
people out.

In a **response**, `supported_endpoints` is an object keyed by capability, and
each entry carries a path *without* a leading slash plus an `enabled` flag:

```json
{
  "chat":      {"path": "v1/chat/completions", "enabled": true,  "label": "Chat Completions"},
  "embedding": {"path": "v1/embeddings",       "enabled": false, "label": "Embeddings"},
  "responses": {"path": "v1/responses",        "enabled": false, "label": "Responses"}
}
```

So test capability with `.supported_endpoints.chat.enabled`, **not** by
searching for the string `"/v1/chat/completions"`. An entry being present does
not mean it is usable - `enabled` is the field that matters:

```bash
bud api GET /models/<id> | jq -r '.model.supported_endpoints
  | to_entries[] | select(.value.enabled) | "\(.key)  \(.value.path)"'
```

As a **query filter**, the same parameter takes the full path *with* a leading
slash, and it works as expected:

```bash
bud api GET /models/ -q table_source=model -q supported_endpoints=/v1/chat/completions
```

Valid filter values:

```
/v1/chat/completions   /v1/embeddings      /v1/responses
/v1/audio/transcriptions  /v1/audio/translations  /v1/audio/speech
/v1/documents          /v1/batch           /v1/images/generations
/v1/images/edits       /v1/images/variations
/v1/classify           /v1/moderations
```

Note `/v1/rerank` appears in some enumerations but is not served as its own
path on the inference gateway - reranking is reached through `/v1/classify`.
Confirm against your installation with `bud paths` before relying on it.

One filtering quirk: on the registry table the filter matches the model's
endpoint set rather than testing containment, so filtering by a single endpoint
can return fewer models than you expect. When precision matters, list and
filter client-side on `enabled`.

## Model relations

Models can be derived from others:

| `base_model_relation` | Meaning |
|---|---|
| `adapter` | a LoRA adapter for a base model |
| `finetune` | a fine-tuned derivative |
| `merge` | a merge of several models |
| `quantized` | a reduced-precision version |

```bash
bud api GET /models/ -q table_source=model -q base_model=<uri> -q base_model_relation=quantized
bud api GET /models/ -q table_source=model -q exclude_adapters=true
```

`model_tree` on the detail response counts each relation type, which is a quick
way to see whether a quantized or fine-tuned variant already exists before
creating another.

## Security scan results

`scan_result` on the detail response carries the most recent scan. Export the
full report with `GET /models/{id}/security-scan/export`. Treat a model that
has never been scanned as unknown rather than clean, and scan anything imported
from a public source before deploying it.
