# Credentials for inference

The gateway accepts one thing: `Authorization: Bearer <token>`. What differs is
where the token came from and what it can see.

## The four kinds of credential

| Kind | How you get it | Lifetime | Sees |
|---|---|---|---|
| **Session token** (`bud token`) | your signed-in session | ~1 hour | your projects' `running` deployments |
| **End-user application key** | created in a customer-facing project | up to 90 days or never | its project's deployments **plus every published deployment** |
| **Internal workspace key** | created in an internal project | up to 90 days or never | its project's deployments only |
| **Ephemeral agent token** | minted by the platform for an agent run | ~30 minutes | its project, no key record |

For anything you are automating, use `bud token`. The other kinds exist and
matter for understanding reachability, but only the first is obtainable and
usable end to end from an API client.

## How `bud token` works, and why it fails

Two calls, and the second is the one people miss:

```
GET  /auth/redirect/ws-token          # mint a token for this session
POST /playground/initialize-with-token # register it with the gateway
```

The gateway does **not** validate tokens. It hashes what you send and looks the
hash up in a registry that the second call writes. So:

- A token that was minted but never registered is rejected as
  `401 {"error":{"message":"Invalid API key"}}`, indistinguishable from garbage.
- Registration is bound to the exact token string. Re-authenticating produces a
  new token and the old registration does not carry over - **run `bud token`
  again after any refresh**.
- The registration expires with the token, about an hour out. There is no
  renewal call; re-mint.
- Registration is effectively instant. If `GET /v1/models` still 401s more than
  about five seconds after `bud token` succeeded, something is wrong - do not
  keep retrying past ~5s.

`bud token --no-register` exists for inspecting the token; the result will not
work for inference.

## Project API keys: the encryption limitation

```bash
bud api POST /credentials/ -d '{"name":"agent-key","project_id":"<id>","expiry":90}'
```

`result.key` is **the key encrypted with the installation's public key**, hex
encoded, 512 characters. No endpoint returns the plaintext; the console decrypts
it in the browser at creation time. Without the installation's private key you
cannot recover it, and the failure does not appear until the gateway rejects it
as `401 Invalid API key`.

Practical consequences:

- Never hand a freshly created key to a process and expect it to work.
- If a human needs a long-lived key, have them create it in the console and
  paste it to you; then it behaves exactly like `bud token` output, but lasts.
- To verify a plaintext key you have been given, call
  `GET /playground/deployments` with header `api-key: <plaintext>`. A `400
  Invalid API key` there means the key has no record. Note that session tokens
  and ephemeral agent tokens legitimately fail this check while working fine at
  the gateway - it is a key-record lookup, not an auth check.

Options that are worth setting at creation, because they are the only
blast-radius controls a key has: `expiry` (`0`, `30`, `60` or `90` days -
anything else is rejected), `max_budget`, `model_budgets`, `ip_whitelist`.
`credential_type` must match the project's own type (`client_app` keys only in
customer-facing projects, `admin_app` only in internal ones) or you get a 400 -
and it is overridden anyway for non-administrator users.

Rotation is create-then-revoke: issue the new key, move traffic, delete the old
one with `DELETE /credentials/<id>`. There is no in-place rotation.

> `GET /credentials/` silently filters by *your* user type when you do not pass
> `credential_type`, and hides keys belonging to benchmark projects. A key you
> created can therefore appear to be missing. Pass the filter explicitly before
> concluding anything.

## What each credential can reach

The alias list attached to a credential is built from the **`running`
deployments of its project**, plus that project's adapters, guardrail profiles
and routers.

- **Publishing does not widen an internal key's view.** Only end-user
  application keys receive the published-catalog merge. A `bud token` or an
  internal workspace key will never see another project's deployment, published
  or not.
- **A newly created key can be up to 60 seconds stale.** The alias list is
  memoised per project. A key minted seconds after a deployment finished may
  `404 Model not found` on the new alias briefly. Wait and retry before
  treating it as a configuration error.
- Only `running` deployments are included. A deployment that is `deploying`,
  `unhealthy` or `failure` is simply absent from `/v1/models`.

## Pointing an existing tool at Bud

Anything OpenAI-compatible needs two settings and nothing else:

| Tool | Setting |
|---|---|
| OpenAI SDK, any language | `base_url = https://gateway.<domain>/v1`, `api_key = $(bud token)` |
| LangChain, LlamaIndex and similar | their OpenAI-compatible provider, same two values |
| Tools that read the environment | `OPENAI_BASE_URL` and `OPENAI_API_KEY` |
| curl | `-H "Authorization: Bearer $(bud token)"` |

`model` is always a deployment name from `GET /v1/models`. If the tool insists
on a known OpenAI model id, it will 404 - there is no aliasing of `gpt-4o` and
friends onto Bud deployments.

## Long-running agents

Ephemeral agent tokens are minted with a fixed lifetime of around 30 minutes and
**there is no mid-run refresh**. A run longer than that fails partway with
`401 Invalid API key` - the classic symptom is an agent that works in testing
and dies an hour into a production job. Design long runs to re-acquire a
credential between steps rather than holding one for the whole run.

The same applies to your own code holding a `bud token` result: catch `401`,
re-mint, retry once, and only then report failure.

## Never do these

- Do not log, echo or commit a token. It is a bearer credential for real
  compute and real spend.
- Do not create keys "to test" in a shared project; they are chargeable and easy
  to lose track of. Use `bud token`.
- Do not embed a gateway token in a browser or mobile client. Front it with your
  own service, or issue a scoped end-user application key with an `ip_whitelist`
  and a `max_budget`.
