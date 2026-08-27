# The inference gateway API surface

Everything here is on the **gateway host** (`https://gateway.<domain>`), takes
`Authorization: Bearer <token from bud token>`, and follows the OpenAI request
and response shapes unless noted. Only `/v1/*` and `/a2a/*` are exposed
externally - the gateway's own status, health and metrics endpoints are not
reachable from outside the installation, so do not use them as a readiness
probe. Use `GET /v1/models` instead.

## Paths and the capability each needs

A deployment advertises `supported_endpoints`; a path only works if the matching
capability is enabled on that deployment.

| Path | Capability | Notes |
|---|---|---|
| `POST /v1/chat/completions` | `chat` | the main one; supports SSE streaming |
| `POST /v1/completions` | `completions` | legacy text completion |
| `POST /v1/messages` | `chat` | Anthropic-style clients, translated onto the chat path |
| `POST /v1/embeddings` | `embedding` | send `encoding_format: "float"` |
| `POST /v1/classify` | `classify` | rerank / sequence classification |
| `POST /v1/moderations` | `moderation` | `model` is a guardrail profile name |
| `POST /v1/responses` | `responses` | stateful / agentic turns |
| `POST /v1/audio/speech` | `text_to_speech` | JSON in, audio bytes out |
| `POST /v1/audio/transcriptions` | `audio_transcription` | multipart - see the caveat below |
| `POST /v1/audio/translations` | `audio_translation` | multipart - see the caveat below |
| `POST /v1/images/generations` | `image_generation` | |
| `POST /v1/images/edits` | `image_edit` | multipart - see the caveat below |
| `POST /v1/images/variations` | `image_variation` | multipart - see the caveat below |
| `POST /v1/documents` | `document` | document parsing / extraction |
| `POST /v1/realtime/sessions` | `realtime_session` | mints a realtime session token |
| `POST /v1/realtime/transcription_sessions` | `realtime_transcription` | |
| `POST /v1/files`, `POST /v1/batches` | `batch` | see the batch caveat below |
| `GET /v1/models` | - | discovery; always available |
| `POST /v1/governance` | - | policy verdict, see below |
| `POST /v1/traces`, `/v1/metrics`, `/v1/logs` | - | forward your own client telemetry |
| `POST /a2a/{agent}/{version}/` | - | agent-to-agent JSON-RPC |

> **Multipart caveat.** With authentication on (i.e. always, in a real
> installation) the four multipart paths reject `multipart/form-data` with
> `400 {"error":{"message":"Invalid request body"}}` before the handler runs,
> because the credential check parses the body as JSON. Published curl examples
> that use `-F` therefore fail. `/v1/audio/speech` takes JSON and is unaffected.
> If you need transcription today, do it outside Bud or ask for the fix.

> **Batch/files caveat.** `GET /v1/files/{id}`, `GET /v1/batches/{id}` and the
> cancel routes fail closed with `503 ... ownership store is unavailable` on
> installations where gateway observability storage is off, which is the default
> in the shipped configuration. `POST /v1/files` still appears to succeed. Treat
> the Batch API as unavailable unless you have confirmed it works.

## `GET /v1/models`

Returns exactly the aliases the presented token may call:

```json
{"object":"list","data":[{"id":"mock-inference-deploy","created":0,"object":"model","owned_by":"bud"}]}
```

`created` is always `0` - it carries no information. Agents are filtered out of
this list even though they are callable; discover those through the agents API.

## Bud-specific request fields

On top of the standard OpenAI body:

| Field | Values | Effect |
|---|---|---|
| `reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `auto` | how much budget a reasoning model spends thinking |
| `modality` | `text`, ... | disambiguates multi-modal embedding models |
| `dimensions` | integer | truncated embedding size, where the model supports it |

Self-hosted deployments also accept the serving engine's own extensions, which
are forwarded rather than rejected:

| Field | Use |
|---|---|
| `guided_json`, `guided_regex`, `guided_choice`, `guided_grammar`, `structural_tag` | constrained decoding; `guided_json` takes a JSON Schema |
| `guided_decoding_backend`, `guided_whitespace_pattern` | tune the constraint engine |
| `chat_template`, `chat_template_kwargs`, `mm_processor_kwargs` | prompt templating and multimodal preprocessing |
| `repetition_penalty`, `ignore_eos` | sampling controls outside the OpenAI set |
| `reasoning` | `{effort, max_tokens, exclude, enabled}` - finer than `reasoning_effort` |
| `thinking`, `thinking_config` | provider-native thinking configuration |

> **Unknown fields are forwarded, not rejected.** A misspelled parameter
> silently does nothing rather than returning 422 - verify the effect, not just
> the status code.

**Response caching is off by default** and is opted into per request with a
literal, namespaced key in the body:

```json
{"model":"my-deploy","messages":[...],
 "tensorzero::cache_options":{"enabled":"on","max_age_s":3600}}
```

`enabled` is `on`, `off`, `read_only` or `write_only`. Moderation and audio
paths never cache. A cache hit is reported as `cached: true` on the recorded
inference, not in the response body.

**Routing headers you send are ignored.** The gateway strips its own internal
routing headers from inbound requests, so you cannot pin a project, identity or
provider from the client side. The single exception is `x-model-name`, which is
required on the Responses sub-resources below.

## Headers

Request headers you may set:

| Header | Effect |
|---|---|
| `Authorization: Bearer <token>` | mandatory; the `Bearer ` prefix is case-sensitive |
| `Content-Type: application/json` | required on JSON bodies |
| `Accept: text/event-stream` | streaming |
| `x-model-name` | selects the alias on `/v1/responses` sub-resources |

`x-api-key` is **not** accepted for authentication, and the gateway's own
internal routing headers are stripped from anything you send.

Response headers worth reading:

| Header | Meaning |
|---|---|
| `x-tensorzero-inference-id` | the inference id; also `.id` in JSON bodies, and the only way to get it from a non-JSON response such as audio |
| `x-tensorzero-model-latency-ms` | upstream model latency for this call |
| `x-ratelimit-limit` / `-remaining` / `-reset` | per-deployment budget; all `0` means no limit is configured |
| `Retry-After` | present on 429 |
| `x-tensorzero-gateway-version` | gateway build; worth quoting in a bug report |

## Responses API

Create with either a model or an agent, never both:

```bash
# model form
curl -s "$BUD_GATEWAY_URL/v1/responses" -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"cbre-v1","input":"Hello","instructions":"Be brief","max_output_tokens":256}'

# agent form (no "model" field)
  -d '{"prompt":{"id":"<agent id>","version":"v2","variables":{}},"input":"..."}'
```

Sending neither gives `400 Model name or prompt.id required in request body`.

For long work add `"background":true,"store":true`; the call returns
immediately with `status` of `queued` or `in_progress`. Then poll:

```bash
curl -s "$BUD_GATEWAY_URL/v1/responses/$RID" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" \
  -H "x-model-name: cbre-v1"          # REQUIRED on every sub-resource call
```

Statuses: `queued` and `in_progress` are non-terminal; `completed`, `failed`,
`cancelled` and `incomplete` are terminal; `requires_action` means a governance
approval is waiting on a human. Sub-resources are `GET /v1/responses/{id}`,
`DELETE /v1/responses/{id}`, `POST /v1/responses/{id}/cancel` and
`GET /v1/responses/{id}/input_items` - **all four need `x-model-name`**, or you
get `404 Model not found: gpt-4-responses`.

Per-deployment ceilings come from the deployment's response configuration and
default to `max_tool_calls` 50, `chain_depth_limit` 200 and
`background_wall_clock_seconds` 3600. Read or change them with:

```bash
bud api GET /endpoints/$EP/responses-config
bud api PUT /endpoints/$EP/responses-config -d '{"responses_config":{ ... }}'
```

That PUT **replaces** the whole object - read it, edit it, write it back.
Setting `enabled: false` removes the path from the deployment.

A useful asymmetry: a chat-capable deployment usually accepts `/v1/responses`
even when `supported_endpoints` does not list it, because Bud adds the
translation automatically. The reverse is not true - a deployment that only
declares `embedding` will never serve chat.

## Governance verdicts

`POST /v1/governance` evaluates an agent-governance policy and **always returns
HTTP 200**; the decision is in the body: `allow`, `observe`, `transform`,
`require_approval`, `deny` or `notify`. `guard_type` is `pre_request`,
`post_request`, `pre_tool` or `post_tool` (`input` and `output` are accepted
aliases for the first two). When authentication is on, the policy is chosen from
the deployment you are calling, not from `model` in the body. Policy authoring
lives in `bud-guardrails`.

## Agent-to-agent

`POST /a2a/{agent_name}/{version}/` proxies JSON-RPC to a Bud agent or an
external one; the agent is resolved from the URL rather than the body. The agent
card at `GET /a2a/{agent_name}/{version}/.well-known/agent-card.json` is public
and needs no credential - useful for checking an agent is reachable before
wiring anything up. See `bud-agents`.

## Forwarding your own telemetry

`POST /v1/traces`, `/v1/metrics` and `/v1/logs` accept client-side OTLP and
forward it into the installation's telemetry store, so a client application's
own spans land beside its inference records. These validate the credential and
never read the body, so a malformed payload is accepted silently. Read the
result back through `bud-observability`.
