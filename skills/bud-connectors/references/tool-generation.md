# Generating tools from an API

When a system has no registry connector and does not speak MCP, Bud can *write*
tools for it from an OpenAPI specification (or, where enabled, from
documentation pages). The result is an ordinary connection you attach to agents
like any other.

## Always start at the capability gate

```bash
bud api GET /connectors/capabilities
```

```json
{"enabled": true, "llm_available": false,
 "sources": {"openapi": true, "apidoc": false},
 "max_tools_per_source": 60, "max_upload_bytes": 20971520,
 "preview_mode": true, "push": true}
```

(Values above are from the installation I verified; read them, do not assume.)

| Field | Why you care |
|---|---|
| `enabled` | false → every `/connectors/generate/*` route returns **404**, not 403 |
| `sources.openapi` / `sources.apidoc` | documentation scraping needs a configured language model and is frequently off |
| `llm_available` | when false, `apidoc_*` sources are unusable |
| `max_tools_per_source` | selecting more than this at confirm time is a 422 |
| `max_upload_bytes` | a bigger file is a 413 |
| `preview_mode` | **changes what candidate ids and job ids mean** - branch on it |

Never probe a generate route to test availability - a 404 there is
indistinguishable from a bad path. Probe capabilities.

## Start a generation

From a URL:

```bash
bud api POST /connectors/generate/url -d '{
  "name": "HRIS API",
  "description": "Employee lookup",
  "url": "https://hris.example.com/openapi.json",
  "source_type": "openapi_url",
  "api_base_url": "https://hris.example.com/v2",
  "auth": {"auth_type": "bearer", "token": "..."},
  "scope": {"enabled": true, "global": false, "projects": ["<project-uuid>"]}
}'
```

Returns `202` with `{job_id, workflow_id, upstream_job_id, connector_id, status}`
flat at the top level.

- `source_type`: `openapi_url` (default) or `apidoc_url`.
- `api_base_url` overrides the spec's own `servers` entry. Specs that declare
  `http://localhost` or a relative path need this or every generated tool points
  at nothing.
- `enable_crawling` (apidoc only, default true) follows links from the page.
- URLs are validated against server-side request forgery, so private addresses
  are rejected.

From a file: `POST /connectors/generate/upload` takes `multipart/form-data`, which
`bud api` does not send (it only carries JSON bodies). **Prefer the URL form
whenever the spec is reachable over http(s)** - serve a local file from a
temporary web server rather than uploading it, and you stay inside the toolkit.

If you genuinely must upload, the form fields are `file`, `name`, `description`,
`source_type` (`openapi_upload` | `apidoc_upload`), `api_base_url`,
`format_hint` (`auto|openapi|markdown|html|pdf|text`), plus **`auth` and `scope`
as JSON-encoded strings** - a form field cannot hold an object, and sending one
un-encoded is a 422. An empty file is a 400.

### Authentication for generated tools

`auth` is applied to every generated tool and is never stored or logged by Bud.

| `auth_type` | Fields |
|---|---|
| `basic` | `username`, `password` |
| `bearer` | `token` |
| `apikey` | `header_key`, `header_value` |

`oauth2` is **rejected**, deliberately: it would be silently dropped and you
would end up with tools that call the API unauthenticated.

## Watch the job

```bash
bud api GET /connectors/generate/jobs/<job_id> | jq '.job | {status, progress, progress_message}'
```

Poll every 2-5s. OpenAPI generation typically takes 10s-3min; documentation
crawling takes longer. The response nests under `.job`.

When `capabilities.preview_mode` is true (where polling is safe), let the toolkit
do the waiting:

```bash
bud wait resource /connectors/generate/jobs/<job_id> \
  --field job.status --equals completed \
  --fail-on failed,cancelled --interval 3 --timeout 900
```

Pass `--equals` explicitly: the default success set counts `running` as done, so
omitting it returns immediately. Keep `interrupted` out of `--fail-on` (see
below), and do not use this loop in legacy mode, where repeated reads are
destructive.

`status` is one of `pending`, `running`, `completed`, `failed`, `cancelled`,
`interrupted`.

> **`interrupted` is not terminal.** It is Bud's own retryable status, raised
> when the upstream generator's stale-job sweep spuriously fails a job that is
> still alive. Keep polling, or re-run; do not report it as a failure.

`GET /workflows/{workflow_id}` shows the same step sequence and is an alternative
progress view. Steps come from a fixed spine per source type - roughly
fetch → parse → extract → store → results, with `crawl` inserted for
documentation sources and `parse` absent from `apidoc_url`.

> **`bud wait job` does not work for generation.** Generation records are
> internal and never appear in the job listing that `bud wait job` and `bud jobs`
> read - on the installation I checked, 504 listed jobs contained none of them.
> Poll the job route above instead.

> **Under legacy mode (`preview_mode: false`) polling is destructive.** Reading a
> job that has reached a terminal non-success state reaps the pending connection,
> and continuing to poll turns one dead job into a 404 storm. Read the status
> once, branch, and stop. In preview mode the read is safe and idempotent.

## Choose from the candidates

On `completed`, read `job.result`:

```json
{"outcome": "preview", "batch_id": "...", "message": "...",
 "tools": [{"id": "c0000", "name": "get_employee", "display_name": "Get Employee",
            "description": "...", "method": "GET", "path": "/employees/{id}",
            "auth_required": true, "auth_configured": true, "group": "employees"}],
 "failed": [{"name": "...", "error": "...", "path": "...", "method": "..."}],
 "api_info": {...}, "source_ref": {...}, "auth_summary": {...}}
```

| `outcome` | Meaning |
|---|---|
| `preview` | preview mode - candidates await confirmation |
| `success` | legacy mode - candidates await confirmation |
| `empty` | nothing extractable from the source; stop and tell the user why |

**The `id` field means different things per mode**, which is why you read
`capabilities.preview_mode` first:

| `preview_mode` | `job_id` is | candidate `id` is |
|---|---|---|
| `true` | Bud's own job id (same value as `workflow_id`) | a candidate key like `c0000` |
| `false` | the upstream generator's job id | a real tool id |

Either way you pass them back as `candidate_keys`. Also check `result.failed[]` -
endpoints the generator could not turn into tools, with reasons worth relaying.

Filter before confirming. A large spec can yield hundreds of candidates; the
per-source cap (60 on the installation I checked) is a hard limit, and an agent
does not want 60 tools anyway. `method`, `path` and `group` are the useful
filters.

## Confirm - this is what creates the connection

```bash
bud api POST /connectors/generate/jobs/<job_id>/finalize -d '{
  "candidate_keys": ["c0000", "c0003", "c0011"],
  "name": "HRIS API",
  "scope": {"enabled": true, "global": false, "projects": ["<project-uuid>"]}
}'
```

Returns flat: `{gateway_id, connector_id, batch_id, created_tool_ids, tool_count,
failed_tools?}`. Nothing exists before this call - abandoning a generation leaves
no connection behind (unconfirmed jobs are cleaned up automatically after about a
day, and the cached parse expires upstream).

The `gateway_id` is a normal connection handle from here on: it appears in
`GET /connectors/configured` with `is_generated: true` and attaches to agents
with the standard `attach-tools` call.

### Confirm-time errors

| Status | `error_code` | What to do |
|---|---|---|
| 422 | - | you selected more than `max_tools_per_source`; select fewer |
| 400 | `unknown_candidate_keys` | the response's `unknown` array lists them - usually preview/legacy id confusion |
| 410 | `parse_expired` | the cached preview aged out; **re-run the generation**, retrying will not help |
| 409 | - | another confirm holds a 5-minute lease; wait and retry |
| 502 | - | **ambiguous** - retry the same call, it is idempotent and will not duplicate |
| 502 | "created none of the selected tools" | the empty connection was unwound for you; investigate the spec |

A replayed successful confirm returns the stored result rather than creating a
second connection.

## Cancelling

```bash
bud api POST /connectors/generate/jobs/<job_id>/cancel
```

Safe on an already-finished job (that is not treated as an error). In legacy mode
it also removes the pending connection; in preview mode there is nothing to
remove.

## After it exists

Generated connections have no URL of their own - they are local tool
definitions. That has one consequence worth remembering:

> **Never send `transport` in a `PUT /connectors/gateways/{id}` to a generated
> connection.** It reverts the connection's kind and breaks it. If you need to
> rename or retag one, send only the fields you are changing.

To add more endpoints later, run a second generation against the same spec and
confirm the additional candidates - it produces a separate connection. There is
no "append to an existing generated connection" operation on this API.
