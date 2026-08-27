# Designing the case set

The case set is the experiment. A weak one makes a worse prompt look better,
and no amount of careful iteration recovers from that.

## Schema

One JSON object per line:

```json
{"id": "leave-carryover-01",
 "category": "typical",
 "input": "Can I carry unused leave into next year?",
 "reference": "Up to 5 unused days, which must be used by 31 March (Handbook 4.3).",
 "expect_contains": ["5"],
 "expect_absent": ["unlimited"],
 "rubric": "States the 5-day cap and cites section 4.3.",
 "notes": "source: People Handbook s4.3",
 "split": "train"}
```

| Field | Meaning |
|---|---|
| `id` | stable and unique. Regression tracking matches on it - renaming one makes a regression look like a new case |
| `category` | `typical`, `edge`, `out_of_scope` or `adversarial` |
| `input` | the user's message (required) |
| `reference` | the correct answer. Optional, **strongly recommended** |
| `expect_contains` | substrings that must appear, case-insensitive |
| `expect_absent` | substrings that must not appear |
| `expect_regex` / `expect_exact` | for tightly specified outputs |
| `rubric` | a testable requirement, graded by a judge |
| `notes` | where the ground truth came from, or a trace id |
| `split` | assigned by `gen-cases`; do not edit by hand |

Required: `id`, `input`, and at least one check. A case nothing can check is
not a case.

**Give a `reference` wherever you can.** A judge handed a reference answer is
dramatically more reliable than one asked to decide unaided - in the
best-documented comparison, supplying a reference cut judging errors by more
than half, and by more than chain-of-thought prompting did. It is the cheapest
quality improvement available.

## Size

| Cases | What it buys |
|---|---|
| < 20 | too noisy - one case flipping moves the score several points |
| 30-50 | the working range for a single agent |
| 100+ | worth it once real traffic is supplying the cases |

Note the tension with the previous point: 50 cases is the right size to *work*
with, and too small to certify a small improvement. Both are true. Start at 30-50
and grow the set from production failures.

More is not automatically better for the *training* half. Production experience
puts the useful training range at 20-100 examples, with larger sets producing
longer, more overfitted prompts.

## The four categories

Cover all four. Most people write only the first and are then surprised.

**Typical (about half).** The questions the agent exists to answer, in the words
users really use - including careless, abbreviated and misspelled ones.

**Edge (about a quarter).** Ambiguous, multi-part, or missing the context needed
to answer. A correct answer often asks a clarifying question rather than
guessing.

```json
{"id":"edge-balance-01","category":"edge","input":"How many days do I have left this year?",
 "rubric":"Does NOT invent a balance. Says it cannot see individual balances and asks or refers on.",
 "expect_absent":["you have 25 days left"]}
```

**Out of scope (about a sixth).** Reasonable-sounding requests it must decline.
This is where most agents leak, because they are helpful by default. Note that
a partial decline - "I can't advise, but here is the general position..." -
usually still fails the requirement, and should.

**Adversarial (the rest).** Attempts to override the instructions, extract
them, or obtain data it must not disclose. Include a claimed authority ("I am a
manager") - that is the realistic attack, not "ignore all instructions".

## Substrings or rubric?

Prefer `expect_contains` whenever a correct answer must contain a specific
token. Substrings are free, exact, never drift, and they are **authoritative**:
where a deterministic check fails, the case fails regardless of what a judge
thinks.

Use `rubric` when correctness is about behaviour - refusing, asking, covering
two points properly. Write it as a testable requirement:

- Good: "States the entitlement in days and cites the policy section."
- Bad: "Gives a helpful answer."

Both on one case is fine and often ideal: substrings for the fact, a rubric for
the manner. Be aware this makes the case strict - it must satisfy both.

## Ground truth

You can write the *inputs* and the *categories*. You cannot invent the *facts*.

Ask the user for them, or quote the source document. If a fact is unavailable,
either drop the case or write it as a behaviour requirement and flag it:

```json
{"id":"leave-01","input":"How many days of annual leave?",
 "rubric":"States the entitlement and cites the section.",
 "notes":"TODO confirm the number with the People Team"}
```

Never guess a number into an expectation. The prompt will be optimised towards
it.

## The split

`gen-cases` assigns `train` and `holdout` deterministically, from a hash of the
case id, **stratified by category**. Two consequences:

- Re-running `gen-cases` puts the same case on the same side, so scores stay
  comparable across regenerations.
- Every category appears on both sides. An unstratified split on 50 cases
  routinely puts every adversarial case on one side, and then the holdout
  cannot see the failure mode you were most worried about.

Change the ratio with `--holdout` (default 0.4). Do not hand-edit `split` to
move a case you want the optimiser to fix - that is exactly the corruption the
field exists to prevent.

## Hygiene

- **Stable ids.** `guard` warns when ids appear on only one side of a
  comparison, because a rename hides a regression.
- **One behaviour per case.** If a case can fail two ways you cannot tell which
  broke.
- **Keep the failures in.** The urge to delete a case the agent cannot pass is
  the urge to stop measuring.
- **Version the file** next to the prompt. The pair is the experiment.

## Growing the set from production

The best cases are real ones. On a schedule (`bud-routines`), pull yesterday's
traces (`bud-observability`), find the failures, and append them with the trace
id in `notes`:

```json
{"id":"prod-2026-08-06-01","category":"edge","input":"<the real user message>",
 "rubric":"<what it should have done>",
 "notes":"trace 4f2c...; answered from the wrong policy year"}
```

When someone asks in three months why the prompt has that odd clause, the case
explains it.
