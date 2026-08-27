# Agent lifecycle: authoring, configuration, schemas, versions

Everything here is the management API (`bud api`). Execution is in
`running-agents.md`.

## The identifier map, in full

| Name here | What it is | Where it is accepted |
|---|---|---|
| **agent UUID** | the row id of the agent | `GET/PATCH/DELETE /prompts/{id}`, `/prompts/{id}/versions/**`, governance bindings, trace `resource_id` |
| **version UUID** | the row id of one version | `/prompts/{id}/versions/{version_id}` |
| **version number** | an integer, `1`-based | the `version` query/body param on every config, tool and schema call; `prompt.version` at run time (as a **string**: `"2"`) |
| **config key** | draft uuid before finalisation, **agent name** after | `/prompts/prompt-config/{key}`, `/prompts/{key}/native-tools/**`, `/prompts/{key}/connectors/**`, `/prompts/{key}/connections/**`, `/prompts/{key}/agents`, `/connect-agent`, `/disconnect-agent`, `/prompts/{key}/triggers`, `prompt.id` on `/v1/responses` |
| **agent name** | globally unique string | `/prompts/{name}/traces`, `/prompts/agent-card/{name}`, `/a2a/{name}/{version}/`, gateway key map entries |

Passing an agent UUID to a route that wants the name (traces, agent card)
returns 404 with no hint about why.

## Listing and finding agents

```bash
bud api GET /prompts -q page=1 -q limit=50 \
  | jq -r '.prompts[] | "\(.id)  \(.name)  v\(.default_version)  \(.status)"'

bud api GET /prompts -q search=true -q name=hr          # fuzzy
bud api GET /prompts -q project_id=$BUD_PROJECT_ID
bud api GET /prompts -q prompt_type=external_a2a_agent
bud api GET /prompts -q order_by=-modified_at
```

Fields on each row: `id`, `project_id`, `name`, `description`, `tags`,
`prompt_type`, `model_icon`, `model_name`, `default_version` (an integer),
`modality`, `status`, `last_health_checked_at`, timestamps. Sortable by `name`,
`created_at`, `modified_at`, `model_name`, `default_version`, `modality`,
`status`.

`status` is **not** the agent's own state - for a normal agent it is the status
of the deployment behind its default version (so `running` means the model is
up), and for an external agent it is the last health-check result
(`healthy`/`unhealthy`). Only active agents are listed at all.

> Listing agents requires the `endpoint:manage` permission, while reading one
> agent requires only `endpoint:view`. A view-only user can open an agent they
> already know the id of but gets a 403 from the list. That asymmetry is real,
> not a bug in your call.

Single agent, with its project, default version, model and cluster:

```bash
bud api GET /prompts/<agent-uuid> | jq '.prompt'
```

Tags: `GET /prompts/tags` (paginated) and `GET /prompts/tags/search`.

Metadata edits - description, tags, or switching the default version:

```bash
bud api PATCH /prompts/<agent-uuid> -d '{"description":"...","tags":[{"name":"hr","color":"#4F8EF7"}]}'
bud api PATCH /prompts/<agent-uuid> -d '{"default_version_id":"<version-uuid>"}'
```

Nulls are ignored (fields you send as `null` are dropped, not cleared).
Switching the default this way and `PATCH .../versions/{id} {"set_as_default":true}`
do the same thing.

## The prompt configuration

`POST /prompts/prompt-config` is the authoring workhorse. Omit `prompt_id` to
mint a new draft; pass the config key to update an existing one.

| Field | Notes |
|---|---|
| `prompt_id` | config key. **Omit** to create a draft. |
| `version` | integer, default 1. Which version's config you are writing. |
| `set_default` | make this version the default in the runtime's pointer |
| `deployment_name` | the **name** of the deployment that will serve it |
| `system_prompt` | Jinja2 template |
| `system_prompt_role` | `system` (default) \| `developer` \| `user` |
| `messages` | `[{role, content}]`; role is `system`/`developer`/`user`/`assistant`. `content` is a string or a list of parts: `{"type":"text","text":...}`, `{"type":"image_url","image_url":{"url":...,"detail":"auto"\|"low"\|"high"}}`, `{"type":"input_file","file_url":...}` |
| `model_settings` | see below |
| `stream` | default streaming behaviour for this agent |
| `llm_retry_limit` | retries on model failure |
| `enable_tools` | must be paired with `allow_multiple_calls: true` |
| `allow_multiple_calls` | lets the agent take more than one model turn (required by any tool use) |
| `permanent` | `false` = draft with a TTL, `true` = saved. One-way. |
| `client_metadata` | free dict, **merged** not replaced; the platform stamps `prompt_id`/`project_id` into it and strips yours |
| `a2a_card` | `{name, description, skills[], provider, documentation_url, icon_url}` - what other agents see |
| `media_inputs` | `{"image":{"required":bool},"file":{"required":bool}}` |
| `supported_modalities`, `supported_endpoints` | **ignored on input** - derived from the deployment |

`model_settings` accepts `temperature` (0-2, default 0.7), `max_tokens`,
`max_completion_tokens`, `top_p`, `frequency_penalty`, `presence_penalty`,
`stop_sequences`, `seed`, `timeout`, `parallel_tool_calls`, `logprobs`,
`logit_bias`, `response_format`, `tool_choice`, `reasoning: {"enabled": bool}`,
`stream_options`, `extra_headers`, plus guided-decoding options
(`guided_json`, `guided_regex`, `guided_choice`, `guided_grammar`,
`structural_tag`, `guided_decoding_backend`, `guided_whitespace_pattern`) and
`chat_template` / `chat_template_kwargs` / `mm_processor_kwargs`.

The response is **flat** - `{"bud_prompt_id": "...", "bud_prompt_version": 1}` -
unlike almost every other Bud response.

Reading a config back:

```bash
bud api GET /prompts/prompt-config/hr-assistant -q version=1 | jq '.data'
bud api GET /prompts/<agent-uuid>/versions/<version-uuid> | jq '.config_data'
```

> Both of these degrade silently. `GET /prompts/prompt-config/{key}` answers
> **200 with an all-empty config** when nothing exists ("returning empty
> config"), and the version route returns an all-null `config_data` if the
> config store read fails. Check `deployment_name` for existence; treat an empty
> config as "unknown", never as "this agent has no configuration".

Two further behaviours to plan around:

- **Partial writes.** Only keys present in the body are written; omitted keys
  keep their old values; a key you send replaces its old value entirely (a
  shorter `messages` list truncates). `client_metadata` is the one exception -
  it merges, and an empty dict is a no-op.
- **`permanent` is one-way.** Once a config key has been saved with
  `permanent: true` its TTL is gone, and any later save without it fails with
  "Cannot downgrade a permanent prompt to temporary". Since `connect-agent`
  forces `permanent: true`, connecting a sub-agent to a *draft* quietly locks
  that draft.

A freshly saved config can be served stale for a few minutes when the runtime is
running more than one replica - the caches invalidate per replica. If a run
seems to use the old prompt right after an edit, wait and re-test before
concluding the write failed.

## Structured input and output schemas

Give an agent a typed contract: a JSON Schema plus natural-language validation
rules that the platform compiles into validators. This is asynchronous.

```bash
bud api POST /prompts/prompt-schema -d '{
  "step_number": 1, "workflow_total_steps": 1, "trigger_workflow": true,
  "prompt_id": "<config key>", "version": 1, "type": "input",
  "deployment_name": "<deployment-name>",
  "schema": {
    "schema": {
      "type": "object",
      "properties": {"question": {"type": "string"}, "employee_band": {"type": "integer"}},
      "required": ["question"]
    },
    "validations": {
      "InputModel": {"employee_band": "must be between 1 and 8"}
    }
  }
}'
# -> {"workflow_id": "..."}

bud wait job <workflow_id> --timeout 300
```

Then repeat with `"type": "output"` for a typed answer. Expect seconds to about
a minute per call - it makes model calls to synthesise the validators. On
failure, `GET /workflows/{id}` carries the `reason`.

> **Reserved names.** Top-level *input* schema property names are checked
> case-insensitively against the governance variable namespace and a collision
> is a 422. Rejected names include `project_id`, `memory`, `budget`, `provider`,
> `channel`, `roles`, `env`, `region`, `city`, `timezone`, `principal`,
> `tenant`, `user_type`, `is_minor`, `has_pii`, and anything whose first dotted
> segment is `bud`, `ctx`, `headers` or `meta`. Fetch the live list with
> `bud api GET /governance/reserved-variable-names`. Output schemas are exempt.

Schemas belong to a config key + version, so a new version needs its schemas
re-applied.

`POST /prompts/prompt-schema` needs a real signed-in session (it 401s with "No
access token in session" otherwise) - just re-run `bud login` if you see that.

## Versions

```bash
bud api GET /prompts/<agent-uuid>/versions -q page=1 -q limit=50
bud api GET /prompts/<agent-uuid>/versions -q version=3        # exact match
bud api GET /prompts/<agent-uuid>/versions/<version-uuid>      # + config_data
```

Create (see SKILL.md for the full iterate-and-promote flow):

```bash
bud api POST /prompts/<agent-uuid>/versions -d '{
  "endpoint_id": "<endpoint-uuid>", "bud_prompt_id": "<draft-uuid>", "set_as_default": false
}'
```

What this does behind the call: assigns `max(existing)+1` as the number, copies
the draft config onto `<agent name>` at that version and makes it permanent,
stamps the agent/project identity into it, re-syncs governance policy references
onto the deployment, and rebuilds the gateway's key map so
`prompt:<name>:v<N>` resolves.

Edit a version:

```bash
bud api PATCH /prompts/<agent-uuid>/versions/<version-uuid> -d '{"set_as_default":true}'
bud api PATCH /prompts/<agent-uuid>/versions/<version-uuid> -d '{"endpoint_id":"<other-endpoint>"}'
```

Repointing at another deployment re-derives the model and cluster - it is the
supported way to move an agent onto a different model. The deployment must be in
the same project.

Delete a version:

```bash
bud api DELETE /prompts/<agent-uuid>/versions/<version-uuid>
```

400 if it is the default. Soft delete: the number stays consumed, so version
numbering can have gaps.

Promotion order matters when something goes wrong. Setting a default writes the
runtime pointer *first*, then the database, then the gateway cache. If the first
step fails you get a 500 and nothing changed - safe. If the last step is what
failed, the agent record says the new version is default but the gateway may
still route to the old one; a no-op `PATCH` on the version forces the rebuild.

## Testing a draft before it is an agent

Draft configs are not in a project key's model map, so a plain gateway token
cannot run one. Drafts are reachable through a playground session instead
(`POST /playground/initialize*`), which injects the caller's drafts into a
session-scoped key map. If you need a pre-finalisation smoke test and that path
is not available to you, the pragmatic route is to finalise into a
throwaway-named agent, test, then delete it.

## External agents (A2A)

Register an agent that runs somewhere else so it can be called and composed like
a local one. There is no config and no version row - it is a pointer plus
credentials.

```bash
bud api GET /prompts/a2a-catalog -q name=<search>        # optional: pre-seeded catalog

bud api POST /prompts/prompt-workflow -d '{
  "step_number": 1, "workflow_total_steps": 1, "trigger_workflow": true,
  "project_id": "'"$BUD_PROJECT_ID"'",
  "name": "vendor-research-agent",
  "prompt_type": "external_a2a_agent",
  "base_url": "https://agents.vendor.example",
  "a2a_version": "0.3",
  "auth_type": "bearer",
  "credential": {"token": "..."}
}'

bud api GET /prompts -q name=vendor-research-agent -q prompt_type=external_a2a_agent
bud api GET /prompts/agent-card/vendor-research-agent | jq '.agent_card'
```

- `auth_type` is `none`, `bearer` or `api_key`.
- The remote agent card is fetched **and validated during the call** - an
  unreachable or malformed `base_url` is a 400, not a deferred health failure.
- `base_url` must be unique among active external agents.
- `GET /prompts/{id}/versions` is empty for these and `/v1/responses` does not
  apply. Invoke them at `POST /a2a/{name}/v0/` on the gateway, or connect them
  as a sub-agent tool of a local agent.
- Health is refreshed on a schedule; read `status` and `last_health_checked_at`
  from the list. `POST /prompts/agent-health-check` triggers a sweep and is an
  unauthenticated cron hook - do not call it as part of normal work.

## Agent cards

```bash
bud api GET /prompts/agent-card/hr-assistant -q version=0     # 0 = default version
```

For a local agent this is generated from its config (`a2a_card` plus derived
skills); for an external one it is fetched live from the remote with a fallback
to the stored copy. The card is what other agents and external A2A clients see,
so keep `a2a_card.description` and `skills` accurate if the agent is meant to be
composable. The public discovery form on the gateway
(`/a2a/{name}/{version}/.well-known/agent-card.json`) is unauthenticated -
assume anything in the card is public.

## Cleanup of abandoned drafts

`POST /prompts/prompt-cleanup` tears down connector gateways and virtual servers
left behind by drafts that were never finalised. It also runs on a schedule.
There is nothing to poll; verify indirectly with
`GET /prompts/connectors -q prompt_id=<key>`. Only reach for it if you
abandoned a draft that had connectors registered.
