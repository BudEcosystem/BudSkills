# Troubleshooting

Work top to bottom: confirm you are connected, then that you are looking at the
right thing, then that the platform is healthy.

```bash
bud health      # reachability, identity, and a real authenticated read
bud whoami      # who the session belongs to
```

## Connection and sign-in

**`Could not reach <url>`** - wrong `BUD_API_URL`, or the host is unreachable.
`BUD_API_URL` must be the **API** origin (commonly `app.<domain>`), not the
console (`admin.<domain>`). Pointing it at the console yields HTML where JSON
is expected.

**`Could not start sign-in (400) ... invalid return_url`** - something passed an
absolute URL where a path is required. Use `bud login`.

**`Sign-in failed: Invalid username or password`** - the credential is wrong.
**Stop.** Retrying variations locks the account. Confirm the password with the
user, or have them sign in via the console to verify it.

**`Reached the sign-in page but could not locate its submit target`** - the
identity provider is showing something other than a password form: SSO-only,
a consent screen, MFA enrolment, or an outage. Headless sign-in cannot proceed;
ask the user to authenticate through the console.

**401 immediately after a successful login** - usually `BUD_HOME` pointing at a
non-writable directory, so the session is never persisted. Check that
`~/.bud/session-*.txt` exists.

## Permissions

**403 on something your role should allow.** Installation-level administrator
does not imply membership of every project. Check:

```bash
bud api GET /users/me/permissions
bud api GET /permissions/<user-id>/projects
```

Then add yourself to the project, or work in one you belong to.

**403 mentioning CSRF** - the request bypassed the toolkit. Mutations need the
CSRF header; use `bud api`.

## Finding the right path

**404 on a collection you are sure exists.** Several collections are nested
under their own prefix - `/clusters/clusters` rather than `/clusters`. Do not
guess:

```bash
bud paths cluster
bud schema /clusters/clusters POST
```

Note also that `/workflow/...` (singular) and `/workflows/...` (plural) are
**different** namespaces. Jobs described in this skill are the plural one; a
404 saying "No such workflow exists" against `/workflow/{id}/status` means you
used the wrong namespace, not that your job is missing.

## Requests the platform rejects

**422** - the field-level reasons are printed. Get the exact shape with
`bud schema <path> <method>`. Recurring causes:

- A name that breaks its charset rule. Deployment names, for instance, accept
  only letters, digits and hyphens - spaces become hyphens, and the value is
  lower-cased.
- A missing id that the endpoint requires but that reads as optional in the
  schema, because it is only required once the session reaches a later step.
- Sending a display name where a UUID belongs. Look the id up first.

**409** - the name is taken, or the resource is in use. Search before creating:

```bash
bud api GET /projects/ --paginate | jq -r '.[].project | "\(.id)  \(.name)"'
```

**400 with a capacity or compatibility message** - the request is valid but the
installation cannot satisfy it. See "capacity" below.

## Jobs that do not finish

First: **`in_progress` alone does not mean running.** A job's status tracks the
session, and abandoned sessions stay `in_progress` indefinitely. On an
established installation most historical jobs look like this; it is not a
fault. See `jobs-and-waiting.md`.

To judge a specific job:

```bash
bud job show <job-id> --data
```

- **Steps present, latest one advancing** - it is working. Compare elapsed time
  against the duration table before worrying.
- **No steps at all, minutes in** - the work was probably never triggered. For
  stepped flows, the final call needs `"trigger_workflow": true`; without it
  the session just sits holding data.
- **A step in a failure state** - the message names the cause. `bud wait job`
  surfaces this automatically.
- **Same step, no movement, well past the expected duration** - likely wedged.
  Capture `bud job show --data`, then use the flow's cancel endpoint.

**The job finished but the resource is not usable.** Job completion and
readiness are different things. Always confirm with the resource:

```bash
bud wait resource /endpoints/<id>/model-cluster-detail --field status --equals running
```

## Capacity and placement failures

Deployments fail at planning time when nothing can host them. The message
usually names the constraint. Common ones:

- **No compatible cluster** - no cluster has the required accelerator type or
  enough free memory. Check what exists: `bud api GET /clusters/clusters` and
  the per-cluster node metrics.
- **Targets unachievable** - the latency or concurrency target cannot be met by
  any available configuration. Relax the target, or allow more replicas.
- **Insufficient storage** - the model does not fit the cluster's storage
  class. Check `/clusters/{id}/storage-classes`.

These are answers, not errors: report the constraint to the user and offer the
relaxation rather than retrying the identical request.

## Platform-side failures

**502/503/504** - a component is restarting or overloaded. The toolkit already
retried with backoff. If it persists, `bud health`, then report it - do not
hammer.

**A read that intermittently returns empty or partial data** right after a
write is normal: parts of the platform are eventually consistent. Re-read after
a short pause rather than treating the first answer as final.

## Rate limits

**429** - the toolkit honours `Retry-After` and backs off. If you are hitting
it, you are running too many calls in parallel. Serialise, page with
`--paginate` instead of many concurrent requests, and raise poll intervals.

## When you need to report an issue

Include: the exact command, the full error, `bud whoami`, `bud health`, and for
job problems `bud job show <id> --data`. Note the time - installation logs are
correlated by timestamp.
