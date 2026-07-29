# SWCFG7001 — Configuration source exceeds the byte limit

## Meaning
The source is larger than the compiler's one-megabyte default bound.

## Trigger
Embedding a large generated payload in `config.yaml` can exceed the bound.

## Correction or recovery
Remove unsupported bulk data and keep only documented configuration fields.

## Operational effect
Reading stops at the bound and the candidate is rejected.

## Rationale
Configuration size is bounded to protect offline and startup compilation.

## Alternatives or migration
Store operational data in its owning subsystem rather than in configuration.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
