# Tools, sub-agents and composition

Three kinds of capability can be attached to an agent version:

| Kind | Source | Attached with |
|---|---|---|
| **Built-in tools** | shipped with the platform (web search, web fetch, waiting, sandboxed code) | `/prompts/{key}/native-tools` |
| **Connector tools** | an external system reached over MCP | `/prompts/{key}/connectors/**` + `/prompts/prompt-config/add-tool` |
| **Sub-agents** | another Bud agent, local or external | `/prompts/{key}/connect-agent` |

All of them target the **config key** (draft uuid before the agent is saved, the
agent **name** after) and a `version`, and apply to that version only. Attaching
tools is a config write, so everything in `agent-lifecycle.md` about partial
saves and `permanent` applies.

**Tool wiring does not carry over to a new version.** When you create version
N+1, re-attach everything on the new draft first.

## Built-in tools

```bash
KEY=hr-assistant

bud api GET /prompts/$KEY/native-tools -q version=1 \
  | jq -r '.tools[] | "\(.name)  connected=\(.connected)  \(.display_name)"'

bud api GET /prompts/$KEY/native-tools -q version=1 -q status=not_connected   # all|connected|not_connected

# read the tool's config schema before writing a config
bud api GET /prompts/$KEY/native-tools/web_search -q version=1 | jq '.tool.config_schema'

# connect (upsert - same call updates an existing config)
bud api POST /prompts/$KEY/native-tools -d '{
  "version": 1, "tool_name": "web_search", "config": {"max_results": 5}, "permanent": true
}'

bud api DELETE /prompts/$KEY/native-tools/web_search -q version=1 -q permanent=true
```

Known tool names are `web_search`, `web_fetch`, `await_completion` and
`code_interpreter`. **Verify against the live catalog before relying on any of
them** - the catalog is data, installations differ, and an unknown name is a
400. `code_interpreter` in particular is disabled in some builds; the list call
above is the authority.

Config shapes:

| Tool | Config |
|---|---|
| `web_search` | `{"max_results": int}` |
| `web_fetch` | `{"max_content_length": 50000, "allow_local_urls": false, "timeout": 30, "allowed_domains": [...], "blocked_domains": [...], "headers": {...}}` |
| `await_completion` | lets a run park and wait; used with durable runs |
| `code_interpreter` | see below |

`permanent: true` on a saved agent, `false` while iterating on an unsaved draft.
Use `false` on a draft and you keep the ability to keep saving it as a draft.

`code_interpreter` has a **side effect**: connecting it provisions a sandbox
environment before the config write. Its config is exactly one of a built-in
tier or a custom template:

```json
{"languages":["python"],"cpu":2,"ram_gb":4,
 "container_expiry_seconds":1200,
 "network_policy":{"type":"disabled"}}
```

```json
{"custom_template_id":"my-tpl"}
```

Never both. `languages` are `python` and `javascript`; `network_policy.type` is
`disabled`, `filtered` or `open` (with `allow_out` / `deny_out` lists when
filtered). Default to `disabled` unless the user has a stated need - an open
sandbox can reach the network from inside your installation.

### Custom sandbox images

```bash
bud api POST /prompts/code-interpreter/templates -d '{
  "name": "my-tpl", "commands": ["RUN pip install pandas"], "cpu_count": 2, "memory_mb": 4096
}'
# -> 202 {"workflow_id":"...","template_id":"my-tpl","status":"pending"}

bud wait resource /prompts/code-interpreter/templates/my-tpl \
  --field template.status --equals ready --fail-on failed --timeout 1800

bud api PUT    /prompts/code-interpreter/templates/my-tpl -d '{"commands":["RUN pip install pandas polars"]}'
bud api DELETE /prompts/code-interpreter/templates/my-tpl
```

Builds take minutes. Both request bodies reject unknown fields. Create, update
and delete need a project in scope (a 400 "Project not found" means the scope is
missing, not the template). Delete is idempotent but 409s while an agent still
uses the template.

## Connector tools (MCP)

A connector is an external system - a ticketing tool, a wiki, a database - that
exposes tools over MCP. `bud-connectors` owns configuring them; here we bind
them to an agent.

**Step 0 is mandatory and easy to forget:**

```bash
bud api POST /prompts/prompt-config -d '{
  "prompt_id": "'"$KEY"'", "version": 1,
  "enable_tools": true, "allow_multiple_calls": true
}'
```

Register without it and you get `400 Allow Multiple Calls must be enabled for
MCP tool usage`.

```bash
# 1. what can this agent use?
bud api GET /prompts/connectors -q prompt_id=$KEY -q version=1
bud api GET /prompts/connectors/<connector-id> | jq '{auth_type, transport, credential_schema}'
```

`auth_type` is `OAuth`, `Open` or `Headers`; `transport` is `SSE`,
`STREAMABLEHTTP` or `STDIO`. The credential payload must match the declared
class - OAuth credentials on a Headers connector is a
`400 Credential type mismatch`.

Then either create a private connection for this agent, or reuse a shared one.

**A. Private connection (own credentials, own gateway):**

```bash
bud api POST /prompts/$KEY/connectors/<connector-id>/register -d '{
  "credentials": {"...": "..."}, "version": 1, "permanent": true
}'
```

**B. Reuse an existing shared connection** (no new credentials, no re-auth):

```bash
bud api POST /prompts/$KEY/connections/<gateway-id>/attach-tools -d '{
  "tool_ids": ["<tool-uuid>", "..."], "version": 1
}'
```

Prefer B when the organisation already has the system connected - it avoids a
duplicate credential and a duplicate gateway.

**If the connector uses OAuth**, a human has to complete it. There is no
headless path:

```bash
bud api POST /prompts/oauth/initiate -d '{"prompt_id":"'"$KEY"'","connector_id":"<id>","version":1}'
# -> {authorization_url, state, expires_in}   <- give this URL to the user
bud api GET  /prompts/oauth/status -q prompt_id=$KEY -q connector_id=<id> -q version=1
bud api POST /prompts/oauth/fetch-tools -d '{"prompt_id":"'"$KEY"'","connector_id":"<id>","version":1}'
```

Hand the `authorization_url` to the user, tell them the `state` expires in
`expires_in` seconds, and poll `status` until a token appears. Then
`fetch-tools` pulls the connector's tool catalog in.

**Choose which tools the agent may call** - this is the step that actually
grants capability:

```bash
bud api GET /prompts/tools -q prompt_id=$KEY -q connector_id=<id> -q version=1 \
  | jq -r '.tools[] | "\(.id)  \(.name)  added=\(.is_added)"'
bud api GET /prompts/tools/<tool-id> | jq '.tool'          # full JSON schema

bud api POST /prompts/prompt-config/add-tool -d '{
  "prompt_id": "'"$KEY"'", "connector_id": "<id>",
  "tool_ids": ["<tool-uuid>","<tool-uuid>"], "version": 1, "permanent": true
}'

# verify
bud api GET /prompts/prompt-config/$KEY -q version=1 | jq '.data.tools[].allowed_tools'
```

> `add-tool` **replaces** the tool set for that connector rather than adding to
> it. To add one tool, send the full list including the existing ones. An empty
> `tool_ids` list removes them all.

Grant the narrowest set that does the job. Every extra tool is extra blast
radius, and the tool list is part of what the model sees.

Detaching:

```bash
# private connection: deletes its gateway too
bud api DELETE /prompts/$KEY/connectors/<connector-id>/disconnect -q version=1

# shared connection: detaches this agent only, gateway survives for other agents
bud api DELETE /prompts/$KEY/connections/<gateway-id>/detach -q version=1
```

Use the right one. `disconnect` on something other agents depend on is the
mistake to avoid; shared gateways are never deleted by any path precisely
because of that.

### Requiring human approval for a tool call

Each connector tool config carries a `require_approval` setting - `never`
(default), `always`, or `auto`. Set it to `always` for anything that spends
money, sends messages outside the organisation, or writes to a system of record.
When a run reaches that tool it parks with `suspend_reason: awaiting-approval`
and shows up in the approvals inbox - see `operations.md` for approving,
editing the arguments, or rejecting.

Approval gates only bite on **durable** runs. A synchronous call has nowhere to
park.

## Sub-agents (multi-agent composition)

Connect another agent - local or external - as a tool of this one.

```bash
# who can I connect? (project_id is required)
bud api GET /prompts/$KEY/agents -q project_id=$BUD_PROJECT_ID -q version=1 \
  | jq -r '.agents[] | "\(.id)  \(.name)  \(.prompt_type)  connected=\(.is_connected)"'

bud api GET /prompts/$KEY/agents -q project_id=$BUD_PROJECT_ID -q version=1 -q agent_type=external_a2a_agent

# connect
bud api POST /prompts/$KEY/connect-agent -q version=1 -d '{"agent_prompt_id":"<child-agent-uuid>"}'

# verify
bud api GET /prompts/$KEY/agents -q project_id=$BUD_PROJECT_ID -q version=1 -q connected_only=true

# disconnect
bud api DELETE /prompts/$KEY/disconnect-agent -q agent_prompt_id=<child-agent-uuid> -q version=1
```

Note the asymmetry: the **parent** is addressed by config key, the **child** by
its agent UUID.

Each row carries the child's `skills`, `description` and `health_status` - those
come from its agent card, and they are what the parent model uses to decide when
to delegate. If a child is never picked, its card description is the first thing
to fix (`a2a_card` in the child's config).

Two consequences to plan for:

- **`connect-agent` saves the parent config as permanent.** Connect a sub-agent
  to an unsaved draft and that draft is promoted for good - every later
  draft-mode save on it fails with "Cannot downgrade a permanent prompt to
  temporary". Finalise the parent first, then compose.
- **A connected child cannot be deleted.** `DELETE /prompts/{child}` returns
  400 naming the parent. Disconnect first - see the retire flow in SKILL.md.

Design notes worth telling the user: composition costs a full model round trip
per delegation, and a child agent's own tools and guardrails still apply when it
is called this way. Keep hierarchies shallow (one level of delegation is usually
enough) and give each child a narrow, well-described job.

External agents are connected exactly the same way once registered - see
`agent-lifecycle.md`.
