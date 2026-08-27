---
name: bud-inference
description: Call a deployed Bud model - chat completions, embeddings, streaming, responses, images, audio, rerank and moderations - over Bud's OpenAI-compatible inference gateway. Use when a Bud request mentions calling, testing, querying or "hitting" a deployment, the inference gateway, an OpenAI-compatible base_url or SDK, bearer tokens or API keys for inference, "Invalid API key" or "Model not found", streaming, token usage or what a call cost, or per-deployment rate limits, retries and fallbacks. Also use to publish a deployment so client applications can reach it.
---

# Bud Foundry: inference

Everything else in Bud happens on the management API. Actually sending a prompt
to a deployed model happens somewhere else: on the **inference gateway**, a
separate host that speaks the OpenAI API. Point any OpenAI-compatible client at
it and it works - same request bodies, same response shapes, same SDKs.

Prerequisite: connect first - see `bud-platform` (`bud login`). You also need a
deployment in `running` state - see `bud-deployments`.

## The two addresses

| | Host | Auth | Serves |
|---|---|---|---|
| Management API | `https://app.<domain>` | your signed-in session (`bud api`) | everything else |
| Inference gateway | `https://gateway.<domain>` | `Authorization: Bearer $(bud token)` | `/v1/*` |

The management host **404s on every `/v1/*` path**, so a call sent to the wrong
host looks like a missing endpoint rather than a wrong address.

### Find the gateway host

No management endpoint reports it. Derive it from the API origin and prove it:

```bash
export BUD_GATEWAY_URL="https://gateway.$(echo "$BUD_API_URL" | sed 's#https\?://[^.]*\.##')"
curl -s -o /dev/null -w '%{http_code}\n' "$BUD_GATEWAY_URL/v1/models"   # want 401
```

**`401` is the success signal for this probe** - it means a gateway answered and
asked for credentials. `404` means you are on the wrong host. If the derivation
fails, ask the user for the gateway address rather than guessing further; the
console shows it in each model's "use this model" snippet.

`scripts/bud-infer` does this discovery, the token handling below, and a smoke
call in one command - see the end of this skill.

## Get a token

```bash
export BUD_GATEWAY_TOKEN=$(bud token)
```

`bud token` does **two** things: it mints a token, then registers it with the
gateway. The gateway does not validate tokens - it looks up a registration. The
consequences bite constantly:

- A token that was never registered fails with
  `401 {"error":{"message":"Invalid API key"}}` even though it is a perfectly
  valid session token. Registration is the whole point of `bud token`.
- **A refreshed session invalidates the old registration.** After any
  re-authentication, run `bud token` again before the next inference call.
- Tokens last about an hour. Re-mint rather than caching one in a long-lived
  process, and treat a mid-run 401 as "re-mint and retry once".
- `Bearer` is matched **case-sensitively**. `authorization: bearer <token>` is a
  401, and it looks identical to a bad credential.

> **Project API keys cannot be used here.** `POST /credentials/` returns the key
> RSA-encrypted (512 hex characters) and no endpoint reveals the plaintext, so a
> key created through the API is unusable by the process that created it - the
> failure surfaces much later as `401 Invalid API key` at the gateway. Use
> `bud token`. A human who needs a long-lived copyable key must create it in the
> console. See `references/auth-and-tokens.md`.

## Find the model name

The `model` field is the **deployment name**, never the underlying model's name.
Sending `Qwen/Qwen3-4B` gets you `404 Model not found: Qwen/Qwen3-4B`; sending
`qwen-4b` works.

```bash
curl -s "$BUD_GATEWAY_URL/v1/models" -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" \
  | jq -r '.data[].id'
```

That list is authoritative for what *this token* is allowed to call. For the
same set plus status and which paths each deployment accepts:

```bash
bud api GET /playground/deployments -q page=1 -q limit=100 \
  | jq -r '.endpoints[] | "\(.name)\t\(.status)\t" +
      ([.model.supported_endpoints | to_entries[] | select(.value.enabled) | .key] | join(","))'
# mock-inference-deploy   running   chat
# cbre-emb-lfm25-350m     running   embedding
# cbre-v1                 running   chat,responses,batch
```

Only `running` deployments appear, and a freshly deployed one can take up to a
further minute to show up for an existing token. LoRA adapters
(`<deployment>-<adapter>`) and routers appear as aliases of their own. Agents do
**not** appear in `/v1/models` - call those via `bud-agents`.

## Send a chat completion

```bash
curl -s "$BUD_GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"mock-inference-deploy",
       "messages":[{"role":"user","content":"Say hello in five words."}],
       "max_tokens":64}'
```

```json
{"id":"019fd323-8098-76e3-ac30-4757cbfa6e8a","object":"chat.completion",
 "model":"mock-inference-deploy",
 "choices":[{"index":0,"finish_reason":"stop",
             "message":{"role":"assistant","content":"...","tool_calls":[]}}],
 "usage":{"prompt_tokens":57,"completion_tokens":17,"total_tokens":74}}
```

Two fields are worth keeping: `id` is the **inference id**, the handle for
looking the call up later in observability, and `usage` is the immediate,
authoritative token count. Response headers carry `x-ratelimit-limit`,
`-remaining` and `-reset`; all three zero means no rate limit is configured on
that deployment.

Standard OpenAI parameters work (`temperature`, `top_p`, `max_tokens`, `stop`,
`tools`, `tool_choice`, `response_format`, `seed`). Bud adds `reasoning_effort`
(`none`|`minimal`|`low`|`medium`|`high`|`xhigh`|`auto`) and opt-in response
caching - see `references/gateway-api.md`.

## Streaming

```bash
curl -sN "$BUD_GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"mock-inference-deploy","messages":[{"role":"user","content":"count to three"}],
       "stream":true,"stream_options":{"include_usage":true}}'
```

Chunks arrive as `data: {...}` lines and the stream ends with the literal
`data: [DONE]`. With `include_usage` the second-to-last chunk carries `usage`
and an empty `choices` array.

> An upstream failure can appear as a **successful but empty stream** that ends
> at `[DONE]` with no content deltas. Count content chunks; do not treat HTTP
> 200 as proof of an answer.

## Embeddings

```bash
curl -s "$BUD_GATEWAY_URL/v1/embeddings" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"cbre-emb-lfm25-350m","input":["hello world","second text"],
       "encoding_format":"float"}'
# -> {"object":"list","model":"...","data":[{"embedding":[ ...1024 floats ]}],"usage":{...}}
```

> **Always send `"encoding_format":"float"`.** Asking for `base64` returns `502`
> with an empty body. This matters more than it sounds: the OpenAI Python SDK
> requests base64 *by default* - see the SDK example below.

Optional: `dimensions` (only on models that support truncation) and `modality`.

## From the OpenAI Python SDK

This is how most integrations will be written. Only `base_url` and `api_key`
change.

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["BUD_GATEWAY_URL"] + "/v1",  # e.g. https://gateway.dev.bud.studio/v1
    api_key=os.environ["BUD_GATEWAY_TOKEN"],         # from `bud token`
)

print([m.id for m in client.models.list()])         # the valid `model` values

r = client.chat.completions.create(
    model="mock-inference-deploy",                  # the DEPLOYMENT name
    messages=[{"role": "user", "content": "Reply with the word OK."}],
    max_tokens=16,
)
print(r.choices[0].message.content, r.usage.total_tokens, r.id)

e = client.embeddings.create(
    model="cbre-emb-lfm25-350m",
    input=["hello"],
    encoding_format="float",                        # REQUIRED - see above
)
```

Build in two things from the start: `encoding_format="float"` on every
embeddings call, and re-authentication on `401` (re-run `bud token`, rebuild the
client), because the token expires after about an hour and the SDK will not
renew it for you.

## Other paths

The gateway exposes the rest of the OpenAI surface too:

| Need | Path | Notes |
|---|---|---|
| Stateful / agentic turns | `POST /v1/responses` | see below |
| Rerank / classify | `POST /v1/classify` | scoring, not generation |
| Moderation | `POST /v1/moderations` | `model` is a guardrail profile, and is required despite reading as optional |
| Text to speech | `POST /v1/audio/speech` | JSON in, audio bytes out |
| Images | `POST /v1/images/generations` | |
| Anthropic-style clients | `POST /v1/messages` | translated onto the chat path |

**Check `supported_endpoints` before choosing a path.** Authorisation only
proves the alias is yours; the deployment's capabilities are checked separately.
A wrong path usually answers `400 Model ... is not configured to support
capability 'embedding'`, but some deployments instead relay a `502` from their
own serving process - so check the capability before assuming a fault. The
multipart paths (`audio/transcriptions`, `audio/translations`,
`images/edits`, `images/variations`) currently reject `multipart/form-data` with
`400 Invalid request body` - see `references/errors.md`.

For `/v1/responses`, every follow-up call (`GET`, `DELETE`, `/cancel`,
`/input_items`) **must** carry `x-model-name: <deployment name>`. Omit it and
you get the baffling `404 Model not found: gpt-4-responses`. Full flow,
including `background: true` polling: `references/gateway-api.md`.

## When a call fails

Read the status and the envelope together - the gateway uses several shapes.

| Response | Cause | Do this |
|---|---|---|
| `401 Invalid API key` | token never registered, expired, refreshed since registering, or lowercase `bearer` | re-run `bud token`; check header capitalisation |
| `404 Model not found: X` | model name used instead of deployment name, or the deployment is not `running` | `GET /v1/models`; check `status` |
| `400 ... not configured to support capability 'x'` | right deployment, wrong path | check `supported_endpoints` |
| `{"message":"This model only supports streaming. Set \"stream\": true"}` | that deployment serves streaming only | resend with `"stream":true` |
| `content: null` with `finish_reason: "length"` | a reasoning model spent the whole budget thinking - **not** an error | raise `max_tokens` substantially, or lower `reasoning_effort`, and retry |
| `500 {"code":"guardrail_failure"}` | the safety policy attached to the deployment is misconfigured or unreachable | a deployment problem, not a request problem - see `bud-guardrails`; do not retry in a loop |
| `502`, empty or upstream-shaped message | the model process rejected the request, or a hosted model's provider did (bad key, exhausted credit) - **the provider's own error is passed through** | read the message literally; for a hosted model check the provider credential and its billing |
| `429 rate_limit_exceeded` | per-deployment limit | honour `Retry-After`; see below |
| `402 insufficient_quota` | spend limit reached - a different layer from 429 | a budget decision: stop and tell the user |

`references/errors.md` has every envelope observed, verbatim.

## Rate limits, retries and fallbacks

Policy is set per deployment on the management API and reaches the gateway
within about a second:

```bash
bud api GET /endpoints/$EP/deployment-settings
bud api PUT /endpoints/$EP/deployment-settings -d '{
  "rate_limits": {"algorithm":"token_bucket","requests_per_minute":600,"burst_size":50,"enabled":true},
  "retry_config": {"num_retries":2,"max_delay_s":1.5},
  "fallback_config": {"fallback_models":["<other-endpoint-uuid>"]}}'
```

- `fallback_models` takes **endpoint UUIDs**, not names, from the **same
  project**, and never the deployment's own id. A name gives
  `400 Invalid fallback endpoint ID`.
- The PUT **merges** - omitted sections keep their current values.
- Limits are counted per *deployment*, not per alias: a router and the
  deployment behind it share one budget.

Field-by-field detail is in `bud-deployments`; what these settings look like
from the caller's side is in `references/traffic-policy.md`.

## Publish a deployment so client applications can reach it

A deployment starts private to its project. Publishing lists it in the catalog
and widens which credentials may call it.

```bash
bud api PUT /endpoints/$EP/publish -d '{"action":"publish",
  "pricing":{"input_cost":0.0005,"output_cost":0.0015,"currency":"USD","per_tokens":1000}}'
bud api GET /models/catalog -q page=1 -q limit=20 | jq -r '.models[].endpoint_name'
```

`pricing` is mandatory on publish and the deployment must be `running`.
Re-publishing an already-published deployment silently ignores new pricing - use
`PUT /endpoints/{id}/pricing`. **Publishing widens reach for end-user
application credentials only**; an internal-workspace credential or a
`bud token` still sees only its own project's deployments, so do not expect your
own `/v1/models` list to grow. Unpublish with `{"action":"unpublish"}`.

## Confirm what a call cost

The `usage` block in the response is immediate and authoritative. For the
recorded record - cost, latency, cache hit, the raw bodies:

```bash
bud api POST /metrics/inferences/list -d '{
  "from_date":"2026-08-05T17:00:00Z","limit":20,
  "endpoint_id":"'"$EP"'","sort_by":"timestamp","sort_order":"desc"}'
bud api GET /metrics/inferences/<inference_id>
```

`from_date` is required. Records land seconds to a minute after the call, so
**absence in the first minute is lag, not failure** - correlate on the `id` you
already have rather than re-sending the prompt. These endpoints can also `500`
on an installation whose analytics storage is degraded while inference itself
works fine; that is an installation problem, not evidence your call failed.
Deeper analysis belongs to `bud-observability`.

## Helper

```bash
scripts/bud-infer models                          # discover host + token, list callable names
scripts/bud-infer check mock-inference-deploy     # end-to-end smoke test, with diagnosis
scripts/bud-infer chat  mock-inference-deploy "Say hello"
scripts/bud-infer embed cbre-emb-lfm25-350m "some text"
```

It resolves the gateway host, mints and registers a token, re-mints once on a
401, and names which of the failures above it hit. `--help` for options.

## Deeper reference

- `references/gateway-api.md` - the full OpenAI-compatible surface: every path,
  Bud-specific request fields, caching, the Responses API and batches
- `references/auth-and-tokens.md` - token lifecycle, what each credential kind
  can see, the encrypted-key limitation, and long-running agents
- `references/errors.md` - every error envelope, verbatim, with cause and fix
- `references/traffic-policy.md` - rate limits, retries, fallbacks, publishing
  reach and response caching from the caller's point of view

## Where to go next

`bud-deployments` to create or scale the deployment behind an alias,
`bud-observability` to analyse the traffic afterwards, `bud-agents` to call an
agent rather than a raw model, `bud-routing` to put one alias in front of
several deployments, `bud-guardrails` for safety and governance policy.
