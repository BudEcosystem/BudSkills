# The loop: what it does, what it costs, what the numbers can bear

## The algorithm

```
0.  Split cases 60 / 40 into train and holdout, stratified by category.
1.  Run the current prompt on train. Score it. The failures are the work list.
2.  Propose 4 candidates from at most 3 failing cases, one per strategy:
       fewshot -> reflect -> constrain -> compress
3.  Run each candidate on train. Score. Keep the best.
4.  guard(incumbent, best) on TRAIN. If it did not win, stop or try a
    different strategy - do not accumulate edits that did nothing.
5.  Repeat 1-4 for at most 5 rounds.
6.  Run the final winner on HOLDOUT. guard(baseline, winner) there.
    That comparison, and only that one, decides whether to promote.
```

Two properties of this design matter more than the details.

**The optimiser never sees the holdout.** Not the cases, not the answers, not
the scores. `propose-prompt` refuses a holdout run outright. Prompt
optimisation overfits readily - training accuracy typically runs 5-20 points
above held-out accuracy - and a split you have peeked at is not a split.

**Candidates are length-capped.** 1500 characters by default. Prompts that grow
each round encode the test set; one production study found that scaling the
training set tenfold made the optimised prompt 75% longer and *worse*, while a
length cap gave fourfold compression at minimal quality cost. The cap is
regularisation, not tidiness.

## Budgets

The cost is dominated by *running* cases, not by generating anything.

| Round design | Calls to the model under test |
|---|---|
| 5 rounds x 6 candidates, full 30-case train set each | ~900 |
| 5 rounds x 6 candidates, screen on 10 then re-score the winner | **~450** |
| 3 rounds x 4 candidates, full 30-case train set | ~360 |

Screening on a random subset before spending the full set roughly halves the
cost for nothing - use `--limit` on the screening runs, then re-run the
survivor on the whole train split.

Generative calls are cheap by comparison: 10-20 candidate proposals for a whole
optimisation, and one grading pass per run. With the default host-agent path
those cost the user nothing at all.

## When to stop

Converging evidence puts the useful range at **3-5 rounds**. Reported
trajectories commonly show the first round contributing ten-plus points and the
fifth contributing a fraction of one.

Stop when any of these is true:

- two consecutive rounds move the train score by less than ~2 points;
- a round produces no candidate that beats the incumbent;
- the winning candidate is at the length cap and still failing the same cases -
  that is a signal the fix belongs somewhere else (a tool, a retrieval source,
  a guardrail policy), not in the prompt;
- the remaining failures all need facts the agent does not have. No prompt
  fixes missing data.

## What the numbers can bear

This is the part most tools get wrong, so the toolkit is deliberately blunt
about it.

Both prompts run on the same cases, so comparisons are **paired**, and the
right test is McNemar's exact test on the discordant cases - the ones where the
two prompts disagree. Cases both get right, or both get wrong, carry no
information about which is better.

**Minimum credible win, by suite size** (clean fixes, zero regressions):

| Cases | Fixes needed for p<0.05 | As a delta |
|---|---|---|
| 20 | 6 | 30 pp |
| 30 | 6 | 20 pp |
| **50** | **6** | **12 pp** |
| 100 | 6 | 6 pp |
| 200 | 6 | 3 pp |

Six is the floor regardless of suite size, because two-sided exact binomial
needs `0.5^b <= 0.025`. A larger suite does not lower the bar; it makes the
same bar a smaller percentage.

**Regressions raise it sharply.** With mixed outcomes:

| Fixes | Regressions | Net on 50 | p |
|---|---|---|---|
| 6 | 0 | +12 pp | **0.031** |
| 8 | 2 | +12 pp | 0.109 |
| 10 | 4 | +12 pp | 0.180 |
| 10 | 2 | +16 pp | 0.039 |

The same headline delta is significant or not depending entirely on how much
damage it did on the way.

**A 95% interval on a single pass rate is wide.** At 50 cases and a true rate
near 80%, it is about ±11 points. `score-run` prints it so a 76% and an 84% are
not mistaken for different things.

**Splitting a small set is a losing game, and that is fine.** On a 20-case
holdout you would need about 30 points to reach significance. The holdout is
therefore **a regression guard, not a second experiment**: its job is to answer
"did this obviously get worse, or obviously fail to transfer?", not to certify
the win. `guard` reports the p-value there too, and will say "no detectable
difference" on a +40-point holdout swing - which is the honest answer on ten
cases.

**There is a noise floor underneath everything.** Temperature 0 does not make a
model deterministic; serving precision and batching produce genuine run-to-run
variation on borderline items. Some of any measured delta is the hardware. If
you need certainty on a small suite, run the comparison twice.

**The variance diagnosis.** Optimisation can only work when the difference
*between prompts* is larger than the variation *between repeated answers to the
same prompt*. If a prompt gives different verdicts on re-run, no algorithm will
find signal. Fix that first - sharpen the rubrics, or accept the case is not
gradeable.

## Where prompt optimisation pays best

It is a small-model technology. Reported gains from black-box prompt
optimisation fall steeply with model capability - roughly 12% on a 7B model,
6% on a 72B, and near 1% on a frontier model. That is exactly the flagship
case: a cheap deployment, where the effect is large enough to clear the
statistical floor. On a strong model, expect the loop to find little, and say
so rather than manufacturing a result.

## What was deliberately left out

- **Crossover and self-referential evolution** - the algorithms that evolve
  their own mutation operators. Powerful, expensive, and they need far more
  evaluations than a 50-case suite can justify.
- **Bayesian search over candidates** - needs hundreds of trials to beat
  keeping the best.
- **Textual-gradient graphs** - the published failure modes (prompts growing
  past the context limit, contradictory edits with no way to reconcile them)
  are the ones a length cap and a single-strategy-per-candidate rule avoid.
- **Score probabilities from token log-probabilities.** A real but tiny gain,
  and it would work on only one of the two generator paths - the host agent has
  no log-probabilities at all. A feature that silently behaves differently
  depending on who generated is worse than not having it.
