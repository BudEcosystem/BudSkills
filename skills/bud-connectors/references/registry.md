# The connector registry

The registry is the catalog of external systems Bud already knows how to talk
to. Entries are *templates*: browsing one costs nothing, and nothing exists until
you configure it into a connection.

```bash
bud api GET /connectors/registry -q limit=100                 # everything
bud api GET /connectors/registry -q name=git -q limit=100     # substring filter on name
bud api GET /connectors/registry/github                       # one entry, with credential_schema
```

The list returns rows flat under `.connectors` (not nested per item); the
single-entry route nests under `.connector`. Both need the endpoint-manage
permission. `limit` caps at 100, so page if the installation grows past that.

Fetching a single entry scans the whole registry server-side, so it is not free -
if you need several, pull one page of 100 and filter locally.

## What is on this installation

Snapshot of the installation I verified against: **90 connectors**. Counts by
authentication type:

| `auth_type` | Count | Means |
|---|---|---|
| `OAuth2.1` | 49 | OAuth; almost all authorization-code (human step) |
| `Open` | 20 | no credentials at all |
| `API Key` | 17 | a header you supply |
| `OAuth` | 2 | OAuth |
| `OAuth2.1 & API Key` | 1 | either works (`stripe`) |
| `API` | 1 | a header you supply (`customgpt`) |

Treat this as a guide, not a contract - registries are updated independently of
Bud releases. Always re-list rather than quoting this table to a user.

| Category | Connectors (`id` and auth type) |
|---|---|
| **AI Evaluation** | `scorecard` (OAuth2.1) |
| **Advertising** | `meta-ads` (OAuth2.1) |
| **Airlines** | `turkish-airlines` (OAuth2.1) |
| **Asset Management** | `cloudinary` (OAuth2.1) |
| **Authentication** | `stytch` (OAuth2.1) |
| **Automation** | `zapier` (API Key) |
| **CMS** | `webflow` (OAuth2.1), `wix` (OAuth2.1) |
| **CRM** | `close-crm` (OAuth2.1), `close-crm-api` (API Key), `hubspot` (OAuth2.1) |
| **Cloud Infrastructure** | `azure-mcp` (Open) |
| **Communication** | `slack` (OAuth2.1), `telnyx` (API Key) |
| **Crypto** | `hive-intelligence` (OAuth2.1) |
| **Customer Support** | `intercom` (OAuth2.1) |
| **Data Analysis** | `llm-text` (Open) |
| **Data Analytics** | `thoughtspot` (OAuth2.1) |
| **Database** | `prisma-postgres` (OAuth2.1) |
| **Design** | `canva` (OAuth2.1) |
| **Document Management** | `box` (OAuth2.1), `egnyte` (OAuth2.1) |
| **Documentation** | `astro-docs` (Open), `cloudflare-docs` (Open), `kollektiv` (OAuth2.1) |
| **E-Commerce** | `mercado-libre` (API Key) |
| **Forecasting** | `manifold` (Open) |
| **Link Shortener** | `shortio` (API Key) |
| **MCP Directory** | `remote-mcp` (Open) |
| **Market Intelligence** | `octagon` (OAuth2.1) |
| **Memory** | `zine` (API Key) |
| **Observability** | `cloudflare-observability` (OAuth2.1) |
| **Other** | `rube` (OAuth2.1) |
| **Outbound Phone Calls** | `dialer` (OAuth2.1) |
| **Payments** | `dodo-payments` (API Key), `mercado-pago` (OAuth), `paypal` (OAuth2.1), `plaid` (OAuth2.1), `square` (OAuth2.1), `stripe` (OAuth2.1 & API Key) |
| **Productivity** | `carbon-voice` (OAuth2.1), `find-a-domain` (Open), `firefly` (OAuth2.1), `listenetic` (OAuth2.1), `monday` (OAuth2.1), `read-ai` (OAuth2.1), `waystation` (OAuth2.1) |
| **Project Management** | `asana` (OAuth2.1), `linear` (OAuth2.1), `notion` (OAuth2.1) |
| **RAG-as-a-Service** | `audioscrape` (OAuth2.1), `customgpt` (API), `dappier` (API Key), `deepwiki` (Open), `needle` (API Key), `onecontext` (OAuth2.1) |
| **Security** | `zenable` (OAuth2.1) |
| **Service Discovery** | `openmesh` (Open) |
| **Software Development** | `atlassian` (OAuth2.1), `buildkite` (OAuth2.1), `cloudflare-workers` (OAuth2.1), `github` (OAuth2.1), `gitmcp` (Open), `globalping` (OAuth2.1), `grafbase` (OAuth2.1), `hugging-face` (Open), `instant` (OAuth), `jam` (OAuth2.1), `javadocs` (Open), `neon` (OAuth2.1), `netlify` (OAuth2.1), `openzeppelin-cairo` (Open), `openzeppelin-solidity` (Open), `openzeppelin-stellar` (Open), `openzeppelin-stylus` (Open), `polar-signals` (API Key), `semgrep` (Open), `sentry` (OAuth2.1), `vercel` (OAuth2.1) |
| **Specialised Dataset** | `context-awesome` (Open) |
| **Time Services** | `fast-time-server` (Open) |
| **Video Platform** | `invideo` (Open) |
| **Web Data Extraction** | `apify` (API Key) |
| **Web Scraping** | `simplescraper` (OAuth2.1) |
| **Web Search** | `exa-search` (API Key), `jina-search` (API Key), `serpapi` (API Key), `serper-search` (API Key), `tavily-search` (API Key), `you-search` (API Key) |

**Notable absences to check before promising anything:** Microsoft Teams,
Outlook/Exchange, Google Workspace (Gmail/Calendar/Sheets), Salesforce,
ServiceNow, Workday and most HR systems are *not* in this registry. Those need a
custom connection or generated tools.

**Web search is available two ways.** Six registry connectors need an API key
from the vendor; the built-in `web_search` tool needs nothing at all. Prefer the
built-in unless the user specifically wants a named provider.

## Reading `credential_schema`

Every registry row is enriched with `credential_schema`: an ordered list of
fields describing exactly what to put in `credentials`.

```json
{"type":"url","field":"token_url","label":"Token URL","order":4,"required":true,
 "description":"OAuth token endpoint URL","visible_when":["client_credentials","authorization_code"],
 "default":"https://github.com/login/oauth/access_token"}
```

- `field` is the JSON key you send.
- `default` is a working value - keep it unless the user has a private tenant.
- `visible_when` lists the `grant_type` values the field applies to. A field
  marked `["authorization_code"]` only exists in that flow.
- `type` is a UI hint (`text`, `password`, `url`, `dropdown`, `key-value-array`)
  - all values go over the wire as JSON.

### By auth type, concretely

**Open** - only an optional passthrough list:

```json
{"credentials": {}}
```

**API Key / API / Headers** - `auth_headers` is a required array of key/value
pairs. The provider decides the header name; check `documentation_url`:

```json
{"credentials": {"auth_headers": [{"key": "Authorization", "value": "Bearer tvly-..."}]}}
```

**OAuth / OAuth2.1** - `grant_type` first, and it decides the rest:

```json
{"credentials": {
  "grant_type": "authorization_code",
  "client_id": "...", "client_secret": "...",
  "token_url": "https://github.com/login/oauth/access_token",
  "authorization_url": "https://github.com/login/oauth/authorize",
  "redirect_uri": "https://<console-host>/oauth/callback",
  "scopes": "repo read:user user:email"
}}
```

`scopes` is a space-separated string, not an array, and comes pre-filled with the
connector's recommended set. `redirect_uri` has no default because it depends on
the installation - read it from an existing connection of the same kind
(`GET /connectors/{gateway_id}/oauth/status` returns the one in use) rather than
inventing one, because it must also be registered in the provider's OAuth app.

**Dynamic client registration.** Some connectors set `supports_dcr: true` in
`oauth_config` (`stripe` does, with a `registration_url`). Their schema marks
`client_id`/`client_secret` as optional with a "leave empty for auto-registration"
hint - the provider mints a client on the fly, so the user does not have to
create an OAuth app.

`passthrough_headers` appears on nearly every connector. It is a comma-separated
list of headers forwarded verbatim from the caller (e.g.
`Authorization, X-Tenant-Id`). Leave it empty unless the target system does
per-caller authorization.

## STDIO connectors

One registry entry (`serper-search`) has `transport: "STDIO"`: instead of a URL
it runs a local process, and its credentials come from `env_schema` rather than
headers:

```json
"env_schema": [{"key":"SERPER_API_KEY","label":"Serper API Key","required":true,"secret":true}]
```

`POST /connectors/gateways` has no credential variant for this, so a STDIO
connector **cannot be made into a shared connection**. It can only be registered
directly against one agent:

```bash
bud api POST /prompts/$AGENT_ID/connectors/serper-search/register -d '{
  "credentials": {"SERPER_API_KEY": "..."}, "version": 1, "permanent": true
}'
```

Every required `env_schema` key must be present or the call fails validation.

## The legacy per-agent register route

`POST /prompts/{agent_id}/connectors/{connector_id}/register` predates shared
connections. It creates a **dedicated** connection owned by one agent version,
named `{agent_id}__v{n}__{connector_id}`, with its own copy of the credentials.

Use it only for STDIO connectors, or when the user explicitly wants credentials
that no other agent can reach. Otherwise prefer configuring a shared connection
and attaching tools - one credential, many agents, one place to revoke.

Known failure: it returns 400 *"Allow Multiple Calls must be enabled for MCP tool
usage"* when the agent's existing configuration has multiple tool calls turned
off. Fix that on the agent first (see `bud-agents`); the attach path sets it
automatically, the register path does not.

Its inverse is `DELETE /prompts/{agent_id}/connectors/{connector_id}/disconnect`,
which deletes the connection as well as unbinding it. That is correct for a
dedicated connection and catastrophic for a shared one.

## Registry ids vs everything else

`connector_id` means three different things depending on where it appears, and
mixing them returns empty results rather than errors:

| Where | What it is | Example |
|---|---|---|
| `/connectors/registry/{id}`, `/prompts/connectors/{id}` | registry slug | `github` |
| `/prompts/tools?connector_id=`, `/prompts/prompt-config/add-tool`, `.../disconnect` | the agent-config key: the connection **slug** for attached connections, the registry slug for registered ones | `linear` |
| generated connections | a Bud-minted uuid | `d9f85626-...` |

Get a connection's slug from `GET /connectors/gateways/{gateway_id}` →
`.gateway.slug`.

## Pre-tagging connections

A connection created before the tagging scheme existed will not appear in
`/connectors/configured`. Backfill it:

```bash
bud api POST /connectors/configured/$GW/tag -d '{"connector_id": "github"}'
```

This adds the identifying tags without touching scope, so the connection still
needs a scope set afterwards before agents can see it.
