# Authorizing an OAuth connection

Roughly half the connector registry uses OAuth, and this is the only part of the
domain where an agent cannot finish the job alone. This page covers what is
automatable, the exact sequence, and every way it goes wrong.

## First: which flow is this?

```bash
bud api GET /connectors/registry/<connector_id> \
  | jq '.connector.credential_schema[] | select(.field=="grant_type")'
```

| `grant_type` | Human needed? | Why |
|---|---|---|
| `client_credentials` | **No** | Bud exchanges the client id and secret for a token directly. |
| `authorization_code` | **Yes** | The provider must show a consent screen to a signed-in person. |

`client_credentials` is a machine-to-machine flow: it authorizes *the
installation*, not a user, so the tools act as a service account. If the user
wants the agent to act as themselves (see only their Slack channels, their Linear
issues), only `authorization_code` will do that.

You choose the grant type at configure time by what you put in `credentials`. If
the provider supports both and the user does not care about identity, prefer
`client_credentials` - it removes the human step entirely.

## Two status endpoints - do not confuse them

```bash
bud api GET /connectors/$GW/oauth/status          # is OAuth CONFIGURED?
bud api GET /connectors/$GW/oauth/token-status    # is the CALLER AUTHORIZED?
```

`oauth/status` returns `oauth_enabled`, `grant_type`, `client_id`, `scopes`,
`authorization_url` and the installation's `redirect_uri`. It is useful for
reading the redirect URI to reuse and for confirming the grant type after the
fact. **It stays `true` forever once OAuth is set up** - it never answers "is
this connected".

`oauth/token-status` is the real check:

```json
{"connected": true, "token_type": "Bearer", "expires_at": "2026-08-06T17:36:07Z",
 "is_expired": false, "scopes": ["comments:create","issues:create","read","write"]}
```

Authorized means `connected == true` **and** `is_expired == false`. An
unauthorized connection returns `connected: false` with every other field null.

Both are flat responses - read `.connected`, not `.data.connected`.

## The authorization-code sequence

```bash
# 0. skip everything if already good
bud api GET /connectors/$GW/oauth/token-status | jq '.connected and (.is_expired|not)'

# 1. start the flow
bud api POST /connectors/$GW/oauth/initiate | jq '{authorization_url, state, expires_in}'

# 2. a human opens authorization_url in a browser and approves

# 3. poll until the token lands
until bud api GET /connectors/$GW/oauth/token-status | jq -e '.connected and (.is_expired|not)' >/dev/null
do sleep 3; done

# 4. discover tools - the automatic fetch usually does not run (see below)
bud api POST /connectors/$GW/fetch-tools
bud api GET  /connectors/$GW/tools -q limit=500
```

`initiate` accepts an optional `return_url` query parameter - where the user's
browser lands after approving. It is validated against an allow-list configured
on the installation: http(s) only, https required for anything that is not
localhost. A URL outside the allow-list is a **400 before the flow even starts**,
so omit it unless the user gave you one they know is allowed.

`expires_in` is the life of the authorization state, typically a few minutes. If
it elapses with `connected` still false, the person abandoned the flow - initiate
again for a fresh URL rather than resending the old one.

## What to say to the user

Be specific: name the provider, give the link, say what expires, and say what you
will do next.

> This connection uses OAuth, so I can't complete it for you - the provider needs
> a person to approve access in a browser.
>
> 1. Open: `<authorization_url>`
> 2. Sign in to <provider> as the account whose data the agent should see
> 3. Approve the requested permissions (<scopes>)
>
> The link expires in about <expires_in/60> minutes. I'll keep checking, and once
> it goes through I'll pull the tool list and attach the ones you need.

Two things worth flagging to the user while they decide:

- **Whose account matters.** The token is stored per user, and the agent runs on
  it. If a leaver authorized the connection, the agent's tools break when their
  access is revoked. For anything durable, suggest a service or shared account.
- **Scopes are permissions.** The connector's default scope set is often broad
  (the GitHub entry requests repo write, workflow, packages and more). If the
  agent only needs to read, say so - narrower scopes can be set in `credentials`
  at configure time, not after.

## Why a freshly authorized connection has zero tools

The provider redirects to a **public, unauthenticated** callback:
`GET /connectors/oauth/public-callback?code=...&state=...`. It exchanges the code
successfully, then tries to fetch the connection's tools - but with no user
session, so that fetch typically fails and is swallowed. The connection ends up
authorized with `tool_count: 0`.

Three ways to deal with it, in order of preference:

1. Call `POST /connectors/$GW/fetch-tools` yourself after the token appears. It
   runs with your session, which is what makes discovery work.
2. Just call `GET /connectors/$GW/tools`. It self-heals: an OAuth connection with
   zero tools triggers discovery with your session and re-reads.
3. If you can capture the `code` and `state` yourself (you controlled the
   redirect), skip the public callback and post them:
   `bud api POST /connectors/oauth/callback -d '{"code":"...","state":"..."}'`.
   The authenticated variant's automatic tool fetch actually succeeds.

## Per-user tokens and the two meanings of `oauth_connected`

Tokens are held per user per connection. Which is why the same field name means
different things on different listings:

| Listing | `oauth_connected` means |
|---|---|
| `GET /connectors/configured` | **you**, the calling user, are authorized |
| `GET /connectors/available` | **anyone** is authorized |

When you are deciding whether an agent can use a connection, `/available` is the
right question - the agent runs on the connection's shared token, so one
authorized user is enough. When you are deciding whether *you* can list tools
right now, `/configured` is the right question.

## Revoking

```bash
bud api DELETE /connectors/$GW/oauth/token                      # your own token
bud api DELETE /connectors/$GW/oauth/token/user@example.com     # someone else's
```

Revoking the last authorized user's token leaves the connection configured but
non-functional: agents keep their bindings and their tool calls start failing.
If the intent is "this agent should stop using Slack", detach the connection from
the agent instead.

> The second route is described as administrator-only but is **not permission
> gated on this API** - any authenticated user can revoke any other user's token
> for a connection. Treat it as a sharp edge: never call it speculatively, and
> when troubleshooting someone else's connection prefer asking them to
> re-authorize over revoking on their behalf.

## Per-agent OAuth (dedicated connections only)

Connections created by the legacy per-agent register route have their own
parallel OAuth routes, keyed by agent plus registry connector id instead of a
connection id:

```bash
bud api POST /prompts/oauth/initiate -d '{"prompt_id":"<agent>","connector_id":"github","version":1}'
bud api GET  /prompts/oauth/status -q prompt_id=<agent> -q connector_id=github -q version=1
bud api POST /prompts/oauth/callback -d '{"code":"...","state":"..."}'
bud api POST /prompts/oauth/fetch-tools -d '{"prompt_id":"<agent>","connector_id":"github","version":1}'
```

These only work for connections made with `/register`. For a shared connection
always use the `/connectors/{gateway_id}/oauth/*` family - the per-agent routes
cannot resolve it.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| 400 on `initiate` before any browser opens | `return_url` not in the installation's allow-list, or not https | drop `return_url` |
| `authorization_url` opens to a provider error page | `client_id`/`client_secret` wrong, or the redirect URI is not registered in the provider's OAuth app | fix at the provider, then reconfigure `credentials` |
| Approved, but `connected` stays false | the state expired before approval, or the callback never reached the installation | re-initiate; check the redirect URI matches `oauth/status` |
| `connected: true`, `tool_count: 0` | the public callback's tool fetch was unauthenticated | `POST .../fetch-tools` |
| 403 on `GET /connectors/{id}/tools` | the connection is disabled, or not exposed to this client | check `enabled`; a 403 here is never "not found" |
| Tools worked, now fail at run time | the authorizing user's token expired or was revoked | `token-status` → re-authorize |
| `is_expired: true` | refresh failed or was never granted | re-run initiate; the flow is the same |
