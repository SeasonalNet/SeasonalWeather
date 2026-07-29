# SWCFG1002 — Configuration document is empty

## Meaning
The selected file contains only whitespace, comments, or no content.

## Trigger
A file containing only `# configuration pending` is empty to the compiler.

## Correction or recovery
Start from `config/config.yaml`, preserve `config_schema: 1`, and supply deployment values separately.

## Operational effect
Compilation stops and no runtime configuration is produced.

## Rationale
An empty document cannot express the required station and service behavior.

## Alternatives or migration
Lint a complete candidate before replacing any active configuration.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
