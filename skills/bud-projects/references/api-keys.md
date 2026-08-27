# Project API keys

A project API key is a project-scoped credential for sending inference traffic.
It is created here; it is *used* per `bud-inference`.

## The limitation you must know first

```bash
bud api POST /credentials/ -d '{"name":"agent-key","project_id":"<id>","expiry":90}'
```

The response contains `result.key` - but **that value is not the key**. It is
the key encrypted with the installation's public key and hex-encoded (512 hex
characters). Decrypting it needs the installation's private key, which the
console holds and the API never returns. There is no endpoint that reveals the
plaintext.

Consequences:

- A key created through the API **cannot be used by the process that created
  it**. The failure appears much later, as `401 Invalid API key` at the
  inference gateway.
- For automation, use `bud token` instead - it derives a working bearer token
  from your session. See `bud-inference`.
- Create keys through the console when a human needs a long-lived key they can
  copy; the console decrypts it in the browser at creation time.

## Options worth setting

| Field | Values | Why it matters |
|---|---|---|
| `expiry` | `0` (never), `30`, `60`, `90` days | anything other than these four is rejected |
| `max_budget` | number | hard spend ceiling; the key stops working when reached |
| `model_budgets` | `{model: amount}` | per-model ceilings within the key |
| `ip_whitelist` | list of addresses | restricts where the key can be used from |
| `credential_type` | `client_app` \| `admin_app` | `client_app` is end-user traffic |

Prefer a bounded `expiry` and a `max_budget` for any key an automated system
will hold. They are the only blast-radius controls a key has.

## Managing keys

```bash
bud api GET /credentials/ -q project_id=<project-id>     # list (no secrets shown)
bud api PUT /credentials/<credential-id> -d '{"max_budget": 100}'
bud api DELETE /credentials/<credential-id>              # revoke immediately
```

Rotation is create-then-revoke: issue the new key, move traffic, then delete
the old one. There is no in-place rotation.

## Timing

A newly created key may not see very recently created deployments for up to a
minute - the gateway caches each project's model map. If a brand-new key
reports an unknown model, wait and retry before assuming a configuration error.
