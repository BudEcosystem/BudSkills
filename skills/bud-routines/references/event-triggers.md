# Event triggers: full reference

An **event trigger** connects an outside system's webhook to a Bud agent. It is
authored at `POST /triggers` and is unrelated to `/budpipeline/event-triggers`
(internal Bud events -> pipeline) despite the similar name.

The pieces:

```
provider app  --webhook-->  shared connection  --subscription-->  trigger descriptor  -->  agent run
   (GitHub)                 (/events/shared-connections)          (/triggers)              (/runs)
```

## 1. The provider catalog

`GET /events/descriptors[?provider=X]` is the **only** source of truth for
provider names, event type strings, credential requirements, sample payloads and
correlation-key suggestions. Nothing about providers is hardcoded in the API.

```json
{"descriptors":[{"provider":"github","provider_type":"issues","canonical_type":"issues",
                 "description":"...","type_label":"Issue",
                 "actions":[{"value":"opened","label":"opened"}, ...]}],
 "providers":[{"provider":"github",
               "credentials":[{"key":"signing_secret","label":"Signing Secret","secret":true,"required":true}],
               "sample_event":{"eventType":"issues.opened","repository":{...},"issue":{...}},
               "grouping_suggestions":[{"path":"$.repository.full_name","label":"Repository","suggested":true},
                                       {"path":"$.issue.id","label":"Issue","suggested":false}]}]}
```

`?provider=` filters **only** `descriptors[]`; `providers[]` always comes back
in full, so filter it client-side:

```bash
bud api GET /events/descriptors -q provider=github \
  | jq '.providers[] | select(.provider=="github")'
```

Providers shipped (60): alertmanager, argocd, asana, azuredevops, bamboohr,
bitbucket, box, calcom, calendly, circleci, clickup, confluence, datadog,
docusign, dropbox, entra, figma, freshdesk, front, github, gitlab, google_drive,
gorgias, grafana, greenhouse, harbor, hubspot, jfrog, jira, jumpcloud, keycloak,
linear, mattermost, minio, notion, okta, onelogin, pagerduty, pipedrive,
ringcentral, rocketchat, rootly, sentry, servicenow, shopify, shortcut, signoz,
slack, smartsheet, snyk, stripe, teams, telegram, typeform, workvivo, wrike,
youtrack, zendesk, zoho_crm, zulip. Confirm against the live catalog - it changes
without an API change.

`GET /prompts/trigger-providers` is a lighter view of the same data (provider,
`conversational` flag, event count, credential spec) used when building an
agent's trigger UI.

### Deriving `event_types` correctly

**Use `providers[].sample_event.eventType`.** It is the normalised string the
matcher compares against.

| Provider | Correct `event_types` entry | Wrong (a trigger that never fires) |
|---|---|---|
| github | `issues.opened`, `pull_request.opened`, `push` | `issues`, `opened`, `com.github.issues` |
| linear | `Issue.create`, `Issue.update`, `Issue.remove` | `issue.created` |
| alertmanager | `firing`, `resolved` | `alert_firing` (that is the `canonical_type`) |

About 11 of the 60 providers compose the string as `{canonical_type}.{action}` -
those are the ones whose `descriptors[]` rows carry a non-empty `actions[]`
array. The rest list atomic strings and their `actions[]` is empty (alertmanager
above is the example: `canonical_type` is `alert_firing` but the event type is
`firing`).

Nothing validates the string you send. A wrong one produces a trigger that looks
healthy in every read and never fires. When a trigger is silent, re-check this
first.

An empty `event_types: []` is rejected with 422 on purpose - it would otherwise
match every event from the connection.

## 2. Shared connections

```bash
bud api POST /events/shared-connections -d '{
  "scope": "project",
  "project_ids": ["<PROJECT_ID>"],
  "provider": "github",
  "credentials": {"signing_secret": "<the secret you will set in GitHub>"},
  "name": "Acme GitHub",
  "semantics": "fire_and_forget"
}'
```

- `scope` is `project` (default) or `global`. `project_ids` is required and
  non-empty for `project`, and must be **omitted** for `global`. Creating a
  `global` connection requires administrator rights, not just project
  permissions.
- `credentials` keys come from the provider's spec and are validated fail-closed
  (a 400 lists what is missing). They are never stored by the API surface and
  are never returned by any read.
- `semantics` defaults to `conversational` for slack/teams/workvivo and
  `fire_and_forget` for everything else.

The response nests under `.connection`:

```json
{"connection":{"id":"<use as connection_id>","connector_id":"<appears in the URL>",
               "webhook_url":"https://<host>/events/github/<connector_id>","provider":"github"}}
```

Paste `webhook_url` into the provider (GitHub: Payload URL, content type
`application/json`, Secret = the same `signing_secret`, and select the events).
A `GET` on the same path answers registration handshakes such as GitHub's ping.

> If the installation's external URL setting is wrong, every connection hands out
> an unreachable `webhook_url` and no trigger will ever fire. If a provider's
> delivery log shows connection failures rather than 4xx, that is the cause.

`GET /events/shared-connections[?project_id=]` re-derives `webhook_url` so it can
be recopied. `PATCH` changes only the display name, the project set (it
**replaces** the set) and the scope - **credentials are immutable**. Rotating a
secret means deleting and recreating the connection, which invalidates every
trigger bound to it.

`DELETE` cascades away every subscription on the connection, and returns 409 if
the connection is a secondary source of a multi-source trigger.

## 3. The trigger descriptor

```json
{
  "project_id": "<uuid>",              // required, immutable afterwards
  "name": "Triage new issues",
  "connection_id": "<uuid>",
  "target": {"prompt_id": "<agent id>", "version": "1"},
  "event_types": ["issues.opened"],
  "descriptor": {
    "correlation": {"key": "$.repository.full_name", "namespace": "...",
                    "multi_match": "newest", "ttl_seconds": 3600},
    "join":       {"require_all": ["a","b"], "timeout_seconds": 1800, "on_timeout": "partial"},
    "escalation": {"tiers": [{"after_seconds": 300}, {"after_seconds": 900}]},
    "debounce":   {"window_seconds": 60},
    "cep":        {"sequence": ["a","b"], "within_seconds": 600}
  },
  "sources": [ ... ]                   // multi-source only; see section 4
}
```

### correlation (required)

`key` is a JSONPath **into the provider's payload** - `$.repository.full_name`,
`$.data.identifier`, `$.groupKey`. Take it from `grouping_suggestions[]`,
preferring the entry with `suggested: true`, unless you have a better case
identifier for the task.

Write it in payload space. The platform rewrites `$.X` into its own envelope
space at registration; a hand-written `$.raw.X` prefix means something different
and usually breaks.

> **A key that resolves to nothing on every event silently degrades the trigger**
> to per-event structural fallback: one agent run per event, no grouping, and any
> join is unreachable. Nothing reports this. Verify by sending one real event and
> checking the correlation actually grouped what you expected.

| Field | Values | Meaning |
|---|---|---|
| `multi_match` | `newest` (default behaviour), `error`, `fanout` | what to do when an event matches several open cases |
| `ttl_seconds` | > 0, commonly 3600 | how long a correlation case stays open |
| `namespace` | string | separates case spaces that would otherwise collide |

### join

Fires the agent only once every named slot has arrived for the same correlation
key. `require_all` holds **event-type strings** (the same ones in `sources[]`).
`timeout_seconds` bounds the wait; `on_timeout` is `partial` (fire with what
arrived) or `cancel` (drop it).

If `correlation.ttl_seconds` is smaller than `join.timeout_seconds` it is
silently raised to match, so the case outlives the wait. The read-back still
shows the value you sent.

### debounce, cep, escalation

- `debounce.window_seconds` - collapse a burst into one run after a quiet window.
- `cep.sequence[]` + `within_seconds` - fire only when events occur in that order
  inside the window.
- `escalation.tiers[]` - `after_seconds` must be **strictly increasing**. Only
  `after_seconds` is enforced; a tier's `notify` and `action` are stored and
  echoed back but dropped at registration.

### hitl

`descriptor.hitl` is **rejected with a 400** before anything is created:
"Human-in-the-loop approvals are not yet supported for triggers". A gate at the
trigger layer would have no release path. Human approval of a *run* is a
different mechanism - see `references/runs-and-approvals.md`.

### What the read-back does not tell you

`GET /triggers/{id}` returns the descriptor you authored, not what is enforced.
Dropped at registration but still echoed: escalation tier `notify`/`action`,
hitl `on_timeout`, and the un-clamped `correlation.ttl_seconds`. Treat the
descriptor in a read as intent, not as configuration.

Worse: **the advanced blocks (join, debounce, cep, escalation) are registered
best-effort.** If that registration fails, the create or update still returns
200, plain correlation routing works, and the algebra is simply not live. There
is no API to inspect the live registry. The only remedies are to re-issue the
identical `PUT` (which re-registers) and to verify behaviourally - for a join,
send one side only and confirm no run appears.

## 4. Multi-source joins

```json
{
  "project_id": "<uuid>",
  "name": "PR and ticket on the same case",
  "connection_id": "<GITHUB_CONN>",
  "target": {"prompt_id": "<agent>"},
  "event_types": ["pull_request.opened"],
  "descriptor": {
    "correlation": {"key": "$.pull_request.title", "ttl_seconds": 3600},
    "join": {"require_all": ["pull_request.opened", "Issue.update"],
             "timeout_seconds": 1800, "on_timeout": "partial"}
  },
  "sources": [
    {"connection_id": "<GITHUB_CONN>", "event_types": ["pull_request.opened"],
     "correlation_key": "$.pull_request.title"},
    {"connection_id": "<LINEAR_CONN>", "event_types": ["Issue.update"],
     "correlation_key": "$.data.identifier"}
  ]
}
```

Rules enforced with a 422:

1. At least two entries in `sources`.
2. `sources[0]` must mirror the top-level `connection_id` **and** the top-level
   `event_types` set.
3. `descriptor.join` is mandatory - without a wait the sources are just
   independent triggers.
4. No event-type string may appear in more than one source (join slots are bare
   event-type strings and would merge).

A fifth rule is enforced later and differently: every `connection_id` must be
`global` or linked to the trigger's project, and one that is not comes back as a
**404**, not a 422 - the API never confirms that a resource in another project
exists.

Each source's `correlation_key` is that provider's path to the **same real-world
identifier** - the ticket key that appears in both payloads. Choosing two paths
that never produce the same string is the usual reason a join never completes,
and it produces no error.

Read back with `GET /triggers/{id}` and confirm `sources[]` has one entry per
source, each with its own non-empty `subscription_id`. Rows created before
multi-source support may have an empty `sources[]`.

## 5. Editing and retiring

```bash
bud api GET    /triggers/<ID> | jq '.trigger'
bud api PUT    /triggers/<ID> -d @body.json
bud api DELETE /triggers/<ID>
```

`PUT` takes the same body as create **minus `project_id`** (immutable; anything
you send is ignored) and is a **full replace**, not a merge.

> The read model is not the write model. `GET` returns `prompt_id` and `version`
> at the top level of `.trigger`; `PUT` wants `{"target":{"prompt_id":...,
> "version":...}}`. Rebuild it, or the version silently blanks.

Reconciliation is by source identity = (`connection_id`, sorted `event_types`,
`correlation_key`):

- unchanged source -> keeps its subscription, **no events dropped**
- new source -> subscribed before the change commits
- removed source -> torn down after the commit, best-effort
- changed `target.prompt_id` or `target.version` -> **every** kept source is
  re-subscribed and the old subscriptions retired afterwards

Verify with a second `GET` that only the `subscription_id`s you expected moved.

### Retiring

There is **no enable/disable flag**. `DELETE /triggers/{id}` is the only pause,
and it unregisters every source's subscription before removing the row. A 502
keeps the row - re-run the delete, it is self-healing (an already-gone
subscription counts as deleted).

Do not improvise a pause:

- `DELETE /events/subscriptions/{id}` leaves an orphan trigger row that never
  fires and that a later trigger delete cannot clean up properly. It exists for
  external-agent secret rotation, not for trigger management.
- Deleting the shared connection cascades away every subscription on it and 409s
  when it is a secondary source of a join.

If you need a reversible pause, record the full trigger body first
(`GET /triggers/{id}`), delete, and recreate from that body later.

## 6. Watching a trigger fire

```bash
bud api GET /runs -q project_id=<PID> -q prompt_id=<AGENT_ID>
bud api GET /runs/<SESSION_ID>/timeline
```

A plain correlation trigger dispatches sub-second from the webhook. A join waits
up to its `timeout_seconds`. A debounce waits its quiet window.

**There is no delivery log.** A webhook that verified but did not match produces
nothing observable through the API. When a trigger is silent, work down this
list:

1. The provider's own delivery log shows a 2xx (if not, the URL or the signing
   secret is wrong).
2. `GET /triggers/{id}` shows a non-empty `subscription_id` on every source.
3. `event_types` matches `sample_event.eventType` exactly, character for
   character.
4. The correlation key actually resolves in that provider's payload.
5. `GET /runs?project_id=...` - if it returns 502 saying an operator token is
   not configured, the agent-run plane is not wired on this installation and you
   cannot observe runs at all here.

## 7. Conversational channel bots - a different mechanism

Making an agent reply in a Slack/Teams/Workvivo thread is **not** an event
trigger:

```bash
bud api POST /prompts/<AGENT_ID>/connectors/<CONNECTOR_ID>/triggers -d '{
  "event_types": [],
  "credentials": {"bot_token":"xoxb-...","signing_secret":"..."},
  "version": 1,
  "descriptor_ref": "slack"}'

bud api GET    /prompts/<AGENT_ID>/triggers -q version=1
bud api DELETE /prompts/<AGENT_ID>/connectors/<CONNECTOR_ID>/triggers -q version=1
```

- Send `event_types: []` for chat providers - the message and mention events are
  derived for you.
- `descriptor_ref` must be `slack`, `teams` or `workvivo`; anything else is a 400
  pointing you back at `POST /triggers`.
- The connector must already be registered on the agent (400 otherwise).
- Calling it again **replaces** the existing bot on that connector (the new
  connection is created before the old one is released).
- Paste the returned `trigger.webhook_url` into the app's event-subscription URL.

`GET /prompts/event-catalog?descriptor_ref=slack` lists selectable event types
for this path as reverse-DNS strings (`com.slack.message`, ...). **Those values
are valid only here** - they are not accepted by `POST /triggers`, which uses the
normalised provider types. Two catalogs, two vocabularies.

`GET /prompts/{id}/triggers` lists only channel bots; authored event triggers are
listed by `GET /triggers?prompt_id=...`.

## 8. Access and visibility

- Creating or editing triggers and connections needs endpoint-manage permission
  plus membership of the project; creating a `global` connection needs
  administrator rights.
- An unknown or unshared `connection_id` returns **404, never 403** - deliberate,
  so a cross-project resource is never confirmed to exist.
- **`GET /triggers` without `project_id` returns every project's triggers to any
  authenticated caller.** The membership check only runs when `project_id` is
  supplied. Always pass it.
- A trigger-dispatched run authenticates with a short-lived credential minted at
  dispatch and never refreshed, so an unusually long triggered run can fail
  partway through with an authentication error. Nothing in the API surface fixes
  this; keep triggered agent work bounded.
