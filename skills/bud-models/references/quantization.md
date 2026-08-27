# Quantization

Quantization converts a self-hosted model to lower-precision weights so it
serves on cheaper or smaller hardware. It trades quality for cost, and the
trade is real - always evaluate the result.

## Before you start

```bash
bud api GET /models/<model-id> | jq '.model | {is_quantizable, quantization_unsupported_reason}'
```

If `is_quantizable` is false, stop and report
`quantization_unsupported_reason`. Common causes are architectures the
quantization toolchain does not support and models that are already quantized.

## Pick a method that your target hardware can actually run

```bash
bud api GET /models/quantization-methods -q page=1 -q limit=1000
```

Each method carries two different hardware lists, and confusing them wastes
hours:

| Field | Means |
|---|---|
| `hardware_support` | where the quantization **job** can run |
| `runtime_hardware_support` | where the **output** can be served |

Choose on `runtime_hardware_support` matching the hardware you intend to serve
on. A model quantized into a format your serving hardware cannot load is a
completely wasted job, and the failure only appears at deploy time.

`method_type` tells you which precisions the method produces (for example
`INT4`, `INT8`).

## The stepped session

```bash
W=$(bud api POST /models/quantize-model-workflow -d '{
  "workflow_total_steps":7,"step_number":1,"trigger_workflow":false,
  "model_id":"<model-id>",
  "quantized_model_name":"Qwen3-8B-INT4",
  "target_type":"INT4","target_device":"cuda"}' | jq -r .workflow_id)

bud api POST /models/quantize-model-workflow -d '{"workflow_id":"'"$W"'","step_number":2,"method":"AWQ"}'

bud api POST /models/quantize-model-workflow -d '{"workflow_id":"'"$W"'","step_number":3,
  "weight_config":{"bit":4,"granularity":"per_group","symmetric":true},
  "activation_config":{"bit":8,"granularity":"per_token","symmetric":false}}'

bud api POST /models/quantize-model-workflow -d '{"workflow_id":"'"$W"'","step_number":5,
  "trigger_workflow":true,"cluster_id":"<cluster-id>"}'

bud wait job $W --timeout 14400 --interval 60     # 20 minutes to several hours
bud job show $W --data | jq -r '.data.quantization_config.quantized_model_id'
```

Field notes:

- `quantized_model_name` must be **globally unique** or the first call 400s.
- `method` is validated against the seeded method list - an unknown name gives
  `Invalid quantization method`.
- `cluster_id` is the cluster's own identifier, the same value deployments use
  (from a recommendation's `cluster_id`, or `GET /clusters/clusters`).
- Quantization runs **on a cluster** and consumes real accelerator time.
  Confirm with the user before starting one; a large model can occupy hardware
  for hours.

Abort with:

```bash
bud api POST /models/cancel-quantization -d '{"workflow_id":"'"$W"'"}'
```

## Afterwards

The output is a new registry model, related to the original. Find related
models with `base_model_relation=quantized`:

```bash
bud api GET /models/ -q table_source=model -q base_model=<original-uri> \
  -q base_model_relation=quantized
```

> **Do not report the quality figure produced by the quantization job as a
> measure of accuracy.** It is not a trustworthy quality signal. To know
> whether the quantized model is good enough, evaluate it against real examples
> with `bud-evaluations` and compare against the original.

Then deploy it like any other self-hosted model (`bud-deployments`) - the
capacity plan will show the cheaper hardware footprint that motivated the
exercise.
