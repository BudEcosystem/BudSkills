---
name: bud-prompt-optimization
description: Automatically improve a Bud agent's prompt against evidence - synthesise a question/answer test set, measure a prompt version against it, propose and screen better candidates, and gate every promotion on a per-case regression check with a real significance test. Use when a Bud request asks to make an agent more accurate, iterate or converge on the best prompt, generate test scenarios or evaluation cases for an agent, compare prompt versions, or find and fix what an agent gets wrong. Needs no third-party API key.
---

# Bud Foundry: prompt optimisation

Improving a prompt by eyeballing a few answers fixes one case and silently
breaks two others. This skill makes it an experiment: a fixed set of cases, a
score with error bars, and a per-case regression gate before anything is
promoted.

Bud versions prompts and can evaluate deployments against catalog datasets. It
does not synthesise test cases or search for better prompts - that is what this
toolkit adds, and it adds it without a third-party account.

Prerequisite: connect first (`bud-platform`). To *run* cases you also need a
deployment or an agent to test (`bud-deployments`, `bud-agents`).

## Where the intelligence comes from

Every generative step - writing cases, grading rubrics, proposing candidates -
needs a capable model. There are exactly two sources, and **neither is a
third-party API key**:

| | How it works | Cost | Default |
|---|---|---|---|
| **You** | The tool writes a brief to a file and stops. You read it, write the response file, re-run the same command. No network call happens at all. | none | yes |
| **A Bud model** | `--via model --model <deployment>` sends the same brief through the inference gateway. | the user's own compute | opt-in |

**Ask before using the second one** - it spends their budget. Say roughly how
much: a 50-case grading pass is ~50 short calls.

You are the better generator anyway. Proposing a prompt is reasoning about
reasoning, and small models are measurably bad at it - they return trivial
rewordings. Use a Bud model for grading if you want the loop unattended;
propose candidates yourself.

## Set up

```bash
bash scripts/setup.sh          # reports exactly what works here; links bud-* tools onto PATH
```

Writing and grading cases needs nothing but `python3`. Only *running* them
touches the installation.

## The loop

```
   write cases ──▶ run baseline ──▶ score ──┐
                                            ▼
   promote winner ◀── guard ◀── score ◀── run candidates ◀── propose from failures
        │                 ▲                                          │
        └── new prompt version in Bud                  only the train split ─┘
```

Cases split into **train** (the optimiser may see it) and **holdout** (it may
not). That split is the only thing standing between you and a prompt tuned into
the shape of its own test set.

### 1. Write the case set

```bash
scripts/gen-cases --task hr-agent.md --n 50 --out cases.jsonl
```

It stops and asks you to write `turns/gen-cases.response.jsonl`. The brief tells
you the schema and the method: derive the agent's intent, then its input space,
then the rules a correct answer must obey, then invert those rules - the
inversions are your out-of-scope and adversarial cases.

Aim for 30-50, covering four categories: **typical** (half), **edge** (a
quarter), **out of scope** (a sixth), **adversarial** (the rest). Below ~20
cases nothing you measure is real.

> **You may invent the questions. You may never invent the answers.** If the
> task description does not state the leave allowance or the approval
> threshold, do not put a number in `reference` or `expect_contains` - write a
> rubric about the required *behaviour* and flag it for the user to confirm. A
> case set built on invented ground truth optimises the prompt towards a
> fiction.

`--prompt <file>` additionally shows the agent's current prompt so the input
space is derived from what it actually claims to do. Full schema and the four
categories: `references/case-design.md`.

### 2. Measure what you have

The prompt text lives on the **version**, not the agent record:

```bash
PROMPT_ID=<agent-uuid>
V=$(bud api GET /prompts/$PROMPT_ID/versions | jq -r '.versions[]|select(.is_default_version)|.id')
bud api GET /prompts/$PROMPT_ID/versions/$V | jq -r '.config_data.system_prompt' > prompts/v1.txt

scripts/run-cases --cases cases.jsonl --target <deployment> --prompt prompts/v1.txt \
                  --split train --out runs/v1-train.json
scripts/score-run --run runs/v1-train.json --cases cases.jsonl
```

`run-cases` caches answers, so an interrupted run resumes without re-spending.

**To test a published agent rather than a bare prompt**, use `--target
agent:<name>` or `--target agent:<name>:v3` for one specific version.

> **Address an agent only that way.** Passing `prompt:<agent-name>` as the
> `model` field is accepted, returns 200, and answers **from the bare model
> with the agent's prompt never applied** - verified on the reference
> installation, where the probe used 24 input tokens instead of 3,408 and the
> assistant introduced itself as the underlying vendor model. You get a clean
> green run that measured nothing. `run-cases` uses the correct form and warns
> when a target's token count says the prompt is missing.

Deterministic checks (`expect_contains`, `expect_absent`, regex, exact) cost
nothing and **decide wherever they apply**. Only rubric cases reach a judge, all
in one batch - one turn for the suite, not one per case.

### 3. Propose better candidates

```bash
scripts/propose-prompt --prompt prompts/v1.txt --run runs/v1-train.json \
                       --cases cases.jsonl --out-dir candidates/r1 --n 4
```

You are shown a handful of **failing** cases - what was asked, what was
required, what came back - and asked for four candidates using different
strategies: add worked examples, rewrite the instructions, tighten scope and
refusal, or compress.

Try **worked examples first**. Across the literature, few-shot demonstrations
move accuracy far more than another paragraph of instruction, and they cost
less to produce. Instructions win only for conditional rules that examples
cannot express.

> Candidates are capped at 1500 characters. That is not tidiness - unbounded
> prompts grow every round, encode the test set, and score *worse* on anything
> new. If a candidate needs more room, something else should come out.

Never let a candidate name a test case ("when asked about carry-over, say 5
days"). That is a lookup table, not a prompt, and the holdout will catch it.

### 4. Screen on train, decide on holdout

```bash
for c in candidates/r1/cand-*.txt; do
  scripts/run-cases --cases cases.jsonl --target <deployment> --prompt $c \
                    --split train --out runs/$(basename $c .txt)-train.json
  scripts/score-run --run runs/$(basename $c .txt)-train.json --cases cases.jsonl
done
scripts/guard --baseline runs/v1-train.json --candidate runs/cand-01-train.json
```

Then take **only the winner** to the holdout, and let `guard` decide:

```bash
scripts/run-cases  --cases cases.jsonl --target <deployment> --prompt candidates/r1/cand-01.txt \
                   --split holdout --out runs/cand-01-holdout.json
scripts/score-run  --run runs/cand-01-holdout.json --cases cases.jsonl
scripts/guard --baseline runs/v1-holdout.json --candidate runs/cand-01-holdout.json --strict
```

`guard` reports the four cells that carry information and tests the difference
properly - McNemar's exact test on the paired cases:

```
SHIP - improvement is real
  baseline     5/14 =  35.7%  (95% CI 16-61%)
  candidate   14/14 = 100.0%  (95% CI 78-100%)
  fixed  9    broken 0        McNemar exact p = 0.0039
```

It also refuses to flatter you:

```
NO DETECTABLE DIFFERENCE
  baseline 5/10 = 50.0%   candidate 9/10 = 90.0%   delta +40.0 pp
  McNemar exact p = 0.1250
  On 10 cases the smallest delta that could ever reach p<0.05 is 60 pp.
```

> **+40 points was not a result.** On 50 cases you need **6 clean fixes and no
> regressions** to clear p<0.05; one case in fifty is 2 points and p = 1.0.
> Report the p-value to the user rather than the headline percentage, and say
> plainly when a change is indistinguishable from noise.

Exit codes gate a pipeline: `0` ship, `1` something regressed, `2` no
detectable difference (with `--strict`), `3` the runs are not comparable.
`guard` refuses outright when the two runs used **different judges** - changing
the ruler is not measuring the thing.

### 5. Stop at the right time

Three to five rounds. Stop when two consecutive rounds move less than about two
points, or when a round produces no candidate that beats the incumbent. The
first round typically carries most of the gain; by the fifth you are fitting
noise. Budget and stopping rules: `references/optimization-loop.md`.

### 6. Promote the winner as a new version

Every iteration becomes a **new prompt version**, so the history is the
experiment log and rollback is one call. Promotion is two calls and the version
endpoint does not take prompt text - `bud-agents` owns this surface and has the
full config shape:

```bash
DRAFT=$(bud api POST /prompts/prompt-config -d '{...config with the winning system_prompt...}' \
        | jq -r '.bud_prompt_id')
bud api POST /prompts/$PROMPT_ID/versions -d '{
  "endpoint_id":"<deployment-uuid>","bud_prompt_id":"'"$DRAFT"'","set_as_default":false}'
# smoke-test that exact version, then promote:
bud api PATCH /prompts/$PROMPT_ID/versions/<new-version-uuid> -d '{"set_as_default":true}'
```

Confirm with `bud schema /prompts/{prompt_id}/versions POST` before composing
the body, and re-run the suite against `--target agent:<name>:v<N>` afterwards
to check the improvement survived deployment.

> **There is nowhere in the version record to store a score.** Keep
> `cases.jsonl`, the run files and the candidate prompts in version control
> beside the prompt - they are the only record of why a version won.

## What to decide, and what to ask

| Decide yourself | Ask the user first |
|---|---|
| Case categories and phrasings | Every fact used as ground truth |
| Which candidate strategies to try | Spending their compute (`--via model`, large runs) |
| When to stop iterating | Shipping a candidate that regressed any case |
| That a 2-point delta is noise | Promoting a new default version |

The regression trade is always theirs: a candidate that fixes two cases and
breaks one may be right or wrong depending on which cases matter. Present the
trade; do not resolve it silently.

## Keep it honest over time

Live traffic finds cases you did not imagine. On a schedule (`bud-routines`),
review yesterday's traces (`bud-observability`), turn every real mistake into a
case with its trace id in `notes`, and re-run. The set only grows, so old bugs
cannot come back unnoticed - and after ~100 real cases the statistics finally
have something to work with.

## Deeper reference

- `references/case-design.md` - the case schema in full, the four categories,
  where ground truth comes from, and how the train/holdout split is assigned
- `references/optimization-loop.md` - the algorithm, round and candidate
  budgets, stopping rules, overfitting, and what the numbers can bear
- `references/judging.md` - judge design, the biases that actually matter,
  calibrating a judge against human labels, and when a human must decide
- `references/research-findings.md` - what was evaluated and rejected
  (PromptPex, DSPy, promptfoo and others), with the evidence, plus exactly how
  to wire the opt-in paths to your installation

## Where to go next

`bud-agents` to version, promote and run the prompt; `bud-evaluations` for the
platform's own evaluation runs over standard datasets; `bud-observability` to
find real-world failures worth adding as cases; `bud-routines` to schedule the
review; `bud-guardrails` when the fix belongs in policy rather than in wording.
