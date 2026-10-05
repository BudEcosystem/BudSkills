---
name: bud-platform
description: Connect to and operate a Bud Foundry installation - sign in, call any Bud API, track long-running jobs, and troubleshoot failures. Use this whenever a request involves Bud, Bud Foundry, the Bud AI OS, or a Bud console/deployment, and load it before any other bud-* skill. Also use it for auth or connection errors (401/403/CSRF), for discovering Bud endpoints, and for waiting on Bud work that takes minutes (model downloads, deployments, cluster onboarding, evaluations).
---

# Bud Foundry: platform access

Bud Foundry is a control plane for GenAI: it manages model registries, compute
clusters, model deployments, agents, connectors, evaluations, guardrails,
routines and observability. Everything in it is reachable over one REST API,
and this skill is how you reach it.

**Load this skill first.** Every other `bud-*` skill assumes the connection,
job-waiting and error conventions defined here.

## 1. Decide the operating mode first

Before starting any task that changes state, ask the user one question:

> Do you want me to run this autonomously (I choose sensible defaults and only
> stop on genuine blockers), or should I check in at each decision point?

Then honour it for the whole task:

| Mode | Behaviour |
|---|---|
| **Autonomous** | Pick defaults, state them, keep going. Stop only for destructive actions, spending decisions, or a genuine ambiguity that would waste real work if guessed wrong. |
| **Guided** | Present the options at each decision point and wait. |

Skip the question for read-only requests ("show me", "list", "what is") - just
answer. Never ask it twice in one task.

## 2. Connect

The toolkit is `scripts/bud` in this skill. It needs only `python3` - no pip
install, no browser.

```bash
export BUD_API_URL=https://app.example.bud.studio   # the Bud API origin
export BUD_EMAIL=you@example.com
export BUD_PASSWORD=...                              # or omit to be prompted
bud login
```

`bud login` caches the session under `~/.bud/` (mode 0600) and later commands
reuse it, renewing automatically when it expires. Confirm with `bud whoami`.

If `bud` is not on your PATH, run `bash scripts/setup.sh` once, or call it by
its path: `<skill-dir>/scripts/bud`.

**Bud sign-in is browser-style single sign-on** (OpenID Connect). There are two
ways in and the toolkit picks whichever fits the installation:

- **Password** (above): works where the identity provider still shows a password
  form; the toolkit drives the redirect handshake over plain HTTP for you.
- **Bearer token** (OIDC-only installations): many installations **disable
  password login** - `bud login` then reports it found no sign-in form. In that
  case reuse a token minted by a real browser login: have the user sign in
  through the **Bud Studio desktop app** (it writes `auth.json`, which the
  toolkit finds automatically), or `export BUD_ACCESS_TOKEN=<jwt>`. The toolkit
  then authenticates with the token and refreshes it on its own.

Consequences worth knowing:

- Mutating calls need a CSRF header in password mode. The toolkit adds it;
  hand-rolled `curl` will get 403. (Bearer mode needs no CSRF.)
- Wrong passwords are reported by the identity provider, and **repeated
  failures can lock the account**. If sign-in fails, stop and confirm the
  credential - do not retry variations.
- If `bud login` reports it landed on an unexpected screen or found no form, the
  install is OIDC-only or needs MFA/consent: switch to the bearer token above.
  Do not try to defeat the browser flow.

See `references/authentication.md` for API keys, multi-tenant profiles,
service accounts and non-interactive/CI setups.

## 3. Call anything

```bash
bud api GET  /projects/ -q page=1 -q limit=20      # query params
bud api POST /projects/ -d '{"name":"Demo","description":"...","tags":[]}'
bud api POST /models/deploy-workflow -d @body.json  # body from a file
bud api GET  /endpoints/ --paginate                 # walk every page
```

Output is JSON on stdout, progress on stderr - safe to pipe into `jq`.

**To call a deployed model**, you need a gateway bearer token, not the session:

```bash
export BUD_GATEWAY_TOKEN=$(bud token)   # mints AND registers it with the gateway
```

Skipping the registration step yields `401 Invalid API key` on a perfectly valid
token. See `bud-inference`.

**Discover endpoints instead of guessing.** The installation describes itself,
so you are never limited to what these skills document:

```bash
bud paths cluster                # search endpoints by keyword
#  multiple words are ANDed against the path AND its summary, so a plausible
#  pair like `cluster onboard` misses POST /clusters/clusters entirely.
#  Start with one word and narrow only if the list is unwieldy.
bud schema /models/deploy-workflow POST   # exact request + response shape
```

Use `bud schema` before composing any unfamiliar request body. It is generated
from the live installation, so it is correct even when documentation drifts.

## 4. Long-running work: jobs

Most substantial Bud operations - importing a model, deploying it, onboarding a
cluster, running an evaluation - are **jobs** that take minutes. They return a
job id immediately.

Bud's job model has one property that will mislead you if you do not know it:

> A job's `status` tracks the *session you are driving*, not the background
> work. It stays `in_progress` until the final step is submitted, and an
> abandoned session stays `in_progress` forever. **Never wait on `status`
> alone** - waiting on an abandoned job never returns.

So use both of these, in this order:

```bash
# 1. Follow the background work (reads the per-step progress feed)
bud wait job <job-id> --timeout 1800

# 2. Confirm the result is actually usable (authoritative)
bud wait resource /endpoints/<id>/model-cluster-detail --field status --equals running
```

> **There is no `GET /endpoints/{id}`** - it 404s. A deployment's own record is
> at `GET /endpoints/{id}/model-cluster-detail` (status at `.result.status`), or
> `GET /endpoints/ -q project_id=<p> -q name=<n>`. The 404 message says "no such
> resource, or the id belongs to another project", which misdirects badly here:
> the id is fine, the path shape is not.

Inspect at any time with `bud job show <job-id>` (add `--data` to see what the
job has collected). `bud jobs` lists recent ones.

`bud wait job` returns when every reported step has succeeded, raises on the
first failed step with the platform's reason, and on timeout tells you how to
resume watching. Jobs are visible only to the user who created them.

Details, including per-operation durations and how to resume an interrupted
job: `references/jobs-and-waiting.md`.

## 5. Conventions that apply everywhere

**Responses nest.** Create and fetch responses wrap the entity under its own
name, and list items do too:

```json
{"object":"project.get","message":"...","project":{"id":"...","name":"..."}}
{"projects":[{"project":{"id":"..."},"endpoints_count":7}]}
```

Read `.project.id`, not `.id`. The toolkit's `unwrap()` and `entity_id()`
helpers do this for you when scripting in Python.

**Collections are paged** with `page` (1-based) and `limit`, and report
`total_record`. Use `--paginate` rather than assuming one page is everything.

**Some collections are nested under their own prefix** - clusters live at
`/clusters/clusters`, not `/clusters`. When a path 404s, run `bud paths <word>`
rather than guessing.

**Nearly everything is scoped to a project.** Get one before you start:

```bash
bud api GET /projects/ -q page=1 -q limit=50 | jq -r '.projects[].project | "\(.id)  \(.name)"'
export BUD_PROJECT_ID=<id>
```

**Names must usually be unique** within their scope; a 409 means it already
exists. Prefer looking up the existing resource over inventing a new name.

## 6. When something fails

The toolkit prints the status, the platform's own message, and a suggested
next step. The common ones:

| Symptom | What it means | Do this |
|---|---|---|
| 401 | Session expired | `bud login` (the toolkit retries once by itself) |
| 403 | Signed in, not permitted | `bud api GET /users/me/permissions`; check project membership |
| 404 on a collection | Wrong path shape | `bud paths <keyword>` |
| 409 | Name taken / resource in use | Look it up instead of recreating |
| 422 | Body failed validation | `bud schema <path> <method>` and fix the fields |
| 429 | Rate limited | The toolkit backs off; lower concurrency |
| 502/503/504 | A component is restarting | Retry after a pause; `bud health` |

`references/troubleshooting.md` covers the non-obvious failures - stuck jobs,
capacity rejections, quota limits, and how to tell "still working" from "wedged".

## 7. Do not destabilise the installation

These are shared, often production, systems.

- **Never** delete a project, cluster, deployment or model without explicit
  confirmation naming that resource. Deletions cascade.
- Keep concurrency low. The toolkit serialises and backs off by default; do not
  fan out parallel deploys or bulk-delete loops.
- Deployments consume real compute and cost money. Confirm size, cluster and
  replica count before deploying, and clean up test deployments afterwards.
- Prefer reading (`GET`) to confirm state over re-issuing a mutation. Retrying a
  create usually makes a duplicate, not an idempotent result.
- When testing, name things with a recognisable prefix so cleanup is easy.

## 8. Which skill to use next

| The user wants to... | Skill |
|---|---|
| Organise work, manage projects, members, API keys | `bud-projects` |
| Find, import, quantize or inspect models (including from Hugging Face) | `bud-models` |
| Add, inspect or manage compute clusters and their capacity | `bud-clusters` |
| Deploy a model, size it to an SLO, scale or publish it | `bud-deployments` |
| Send inference traffic to a Bud endpoint | `bud-inference` |
| Build, version, test or run agents and prompts | `bud-agents` |
| Give an agent tools or connect external systems | `bud-connectors` |
| Make an agent more accurate - test cases, prompt iteration, regression checks | `bud-prompt-optimization` |
| Generate a test set and converge on the best prompt automatically | `bud-prompt-optimization` |
| Measure quality with evaluations, benchmarks or datasets | `bud-evaluations` |
| Inspect traffic, traces, cost, latency or drift | `bud-observability` |
| Apply safety policy, moderation, agent governance or approvals | `bud-guardrails` |
| Route between models, split traffic, cut inference cost | `bud-routing` |
| Schedule recurring work or react to events | `bud-routines` |

Each of those skills assumes you have already connected using this one.
