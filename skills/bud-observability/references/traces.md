# Traces and spans

A **trace** is one end-to-end unit of work: an inbound agent event, an agent
run, or a single model call. It contains **spans** - one per operation inside it.
Every span carries the Bud attribution attributes (`bud.project_id`,
`bud.endpoint_id`, `bud.model_id`, `bud.prompt_id`, `bud.api_key_project_id`,
`bud.user_id`), which is exactly what the resource filters below match on.

## Listing traces

`GET /metrics/observability/traces`

| Param | Required | Notes |
|---|---|---|
| `resource_type` | yes | `all`, `endpoint`, `prompt`, `project`, `model` |
| `resource_id` | yes except `all` | UUID of that resource; 400 without it |
| `from_date`, `to_date` | yes | ISO-8601; **any offset is ignored here** and the wall-clock digits are read as UTC - always send `Z` |
| `page` | no | 1-based, default 1 |
| `limit` | no | 1..1000, default 50; 422 above 1000 |
| `flatten` | no | default false |

Response is flat (not nested under an entity key):

```json
{"object":"trace_list","message":"…","page":1,"limit":50,
 "total_record":12,"total_pages":1,"items":[ TraceItem, … ]}
```

`TraceItem` = `timestamp`, `trace_id`, `span_id`, `parent_span_id`,
`trace_state`, `span_name`, `span_kind`, `service_name`, `resource_attributes`,
`scope_name`, `scope_version`, `span_attributes`, `duration` (**nanoseconds**),
`status_code` (`Ok` / `Error` / `Unset`), `status_message`, `events[]`,
`links[]`, `child_span_count`.

In the default mode each item is the **root span of one trace** and
`child_span_count` is its direct children. `bud api … --paginate` works here and
returns one flat array.

### resource_type semantics

- `prompt` - agent runs (the resource id is the prompt UUID).
- `endpoint` - direct traffic to a deployment, **excluding** anything an agent
  originated. This is intentional and is the usual reason an endpoint looks
  quiet.
- `model`, `project` - no such exclusion; everything attributed to that id.
- `all` - the whole installation for the window.

### flatten=true

Returns **every span** of every matching trace as its own row, sorted by time.
Consequences: `limit`/`total_record` now count spans (a 12-trace day was 5459
spans on a live installation), `child_span_count` is forced to 0, and the query
bypasses the pre-computed trace index - fresher, slower. Useful for two things:
sweeping a whole window for `status_code == "Error"` spans in one pass, and
seeing runs that have not yet been indexed.

### Visibility lag

A trace enters the default listing only when its **true root span** has been
exported and the index rebuilt - roughly one to two minutes after the run ends.
Long agent runs export their root last, so an in-flight run is deliberately
absent. If you know the `trace_id` you can always fetch the detail directly; it
does not depend on the index.

## Fetching one trace

`GET /metrics/observability/traces/{trace_id}`

```json
{"object":"trace_detail","trace_id":"…","total_spans":130,"spans":[ TraceItem, … ]}
```

Spans are ordered by timestamp ascending; rebuild the hierarchy from
`parent_span_id` (the root has `""`). Here `child_span_count` counts **all**
descendants, not direct children.

Attribute values that look like JSON are decoded for you, so
`gen_ai.input.messages` is a real list and `gateway_analytics.request_headers`
is a real object. Streamed response bodies stored as attributes are not valid
JSON and stay strings.

## Span taxonomy

Root span names tell you what kind of trace you are looking at:

| Root `span_name` | Meaning |
|---|---|
| `POST /v1/chat/completions`, `POST /v1/embeddings`, `GET /v1/models`, … | a direct call to a deployment through the gateway |
| `POST /events/{provider}` | an inbound agent event (a chat message, a webhook, a trigger firing) |
| `Bud Studio agent invoke`, `bda.run` | an agent run started inside the platform |

Inside an agent trace, the spans worth reading are identified by
`gen_ai.operation.name`:

**`chat` - one model call**

| Attribute | Content |
|---|---|
| `gen_ai.input.messages` | list of `{role, parts:[{type, content}]}` as sent |
| `gen_ai.output.messages` | the assistant reply, including `thinking` parts |
| `gen_ai.request.model`, `gen_ai.response.model` | model names |
| `gen_ai.request.temperature`, `model_request_parameters` | sampling settings |
| `gen_ai.response.finish_reasons` | e.g. `["tool_call"]`, `["stop"]` |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.details.reasoning_tokens` | usage |
| `gen_ai.tool.definitions` | the tool schema the model was given |

Every model call is recorded **twice**, by two sibling spans: the agent-side
`chat <model>` span above, and a gateway-side span named `chat completion` that
has no `gen_ai.operation.name` and carries the settled record instead -
`model_inference.inference_id` (the join key to
`GET /metrics/inferences/{id}`), `model_inference.model_name`,
`.input_tokens`, `.output_tokens`, `.finish_reason`, and
`model_inference_details.cost` / `.inference_cost`. Read messages from the
first, tokens and cost from the second. Note attribute values are strings even
when they hold numbers - cast before arithmetic.

**`execute_tool` - one tool call or delegated sub-agent**

`gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`,
`gen_ai.tool.call.result`. A sub-agent delegation looks like a tool call whose
name is the sub-agent's name.

> Tool failures frequently arrive as a normal result string
> (`"Failed to execute prompt"`, `"Error: …"`) on a span with `status_code`
> `Unset`. Always check the result text, not just the status.

**`invoke_agent` - the agent turn** - `gen_ai.agent.name` (may be the agent's
name *or* its UUID), `gen_ai.conversation.id`, `gen_ai.agent.call.id`.

**Errors** - `status_code == "Error"` plus `status_message`, and the attributes
`error.type` and `error.message`.

**Gateway spans** additionally carry `gateway_analytics.*`: `client_ip`,
`device_type`, `browser_name`, `os_name`, `status_code`, `total_duration_ms`,
`gateway_processing_ms`, `is_blocked`, `is_bot`, `path`, `request_headers`,
`response_headers`, `inference_id`. This is where per-client and per-geography
analysis ultimately comes from.

## Prompt-scoped variants (ownership-checked)

```
GET /prompts/{agent-name}/traces?project_id=<uuid>&from_date=…&to_date=…&page=&limit=&flatten=
GET /prompts/{agent-name}/traces/{trace_id}?project_id=<uuid>
```

`{agent-name}` is the agent's **name**, not its UUID, and it is resolved within
that project - a 404 means "no active agent with that name in this project".
Same response shapes as above. Prefer these when you have a project context:
unlike the `/metrics/observability/traces` pair, they validate that the agent
belongs to the project.

## Attribute-filtered telemetry query

`POST /prompts/telemetry/query` is the most expressive trace API: depth-limited
tree traversal with attribute projection and filters. It authenticates with a
**project API key** (`Authorization: Bearer <api key>`), not a user session, and
derives the project from the key - never send `project_id`.

```json
{"prompt_id":"<agent NAME>","from_date":"…","to_date":"…",
 "depth":-1,"include_all_attributes":true,
 "span_filters":[{"field":"gen_ai.operation.name","op":"eq","value":"execute_tool"}],
 "order_by":[{"field":"timestamp","direction":"desc"}],
 "page":1,"limit":50}
```

- `depth`: `0` = roots only, `-1` = whole tree (capped at 10).
- `op`: `eq|neq|gt|gte|lt|lte|in_|not_in|like|is_null|is_not_null`.
- `order_by.field` must be a span column (`timestamp`, `duration`,
  `status_code`, `span_name`, …), not an attribute.
- Limits: 50 selected attributes, 20 span filters, 20 resource filters, 20 span
  names, depth 10, window <= 90 days, 10000 rows.
- Response: `{data:[…], page, limit, total_record, total_pages}` where each item
  nests its `children`. There is no `has_more`; compute it from `total_record`.

Because it needs a project API key rather than the signed-in session, reach for
it only when you are already operating with one; the two endpoints above cover
the same ground with the session.

## Retention

| Data | Kept |
|---|---|
| Raw spans (everything in this file) | ~30 days |
| Per-request and per-agent rows | ~90 days |
| Agent message/response bodies | 7 days (row survives, content nulls) |

Windows are dropped whole, so the boundary moves in steps rather than smoothly.
An empty result for an old window is far more likely to be expiry than absence.
