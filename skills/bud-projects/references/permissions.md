# Permissions: scopes, roles, and debugging a 403

Bud authorises on **two independent levels**. Almost every confusing 403 comes
from satisfying one and not the other.

## The two levels

**Global scopes** apply across the whole installation and are set per user:

```bash
bud api PUT /permissions/<user-id>/global -d '{
  "permissions": [
    {"name":"project:view","has_permission":true},
    {"name":"model:manage","has_permission":true}
  ]
}'
```

Common global scopes follow a `<resource>:<action>` shape, with `view` and
`manage` as the actions:

| Resource | Meaning of `manage` |
|---|---|
| `project` | create, edit and delete projects; `view` is needed even to *list* them |
| `model` | add, edit, quantize and remove models |
| `cluster` | onboard, configure and delete clusters |
| `endpoint` | create and operate deployments |
| `user` | invite, edit and deactivate users |
| `benchmark` | run benchmarks and evaluations |

`manage` does not always imply `view` in the data model - grant both.

**Project scopes** apply to one project and are limited to two values:
`endpoint:view` and `endpoint:manage`. Anything else is rejected with 422.

```bash
bud api PATCH /permissions/project -d '{
  "user_id": "<uuid>", "project_id": "<uuid>",
  "permissions": [
    {"name":"endpoint:view","has_permission":true},
    {"name":"endpoint:manage","has_permission":false}
  ]
}'
```

This call is idempotent and is the right tool for changing an existing member.

> Setting every permission to `false` for a member **removes them from the
> project**. To express "read only", grant `endpoint:view` explicitly.

## How roles fit in

A user has a role (`super_admin`, `admin`, `developer`, `devops`, `tester`) and
a type (`admin` or `client`). Roles seed a default scope set at creation; they
do not override scopes afterwards. So the reliable question is never "what is
their role" but "what scopes do they actually have".

Note that self-registration always produces a `client`-type user regardless of
what the request asks for - privilege cannot be escalated at signup.

## Debugging a 403 in order

```bash
# 1. Who am I, really?
bud whoami

# 2. What scopes do I actually hold?
bud api GET /users/me/permissions

# 3. Am I a member of the project in question?
bud api GET /permissions/<my-user-id>/projects

# 4. Who are the members of that project, and with what scopes?
bud api GET /projects/<project-id>/users -q page=1 -q limit=50
```

Then match against the two levels:

| Symptom | Usual cause |
|---|---|
| 403 listing or reading *any* project | missing global `project:view` |
| Can list projects, 403 on one project's contents | not a member of that project |
| Can read a project, 403 creating a deployment in it | missing project `endpoint:manage` |
| Administrator still 403 | membership is not implied by role - add yourself |
| 403 mentioning CSRF | the request bypassed the toolkit; use `bud api` |

Being an installation administrator does **not** make you a member of every
project. This surprises people constantly.

## Granting access to a new person, end to end

```bash
# 1. Invite + add to the project in one call (they get an invitation email)
bud api POST /projects/<project-id>/add-users -d '{
  "users":[{"email":"new@example.com","scopes":["endpoint:view"]}]
}'

# 2. Give them the global scope needed to see projects at all
bud api PUT /permissions/<their-user-id>/global -d '{
  "permissions":[{"name":"project:view","has_permission":true}]
}'
```

Step 2 is the one people forget; without it the new member gets 403 everywhere
and it looks like the invitation failed.
