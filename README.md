# Bud Foundry skills

Give a coding agent (Claude Code, Codex, or anything that reads skill folders)
the ability to operate a whole Bud Foundry installation through its API —
models, clusters, deployments, agents, connectors, evaluations, guardrails,
routing, routines and observability.

Ask for outcomes, not endpoints:

> "Deploy the cheapest model that can handle 100 concurrent requests under a
> 10-second latency budget, and scale it to hold that."

> "Every night at midnight IST, review yesterday's traces for the HR agent,
> find mistakes, and propose a prompt fix that doesn't regress anything."

## Install

```bash
./install.sh              # for your user   (~/.claude/skills)
./install.sh --project    # for this repo   (./.claude/skills)
./install.sh --dest DIR   # somewhere else
./install.sh --copy       # copy instead of symlink, for shipping elsewhere
```

Then connect:

```bash
export BUD_API_URL=https://app.<your-domain>    # the Bud API origin
export BUD_EMAIL=you@example.com
export BUD_PASSWORD=...                          # or omit to be prompted
bud login && bud whoami
```

The only dependency is `python3`. No pip install, no browser — sign-in
completes Bud's single-sign-on handshake over plain HTTP.

## What is here

| Skill | Owns |
|---|---|
| **bud-platform** | connect, call anything, track long-running jobs, troubleshoot. **Load this first.** |
| bud-projects | projects, members, permissions, API keys, audit trail |
| bud-models | the registry: browse, import from Hugging Face, add hosted models, quantize, compare |
| bud-clusters | onboard and operate compute; capacity, nodes, storage, health |
| bud-deployments | deploy a model, size it to an SLO, scale, autoscale, publish |
| bud-inference | send traffic: chat, embeddings, streaming, the OpenAI-compatible API |
| bud-agents | build, version, run and supervise agents |
| bud-connectors | connect external systems and give agents tools |
| bud-observability | traces, latency, cost, drift |
| bud-evaluations | evaluations, experiments, benchmarks, scores |
| bud-guardrails | safety policies, governance, approvals |
| bud-routing | route between models, split traffic, canary rollouts |
| bud-routines | schedules, event triggers, pipelines |
| bud-prompt-optimization | iterate a prompt to a measured score without external API keys |

Each skill keeps `SKILL.md` short and defers depth to `references/`, so only
what a task needs gets loaded.

## The `bud` command

Installed onto your PATH by `install.sh`. Useful beyond the skills:

```bash
bud paths cluster onboard          # search this installation's endpoints
bud schema /models/deploy-workflow POST   # exact request/response shape
bud api GET /projects/ --paginate  # call anything; auth, CSRF, retries handled
bud jobs                           # recent long-running work
bud wait job <id>                  # block until it settles
bud token                          # a bearer for the inference gateway
bud health
```

`bud paths` and `bud schema` read the installation's own API description, so an
agent can drive endpoints these skills never documented.

## Maintaining this bundle

```bash
python3 tools/lint_skills.py       # structure, trigger quality, dead links,
                                   # and no internal component names in prose
python3 tools/verify_examples.py   # RUN every documented command against a
                                   # real installation and report what breaks
```

`verify_examples.py` is the anti-drift mechanism: it extracts every `bud api`
command from the skills, resolves placeholders from live data, executes the
read-only ones, and fails on anything that no longer works. Run it against a
real installation after changing a skill.

## Known platform issues

`bugs.md` records defects found while building and testing these skills, each
with a reproduction, the impact on an API consumer, and how the skills work
around it. Worth reading before trusting an unfamiliar corner of the API.
