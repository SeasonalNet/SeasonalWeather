# SWCFG1001 — Configuration source is not UTF-8

## Meaning
The selected configuration contains bytes that cannot be decoded as UTF-8.

## Trigger
Saving `config.yaml` in a legacy or binary encoding can trigger this condition.

## Correction or recovery
Re-save the complete file as UTF-8 without replacing meaningful characters.

## Operational effect
Compilation stops before YAML parsing, and the candidate is not activated.

## Rationale
One strict encoding keeps source spans, hashing, and operator output deterministic.

## Alternatives or migration
Convert legacy text with a trusted editor or encoding tool, then lint the converted candidate.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
