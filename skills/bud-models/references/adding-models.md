# Adding models: both flows in full

## Provider credentials (hosted models)

One credential per provider, reused by every model from it.

```bash
bud api GET /models/providers -q limit=100
bud api GET /proprietary/credentials/provider-info -q provider_name=openai
```

`provider-info` returns `result.credentials[]` describing the exact fields that
provider needs and which are required - read it rather than assuming `api_key`.

```bash
bud api POST /proprietary/credentials/ -d '{
  "name": "prod-openai",
  "type": "openai",
  "provider_id": "<provider-uuid>",
  "other_provider_creds": {"api_key": "sk-..."}}'
```

The id is at `result.id`, not the top level. Secrets are never returned again.

```bash
bud api GET /proprietary/credentials/ -q limit=50
```

## Hosted model, step by step

| Step | Body | Purpose |
|---|---|---|
| open | `{"workflow_total_steps":5,"step_number":2,"provider_type":"cloud_model","add_model_modality":["llm"]}` | returns `workflow_id` |
| provider | `{"workflow_id":W,"step_number":3,"provider_id":"<uuid>"}` | which provider |
| model | `{"workflow_id":W,"step_number":4,"cloud_model_id":"<catalog id>"}` | which catalog entry |
| finish | `{"workflow_id":W,"step_number":5,"trigger_workflow":true,"name":"...","modality":[...],"tags":[...]}` | creates it |

`add_model_modality` accepts `llm`, `mllm` and similar family labels;
`modality` on the final call takes the concrete
`text_input`/`text_output`/`image_input`/... values.

Synchronous: the final response carries
`workflow_steps.workflow_execution_status.status == "success"` and
`workflow_steps.model_id`.

Failures: `Model name already exists` (names are globally unique,
case-insensitive) and `Duplicate model uri and source found`. Search the
registry first.

## Self-hosted (Hugging Face) model, step by step

| Step | Body | Purpose |
|---|---|---|
| open | `{"workflow_total_steps":5,"step_number":1,"provider_type":"hugging_face","add_model_modality":["llm"]}` | returns `workflow_id` |
| describe | `{"workflow_id":W,"step_number":2,"trigger_workflow":false,"name":"...","uri":"Org/Repo","author":"...","tags":[...]}` | `uri` is the repository path |
| start | `{"workflow_id":W,"step_number":3,"trigger_workflow":true,"proprietary_credential_id":"<hf-cred or omit>"}` | begins the download |

`provider_type` may also be `url` (a downloadable archive) or `disk` (already
present on the installation's storage).

```bash
bud wait job $W --timeout 7200 --interval 30
bud job show $W --data | jq -r '.data.model_id'
```

Progress appears as steps under `model_extraction_events`. Success is the job
settling with a non-null `model_id`; failure surfaces with a `reason`.

Common failures:

- **Gated repository** - needs a Hugging Face token as
  `proprietary_credential_id`. The error names the repository.
- **Not enough storage** - reported as a failed capacity step *before* any
  download. Free space or choose a smaller model; retrying identically will
  fail identically.
- **Repository not found** - check the `uri` is `Org/Repo`, not a URL.

Import derives modality and supported endpoints from the repository contents,
so verify them afterwards rather than assuming - especially for embedding and
multimodal models, where a mis-derived modality makes the model undeployable
for the endpoint you wanted.

## After adding

```bash
bud api GET  /models/<model-id>                       # verify what was derived
bud api POST /models/security-scan -d '{"workflow_total_steps":3,"step_number":1,
                                        "trigger_workflow":true,"model_id":"<id>"}'
bud api PATCH /models/<model-id> -d '{"tags":[{"name":"approved","color":"#22aa55"}]}'
bud api DELETE /models/<model-id>                     # blocked while deployments reference it
```

Deleting a model fails while any non-deleted deployment uses it - remove those
first (`bud-deployments`).
