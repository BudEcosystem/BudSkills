# Inference failures, verbatim

The gateway does not use one error envelope. Recognising which one you got is
half the diagnosis.

| Shape | Emitted by |
|---|---|
| `{"error":{"message":"...","type":"...","code":401}}` | credential and routing checks - the OpenAI-style envelope |
| `{"error":"Model `<uuid>` is not configured to support capability `embedding`. ..."}` | capability check - note `error` is a **plain string** and names an internal id, not your alias |
| `{"message":"This model only supports streaming. Set \"stream\": true"}` | the serving process itself, passed through |
| `{"error":{"message":""},"provider_error":""}` | upstream failure with nothing useful to relay |
| `{"object":"error","message":"...","type":"...","param":null,"code":"..."}` | the management API, not the gateway - you are on the wrong host |

## Credential failures

**`401 {"error":{"message":"Missing authorization header",...}}`**
No `Authorization` header at all. Also the healthy response to an
unauthenticated probe, which is how you confirm you found the gateway.

**`401 {"error":{"message":"Invalid API key","type":"invalid_request_error","code":401}}`**
The single most common failure, and it has five distinct causes:

1. the token was minted but never registered (`bud token` does both - a
   hand-rolled mint does not);
2. the session was refreshed after registering, so the registered token is no
   longer the one you are sending - re-run `bud token`;
3. the token is older than about an hour;
4. the header says `bearer` rather than `Bearer` - the prefix match is
   case-sensitive;
5. it is a project API key straight out of `POST /credentials/`, which is
   encrypted ciphertext, not a key.

There is no way to tell these apart from the response. Work down the list.

## Naming and routing failures

**`404 {"error":{"message":"Model not found: Qwen/Qwen3-4B","code":404}}`**
You sent a model name instead of a deployment name. Or the deployment is not
`running`, or belongs to a project this credential cannot see, or was created in
the last minute and the credential's alias list is still stale. Check
`GET /v1/models` first - it is the credential's own view.

**`404 {"error":{"message":"Model not found: gpt-4-responses"}}`**
You called a Responses sub-resource (`GET`/`DELETE`/`cancel`/`input_items`)
without the `x-model-name` header. There is no such deployment - that literal
name is the fallback the gateway uses when the header is missing. Add
`x-model-name: <your deployment name>`.

**`400 {"error":"Model \`<uuid>\` is not configured to support capability \`embedding\`. ..."}`**
The alias is yours, but that deployment does not serve that path. The uuid is
the deployment's internal id, not something you sent. Check
`supported_endpoints` on the deployment and pick the right path, or deploy a
model that has the capability.

**`400 Missing model name in request body`**
`model` is required on every path that carries a JSON body, including
`/v1/moderations`, whose own schema misleadingly marks it optional.

**`400 {"error":{"message":"Invalid request body"}}` on a multipart request**
`audio/transcriptions`, `audio/translations`, `images/edits` and
`images/variations` are rejected before the handler because the credential check
parses the body as JSON. Documented `-F` examples do not work against an
authenticated installation.

**`404` from the management host on a `/v1/...` path**
Wrong host entirely. `/v1/*` only exists on the gateway.

## Model behaviour that looks like an error, and is not

**`{"message":"This model only supports streaming. Set \"stream\": true"}`**
That deployment's serving configuration is streaming-only. Resend with
`"stream": true` and consume the SSE stream. Nothing is wrong with the
deployment.

**`content: null` with `finish_reason: "length"`**
A reasoning model consumed its entire token budget on internal reasoning and
never emitted a visible answer. HTTP 200, empty content, no error. Raise
`max_tokens` substantially - a reasoning model may need several thousand - or
lower `reasoning_effort`. Never report this to a user as "the model returned
nothing"; report it as "the token budget was too small".

**A stream that ends immediately at `data: [DONE]` with no content deltas**
An upstream failure that arrived after the response had already started, so the
status code is 200. Count content chunks and treat zero as a failure.

## Deployment and upstream failures

**`500 {"code":"guardrail_failure"}`**
The safety policy attached to that deployment is misconfigured or its evaluator
is unreachable. This is a property of the deployment, not of your request -
retrying the same call in a loop will only repeat it. Report it and go to
`bud-guardrails`.

**`502 {"error":{"message":""},"provider_error":""}`**
The model process rejected the request and returned nothing usable. Seen when a
path is dispatched to a deployment that cannot really serve it. Check
`supported_endpoints`, then the deployment's health via `bud-deployments`.

**`502` whose `provider_error` is the serving process's own 404**, e.g.
`{"error":{"message":"{\"detail\":\"Not Found\"}"},"provider_error":{...}}` -
observed when sending chat to an embedding-only deployment. **A wrong path does
not always produce the clean `400 ... capability ...` above**; some deployments
answer with whatever their serving process said. If a call fails oddly, check
`supported_endpoints` before assuming the deployment is broken.

**`502` with an empty body on `/v1/embeddings`**
You asked for `"encoding_format":"base64"`. Send `"float"`. This is the default
in the OpenAI Python SDK, so it bites SDK users and not curl users.

**`502` / `503` carrying a provider's own wording**
For a hosted (commercial) model, the upstream provider's error is passed
through - invalid API key, exhausted credit, model deprecated, region blocked.
Read the message literally: it is the provider talking, not Bud. Fix it on the
provider credential (`GET /proprietary/credentials/`) or with the provider's
billing, not by retrying.

**`502` on `/v1/chat/completions` with "Mismatched thinking tags"**
A reasoning model truncated mid-thought. Same fix as the `content: null` case:
more `max_tokens`.

## Limits

**`429 {"error":{"message":"Rate limit exceeded","type":"rate_limit_error","code":"rate_limit_exceeded"}}`**
The per-deployment limit. `Retry-After` and `X-RateLimit-Reset` tell you when to
resume. Do not raise concurrency to compensate. Remember the budget is per
deployment, so a router and the deployment behind it share it.

**`402 {"error":{"message":"Usage quota exceeded: ...","type":"insufficient_quota","code":402}}`**
A spend limit, not a rate limit, and keyed to the credential's owner rather than
the deployment. This is a budget decision - stop and tell the user. Retrying
cannot succeed.

## Slow, not failing

1. Is the deployment scaled for the load? `bud api GET /endpoints/<id>/workers`
2. Was it scaled to zero? The first call after a pause pays a cold start.
3. Are gateway-side retries configured? A call that took three attempts still
   looks like one request, but its latency includes all three.
4. Measure rather than guess - `bud-observability`.

Sustained slowness on a self-hosted deployment usually means under-provisioning:
revisit the concurrency and latency targets in `bud-deployments`.

## `503 ... ownership store is unavailable`

The Batch and Files APIs fail closed when the gateway's record store is off,
which is the default in the shipped configuration. `POST /v1/files` still
appears to succeed. Treat batches as unavailable unless you have confirmed
end to end that they work on this installation.

## Nothing works and you are not sure why

```bash
bud health                                    # is the installation up?
bud whoami                                    # is the session valid?
curl -s -o /dev/null -w '%{http_code}\n' "$BUD_GATEWAY_URL/v1/models" \
     -H "Authorization: Bearer $(bud token)"  # is the gateway reachable, is auth good?
```

If the management API answers and the gateway does not, the gateway or its route
is down. That is a platform problem to report - with the time and the exact
response - not something to work around.

## When nothing is obviously wrong

If a call succeeds but you cannot see it afterwards, that is expected for the
first minute - records lag. If it still is not there, and the observability
endpoints are also returning 500 or empty for known-good traffic, the
installation's analytics storage is degraded; inference keeps working
regardless. Say so plainly rather than doubting the inference call.

## 502 with an empty or "Not Found" error body

```json
{"error":{"message":""},"provider_error":""}
{"error":{"message":"{\"detail\":\"Not Found\"}"}}
```

**The deployment does not serve the path you called.** The model name resolved,
so this is not a naming problem - the deployment has no handler for that
capability. Most commonly: calling `/v1/responses` on a chat-only deployment
(which is what an agent does on every run), or `/v1/chat/completions` on an
embedding deployment.

**Do not retry.** This is a permanent configuration mismatch, not a restarting
component, so the generic 502 advice ("retry after a pause") loops forever.

```bash
bud api GET /playground/deployments -q limit=100 \
  | jq -r '.endpoints[] | select(.name=="<deployment>") | .model.supported_endpoints
      | to_entries[] | select(.value.enabled) | "\(.key)\t\(.value.path)"'
```

Beware the asymmetry: the *reverse* mismatch (embeddings on a chat deployment)
returns a helpful `400` naming the capability. A clear error in one direction
does not mean a clear error in the other - the empty 502 is the same class of
fault wearing a worse disguise.
