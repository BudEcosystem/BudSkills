# Authentication and access

Bud separates **managing the platform** from **sending traffic to it**, and the
two use different credentials. Picking the wrong one is the most common cause of
a 401 that "should" work.

| | Management API | Inference gateway |
|---|---|---|
| Host | `app.<domain>` | `gateway.<domain>` |
| Credential | signed-in user session (`bud login`) | bearer token (`bud token`) |
| Use for | projects, models, clusters, deployments, agents, evaluations | chat completions, embeddings, responses |
| Lifetime | hours, renewed automatically | ~1 hour, re-mint with `bud token` |

Both come from the same sign-in. `bud login` establishes the session; `bud token`
converts that session into a gateway bearer.

There is also a **project API key**, but it has a real limitation that makes it
unsuitable for automation - see "Project API keys" below. Prefer `bud token`.

## Signing in

Bud uses browser-style single sign-on (OpenID Connect, authorization code with
PKCE). The old password endpoint (`POST /auth/login`, "resource owner password
credentials") has been removed on current installations - calling it returns an
error whose message is *"Password login is disabled. Use the OIDC redirect
flow."* There are two ways to sign in headlessly; the toolkit picks whichever
fits your installation.

### Option A - email + password (installations that still allow it)

Some installations keep a password form on their identity provider. The toolkit
drives the redirect handshake over plain HTTP, so no browser is needed:

```bash
export BUD_API_URL=https://app.example.bud.studio
export BUD_EMAIL=you@example.com
export BUD_PASSWORD='...'
bud login
```

This fails on **OIDC-only installations** - ones that disable password login, or
require MFA, a consent screen, or a corporate identity provider. There is no
password form to submit, so `bud login` reports it could not find the sign-in
form or that no session was issued. Use Option B instead.

### Option B - reuse a bearer token (OIDC-only installations)

The management API accepts a bearer token (`Authorization: Bearer <jwt>`) on
every endpoint, so the toolkit can authenticate by **reusing a token that a real
browser login already minted** - no password, no scraping. When any token source
below is present, the toolkit uses it automatically (for every command, not just
`bud login`) and refreshes it as it expires.

Token sources, auto-detected; the freshest (latest-expiring) one wins, so a
background refresh by the desktop app is picked up automatically:

| Source | How to provide it |
|---|---|
| **Desktop app** | Sign in through the Bud Studio desktop app. It stores the token in `auth.json`, which the toolkit finds automatically (override with `BUD_DESKTOP_AUTH_FILE`). |
| **Environment** | `export BUD_ACCESS_TOKEN=<jwt>` (and `BUD_REFRESH_TOKEN=<jwt>` so it can refresh itself). |
| **Token file** | `export BUD_TOKEN_FILE=/path/to/token.json` - a JSON file with `accessToken`/`refreshToken`/`expiresAt` (or a bare token string). |

```bash
export BUD_API_URL=https://app.example.bud.studio
# then just sign in through the desktop app, or export BUD_ACCESS_TOKEN
bud login     # validates the token and prints who you are
```

The toolkit refreshes the access token via `POST /auth/refresh-token` and caches
the rotated pair under `~/.bud/token-<profile>.json` (mode 0600). When the
**refresh token** itself expires you get a `401 "Token Expired or Invalid"` - the
only fix is to sign in again through the desktop app (which re-mints the token),
then re-run the command. **Ask the user to do this; do not try to work around
it.**

> A bearer token obtained this way does not need a CSRF header - that is a
> cookie-session construct - so mutating calls work the same way.

### When the session dies mid-task - device sign-in

If the token is revoked or its refresh token expires while you are working, the
toolkit cannot refresh on its own. On an OIDC-only install there is no password to
submit, so re-auth uses the **OIDC device authorization grant** - the toolkit
drives it; you only surface the code. **Any** command that fails for an auth
reason (a 401, or a refresh that cannot complete) does two things:

- exits with the distinct code **`77`** (ordinary failures exit `1`), and
- prints the sentinel **`__BUD_AUTH_REQUIRED__`** on stderr.

When you see that (sentinel or exit `77`), run the device flow and show the user
the code with a generic artifact - do **not** collect a password, and do not
expect a dedicated login tool (the UI is a normal artifact):

```bash
# 1. Start device sign-in. Prints JSON with the verification URL and user code.
bud login --device
#   -> {"verification_uri_complete":"https://auth.<domain>/realms/<realm>/device?user_code=WXYZ-1234",
#       "verification_uri":"https://auth.<domain>/realms/<realm>/device",
#       "user_code":"WXYZ-1234","expires_in":600}
```

2. **Render a generic `create_artifact` card** showing the code and a **"Sign in"**
   button that opens `verification_uri_complete` (it's an `https://` URL, so an
   `@OpenUrl` button works with the stock components - no custom tool). A suitable
   intent:

   > "A small 'Sign in to Bud' card. Body: 'Your session expired. Click Sign in
   > (or go to &lt;verification_uri&gt; and enter code &lt;user_code&gt;) to continue.' A
   > primary button labelled 'Sign in' that opens the URL
   > &lt;verification_uri_complete&gt;."

```bash
# 3. Block until the user approves in their browser, then cache the token.
bud login --device --wait
# 4. Re-run the command that originally failed — it now reads the fresh token.
```

On installs that still allow it you can fall back to `bud login` (password /
bearer). Outside a UI (plain Claude Code / CI), just run `bud login --device`,
show the user the printed URL + code, then `bud login --device --wait`.

> Requires the identity provider to expose a **`bud-cli`** public client with the
> device grant enabled (an infra/realm config change). Set `BUD_OIDC_ISSUER` if the
> issuer can't be derived from an existing token, and `BUD_OIDC_CLIENT_ID` to
> override the default `bud-cli`. Never put a password field in any artifact -
> values submitted from an artifact are persisted into the transcript.

What Option A does: starts the flow at `/auth/redirect/authorize`, follows the
redirect to the identity provider, submits the credentials to the form target
embedded in its sign-in page, and follows the callback back to Bud, which mints
the session. The cookie jar is saved to `~/.bud/session-<profile>.txt`
(mode 0600).

### Things that will bite you (Option A)

- **A wrong password is not an HTTP error.** The identity provider re-renders
  its sign-in page with HTTP 200 and the reason inside it. The toolkit parses
  that out and reports it. Do not treat 200 as success elsewhere in this flow.
- **Do not retry password guesses.** Providers lock accounts after a few
  failures, which locks the human user out too. Fail loudly and ask.
- **`return_url` must be a path**, never an absolute URL - absolute values are
  rejected outright as open-redirect attempts.
- **The console origin matters.** The handshake sends the console origin so the
  installation knows which application the session is for. The toolkit derives
  it from `BUD_API_URL` (`app.` -> `admin.`); set `BUD_UI_URL` when your
  installation names hosts differently.
- **Corporate identity providers may demand a real browser** (device
  compliance, hardware MFA, WebAuthn), and OIDC-only installations disable the
  password form entirely. If sign-in lands on an unexpected screen or reports it
  found no form, switch to **Option B** - have the user sign in through the Bud
  Studio desktop app, then let the toolkit reuse that token. Do not retry
  password variations.

## CSRF

Mutating requests (`POST`/`PUT`/`PATCH`/`DELETE`) must echo the CSRF cookie in
an `x-csrf-token` header and carry a same-site referer. The toolkit does this
automatically. Hand-written `curl` that only sends the session cookie gets 403
with a CSRF message - use `bud api` instead.

## Session expiry

Expiry is handled transparently: on a 401 the toolkit renews once and replays
the request.

- **Option A (password):** `BUD_EMAIL` and `BUD_PASSWORD` must remain in the
  environment so the re-sign-in is non-interactive. Without them you get a clear
  "run bud login" error instead of a hang.
- **Option B (bearer):** the access token is refreshed via `POST
  /auth/refresh-token` using the refresh token, and the rotated pair is cached
  under `~/.bud/token-<profile>.json`. When the refresh token *itself* expires
  the renewal fails with `401 "Token Expired or Invalid"` - have the user sign
  in again through the desktop app, then retry.

## Calling deployed models (inference auth)

Inference does **not** go through the management API, and the session cookie is
not accepted there. Use a bearer token:

```bash
export BUD_GATEWAY_TOKEN=$(bud token)
curl https://gateway.<your-domain>/v1/chat/completions \
  -H "Authorization: Bearer $BUD_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<deployment-name>","messages":[{"role":"user","content":"hi"}]}'
```

`bud token` does two things, and the second is the one people miss:

1. mints a bearer token from your signed-in session, and
2. **registers it with the inference gateway.**

The gateway does not validate tokens itself - it looks them up in a registry.
An unregistered token is rejected as `401 Invalid API key`, which reads like a
bad credential rather than a missing registration step. Re-run `bud token`
after the token expires (roughly hourly); registration does not survive it.

The `model` field must be the **deployment name**, not the underlying model
name. List valid values with `GET /v1/models` on the gateway, or
`bud api GET /playground/deployments`.

## Project API keys - and their significant limitation

```bash
bud api POST /credentials/ -d '{"name":"agent-key","project_id":"<id>","expiry":90}'
```

> **The `result.key` this returns is not a usable key.** It is the real key
> encrypted with the installation's RSA public key and hex-encoded (512 hex
> characters). Turning it into a bearer token requires the installation's
> private key, which the console holds and the API never exposes. **No endpoint
> returns the plaintext.**

So for anything automated, prefer `bud token`. Reach for a project API key only
when a human can supply the plaintext out of band (for example by creating the
key in the console, which decrypts it client-side).

Options accepted at creation, which are worth setting when a key is issued:

| Field | Effect |
|---|---|
| `expiry` | days until auto-expiry: `0` (never), `30`, `60`, `90` |
| `max_budget` | hard spend ceiling for the key |
| `model_budgets` | per-model spend ceilings |
| `ip_whitelist` | restrict the key to given source addresses |
| `credential_type` | `client_app` (default, end-user traffic) or `admin_app` |

List with `GET /credentials/`, revoke with `DELETE /credentials/{id}`.

A newly created key can take up to a minute to see very recent deployments, as
the gateway caches each project's model map.

See `bud-inference` for streaming, embeddings, and the rest of the surface.

## Provider credentials

Keys for third-party model providers (for hosted commercial models) are stored
separately under `/proprietary/credentials`. They are referenced by id when
importing or deploying a hosted model, and their secrets are never returned by
the API. See `bud-models`.

## Profiles and multiple installations

```bash
BUD_PROFILE=staging BUD_API_URL=https://app.staging... bud login
BUD_PROFILE=staging bud whoami
```

Each profile keeps its own session and cached API description. `~/.bud/config.json`
stores non-secret defaults (`api_url`, `ui_url`, `email`); secrets are never
written there.

## Non-interactive and CI use

Provide `BUD_API_URL`, `BUD_EMAIL`, `BUD_PASSWORD` from your secret store and
call `bud login` once at the start. Set `BUD_HOME` to a writable scratch
directory when the home directory is read-only:

```bash
export BUD_HOME="$RUNNER_TEMP/bud"
bud login && bud whoami
```

Never commit credentials, and never echo `BUD_PASSWORD` into logs. The toolkit
redacts secrets from its own diagnostic output.

## Who am I and what may I do?

```bash
bud whoami                              # identity and role
bud api GET /users/me/permissions       # effective permissions
bud api GET /permissions/<user-id>/projects   # per-project access
```

A 403 on an action that should be allowed is usually project membership, not
role: being an administrator of the installation does not automatically grant
membership of every project.
