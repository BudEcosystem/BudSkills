# Router DAG reference

Everything a router does is expressed in `dag_config`. This file is the
detail behind the summary in `SKILL.md`.

Always cross-check against the installation itself - it ships the registry:

```bash
bud api GET /routers/actions | jq -r '.result.actions[] | "\(.type)\t\(.category)\t\(.signal_class)"'
bud api GET /routers/actions/signal_complexity | jq '.result.params'
bud api POST /routers/actions/validate \
  -d '{"action_type":"signal_complexity","params":{"threshold":0.7,"hard_candidates":["..."],"easy_candidates":["..."]}}'
```

## The step object

```json
{
  "step_id": "unique_within_this_dag",
  "name": "Human Name",          // the identity rules reference - required in practice
  "action_type": "signal_complexity",
  "params": { },
  "depends_on": ["decision_step_id"]   // Algorithms and Plugins only
}
```

`dag_config` itself is `{name, description, entry_step, steps[], parameters[],
outputs{}}`. `entry_step` must be one of the `step_id`s. An empty `steps` list
is legal and skips validation entirely - useful only for a `draft` placeholder.

**`name` versus `step_id`:** rules, projection inputs and partition members all
reference the **`name`**. `step_id` is only used by `depends_on` and
`entry_step`. Names must be non-empty and unique per signal class.

## Rule grammar

A Decision's `rules` (and `signal_complexity.composer`) is a tree:

- Branch: `{"operator": "AND" | "OR" | "NOT", "conditions": [ ...nodes ]}`
- Leaf: `{"type": "<signal_class>", "name": "<referenced step name>"}`

Rules:

- `AND` with `conditions: []` matches everything - the canonical catch-all.
  `OR` and `NOT` require at least one child.
- `NOT` with several children means "none of them matched".
- No keys other than `operator`/`conditions` on a branch or `type`/`name` on a
  leaf; extras are rejected.
- `type` is the referenced action's **signal class**, not its action type:

| Action | leaf `type` |
|---|---|
| `signal_keyword` | `keyword` |
| `signal_embedding` | `embedding` |
| `signal_domain` | `domain` |
| `signal_user_feedback` | `user_feedback` |
| `signal_reask` | `reask` |
| `signal_preference` | `preference` |
| `signal_language` | `language` |
| `signal_geography` | `geography` |
| `signal_context` | `context` |
| `signal_structure` | `structure` |
| `signal_complexity` | `complexity` (with `:easy` / `:medium` / `:hard`) |
| `signal_kb` | `kb` |
| `signal_conversation` | `conversation` |
| any `projection_*` | `projection` |

Projection leaves reference the **band or member name** the projection
produces (e.g. `{"type":"projection","name":"High"}`), not the projection
step's name.

## Signals

| Action | Required params | Optional params |
|---|---|---|
| `signal_keyword` | `operator` (`AND`/`OR`/`NOR`), `keywords[]` | `method` (`regex`/`bm25`/`ngram`), `case_sensitive`, `fuzzy_match`, `fuzzy_threshold`, `bm25_threshold`, `ngram_threshold`, `ngram_arity` |
| `signal_embedding` | `threshold` (def 0.78), `candidates[]` | `aggregation_method` (`mean`/`max`/`any`) |
| `signal_domain` | `mmlu_categories[]` from biology, business, chemistry, computer science, economics, engineering, health, history, law, math, other, philosophy, physics, psychology | - |
| `signal_user_feedback` | `feedback_type` (`satisfied`/`need_clarification`/`wrong_answer`/`want_different`) | - |
| `signal_reask` | - | `threshold` (def 0.8), `lookback_turns` (def 1) |
| `signal_preference` | - | `examples[]`, `threshold` |
| `signal_language` | `language_code` (en, es, fr, de, it, pt, ru, zh, ja, ko, ar, hi) | - |
| `signal_geography` | `country_codes[]` (ISO-3166 alpha-2; `UNKNOWN` included for unresolvable origins) | - |
| `signal_context` | `min_tokens`, `max_tokens` (strings, `K`/`M` shortcuts allowed) | - |
| `signal_structure` | `feature_type` (`count`/`exists`/`sequence`/`density`), `source_type` (`regex`/`keyword_set`/`sequence`) | `pattern`, `keywords[]`, `case_sensitive`, `sequences`, `predicate_gt/gte/lt/lte` |
| `signal_complexity` | `threshold` (def 0.7), `hard_candidates[]`, `easy_candidates[]` | `composer` (a rule tree gating the match) |
| `signal_kb` | `kb` (`privacy_kb`/`mmlu_kb`), `target_kind` (`label`/`group`), `target_value` | `match` (`best`/`threshold`) |
| `signal_conversation` | `feature_type` (`count`/`exists`), `source_type` (`message`/`tool_definition`/`assistant_tool_call`/`assistant_tool_cycle`) | `role`, `predicate_gt/gte/lt/lte` |

One rule per value: a Language signal binds to one language code, a user
feedback signal to one feedback type. Route three languages differently with
three signal steps and three Decisions.

## Decision

| Param | Notes |
|---|---|
| `rules` | required, the tree above |
| `modelRefs` | required, list of **endpoint ids** (UUIDs of running deployments) |
| `priority` | default 0, higher is evaluated first, first match wins |
| `tier` | bookkeeping only, no routing effect |

With more than one endpoint in `modelRefs`, the attached Algorithm chooses.
With no Algorithm attached, the first candidate is used.

## Projections

`projection_score` - fuse signals into a number:

```json
{"step_id":"score","name":"Score Difficulty","action_type":"projection_score",
 "params":{"method":"weighted_sum",
   "inputs":[{"type":"complexity","name":"Needs Reasoning:hard","weight":0.7,"match":1.0,"miss":0.0},
             {"type":"structure","name":"Has Code Block","weight":0.3,"match":1.0,"miss":0.0}]}}
```

`method` is `weighted_sum` (default), `max`, `min` or `avg`. Signal inputs can
carry `value_source` of `binary`, `confidence` or `raw`; knowledge-base inputs
use `score`.

`projection_mapping` - cut that number into named bands referenced by
Decisions. Bound keys are `lt`, `lte`, `gt`, `gte`:

```json
{"step_id":"band","name":"Difficulty Band","action_type":"projection_mapping",
 "params":{"source":"Score Difficulty","method":"threshold_bands",
   "outputs":[{"name":"Low","lte":0.3},{"name":"Mid","gt":0.3,"lte":0.7},{"name":"High","gt":0.7}]}}
```

`method` is `threshold_bands`, `percentile_bands` or `sigmoid`; optional
`calibration_method` (`sigmoid_distance`/`linear`/`logistic`) and
`calibration_slope` attach a confidence number to the chosen band. `source` is
the **name** of the score step.

`projection_partition` - group related signals and pick one:
`members[]` (step names), `semantics` (`exclusive` default, or `overlapping`),
optional `default` (must be one of the members) and `temperature` for
probabilistic picking.

Three Decisions then reference `{"type":"projection","name":"Low"|"Mid"|"High"}`.
This is the cleanest shape for a three-tier cost ladder.

## Algorithms

Attach with `depends_on: ["<decision step_id>"]`. **At most one Algorithm per
Decision** - two is a validation error, because the winner would be arbitrary.

Selection (choose one candidate, then call it once):

| Action | Params | Use for |
|---|---|---|
| `algorithm_static` | none | Deterministic: always the first candidate |
| `algorithm_round_robin` | none | Even rotation across equivalent deployments |
| `algorithm_random` | none | Uniform random |
| `algorithm_weighted_random` | `endpoint_weights[{endpoint_id, weight}]` | Canary / A-B splits |
| `algorithm_least_busy` | none | Prefer whichever deployment has fewest in-flight requests |
| `algorithm_latency_aware` | `tpot_percentile`, `ttft_percentile` (integers 1-100; 0/unset disables that metric) | Interactive UIs with a latency budget |
| `algorithm_router_dc` | `temperature`, `require_descriptions`, `use_capabilities` | Models with distinct strengths, matched from their descriptions |
| `algorithm_automix` | `verification_threshold`, `max_escalations`, `cost_aware_routing`, `cost_quality_tradeoff`, `discount_factor`, `use_logprob_verification` | Cheap-first with a verification gate |
| `algorithm_hybrid` | `router_dc_weight`, `automix_weight`, `cost_weight`, `elo_weight`, `quality_gap_threshold`, `normalize_scores` | A blended quality/cost score |

Loopers (may call several candidates for one request - slower and more
expensive, but this is where quality-driven fallback lives):

| Action | Params |
|---|---|
| `algorithm_confidence` | `confidence_method` (`hybrid`/`entropy`/`logprob`), `threshold`, `logprob_weight`, `margin_weight`, `on_error` (`skip`/`escalate`), `escalation_order` (`small_to_large`/`large_to_small`), `cost_quality_tradeoff`, `token_filter` (`all`/`tool_call_args`) |
| `algorithm_ratings` | `max_concurrent` (def 3), `on_error` - runs candidates in parallel, returns the best-rated |
| `algorithm_remom` | `breadth_schedule` (def `[3,2,1]`), `model_distribution`, `temperature`, `include_reasoning`, `compaction_strategy`, `compaction_tokens`, `synthesis_template`, `max_concurrent`, `on_error`, `shuffle_seed`, `include_intermediate_responses`, `max_responses_per_round` |

`elo_weight` on `algorithm_hybrid` currently has no backing rating store -
leave it at 0.

## Plugins

Also attached with `depends_on` to one Decision. Several plugins may share a
Decision.

| Action | Params |
|---|---|
| `plugin_semantic_cache` | `enabled`, `similarity_threshold` (def 0.92), `ttl_seconds` (def 86400) |
| `plugin_system_prompt` | `system_prompt` (required), `mode` (`insert` replaces / `append` adds), `enabled` |
| `plugin_request_params` | `blocked_params[]`, `max_tokens_limit`, `max_n`, `strip_unknown` |
| `plugin_fast_response` | `message` (required) - answers immediately without calling a model |

`plugin_fast_response` is the maintenance-window / canned-refusal switch: put
it on a narrow Decision, never on the catch-all, unless you intend to stop
serving.

## Validation rules

Save-time validation returns `422` with a per-step breakdown:

```json
{"detail":{"message":"DAG validation failed",
  "steps":[{"step_id":"split","action_type":"algorithm_weighted_random",
            "errors":[{"code":"invalid_reference","message":"..."}]}]}}
```

Codes: `unknown_action_type`, `invalid_param`, `invalid_attachment`,
`invalid_topology`, `invalid_reference`. What gets checked:

- Every `action_type` exists **and is enabled**. Four types are disabled and
  will be rejected even though the names look plausible: `signal_fact_check`,
  `signal_modality`, `plugin_response_jailbreak`, `plugin_hallucination`.
- Step names non-empty and unique within a signal class; every rule leaf, score
  input and partition member resolves to a real name.
- Complexity references use `<name>:<level>`.
- Algorithms and Plugins have exactly one `depends_on`, pointing at a Decision.
  A Decision, Signal or Projection may never depend on an Algorithm or Plugin.
  At most one Algorithm per Decision.
- No cycles in `depends_on`.
- `endpoint_weights` endpoint set equals the target Decision's `modelRefs` set.
- `algorithm_latency_aware` needs at least one percentile set, as an **integer**
  (`50.0` is rejected; setting only one produces a warning).

Two traps this validation does **not** catch:

> **A misspelled param name is a warning, not an error.** `POST
> /routers/actions/validate` reports `Unknown param 'treshold' (will be
> ignored)` and returns `valid: true`; the router saves, and the action runs on
> its defaults. Read the `warnings` array, not just `valid`.

> **Endpoint ids that no longer exist are not rejected at sync time.** They
> drop out of the router's overall candidate list and stay as unresolvable
> references on the Decision that named them. The router reports `synced`; the
> branch fails only when a request reaches it.
