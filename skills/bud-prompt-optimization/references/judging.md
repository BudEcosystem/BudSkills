# Judging

Two mechanisms, and the cheap one should carry most of the load.

## Deterministic checks decide wherever they apply

`expect_contains`, `expect_absent`, `expect_regex` and `expect_exact` are plain
matching. They cost nothing, never drift, and are perfectly reproducible.

The collapse rule, in order:

1. If a deterministic check **failed**, the case fails. A model's opinion does
   not override an exact check.
2. Otherwise a rubric verdict decides, if there is one.
3. Otherwise the case fails. An ungraded case is not a free pass.

## The judge is deliberately ignorant

`score-run` sends the judge only: the requirement, the question, the reference
answer if there is one, and the answer. It never sees the prompt under test,
which candidate produced the answer, or the other candidates' answers. It
cannot favour one because it does not know there is a contest.

Design choices, each for a measured reason:

**Binary PASS/FAIL, never a 1-10 scale.** Coarse labels are markedly more
self-consistent: the same model reproduces a binary judgment far more reliably
than a graded one, and agreement with human raters is best on short scales.
A 1-10 score also invites the judge to hold a calibrated scale in its head,
which it cannot do. Several sharp binary criteria beat one blurry number.

**Reference-guided wherever possible.** Supplying a reference answer is the
single largest improvement available to a judge - larger than
chain-of-thought - because it converts an open judgement into a comparison.

**Markdown is flattened before judging.** Formatting bias is now the dominant
judge bias, an order of magnitude larger than the famous verbosity bias: the
same content scores differently as bullets and as prose. `score-run` strips
headings, bullets, emphasis and code ticks before the judge sees the answer.
`--keep-formatting` disables this when the format is itself the requirement.

**Short structured reasoning, then a discrete label.** The output contract is
one JSON object per case with a twelve-word reason. Long free-form
justification does not reliably improve accuracy and gives the judge room to
talk itself into a verdict.

**One batch, not one call per case.** All rubric cases go in a single brief.
That is what makes host-agent grading practical, and it also means the judge
sees a consistent standard across the suite rather than drifting between calls.

## Choosing a judge

**Prefer yourself.** You are a strong model and the grading pass costs nothing.

**If you use a Bud deployment, use the strongest one available**, even when the
agent under test runs on a cheap one. Grading is easier than answering, but a
weak grader produces noise that looks like signal, and small models fail
particularly badly at anything requiring correctness rather than preference -
some frontier judges score near chance on benchmarks where the right answer is
a matter of fact rather than style.

**Never use the same model as judge and as target when you can avoid it.**
Models measurably prefer their own idiom, so the score is inflated and, worse,
the optimisation drifts towards writing that the judge likes rather than
answers that are correct. `score-run` warns when they match.

**Keep the judge fixed for the whole experiment.** Changing it mid-flight
changes the ruler, not the thing measured. `guard` refuses to compare two runs
graded by different judges - override with `--force` only if you know why.

## Reducing noise

- Leave temperature unset. The default is already the least surprising choice,
  and some deployments reject an explicit one.
- Make rubrics binary and specific. "Cites the policy section" is gradeable;
  "is helpful" flaps between runs.
- **Re-grade the same run file and see how much the judge disagrees with
  itself.** Delete the response file in `turns/`, re-run `score-run`, and diff
  the verdicts. If more than a case or two moves, the rubrics are too vague to
  iterate against - fix them before trusting any delta. Single-trial verdicts
  from mid-sized judges match a many-trial consensus only around 85% of the
  time, so this is not a hypothetical.
- A judge that returns no usable verdict for a case scores it as a failure and
  says so. Investigate rather than re-running until it answers.

## Calibrating the judge against a human

Worth doing once, when the agent matters. Take ~50 cases spread across
categories, have the user label them PASS/FAIL, and compare with the judge's
verdicts.

Report **Cohen's kappa**, not raw agreement. Raw agreement does not correct for
chance and overstates a judge badly - the gap between the two is routinely
30-40 points. The conventional practitioner bar is kappa >= 0.6; be suspicious
of anything above ~0.8 on an open-ended task, because it usually means the
rubric was so mechanical that a substring check would have done the job.

If kappa is poor, the rubrics are the problem far more often than the judge is.

## When a judge is the wrong tool

Escalate to the user when:

- correctness is checkable but you lack the source of truth;
- the case turns on tone or legal risk, where "acceptable" is their call;
- the deterministic check and the judge disagree - that usually means the
  substrings and the rubric encode different ideas of correct;
- the requirement is arithmetic or code. Execute or diff it instead; judges are
  notably weaker at objective correctness than at preference.

Surface these rather than silently taking the judge's word.
