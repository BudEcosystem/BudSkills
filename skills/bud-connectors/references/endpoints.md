# Endpoint reference: connectors and tools

Every endpoint in this domain, with the response shape you actually have to read.
Responses either **nest** the entity under its own key (`.gateway`, `.connector`,
`.job`, `.tool`) or are **flat** (fields at the top level) - the tables say which,
because guessing wrong yields `null` rather than an error.

Permissions: writes need the endpoint-manage scope, most reads need
endpoint-view. Exceptions are called out.

## Registry (what you *can* configure)

| Method | Path | Key params | Response |
|---|---|---|---|
| GET | `/connectors/registry` | `name` (substring), `page`, `limit` (max 100) | flat list under `.connectors`, plus `total_record` |
| GET | `/connectors/registry/{connector_id}` | registry slug | nested under `.connector` |
| GET | `/prompts/connectors` | `prompt_id`, `is_registered`, `version`, `search`, paging | `.connectors` - registry as seen from one agent |
| GET | `/prompts/connectors/{connector_id}` | registry slug | nested under `.connector` |

`/prompts/connectors?is_registered=true` only reports connections added by the
legacy per-agent register route. Connections attached via `attach-tools` are keyed
by slug and generally do **not** appear there - do not use it to answer "what
does this agent have".

Row fields: `id`, `name`, `icon`, `category`, `url`, `provider`, `description`,
`documentation_url`, `auth_type`, `transport`, `oauth_config?`, `env_schema?`,
`credential_schema`.

## Creating connections

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/connectors/gateways` | `{connector_id, name?, credentials, scope?}` | 201, nested `.gateway`, id at `.gateway.id` |
| POST | `/connectors/custom` | `{name, url, description?, transport?, auth_type, credentials, scope?}` | 201, nested `.gateway` |
| GET | `/connectors/custom/credential-schema` | - | `.schema` keyed by `OAuth`/`Headers`/`Open` |

`credentials` must match `auth_type` or the call is a 400/422. Custom connections
accept `SSE` or `STREAMABLEHTTP` only (auto-detected from the URL if omitted) -
no STDIO. `scope` shape:

```json
{"enabled": true, "global": false, "projects": ["<project-uuid>"]}
```

Omitting `scope` entirely defaults to **global**.

## Listing connections

| Method | Path | Use it for | Notes |
|---|---|---|---|
| GET | `/connectors/available` | **the agent-builder view** | pass `client=studio` **and `project_id`**; no permission gate; `oauth_connected` = anyone |
| GET | `/connectors/configured` | the administrative view | `client`, `include_disabled`, `limit` (max 500); `oauth_connected` = you |
| GET | `/connectors/gateways` | raw, unfiltered | includes per-agent and pending connections; prefer the two above |
| GET | `/connectors/gateways/{gateway_id}` | detail **including `tools[]`** | cheapest way to get `slug` and tool ids in one call |

Row fields on `/available` and `/configured`: `id`, `gateway_id` (same value),
`connector_id`, `name`, `enabled`, `tags`, `icon`, `description`, `category`,
`auth_type`, `tool_count`, `oauth_connected`, `is_custom`, `is_generated`.

Two scale limits worth knowing: the listings pull at most 500 connections and
filter in memory, so a very large installation can silently truncate; and the
per-connection enrichment fan-out is capped internally. **Do not build your own
wide parallel loop over `/connectors/gateways/{id}`** - an unbounded fan-out has
previously overwhelmed the connection service. Use the list endpoints, which are
already batched.

## Modifying connections

| Method | Path | Body | Effect |
|---|---|---|---|
| PATCH | `/connectors/configured/{gateway_id}` | `{clients?: ["studio"], agents?: {enabled, global, projects}}` | rewrites the visibility tags; an omitted block is left alone |
| PATCH | `/connectors/configured/{gateway_id}/toggle` | `{enabled: bool}` | the **only** way to switch a connection on/off |
| POST | `/connectors/configured/{gateway_id}/tag` | `{connector_id}` | backfill identifying tags on a pre-tagging connection |
| PUT | `/connectors/gateways/{gateway_id}` | free-form, forwarded verbatim | name/description/url/tags; `enabled` is silently ignored here |
| DELETE | `/connectors/gateways/{gateway_id}` | - | asynchronous detach-from-all + delete |

> Narrowing `agents` scope (to `enabled: false` or a smaller project set) and
> `toggle {enabled:false}` both start a background job that **detaches the
> connection from every out-of-scope agent**. There is no notification and no
> status endpoint. Widening or re-enabling never re-attaches. Confirm before
> either.

> `PUT` on a generated connection must not include `transport` - it reverts the
> connection's kind and breaks it.

`DELETE` returns `200` with a `workflow` object *before* any work happens.
Success or failure is reported only as an in-app notification to you; verify by
polling `/connectors/configured?include_disabled=true` until the id disappears.
Seconds to about a minute, scaling with the number of attached agents.

## OAuth

| Method | Path | Response | Notes |
|---|---|---|---|
| GET | `/connectors/{gw}/oauth/status` | flat | OAuth **configuration**, not connectivity |
| GET | `/connectors/{gw}/oauth/token-status` | flat | `connected` + `is_expired` = the real check |
| POST | `/connectors/{gw}/oauth/initiate` | flat: `authorization_url`, `state`, `expires_in` | optional `return_url` query, allow-listed |
| GET | `/connectors/oauth/public-callback` | flat | unauthenticated; the provider's redirect target |
| POST | `/connectors/oauth/callback` | flat | authenticated variant; its auto tool-fetch succeeds |
| DELETE | `/connectors/{gw}/oauth/token` | flat | revoke your own token |
| DELETE | `/connectors/{gw}/oauth/token/{user_email}` | flat | revoke another user's - **not permission gated** |

Full flow and failure table: `oauth.md`.

## Tools on a connection

| Method | Path | Notes |
|---|---|---|
| POST | `/connectors/{gw}/fetch-tools` | force discovery; runs with your session |
| GET | `/connectors/{gw}/tools` | `page`, `limit` (max 500); self-heals empty OAuth connections |
| GET | `/prompts/tools/{tool_id}` | one tool with its full input schema, nested `.tool` |

Tool rows carry `id` (the uuid you attach), `originalName` (the callable name),
`displayName`, `description`, `enabled`, `inputSchema`.

`GET /connectors/{gw}/tools` returns **403, not 404**, when the connection is
disabled or not exposed to the console client.

## Binding tools to an agent

| Method | Path | Body / params | Notes |
|---|---|---|---|
| POST | `/prompts/{agent_id}/connections/{gateway_id}/attach-tools` | `{tool_ids: [uuid], version, permanent}` | **the modern path**; replaces the set |
| DELETE | `/prompts/{agent_id}/connections/{gateway_id}/detach` | `version`, `permanent` query | unbind, keep the connection |
| POST | `/prompts/{agent_id}/connectors/{connector_id}/register` | `{credentials, version, permanent}` | legacy: creates a dedicated connection |
| DELETE | `/prompts/{agent_id}/connectors/{connector_id}/disconnect` | `version`, `permanent` query | **deletes the connection too** |
| POST | `/prompts/prompt-config/add-tool` | `{prompt_id, connector_id, tool_ids, version, permanent}` | low-level equivalent of attach-tools |
| GET | `/prompts/tools` | `prompt_id`, `connector_id`, `version`, paging, `search` | rows carry `is_added` |
| POST | `/prompts/prompt-cleanup` | `{prompts: [...], debug}` | tear down draft/temporary agent tool wiring |

Rules that apply to all of them:

- `permanent` defaults to **false**, which writes the agent's tool wiring with a
  time-to-live. Always pass `true` for a saved agent.
- `tool_ids` **replaces** the set for that connection; `[]` removes all.
- `version` selects which agent version you are editing.
- `connector_id` on the agent-side routes is the **agent-config key**: the
  connection slug for attached connections, the registry slug for registered
  ones. Get the slug from `.gateway.slug`.
- `GET /prompts/tools` returns an empty list (not 404) for a missing config or an
  unknown key - an empty result is ambiguous.
- `add-tool` resolves every tool id across every connection one at a time, so it
  is slow for large sets and a single stale id fails the whole call with a 404.

`attach-tools` returns `virtual_server_id` and `virtual_server_name`
(`{agent_id}__v{version}`) - the tool server the agent uses at run time.

## Tool generation

| Method | Path | Notes |
|---|---|---|
| GET | `/connectors/capabilities` | always 200, even when the feature is off - probe this |
| POST | `/connectors/generate/url` | 202, flat `{job_id, workflow_id, ...}` |
| POST | `/connectors/generate/upload` | multipart; `auth`/`scope` as JSON **strings** |
| GET | `/connectors/generate/jobs/{job_id}` | nested `.job`; destructive to poll in legacy mode |
| POST | `/connectors/generate/jobs/{job_id}/cancel` | nested `.job` |
| POST | `/connectors/generate/jobs/{job_id}/finalize` | flat; **this is what creates the connection** |

Every `/connectors/generate/*` route returns 404 when the feature is disabled.
Details: `tool-generation.md`.

## Built-in tools

| Method | Path | Notes |
|---|---|---|
| GET | `/prompts/{agent_id}/native-tools` | `version`, `status` = `all|connected|not_connected`, paging, `search` |
| GET | `/prompts/{agent_id}/native-tools/{tool_name}` | nested `.tool`, includes `config_schema` and `connected_config` |
| POST | `/prompts/{agent_id}/native-tools` | `{version, tool_name, config, permanent}` - upsert |
| DELETE | `/prompts/{agent_id}/native-tools/{tool_name}` | `version`, `permanent` query; 404 if not connected |

Exposed names: `web_search`, `web_fetch`, `await_completion`. A code interpreter
exists in the catalog but is disabled, so `tool_name: "code_interpreter"` is a
400 - read the list route rather than assuming.

Known configuration keys:

| Tool | Config |
|---|---|
| `web_search` | `max_results` |
| `web_fetch` | `max_content_length`, `allow_local_urls`, `timeout`, `allowed_domains`, `blocked_domains`, `headers` |
| `await_completion` | `max_wait_seconds` |

Read `config_schema` from the detail route for types, defaults and requiredness -
it is authoritative.

### Code-interpreter sandbox templates

Live even though the tool is not exposed. Project scope comes from the API key,
or an `X-Project-Id` header for session callers.

| Method | Path | Notes |
|---|---|---|
| POST | `/prompts/code-interpreter/templates` | `{name, commands, cpu_count, memory_mb}` → 202, build is async (minutes) |
| GET | `/prompts/code-interpreter/templates/{id}` | nested `.template`, carries build status |
| PUT | `/prompts/code-interpreter/templates/{id}` | `{commands}`, re-enqueues the build; scope required |
| DELETE | `/prompts/code-interpreter/templates/{id}` | scope required; 409 when in use |

## Visibility is encoded as tags

There is no separate access-control table - a connection's tags **are** the
model. Read them from any listing row.

| Tag | Meaning |
|---|---|
| `connector-id:{slug}` | which registry connector this came from |
| `source:*` | how it was created - the literals are `source:budapp` (from the catalog), `source:custom` (a URL you supplied), `source:generated` (the tool generator) |
| `client:studio` | exposed to the console; **required** or tool listing 403s |
| `scope:global` | visible to agents in every project |
| `project:{uuid}` | visible to agents in that project (lowercased uuid) |
| `oauth:shared-token` | the connection holds an OAuth token |
| `gen-batch:{id}`, `gen-status:pending` | generated-connection bookkeeping; pending ones are hidden |

A connection with **no** `scope:*` and no `project:*` tag is invisible to
`/connectors/available` entirely, even though it appears in `/connectors/configured`
and its tools list fine. On the installation I checked, 15 configured connections
yielded only 10 available to a project - the difference was untagged ones. That is
the single most common "the connection exists but my agent can't see it".

## Not connectors: event connections

`/events/*` is the **trigger** surface - inbound events from external systems, not
tools. Listed here only so you recognise the name collision. Owned by
`bud-routines`.

| Method | Path | Notes |
|---|---|---|
| GET | `/events/descriptors` | provider catalog with per-provider credential field spec; optional `provider` filter |
| POST | `/events/shared-connections` | `{scope, project_ids, provider, credentials, name?, semantics?}` |
| GET | `/events/shared-connections` | optional `project_id`; never returns credentials |
| PATCH | `/events/shared-connections/{id}` | name / project set / scope; credentials immutable |
| DELETE | `/events/shared-connections/{id}` | cascades subscriptions; 409 if a multi-source trigger depends on it |
| POST | `/events/shared-connections/{connector_id}/bda-subscription` | uses the `connector_id` from create, not the row `id` |
| DELETE | `/events/subscriptions/{subscription_id}` | idempotent |

Sixty providers are catalogued on the installation I checked, covering 568 event
types (github, gitlab, jira, confluence, slack, teams, asana, clickup, datadog,
grafana, alertmanager, argocd, calendly, docusign, dropbox, figma, greenhouse,
hubspot, bamboohr, workvivo and more).

**Credential shapes are not uniform.** 57 of the 60 declare a single
`signing_secret`, but three do not - including the two people ask for most:

| Provider | Required credential keys |
|---|---|
| `slack` | `bot_token`, `signing_secret` |
| `teams` | `app_id`, `client_secret`, `tenant_id` |
| `workvivo` | `api_base_url`, `api_token`, `workvivo_id` |

Never assume the shape. Read `/events/descriptors?provider=<name>` and send
exactly the keys it lists under `providers[].credentials`.

Creating one returns `{id, connector_id, webhook_url, provider}` under
`.connection`; the `webhook_url` is what you register with the provider.
`scope: "global"` requires administrator rights and must omit `project_ids`;
`scope: "project"` requires a non-empty `project_ids` and membership of each.

These connections have **no tools** and cannot be attached to an agent.

## Seeing whether tools are used

```bash
bud api POST /metrics/observability/tools/analytics -d '{"from_date":"2026-08-01","to_date":"2026-08-06"}'
```

Per-tool call counts, error rate and latency percentiles - the only way to
confirm that tools you attached are actually being called. Owned by
`bud-observability`.
