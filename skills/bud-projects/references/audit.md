# Audit trail

Every state-changing action is recorded with the actor, the resource, a
timestamp and the details of the change. Records carry an integrity hash, so
tampering is detectable.

## Querying

```bash
# everything that happened to one project
bud api GET /audit/records -q resource_type=project -q resource_id=<project-id>

# what one person did in a window
bud api GET /audit/records -q user_id=<user-id> \
  -q start_date=2026-08-01 -q end_date=2026-08-31

# aggregate counts by action and resource type
bud api GET /audit/summary -q start_date=2026-08-01 -q end_date=2026-08-31

# one record, with the actor resolved to a name and email
bud api GET /audit/records/<audit-id>
```

Useful filters: `resource_type`, `resource_id`, `user_id`, `action`,
`start_date`, `end_date`, plus the usual `page`/`limit`. Results are paged -
use `--paginate` when you need the whole set.

Common `resource_type` values mirror the platform's objects: `project`, `user`,
`model`, `endpoint`, `cluster`, `credential`, `prompt`. Common actions include
`create`, `update`, `delete`, `login`, `login_failed`.

## Integrity verification

```bash
bud api GET  /audit/records/<audit-id>/verify   # one record
bud api POST /audit/verify-batch -d '{"audit_ids":["<id>","<id>"]}'
bud api GET  /audit/find-tampered               # scan recent records
```

A verification failure means the stored record no longer matches its hash.
Treat it as a security incident and report it with the record id - do not
attempt to "fix" it.

## Answering common questions

| Question | Query |
|---|---|
| Who deployed this model? | `resource_type=endpoint`, `resource_id=<endpoint-id>` |
| Who was added to this project and when? | `resource_type=project`, `resource_id=<project-id>` |
| Did anyone try and fail to sign in? | `action=login_failed` over a window |
| What did this contractor touch last month? | `user_id=<id>` + date range |

## Access

On many installations any administrator can read the entire trail, and reads
are not themselves narrowly restricted. Treat exported audit data as sensitive:
it contains user emails and resource names. Do not paste it into shared
channels without checking.
