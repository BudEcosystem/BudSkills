---
name: bud-projects
description: Manage Bud Foundry projects - create a project, add or remove members, set per-member permissions, issue and revoke project API keys, and read the audit trail. Use when a Bud request mentions projects, workspaces, team members, access or roles, API keys/credentials, quotas and budgets, or "who changed what". Start here when any other Bud task needs a project_id.
---

# Bud Foundry: projects

A **project** is Bud's unit of tenancy. Deployments, agents, evaluations,
routers, triggers, guardrail profiles and API keys all belong to one, and
almost every other Bud API call takes a `project_id`. If a task does not have
one yet, getting one is the first step.

Prerequisite: connect first - see `bud-platform` (`bud login`).

## Get a project id

Reuse an existing project when the user names one; only create when they ask
for something new.

```bash
# find by name
bud api GET /projects/ --paginate | jq -r '.[].project | "\(.id)  \(.name)"'

# create
bud api POST /projects/ -d '{
  "name": "HR Assistant",
  "description": "HR question answering",
  "tags": [{"name":"hr","color":"#4F8EF7"}],
  "icon": "💼"
}'
```

The id is at `.project.id` (responses nest - see `bud-platform`).

Then keep it around: `export BUD_PROJECT_ID=<id>`.

**Always send an `icon`.** It is optional in the API, but a project created
without one shows a blank tile in the console - the web console never omits it,
so neither should you. Pick the icon with the rules in "Choosing a project
icon" below; when in doubt, use the globe `🌐` (the console's own default).

**Two project kinds exist**, and you do not choose directly - the platform
decides from who you are:

| Kind | Meaning |
|---|---|
| `admin_app` | internal workspace; where models get deployed and operated |
| `client_app` | customer-facing workspace that end-user API keys and apps hang off |

Administrators creating a project get `admin_app` unless they ask for
`client_app` explicitly; non-admin users always get `client_app`. If you are
building an app for end users to consume, say so - it needs `client_app`.

## Choosing a project icon

The `icon` field is a single string. The backend accepts two forms:

| Form | Example | Notes |
|---|---|---|
| **Emoji** | `"🌐"`, `"💼"`, `"🤖"` | the normal case; what the console's picker produces |
| **Static asset key** | `"icons/providers/openai.png"` | a path to a file that already exists under the server's static dir |

Anything else - a full `https://` URL, a `data:` URI, or a root-relative
`/images/...` path - **renders but fails validation the moment the project is
edited**, so never store those. When unsure whether a value is an asset key that
exists on the server, prefer an emoji.

> **Gotcha:** create (`POST`) does *not* validate `icon` today, but edit
> (`PATCH`) does - it must be an emoji on the platform's allow-list or an
> existing static file. Choosing a valid value up front avoids a project whose
> icon can never be changed without a 422.

**Pick the icon in this order:**

1. **Reuse the related entity's icon, if safe.** When the project is being
   created for/around a specific deployed agent, model or endpoint, read that
   entity's icon and reuse it **only if** it is an emoji or an existing static
   asset key. An agent's `a2a_card.icon_url` and many model icons are full URLs
   or `/public` paths - do **not** copy those; fall through to step 2 instead.
   A model/provider asset key such as `icons/providers/openai.png` is safe.
   (Endpoints have no icon of their own - use their model's: `endpoint.model.icon`.)

2. **Infer a fitting emoji from the project's purpose** (name, tags,
   description) from the safe set below. E.g. an HR project → `💼`, banking →
   `🏦`, a research project → `🔬`, an agent/bot workspace → `🤖`. If nothing
   fits, use `🌐`.

3. **Ask the user when a human is in the loop.** If the runtime exposes an
   interactive question tool (the bda-desktop agent exposes `ask_user_questions`),
   offer a short emoji menu plus a freeform option, and set the step 1/2 choice as
   the **recommended** default. Honor whatever they pick.

4. **Otherwise default to `🌐`** - identical to the console's default, so the
   tile is never blank.

**Safe emoji set** (all on the platform allow-list, survive edit-validation):

```
🌐 🤖 🚀 💡 📊 🔬 🧠 💬 🏦 🏥 ⚙️ 📚 🛡️ ⚖️ 📝 💼 🔭 📦 🔑 ✨
```

Full rules, the agent-icon reuse check, and a concrete `ask_user_questions`
example are in `references/icons.md`.

## Members and what they can do

Bud has permissions at **two levels**, and mixing them up is the usual cause of
a confusing 403.

| Level | Examples | Set with |
|---|---|---|
| Global (whole installation) | `project:view`, `project:manage`, `model:manage`, `cluster:manage` | `PUT /permissions/{user_id}/global` |
| Per project | `endpoint:view`, `endpoint:manage` | `PATCH /permissions/project` or at add-time |

Only `endpoint:view` and `endpoint:manage` are valid per-project scopes -
sending anything else returns 422.

> Membership alone does not grant access to the project APIs. A member still
> needs the **global** `project:view` scope to list or read projects at all.
> This is why a freshly added member can get 403 on everything.

```bash
# add members (existing user by id, or a new person by email - they get invited)
bud api POST /projects/$BUD_PROJECT_ID/add-users -d '{
  "users": [
    {"user_id": "<uuid>",          "scopes": ["endpoint:view"]},
    {"email":   "new@example.com", "scopes": ["endpoint:view","endpoint:manage"]}
  ]
}'

# list members with their effective permissions
bud api GET /projects/$BUD_PROJECT_ID/users -q page=1 -q limit=50

# change one member's project scopes (idempotent - prefer this for updates)
bud api PATCH /permissions/project -d '{
  "user_id": "<uuid>", "project_id": "'"$BUD_PROJECT_ID"'",
  "permissions": [{"name":"endpoint:manage","has_permission":true}]
}'
```

**`add-users` is all-or-nothing and not idempotent.** If any listed user is
already a member the whole batch fails with 400. Check membership first, or use
`PATCH /permissions/project` to adjust someone who is already in.

**`PATCH /permissions/project` with everything set to `false` removes the user
from the project.** That is a removal, not a downgrade - if you mean "read
only", grant `endpoint:view` explicitly.

Remove members with `POST /projects/{id}/remove-users`. Pass only ids you have
confirmed are members; a mixed list of member and non-member ids can fail
part-way.

## API keys

Project API keys are created here but are covered in depth by `bud-inference`,
because there is an important limitation: **the key returned by the API is
encrypted and cannot be used as-is.** For anything automated use `bud token`
instead. See `references/api-keys.md`.

```bash
bud api GET /credentials/ -q project_id=$BUD_PROJECT_ID   # list / audit keys
bud api DELETE /credentials/<credential-id>               # revoke
```

Set `expiry` (0/30/60/90 days), `max_budget` and `ip_whitelist` when a key is
issued - they are the only spend and blast-radius controls a key has.

## Audit trail

Every state change is recorded, and records are hash-chained so tampering is
detectable.

```bash
bud api GET /audit/records -q resource_type=project -q resource_id=$BUD_PROJECT_ID
bud api GET /audit/summary -q start_date=2026-08-01 -q end_date=2026-08-31
bud api GET /audit/records/<audit-id>/verify        # integrity check one record
bud api GET /audit/find-tampered                    # scan for hash mismatches
```

Use this to answer "who deployed this", "who was added to the project", and
compliance questions. Note that on many installations any administrator can
read the whole trail, so treat its contents as sensitive.

## Finding things

```bash
bud api GET /projects/ -q search=true -q name=hr        # fuzzy name search
bud api GET /projects/tags                              # every tag in use
bud api GET /projects/$BUD_PROJECT_ID/clusters          # clusters serving this project
```

Two search quirks worth knowing: `search=true` ignores a `project_type` filter,
and filtering for `client_app` returns only projects you are a member of even
if you are an administrator. If a project you expect is missing, list without
filters before concluding it does not exist.

## Deleting

```bash
bud api DELETE /projects/<project-id>
```

**Confirm with the user first, naming the project.** Deletion cascades to the
deployments, agents and keys inside it. It is a soft delete - the record is
marked deleted rather than erased - but the resources inside stop working.

Prefer removing members or revoking keys over deleting a shared project.

## Deeper reference

- `references/icons.md` - valid icon values, the reuse-an-agent-icon rule, and
  the human-in-the-loop picker pattern
- `references/permissions.md` - the full scope list, how roles map to scopes,
  and how to debug a 403
- `references/api-keys.md` - key options, budgets, rotation, and the
  encryption limitation
- `references/audit.md` - record shape, filters, and integrity verification

## Where to go next

With a `project_id` in hand: `bud-models` to get a model, `bud-deployments` to
serve it, `bud-agents` to build on it, `bud-inference` to call it.
