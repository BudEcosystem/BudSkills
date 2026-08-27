# What was evaluated, what was rejected, and why

This toolkit is ~1,100 lines of Python standard library. That was a decision,
not an accident. Every candidate framework was checked against three
requirements:

1. **No third-party API key**, ever.
2. **Degrades gracefully when absent** - the loop must work with nothing
   installed.
3. **Justified weight.** A dependency must earn its install size.

Verdicts below are from testing the projects, not from reading their marketing.

---

## Microsoft PromptPex - rejected as a dependency, method reimplemented

PromptPex ([paper](https://arxiv.org/abs/2503.05070)) generates tests from a
prompt: derive the intent, the input specification, the output rules, then
invert those rules to produce inputs that should be refused. The method is
good. **The runtime is dead.**

- PromptPex is built on GenAIScript. That project's repository is **archived,
  and its description is the single word `DEPRECATED`.** Last real commits May
  2025.
- The published package `genaiscript@2.5.2` is a **216-byte deprecation stub**
  containing a README that says "deprecated" and no executable at all.
- PromptPex's own manifest depends on `genaiscript: ^2.5.1`. **That range
  resolves to the stub**, so an ordinary install produces a silently broken
  tree. Only installing from the committed lockfile works.
- When it does work: **768 packages, 523 MB**, Node >= 22.15.
- PromptPex itself: last release July 2025, 56 open issues, and its own notice
  says it is "intended for use for research purposes... should not be used in
  any downstream applications without additional detailed evaluation."

**On the key constraint it would actually have been fine.** GenAIScript
supports arbitrary OpenAI-compatible providers through `<NAME>_API_BASE` /
`<NAME>_API_KEY` environment pairs, so pointing it at a Bud gateway needs no
vendor account. That is not the problem. The problem is depending on
abandonware whose package manager entry is a tombstone.

**What was done instead:** the method is reimplemented in `gen-cases`. Its
brief walks the same four stages - intent, input space, output rules, inverse
rules - and the inverse rules are exactly where the `out_of_scope` and
`adversarial` categories come from. No dependency, and the generator is the
host agent rather than a hosted model.

---

## DSPy - rejected as a dependency, kept as an opt-in and as prior art

DSPy is the opposite of PromptPex on every maintenance axis: MIT, ~36k stars,
released within days of this being written, and it genuinely works against an
arbitrary endpoint with no vendor account:

```python
import dspy, os
lm = dspy.LM("openai/<deployment-name>",
             api_base="https://gateway.<your-domain>/v1",
             api_key=os.environ["BUD_GATEWAY_TOKEN"],
             model_type="chat", cache=False)
dspy.configure(lm=lm)
```

Verified: no network access at import time, nothing downloaded at optimiser
time beyond the model itself.

It was still rejected as a *dependency*, for three measured reasons.

**Weight.** `pip install dspy` is **166 MB across 56 packages**, most of it a
provider-routing layer this toolkit does not need since it only ever talks to
one endpoint.

**MIPROv2 does not fit a 50-case suite.** Measured call counts on a 50-example
set, against a counting stub:

| Optimiser | Calls |
|---|---|
| BootstrapFewShot | **8** |
| MIPROv2 `auto="light"` | 920 |
| MIPROv2 `auto="medium"` | 1,596 |
| GEPA `auto="light"` | 1,162 |
| SIMBA (8 steps) | 7,356 |

Worse, when no validation set is passed MIPROv2 takes **80% of your data for
validation**: 50 cases become 10 train / 40 validation. It bootstraps
demonstrations from ten examples while spending 920 calls evaluating on forty,
and because 40 is below its minibatch threshold every trial is a full sweep.
You pay heavy-optimiser prices for a dataset below even its "light" design
point. `pip install dspy` also **cannot run MIPROv2** - optuna is an optional
extra, and the failure only appears at optimiser time.

**Its reflective optimiser needs a strong reflector.** GEPA requires a
`reflection_lm` and its documentation says it must be a strong model. With one
cheap self-hosted deployment you would point it at itself, which is precisely
the configuration it is not designed for.

**What was kept.** Two findings shaped `propose-prompt`:

- *Bootstrapped few-shot examples beat instruction rewriting.* Random search
  over demonstrations outperformed instruction-only optimisation in all but one
  reported case; ablations attribute roughly +21% to examples against +5% to
  instructions. This is why `fewshot` is the first strategy in the mixed
  rotation, drawing demonstrations from cases the prompt already passes.
- *Reflective mutation works on a tiny sample.* The reference implementation's
  reflection minibatch defaults to **three** failing traces. `--failures`
  defaults to 3 for the same reason: more failures dilute the diagnosis into
  generic advice.

**Enabling it anyway.** `scripts/setup.sh --optional` prints the exact
commands. Use `dspy` (BootstrapFewShot, ~8 calls) rather than `dspy[optuna]`
unless you have several hundred cases.

---

## promptfoo - rejected on weight, worth an escape hatch

Tested and it **does work with no third-party key**: pointed at a local
OpenAI-compatible endpoint with all vendor keys unset, it ran both deterministic
assertions and `llm-rubric` model-graded assertions, and produced a
self-contained HTML report with no external references.

Rejected because `npm install promptfoo` produces **~2.1 GB across 714
packages** and requires Node >= 22.22. That is two thousand times the size of
this toolkit to gain a browser UI. It was also acquired by a model vendor in
March 2026 - still MIT, but an odd dependency for a tool whose selling point is
vendor independence. And it does no significance testing at all, which is the
part that matters most here.

If you want it, the mapping is:

```yaml
providers:
  - id: openai:chat:<deployment-name>
    config:
      apiBaseUrl: https://gateway.<your-domain>/v1   # must end in /v1
      apiKeyEnvar: BUD_GATEWAY_TOKEN
```

Three traps: `apiBaseUrl` must be the `/v1` root, not the full chat path;
putting a bare provider string on an individual assertion **silently discards
`apiBaseUrl`** and falls back to environment keys; and reasoning models need
`showThinking: false`.

---

## The rest

| Tool | Key-free | Weight | Verdict |
|---|---|---|---|
| **autoevals** | yes | 70 KB, 4 deps | The only defensible pip dependency here. Even so, its judge templates are MIT and worth reading rather than importing - it is a scorer library, not a runner. |
| **Inspect AI** | yes, good local story | ~35 MB, 40 deps | Best-engineered thing surveyed. No optimiser, high ceremony, and 40 dependencies for sandboxing this does not need. |
| **DeepEval** | nominally | 30 deps | **Avoid.** At *import time* it hijacks the global tracing provider, initialises crash reporting at full sampling, overrides the exception hook, and makes a blocking call to an external IP-lookup service. Unacceptable inside someone else's agent. |
| **Ragas** | yes | LangChain tree | Installing an entire orchestration ecosystem to obtain four rubric prompts. |
| **OpenAI Evals** | **no** - requires a vendor key | 46 MB | Fails the first constraint. Effectively unmaintained. |
| **lm-evaluation-harness** | with a dummy key | ~1 GB | Wrong category - log-likelihood scoring over academic multiple-choice benchmarks. |
| **Langfuse evals** | yes | 5 stateful services | Judging is a server-side feature. Not proportionate. |
| **TextGrad** | yes | 12 deps | Dormant ~17 months. Its own reported failure modes - prompts ballooning past the context limit, contradictory edits with no aggregation - are the ones this toolkit guards against. Read the idea, do not depend on it. |
| **"EvalLite"** | - | - | **Does not exist, and the name is dangerous.** There is no such npm package; the PyPI name is squatted by an unrelated web-scraping package with a placeholder homepage. Do not `pip install evallite`. The project people mean is `evalite` - genuinely light (1.6 MB, 13 deps, "No API key required") but TypeScript-first with a JS test runner, so it is the wrong shape for a Python skill. |

---

## Wiring the opt-in paths to your installation

Everything above needs the same three values, and none of them is a vendor
credential:

```bash
export BUD_GATEWAY_TOKEN=$(bud token)     # mints AND registers; expires ~1h
# base URL: https://gateway.<your-domain>/v1
# api key:  $BUD_GATEWAY_TOKEN
# model:    a DEPLOYMENT name, from:
bud api GET /playground/deployments -q limit=100 | jq -r '.endpoints[].name'
```

`bud token` does two things and the second is the one people miss: it registers
the token with the gateway. An unregistered token is rejected as an invalid key
even though it is perfectly valid.

Two gateway behaviours worth knowing before you point any tool at it:

- **Do not send `temperature` unless you must.** Some deployments reject an
  explicit value outright ("does not support 0 with this model. Only the
  default (1) value is supported"). The tools here omit it by default and retry
  without it if a model complains; a third-party tool that always sends
  `temperature: 0` will fail against those deployments.
- **Request `encoding_format: "float"` for embeddings.** The default in at
  least one popular SDK is base64, which returns an empty 502.

---

## Why a bespoke loop wins here

The parts a framework would have supplied are small: substring and regex
assertions are a handful of lines; a rubric judge is a request, a parse and a
retry; McNemar's exact test is a binomial tail using `math.comb`; a Wilson
interval is four lines.

The parts that are genuinely hard are not the parts frameworks solve. They are
concurrency with correct backoff, response caching so an interrupted run does
not re-spend, and above all **a rubric that produces stable verdicts from a
mid-sized model**. That is where the effort saved on integration went.

And the single most useful behaviour in this toolkit is one no surveyed
framework offers: **refusing to call a small difference an improvement.**
`guard` prints the p-value and the smallest delta the suite could ever detect,
and says "no detectable difference" when that is the truth.
