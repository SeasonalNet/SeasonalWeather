# SWCFG1014 — Required configuration field is missing

## Meaning
A field required by configuration schema 1 is absent.

## Trigger
Removing the required `station.name` field triggers this condition.

## Correction or recovery
Add the reported field at the source-addressed containing mapping.

## Operational effect
No partial `AppConfig` is produced or activated.

## Rationale
Required fields have no safe deployment-independent value to invent.

## Alternatives or migration
Begin with the repository example to retain the complete required shape.

## Related diagnostics
No related diagnostic is assigned in catalog version 1.
