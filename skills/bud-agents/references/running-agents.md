# Running agents: synchronous, streaming, durable

Execution happens on the **inference gateway** (`gateway.<domain>`), not the
management API. It takes a bearer token, and `bud api` does not target it.

```bash
export BUD_GATEWAY=https://gateway.<your-domain>
export BUD_GATEWAY_TOKEN=$(bud token)
```

`bud token` mints **and registers** the token; an unregistered one fails as
`401 Invalid API key` even though it is perfectly well formed. Re-run it after
about an hour. See `bud-platform`.

## The request

`POST /v1/responses`

| Field | Notes |
|---|---|
| `prompt` | `{"id": "<agent name>", "version": "2", "variables": {...}}`. `version` is a **string**. Omit it for the default version. |
| `model` | alternative to `prompt` - a raw deployment name, no agent. Exactly one of the two is required. |
| `input` | a string, or a list of typed items |
| `instructions` | per-call override of the system instruction |
| `previous_response_id` | continue from an earlier response |
| `stream` | `false` for one JSON body, `true` for SSE. **Send it explicitly** - omitted it inherits the agent's stored default. |
| `background` | `true` for a durable run. Requires `store: true`. |
| `store` | default `true`; must stay `true` for background, resume and retrieval |
| `tools`, `tool_choice`, `parallel_tool_calls`, `max_tool_calls` | per-call tool control on top of the agent's own tools |
| `max_output_tokens`, `temperature`, `top_p`, `top_logprobs`, `reasoning`, `text`, `modalities`, `truncation` | per-call model controls |
| `governance_approval` | `{"decision":"approve"\|"deny","escalation_id":"..."}` - used when resuming a run that a policy stopped |
| `metadata` | at most 16 keys; keys ≤ 64 chars, values ≤ 512 |
| `service_tier`, `safety_identifier`, `prompt_cache_key`, `include`, `context_management`, `stream_options` | passthrough options |

Rejected outright: `conversation` (422 - use `previous_response_id`).

## The response envelope

A standard Responses envelope (OpenAI-compatible shape) with **no Bud wrapper** -
unlike the management API, read `.id` and `.status` directly.

```
id                  resp_...  the handle for retrieval, cancel, approvals, traces
object              "response"
status              see the state table below
output[]            the items; text is at output[*].content[*].text
usage               {input_tokens, output_tokens, total_tokens, ...details}
previous_response_id, instructions, input[], tools[], tool_choice, text,
reasoning, modalities, parallel_tool_calls, max_output_tokens, temperature,
top_p, metadata, background, store, error, incomplete_details, approval,
prompt {id, version, variables}
```

Extracting the answer:

```bash
jq -r '[.output[].content[]? | select(.text) | .text] | join("")'
```

Do not assume `output[0]` is the message - tool calls and reasoning items share
the list.

## Synchronous

```bash
curl -sS "$BUD_GATEWAY/v1/responses" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":{"id":"hr-assistant","variables":{"question":"leave policy?"}},
       "input":"leave policy?","stream":false}'
```

Multi-turn: pass `previous_response_id` and the new `input`. The stored prior
turn supplies the history, so do not resend it.

Failure shapes specific to this path:

| Symptom | Cause |
|---|---|
| `404 Prompt not found: X` | the key's model map has no `prompt:X` (or `prompt:X:vN`) entry - the agent is a draft, was just created, or the cache rebuild failed. Force a rebuild with a no-op `PATCH` on the version. |
| `400 This deployment does not support image input` | you sent an image to a text-only deployment |
| `403` with an error envelope | a governance policy denied or escalated the run (`bud-guardrails`) |
| `200` with `status: "requires_action"` | a policy raised an escalation someone must resolve |

## Streaming, and resuming a broken stream

```bash
curl -sSN "$BUD_GATEWAY/v1/responses" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":{"id":"hr-assistant"},"input":"...","stream":true,"store":true}'
```

Every event carries a `sequence_number`. **Track the last one you processed.**
After a disconnect, reconnect and replay from there:

```bash
curl -sSN "$BUD_GATEWAY/v1/responses/resp_...?stream=true&starting_after=42" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" \
  -H 'x-model-name: hr-assistant'
```

It replays every buffered event after that sequence number, then tails live
events to a terminal one. Resume only works when the response was stored.

Two caveats: streamed runs do not get the governed-execution record that the
non-streaming path creates, so a policy escalation surfaces as a plain 403
rather than a resolvable `requires_action`; and stored bodies for streamed runs
can be raw event text rather than JSON, which matters when you later read them
back through traces.

## Durable (background) runs

```bash
curl -sS "$BUD_GATEWAY/v1/responses" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":{"id":"hr-assistant"},"input":"long task","background":true,"store":true}'
# -> {"id":"resp_...","status":"queued"}
```

The `resp_*` id is simultaneously the response handle (gateway), the run
`session_id` (`/runs`, `/approvals`) and the execution id for escalations. One
id, three surfaces.

### State machine

| Status | Terminal? | Meaning |
|---|---|---|
| `queued` | no | accepted, not yet claimed |
| `runnable` | no | ready for the next turn |
| `in_progress` | no | a turn is executing |
| `suspended` | no | parked - read `suspend_reason` |
| `awaiting_approval` | no | parked on a human tool-call gate |
| `requires_action` | no | parked on a governance escalation |
| `completed` | **yes** | finished normally |
| `incomplete` | **yes** | stopped early (commonly `deadline_exceeded`) |
| `failed` | **yes** | errored |
| `cancelled` | **yes** | aborted |

`suspend_reason` is `awaiting-approval` (a human must approve a tool call - see
`operations.md`), `awaiting-join` (waiting on parallel work) or
`awaiting-callback` (waiting on an external event). A suspended run holds no
compute; it can sit for days, bounded by its `expires_at`, after which a sweep
terminates it as `incomplete (deadline_exceeded)`.

### Waiting for one

Poll from the management side - it is one command and avoids the header trap:

```bash
bud wait resource /runs/resp_... --field state.status \
  --equals completed,incomplete,failed,cancelled \
  --fail-on failed --timeout 1800 --interval 10
```

Pick a timeout that matches the work: a few minutes for a summarisation, 30+
minutes for anything that browses or calls tools in a loop, longer still if a
human gate is in the path. If the wait returns `suspended` or
`awaiting_approval`, that is not a failure - go to `operations.md`.

Then read the result from the gateway:

```bash
curl -sS "$BUD_GATEWAY/v1/responses/resp_..." \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'x-model-name: hr-assistant'
```

### The `x-model-name` header

> Mandatory on **every** gateway sub-resource call: `GET /v1/responses/{id}`,
> `DELETE`, `/cancel`, `/input_items`, and the SSE resume. These requests have
> no body, so the gateway resolves your key's permissions from this header
> alone. Omit it and it falls back to a literal placeholder model name and
> answers `404 Model not found: gpt-4-responses` - which reads like the response
> is missing when it is not. The value is the **agent name** (or deployment name
> for a raw model run).

Ownership is scoped to the key that created the response; someone else's id is a
404, not a 403.

### Cancelling

```bash
curl -sS -X POST "$BUD_GATEWAY/v1/responses/resp_.../cancel" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'x-model-name: hr-assistant'
```

Idempotent; 409 if the run was not a background run. From the management side,
`POST /runs/{session_id}/abort` does the same thing with a choice of graceful or
immediate (`operations.md`).

### Other sub-resources

```bash
# what was fed in, cursor-paginated
curl -sS "$BUD_GATEWAY/v1/responses/resp_.../input_items" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'x-model-name: hr-assistant'

# hard-delete a stored response
curl -sS -X DELETE "$BUD_GATEWAY/v1/responses/resp_..." \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'x-model-name: hr-assistant'
```

`POST /v1/responses/{id}/input_tokens` is documented in places but is not
routed through the gateway and its implementation returns 0. Do not use it -
take token counts from `usage` on the envelope.

### Things that make a background run fail before it starts

- `background: true` with `store: false` - hard 400 (`background_requires_store`).
- The deployment's execution config has `background_enabled: false` - see below.
- Sustained load: the platform sheds *new* background runs with `503` and a
  `Retry-After` once its backlog is full. Resumes of in-flight runs are never
  shed. Honour the header; do not retry in a tight loop.
- On installations where the durable engine is switched off, `background: true`
  with a `prompt.id` **silently runs synchronously** instead of returning
  `queued`. If you get a completed envelope back immediately when you asked for
  a background run, that is what happened - the answer is still valid, but there
  is no run to steer or approve.

## Per-deployment execution switches

The deployment behind a version gates what execution modes exist:

```bash
bud api GET /endpoints/<endpoint-uuid>/responses-config | jq '.responses_config'
```

| Key | Effect |
|---|---|
| `enabled` | `false` removes `/v1/responses` from the deployment entirely |
| `background_enabled` | `false` makes every `background: true` run fail |
| `store_default` | the default for `store` when the caller omits it |
| `limits` | e.g. `max_tool_calls`, `chain_depth_limit`, `background_wall_clock_seconds`, `max_output_tokens` |
| `defaults` | `reasoning_effort`, `truncation`, `instructions` |
| `server_tools` | which gateway-side tools this deployment may use |

`limits.background_wall_clock_seconds` is the real ceiling on a durable run
(commonly 3600). Set your `bud wait` timeout below it, not above.

Updating is a **whole-object PUT** - read it, modify the object, put it all
back. There is no partial merge.

```bash
bud api GET /endpoints/<id>/responses-config | jq '{responses_config}' > rc.json
# edit rc.json
bud api PUT /endpoints/<id>/responses-config -d @rc.json
```

Check this first when background runs fail on one deployment but work on
another.

## Invoking an agent over A2A

Agents are also reachable as A2A (Agent2Agent) JSON-RPC services, which is how
other agents and external clients call them.

```bash
curl -sS "$BUD_GATEWAY/a2a/hr-assistant/v0/" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send",
       "params":{"message":{"role":"user","parts":[{"kind":"text","text":"leave policy?"}]}}}'
```

`v0` in the path means "the default version"; `v2` pins version 2. A missing key
map entry is `404 Agent not found`. An unsupported protocol version comes back
as JSON-RPC error `-32008`. The card at
`/a2a/{name}/{version}/.well-known/agent-card.json` needs no authentication.

Confirm the method names an agent actually supports by reading its card
(`bud api GET /prompts/agent-card/<name>`) rather than assuming.
