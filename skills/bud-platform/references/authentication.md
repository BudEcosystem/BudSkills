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
PKCE). There is no password endpoint - `POST /auth/login` is permanently
retired and answers 410.

The toolkit completes the redirect handshake over plain HTTP, so no browser and
no extra packages are needed:

```bash
export BUD_API_URL=https://app.example.bud.studio
export BUD_EMAIL=you@example.com
export BUD_PASSWORD='...'
bud login
```

What that does: starts the flow at `/auth/redirect/authorize`, follows the
redirect to the identity provider, submits the credentials to the form target
embedded in its sign-in page, and follows the callback back to Bud, which mints
the session. The cookie jar is saved to `~/.bud/session-<profile>.txt`
(mode 0600).

### Things that will bite you

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
  compliance, hardware MFA, WebAuthn). If sign-in lands on an unexpected
  screen, the toolkit says so - ask the user to sign in through the console
  rather than trying to defeat it.

## CSRF

Mutating requests (`POST`/`PUT`/`PATCH`/`DELETE`) must echo the CSRF cookie in
an `x-csrf-token` header and carry a same-site referer. The toolkit does this
automatically. Hand-written `curl` that only sends the session cookie gets 403
with a CSRF message - use `bud api` instead.

## Session expiry

Sessions are renewed transparently: on a 401 the toolkit signs in once and
replays the request. For that to work non-interactively, `BUD_EMAIL` and
`BUD_PASSWORD` must remain in the environment. Without them you get a clear
"run bud login" error instead of a hang.

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
