---
name: bud-agents
description: Build, version, run and supervise Bud Foundry agents - write a system prompt against a deployed model, add structured input/output schemas, ship every edit as a new version and promote the best one, then run the agent synchronously, streamed, or as a durable background run. Use when a Bud request mentions agents or prompts, prompt versions, system prompts, running or testing an agent, giving an agent tools or sub-agents, approving or steering a live run, an agent in a Slack/Teams channel, or retiring an agent.
---

# Bud Foundry: agents

An **agent** is a named, versioned prompt bound to a deployed model. Each
version carries its own system prompt, messages, model settings, structured
input/output schemas, tools and sub-agents. The console calls them Agents; the
API calls them prompts, so every path below is `/prompts/**`.

You will use two planes:

| Plane | Host | What it does |
|---|---|---|
| Management | `app.<domain>` - `bud api` | author, version, promote, inspect, supervise |
| Inference gateway | `gateway.<domain>` - bearer token | actually **runs** the agent (`/v1/responses`, `/a2a/...`) |

Prerequisite: connect first - see `bud-platform` (`bud login`). You also need a
`project_id` (`bud-projects`) and a **running deployment that serves Responses**.

> If you **create a new project** to house this agent, give that project a
> fitting `icon` - and reuse the agent's own icon when it is safe to (an emoji or
> an existing static asset key, not a URL). See the `bud-projects` skill's
> project-icon reference for the rules.

> **Chat support is not enough, and this is the trap that wastes the most time.**
> Agents run on `/v1/responses`, not `/v1/chat/completions`. Most deployments do
> not serve it: on the reference installation **28 of 29 deployments have chat
> enabled and only 2 have Responses enabled**. Build an agent on a chat-only
> deployment and every run fails with an empty-bodied 502 that looks like an
> outage.

Check before you build anything:

```bash
bud api GET /playground/deployments -q limit=100 \
  | jq -r '.endpoints[]
      | select(.model.supported_endpoints.responses.enabled == true)
      | "\(.name)\t\(.status)\t\(.project.name)\t\(.id)"'
```

If that returns nothing, no agent can run on this installation until a
deployment is configured for Responses - see `bud-deployments`
(`PUT /endpoints/{id}/responses-config`, `enabled: true`). Say so rather than
building an agent that cannot answer.

## Three different things are called "prompt id"

Get this wrong and you will chase 404s - they are not interchangeable:

| Identifier | Looks like | Used by |
|---|---|---|
| **Agent UUID** | `c1977027-...` | `/prompts/{id}`, `PATCH`, `DELETE`, `/versions` |
| **Config key** | a draft uuid *before* the agent is saved, the agent **name** after | `/prompts/prompt-config/{id}`, `/prompts/{id}/native-tools`, `/connectors/**`, `/connect-agent`, and `prompt.id` when you run it |
| **Agent name** | `my-agent` | `/prompts/{name}/traces`, `/prompts/agent-card/{name}`, gateway routing |

The trap: while you are authoring, the config key is the draft uuid the platform
minted. **The moment the agent is saved, the config key becomes its name** - all
later config, tool and sub-agent calls must use the name, not the draft uuid.

## Create an agent

There is no `POST /prompts`. Creation is two calls: save a **draft config**,
then **finalise** it.

```bash
# 1. pick the deployment that will back it - you need both id and name
bud api GET /endpoints/ -q project_id=$BUD_PROJECT_ID \
  | jq -r '.endpoints[] | "\(.id)  \(.name)  \(.status)"'

# 2. save the draft config - OMIT prompt_id to mint a new draft
bud api POST /prompts/prompt-config -d '{
  "deployment_name": "<deployment-name>",
  "system_prompt": "You are a concise HR policy assistant.",
  "messages": [{"role":"user","content":"{{ question }}"}],
  "model_settings": {"temperature": 0.3},
  "stream": false, "enable_tools": false, "allow_multiple_calls": false,
  "version": 1, "set_default": true
}'
# -> {"bud_prompt_id":"<draft-uuid>","bud_prompt_version":1}   (FLAT, not nested)

# 3. finalise: this creates the agent and version 1 synchronously
bud api POST /prompts/prompt-workflow -d '{
  "step_number": 1, "workflow_total_steps": 1, "trigger_workflow": true,
  "project_id": "'"$BUD_PROJECT_ID"'",
  "endpoint_id": "<endpoint-uuid>",
  "name": "hr-assistant", "description": "Answers HR policy questions",
  "tags": [{"name":"hr","color":"#4F8EF7"}],
  "prompt_type": "simple_prompt", "concurrency": [1, 10], "rate_limit": false,
  "bud_prompt_id": "<draft-uuid>"
}'

# 4. the response does NOT contain the new id - resolve it by name
bud api GET /prompts -q name=hr-assistant -q limit=1 | jq -r '.prompts[0].id'
```

`system_prompt` and message `content` are Jinja2; `{{ question }}` is filled
from `prompt.variables` at run time. Four things bite here:

- **Agent names are globally unique across every project**, not per project. A
  clash is a 400 at step 3, after you have already built the draft. Check first:
  `bud api GET /prompts -q name=<name> -q limit=1`.
- **Drafts expire after 24 hours** - finalise the same session, or start over.
- `concurrency` (a `[min, max]` pair) is **required** for `simple_prompt`.
- `enable_tools: true` requires `allow_multiple_calls: true`, or the save 400s.

Config saves are **partial**: keys you omit are preserved, keys you send replace
wholesale. So a later "edit" is just another `POST /prompts/prompt-config` with
`prompt_id` set to the config key and only the changed fields.

Structured input/output schemas are a separate asynchronous call
(`POST /prompts/prompt-schema`, then `bud wait job <workflow_id>`); a
third-party agent that already runs elsewhere is
`prompt_type: external_a2a_agent`. Both in `references/agent-lifecycle.md`.

## Run it and read the answer

Execution is on the gateway, with a bearer token - not `bud api`:

```bash
export BUD_GATEWAY=https://gateway.<your-domain>
export BUD_GATEWAY_TOKEN=$(bud token)      # mints AND registers - see bud-platform

curl -sS "$BUD_GATEWAY/v1/responses" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":{"id":"hr-assistant","variables":{"question":"How much leave do I get?"}},
       "input":"How much leave do I get?","stream":false}' \
  | jq -r '.output[].content[]?.text'
```

`prompt.id` is the agent **name**; add `"version":"2"` to pin a specific
version, otherwise the default version answers. The reply is a standard
Responses envelope (OpenAI-compatible shape): the text lives in
`output[*].content[*].text`, token counts in `usage`, and `id` is the
`resp_*` handle you use for everything afterwards.

Continue a conversation by adding `"previous_response_id":"resp_..."` to the
next call instead of resending the history. Watch for:

- **Send `stream` explicitly.** Omitted, it inherits the stored config's flag,
  so you can get an event stream when you expected one JSON body.
- A `conversation` field is a hard 422. Use `previous_response_id`.
- `404 Prompt not found` means the gateway's routing map has no entry for this
  agent/version yet. It is rebuilt when a version is created or promoted, but
  best-effort; force a rebuild with a no-op `PATCH` on the version.
- A governance policy can turn a run into a 403 or a
  `status: "requires_action"` (see `bud-guardrails`).

Streaming and stream resume: `references/running-agents.md`.

## Iterate: every edit is a new version

This is the core workflow. **A version is immutable** - to change a prompt you
build a new draft, attach it as a new version, test that version by name, and
only then promote it. The previous version keeps serving traffic the whole time,
so rollback is one call.

```bash
PROMPT_ID=<agent-uuid>

# 1. read the version you are iterating from, including its full config
bud api GET /prompts/$PROMPT_ID/versions | jq -r '.versions[] | "\(.version) \(.id) default=\(.is_default_version)"'
bud api GET /prompts/$PROMPT_ID/versions/<version-uuid> | jq '.config_data' > cfg.json

# 2. build a NEW draft with your edits (omit prompt_id => new draft); replay the
#    fields you want to keep, change the ones you are testing
bud api POST /prompts/prompt-config -d '{
  "deployment_name": "<deployment-name>",
  "system_prompt": "You are a concise HR policy assistant. Always cite the section.",
  "messages": [{"role":"user","content":"{{ question }}"}],
  "model_settings": {"temperature": 0.1}, "version": 1
}'   # -> new <draft-uuid>

# 3. attach it as the next version - NOT default yet
bud api POST /prompts/$PROMPT_ID/versions -d '{
  "endpoint_id": "<endpoint-uuid>", "bud_prompt_id": "<draft-uuid>", "set_as_default": false
}' | jq '.version | {id, version}'

# 4. smoke-test that exact version through the gateway
curl -sS "$BUD_GATEWAY/v1/responses" -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":{"id":"hr-assistant","version":"2"},"input":"How much leave do I get?"}' \
  | jq -r '.output[].content[]?.text'

# 5. promote it
bud api PATCH /prompts/$PROMPT_ID/versions/<new-version-uuid> -d '{"set_as_default":true}'

# 6. roll back by promoting the old one again
bud api PATCH /prompts/$PROMPT_ID/versions/<old-version-uuid> -d '{"set_as_default":true}'
```

Judge candidates on evidence: run the same inputs against both versions, or
score them with an evaluation (`bud-evaluations`). Promote only on a win.

Version rules worth knowing:

- The new number is `max(existing) + 1`, and **deleted versions still occupy
  numbers**, so numbering can skip. Never assume "next is N+1 of what I see".
- The endpoint must be in the **same project** as the agent (400 otherwise).
  `PATCH .../versions/{id} {"endpoint_id":"..."}` repoints an existing version -
  that is how you move an agent onto a new model without touching its prompt.
- **Tools, sub-agents and schemas do not carry over** to a new version unless
  the config you replayed contained them. Re-attach on the new draft before
  step 3.
- `GET /prompts/prompt-config/{key}` returns **200 with an empty config** when
  nothing is there. Test `data.deployment_name`, never the status code.

## Long-running work: background (durable) runs

For work that takes minutes to days, or that must pause for a human, run it in
the background - the run survives restarts, can park on an approval, and is
steerable.

```bash
curl -sS "$BUD_GATEWAY/v1/responses" \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":{"id":"hr-assistant"},"input":"Audit every policy doc for stale dates",
       "background":true,"store":true}'
# -> {"id":"resp_...","status":"queued"}
```

Then wait on it from the management side, which avoids the gateway's header trap
entirely:

```bash
bud wait resource /runs/resp_... --field state.status \
  --equals completed,incomplete,failed,cancelled --timeout 1800

curl -sS "$BUD_GATEWAY/v1/responses/resp_..." \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" -H 'x-model-name: hr-assistant' \
  | jq -r '.output[].content[]?.text'
```

- **If `/runs` answers 502** complaining about an operator token, the run
  console is simply not enabled on that installation. The run itself is fine -
  poll the gateway instead (`GET /v1/responses/{id}` with `x-model-name`, every
  few seconds) and tell the user supervision is unavailable there.
- **`x-model-name: <agent name>` is mandatory on every gateway sub-call**
  (`GET`, `DELETE`, `cancel`, `input_items`, stream resume). Without it the
  gateway resolves a placeholder model and answers
  `404 Model not found: gpt-4-responses`.
- `background: true` with `store: false` is a hard 400.
- Non-terminal states are `queued`, `runnable`, `in_progress`, `suspended`,
  `awaiting_approval`, `requires_action`; terminal are `completed`,
  `incomplete`, `failed`, `cancelled`. `suspended` is not stuck - it usually
  means a human gate.
- Under load the platform sheds new background runs with `503` + `Retry-After`.

`references/running-agents.md` covers streaming with resume, the run state
machine, and the per-deployment switches that can disable background runs.

## Giving an agent tools, and composing agents

All tool wiring targets the **config key** and a `version`, and applies to that
version only.

```bash
# built-in tools (web search, web fetch, ...)
bud api GET  /prompts/hr-assistant/native-tools -q version=1
bud api POST /prompts/hr-assistant/native-tools \
  -d '{"version":1,"tool_name":"web_search","config":{"max_results":5},"permanent":true}'

# tools from a connected system (MCP connectors)
bud api POST /prompts/prompt-config -d '{"prompt_id":"hr-assistant","version":1,"enable_tools":true,"allow_multiple_calls":true}'
bud api GET  /prompts/connectors -q prompt_id=hr-assistant -q version=1

# another agent as a sub-agent
bud api GET  /prompts/hr-assistant/agents -q project_id=$BUD_PROJECT_ID -q version=1
bud api POST /prompts/hr-assistant/connect-agent -q version=1 -d '{"agent_prompt_id":"<child-uuid>"}'
```

Two sharp edges: `enable_tools` must be on **before** registering a connector,
and `connect-agent` permanently promotes a draft config - after it, every
non-permanent save on that key fails. Full sequences, OAuth connectors, tool
selection and the sandboxed code interpreter are in
`references/tools-and-composition.md`; configuring the connectors themselves
belongs to `bud-connectors`.

## Supervising a live run

A durable run can pause for a human to approve a tool call, and can be nudged or
stopped mid-flight.

```bash
bud api GET  /approvals -q project_id=$BUD_PROJECT_ID      # what is waiting on me
bud api POST /approvals/<session-id>/approve -d '{"call_id":"<from pending_call>"}'
bud api POST /runs/<session-id>/inject -d '{"text":"the address changed to ..."}'
bud api POST /runs/<session-id>/abort  -d '{"mode":"after_turn"}'
```

Show the user `pending_call.args` before approving - that is the point of the
gate. Never invent an `awaited_token`; a stale one is a 409 (re-read
`/runs/{id}` and retry). `inject` and `abort` are **not** idempotent - a
retried inject applies twice - while approve and reject are.
`references/operations.md` has the whole supervision surface, including
policy-driven escalations.

## Channel bots

Put an agent in Slack, Teams or Workvivo so each thread is a conversation:

```bash
bud api GET  /prompts/trigger-providers | jq '.providers[] | select(.conversational)'
bud api POST /prompts/hr-assistant/connectors/<connector-id>/triggers -d '{
  "descriptor_ref":"slack","credentials":{"signing_secret":"...","bot_token":"..."},
  "event_types":[], "version":1
}'
```

The returned `webhook_url` must be pasted into the provider app's event
subscriptions by a human - tell the user, because until they do it the bot is
silent and nothing in Bud looks wrong. Only `slack`, `teams` and `workvivo` are
accepted here; fire-and-forget event triggers belong to `bud-routines`. Details:
`references/operations.md`.

## Retire an agent

```bash
bud api DELETE /prompts/<agent-uuid>
```

**Confirm with the user first, naming the agent.** Before deleting: disconnect
it from any parent agent (a connected child is a hard 400 that names the
parent), and remove its channel bots. To drop a single version instead, promote
a different one first - deleting the default version is a 400.

Deletion is soft and the follow-up teardown is best-effort: the call returns 200
even when attached resources fail to clean up. Shared connections are never
deleted, because other agents use them.

## Deeper reference

- `references/agent-lifecycle.md` - full config field list, structured schemas
  and the reserved-name rule, external A2A agents, agent cards, identifiers
- `references/running-agents.md` - synchronous, streaming with resume and
  durable execution; the run state machine; per-deployment switches; A2A
- `references/tools-and-composition.md` - built-in tools, connector tools and
  OAuth, tool selection, sub-agents, the code-interpreter sandbox
- `references/operations.md` - run console, steering and aborting, approvals,
  governance escalations, channel bots, and a triage checklist

## Where to go next

`bud-prompt-optimization` to improve a prompt measurably - build a case set, compare
candidates per case, and promote only when nothing regressed;
`bud-connectors` to configure the systems an agent's tools reach;
`bud-observability` to see what a run did; `bud-evaluations` to score versions
against each other; `bud-guardrails` for safety and governance policy;
`bud-routines` to run an agent on a schedule or an event.
