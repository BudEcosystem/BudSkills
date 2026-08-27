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
  "tags": [{"name":"hr","color":"#4F8EF7"}]
}'
```

The id is at `.project.id` (responses nest - see `bud-platform`).

Then keep it around: `export BUD_PROJECT_ID=<id>`.

**Two project kinds exist**, and you do not choose directly - the platform
decides from who you are:

| Kind | Meaning |
|---|---|
| `admin_app` | internal workspace; where models get deployed and operated |
| `client_app` | customer-facing workspace that end-user API keys and apps hang off |

Administrators creating a project get `admin_app` unless they ask for
`client_app` explicitly; non-admin users always get `client_app`. If you are
building an app for end users to consume, say so - it needs `client_app`.

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

- `references/permissions.md` - the full scope list, how roles map to scopes,
  and how to debug a 403
- `references/api-keys.md` - key options, budgets, rotation, and the
  encryption limitation
- `references/audit.md` - record shape, filters, and integrity verification

## Where to go next

With a `project_id` in hand: `bud-models` to get a model, `bud-deployments` to
serve it, `bud-agents` to build on it, `bud-inference` to call it.
