---
name: bud-connectors
description: Connect Bud Foundry to external systems and give agents tools - browse the connector registry (GitHub, Slack, Linear, Notion, Stripe and ~90 more), configure a connection with API keys or OAuth, discover the tools it offers, generate tools from an OpenAPI spec or API docs, and attach them to an agent. Use when a Bud request mentions connectors, connections, integrations, MCP servers, tool calling, "let my agent read/post to X", third-party credentials or OAuth authorization, or built-in tools like web search and web fetch.
---

# Bud Foundry: connectors and tools

An agent is only as useful as what it can reach. This domain is how you give one
hands: a **connection** to an external system, the **tools** that connection
exposes, and the call that binds chosen tools to a specific agent.

Prerequisite: connect first - see `bud-platform` (`bud login`).

## Three kinds of tool - pick before you start

| You need... | Use | Credentials? |
|---|---|---|
| A known SaaS product (Slack, GitHub, Linear, Stripe, Notion...) | the **connector registry** | yes - API key or OAuth |
| An internal or self-hosted service that already speaks MCP | a **custom connection** to its URL | maybe |
| An internal REST API with an OpenAPI spec (or just docs) | the **tool generator** | optional, applied to every tool |
| Web search, page fetch, long-running-task waiting | **built-in tools** | none |

The first three all end up as a *connection*, bound to an agent the same way;
built-in tools skip connections entirely. **A connection's handle is its
`gateway_id`** - that one uuid drives OAuth, tool listing, scoping,
attach/detach, enable/disable and deletion. Grab it early and keep it.

## The critical path

Concrete goal: *an HR agent that looks up employee data and posts to a team chat.*

```bash
export BUD_PROJECT_ID=<the agent's project>          # see bud-projects
export AGENT_ID=<the agent's prompt id>              # see bud-agents

# 1. What can this agent already reach?
bud api GET /connectors/available -q client=studio -q project_id=$BUD_PROJECT_ID -q limit=100 \
  | jq -r '.connectors[] | "\(.gateway_id)  \(.name)  tools=\(.tool_count) oauth=\(.oauth_connected)"'
```

If the system you want is already listed, authorized (`oauth_connected: true`)
and has tools, jump straight to **Attach**. Otherwise configure it first.

> **Always pass `project_id`.** Without it the listing silently drops every
> project-scoped connection and returns only global ones - the filter is
> fail-closed. On the installation I checked the same query returned 7
> connections without it and 10 with it; a "missing" connection is almost always
> this, not a real absence.

```bash
# 2. Find the connector in the registry
bud api GET /connectors/registry -q name=slack -q limit=100 \
  | jq -r '.connectors[] | "\(.id)  \(.auth_type)  \(.name)"'
# -> slack  OAuth2.1  Slack

# 3. Read its exact credential fields BEFORE composing anything
bud api GET /connectors/registry/slack | jq '.connector | {auth_type, transport, credential_schema}'

# 4. Configure the connection
bud api POST /connectors/gateways -d '{
  "connector_id": "slack",
  "name": "Slack (HR)",
  "credentials": { "...": "shape depends on auth_type - see below" },
  "scope": {"enabled": true, "global": false, "projects": ["'"$BUD_PROJECT_ID"'"]}
}' | jq -r '.gateway.id'
export GW=<that id>

# 5. Authorize if OAuth (see the next section), then discover tools
bud api GET /connectors/$GW/tools -q limit=500 \
  | jq -r '.tools[] | "\(.id)  \(.originalName)  \(.description[0:60])"'

# 6. Bind the chosen tools to the agent
bud api POST /prompts/$AGENT_ID/connections/$GW/attach-tools -d '{
  "tool_ids": ["<uuid>", "<uuid>"], "version": 1, "permanent": true
}'
```

Registry ids are lowercase slugs (`slack`, `github`, `linear`, `notion`,
`hubspot`, `atlassian`, `stripe`...); the installation I checked carries **90
connectors** across ~40 categories. Search it rather than assuming - there is no
Microsoft Teams connector, so "post to Teams" needs a custom connection or
generated tools, while Slack works off the shelf.

## Credentials by auth type

The registry's `auth_type` decides the shape of `credentials`, and a mismatch is
a 400 with an explicit message. Read `credential_schema` from
`GET /connectors/registry/{connector_id}` - it names every field, marks what is
required, and pre-fills provider defaults.

| `auth_type` | Send |
|---|---|
| `Open` | `{}` (optionally `{"passthrough_headers": "..."}`) |
| `API Key` / `API` / `Headers` | `{"auth_headers": [{"key": "Authorization", "value": "Bearer ..."}]}` |
| `OAuth` / `OAuth2.1` | `{"grant_type": "...", "client_id": "...", "client_secret": "...", "token_url": "...", "authorization_url": "...", "redirect_uri": "...", "scopes": "..."}` |
| `OAuth2.1 & API Key` | either of the above two, whichever the user has |

`token_url`, `authorization_url` and `scopes` arrive pre-filled as `default`
values in the schema - keep them unless the user has a private tenant. Some
connectors advertise `supports_dcr: true` in `oauth_config` (Stripe does), which
makes `client_id`/`client_secret` optional. Credentials are encrypted on arrival
and Bud stores none of them: you can replace one but never read it back. One
registry entry (`serper-search`) runs over STDIO and cannot become a shared
connection at all - see `references/registry.md`.

## OAuth: what can and cannot be automated

This is the one place a human is genuinely required, so be precise about it.

| Situation | Automatable? |
|---|---|
| `Open`, `API Key`, `Headers` | **Fully.** Configure, fetch tools, attach. No human. |
| OAuth with `grant_type: client_credentials` | **Fully.** Bud gets the token itself. |
| OAuth with `grant_type: authorization_code` | **No.** A person must approve in a browser. |

Most registry OAuth connectors (Slack, GitHub, Linear, Notion, Asana, Atlassian)
are `authorization_code`. Check before promising anything:

```bash
bud api GET /connectors/$GW/oauth/token-status | jq '{connected, is_expired, expires_at}'
```

`connected: true` **and** `is_expired: false` means authorized. Do not use
`GET /connectors/$GW/oauth/status` for this - that reports whether OAuth is
*configured*, and stays `true` forever once it is.

When a human step is needed, do this and then say exactly this:

```bash
bud api POST /connectors/$GW/oauth/initiate | jq -r '.authorization_url, .expires_in'
```

> "This connection uses OAuth, so I can't finish it for you. Open this link,
> sign in to <provider> and approve access - the link expires shortly
> (`expires_in` is in seconds, usually a few minutes): <authorization_url>.
> Tell me when you're done and I'll pick up from there."

Then wait for them to finish, and follow with tool discovery:

```bash
bud wait resource /connectors/$GW/oauth/token-status \
  --field connected --equals true --interval 5 --timeout 900

bud api POST /connectors/$GW/fetch-tools
bud api GET  /connectors/$GW/tools -q limit=500
```

> The redirect that completes the flow runs unauthenticated, so its automatic
> tool fetch usually fails silently - a freshly authorized connection commonly
> shows `tool_count: 0` until you call `fetch-tools`. `GET .../tools` self-heals
> for the same reason: it retries discovery with your session.

Tokens are **per user**. `oauth_connected` on `/connectors/configured` means
*you* are authorized; on `/connectors/available` it means *anyone* is. An agent
runs on the shared connection token, so one authorized user is enough - but if
that person revokes, the agent's tools stop working.

Details, the authenticated-callback shortcut and revocation: `references/oauth.md`.

## Attaching tools to an agent

```bash
bud api POST /prompts/$AGENT_ID/connections/$GW/attach-tools -d '{
  "tool_ids": ["6488d3a651f54f6fa2405095fde5cf5e"], "version": 1, "permanent": true
}'
```

Four things will bite you here:

- **`permanent: true` is not the default.** Omit it and the agent's tool wiring
  is written with a time-to-live and silently disappears later.
- **`tool_ids` replaces, it does not append.** To add one tool, send the full
  desired list. `[]` removes every tool from that connection.
- **`version` must match the agent version you are editing.** Attaching to
  version 1 does nothing for version 2.
- **Attach only what the agent needs.** A large connection can carry 40-60 tools
  (Linear exposes 57, GitHub 44); all of them bloats the agent's context and its
  choices. Pick the five it actually uses.

Verify with the agent-side view, which keys on the connection's **slug**, not the
registry id. To unbind, use **detach**:

```bash
SLUG=$(bud api GET /connectors/gateways/$GW | jq -r '.gateway.slug')
bud api GET /prompts/tools -q prompt_id=$AGENT_ID -q connector_id=$SLUG -q version=1 \
  | jq -r '.tools[] | "\(.is_added)  \(.name)"'

bud api DELETE /prompts/$AGENT_ID/connections/$GW/detach -q version=1 -q permanent=true
```

An empty verify result is ambiguous - "no tools" *or* "wrong key", never 404. If
it looks empty, re-check the slug before concluding anything.

> Never use `DELETE /prompts/{agent}/connectors/{connector_id}/disconnect` to
> unbind a shared connection. It deletes the underlying connection for **every**
> agent that uses it, not just this one.

What the agent then does with those tools - allowing multiple calls, tool choice,
testing a run - is `bud-agents`.

## No registry entry? Two ways forward

**A. It already speaks MCP** (an internal server, a vendor's hosted endpoint):

```bash
bud api GET /connectors/custom/credential-schema | jq '.schema | keys'   # OAuth|Headers|Open
bud api POST /connectors/custom -d '{
  "name": "Internal HRIS", "url": "https://mcp.internal.example.com/mcp",
  "auth_type": "Headers",
  "credentials": {"auth_headers": [{"key": "X-API-Key", "value": "..."}]},
  "scope": {"enabled": true, "global": false, "projects": ["'"$BUD_PROJECT_ID"'"]}
}'
```

Transport is auto-detected from the URL; only `SSE` and `STREAMABLEHTTP` are
accepted here.

**B. It is a plain REST API** - point the generator at its OpenAPI spec:

```bash
bud api GET /connectors/capabilities     # ALWAYS read this first
bud api POST /connectors/generate/url -d '{
  "name": "HRIS API", "url": "https://hris.example.com/openapi.json",
  "source_type": "openapi_url",
  "auth": {"auth_type": "bearer", "token": "..."}
}' | jq -r '.job_id'
# poll until job.status is completed (10s-3min), then choose from job.result.tools
bud api GET  /connectors/generate/jobs/<job-id> | jq '.job | {status, progress, result}'
bud api POST /connectors/generate/jobs/<job-id>/finalize -d '{"candidate_keys": ["c0000","c0003"]}'
```

The generator is a feature gate, and when it is off every
`/connectors/generate/*` route returns **404, not 403** - so probe capabilities,
never a route. Capabilities also tells you whether documentation scraping is
available (it needs a configured language model and is often off), the
per-source tool cap, and `preview_mode`, which changes what the candidate ids
mean.

Generation produces candidates you must **confirm** - nothing exists until
finalize, which is what creates the connection. From there it behaves like any
other connection. `auth` accepts only `basic`, `bearer` or `apikey`; OAuth is
rejected rather than silently producing unauthenticated tools.

Full flow, both modes, and every error code: `references/tool-generation.md`.

## Built-in tools (no external system)

Web search, page fetch and long-task waiting need no connection and no
credentials:

```bash
bud api GET  /prompts/$AGENT_ID/native-tools -q version=1 -q status=all
bud api GET  /prompts/$AGENT_ID/native-tools/web_search -q version=1 | jq '.tool.config_schema'
bud api POST /prompts/$AGENT_ID/native-tools -d '{
  "version": 1, "tool_name": "web_search", "config": {"max_results": 5}, "permanent": true
}'
bud api DELETE /prompts/$AGENT_ID/native-tools/web_search -q version=1 -q permanent=true
```

Available names are `web_search`, `web_fetch` and `await_completion`; same
`permanent: true` rule as attach-tools. A code interpreter exists in the platform
with its own sandbox-template APIs, but it is **not currently exposed as a
built-in tool** - `tool_name: "code_interpreter"` returns 400. Read the catalog
from the list route rather than assuming. `web_fetch` is worth configuring when
an agent browses on a user's behalf: `allowed_domains`, `blocked_domains`,
`max_content_length`, `timeout`, `allow_local_urls`.

## Sharing, scoping and switching off

Connections are shared infrastructure. Who can see one is set by its scope:

```bash
# restrict to specific projects (or set global:true for everyone)
bud api PATCH /connectors/configured/$GW -d '{"agents": {"enabled": true, "global": false, "projects": ["<uuid>"]}}'

bud api PATCH /connectors/configured/$GW/toggle -d '{"enabled": false}'   # off
```

> **Narrowing scope, and disabling, both detach the connection from every agent
> that is now out of scope** - in the background, with no confirmation and no
> notification. Widening or re-enabling does **not** re-attach anything; agents
> must be re-bound by hand. Treat both as destructive and confirm first.

Omitting `scope` at create time makes a connection **global** - visible to every
project's agent builder. Scope deliberately. The on/off flag lives only on
`/toggle`; sending `enabled` to `PUT /connectors/gateways/{id}` is ignored.

## Removing a connection

```bash
bud api DELETE /connectors/gateways/$GW
```

Confirm with the user by name first. This detaches from every agent, then
deletes - and the `200` means *scheduled*, not *done*. There is no status
endpoint, so verify by disappearance: re-list
`GET /connectors/configured -q include_disabled=true -q limit=500` every few
seconds until `$GW` is absent (seconds to about a minute, longer with many agents
attached). Still listed after a few minutes means the detach failed and the
connection is intact - retry the DELETE.

Prefer `toggle {enabled:false}` when the user might want it back. Deletion is not
reversible and takes every agent's binding with it.

## Not this skill: event connections

`/events/shared-connections` are **not** connectors. They are inbound provider
connections (GitHub, Jira, Slack, Teams, Datadog and ~55 others) that deliver *events* into Bud
so triggers can fire - no tools, cannot be attached to an agent, sharing only the
word "connection". "When someone opens a GitHub issue, run my agent" is
`bud-routines`. Summarised in `references/endpoints.md` for recognition only.

## Deeper reference

- `references/registry.md` - the live catalog by category, credential shapes per
  auth type, STDIO connectors and the legacy per-agent register route
- `references/oauth.md` - the full flow, both callbacks, token vs configuration
  status, per-user tokens, revocation, failure table
- `references/tool-generation.md` - generation end to end: capabilities gate,
  preview vs legacy candidate ids, finalize error codes
- `references/endpoints.md` - every endpoint with params and response shape, the
  tag vocabulary that encodes visibility, and event connections

## Where to go next

`bud-agents` to build the agent that uses these tools and test it end to end.
`bud-routines` to trigger agents from external events. `bud-observability` to
check the tools you attached are actually being called
(`POST /metrics/observability/tools/analytics`).
