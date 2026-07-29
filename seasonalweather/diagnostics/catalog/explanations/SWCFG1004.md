# SWCFG1004 — Multiple YAML documents

## Meaning
The source contains more than one YAML document separator and document.

## Trigger
Appending `---` followed by a second configuration triggers this condition.

## Correction or recovery
Keep exactly one complete mapping in each configuration file.

## Operational effect
The compiler rejects all documents rather than choosing one.

## Rationale
Selecting the first or last document would make effective configuration ambiguous.

## Alternatives or migration
Store alternatives as separate candidate files and lint each explicitly.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
