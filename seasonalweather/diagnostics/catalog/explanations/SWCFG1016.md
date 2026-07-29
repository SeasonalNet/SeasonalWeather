# SWCFG1016 — Configuration value is outside its allowed choices

## Meaning
A structurally valid scalar is not one of the choices supported by its field.

## Trigger
Using an undocumented mode name where the schema defines a fixed enum triggers this condition.

## Correction or recovery
Select one of the choices documented for the reported path.

## Operational effect
The candidate is rejected before unsupported policy reaches runtime code.

## Rationale
Unknown enum values are not safe forward-compatibility defaults.

## Alternatives or migration
Upgrade only when a newer release explicitly documents the desired choice.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
