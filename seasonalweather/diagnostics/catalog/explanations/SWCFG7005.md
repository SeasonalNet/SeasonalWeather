# SWCFG7005 — Configuration scalar exceeds the size limit

## Meaning
One YAML scalar is longer than the compiler's code-point bound.

## Trigger
Embedding a large document or binary-like text block in one field can trigger this condition.

## Correction or recovery
Replace it with the bounded value documented for the field.

## Operational effect
The candidate is rejected and the oversized value is not echoed.

## Rationale
Scalar bounds protect memory, source frames, reports, and secret redaction.

## Alternatives or migration
Keep external documents in their owning resource location when the application supports one.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
