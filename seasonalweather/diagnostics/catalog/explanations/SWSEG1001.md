# SWSEG1001 — Segment registry definition is invalid

## Meaning
An authoritative segment definition is invalid input. This includes duplicate stable keys, malformed identity or title, invalid or unavailable builder declarations, invalid enablement or fallback declarations, malformed enum or policy values, invalid numeric or ordering values, malformed capability declarations, malformed policy metadata, and invalid declared typed-configuration paths.

## Trigger
Registry construction finds any invalid definition in the governed invalid-input family, including a duplicate stable key, malformed identity/title, a builder declaration that is not one of the existing executable P1-19 seams, an invalid enablement/fallback declaration, malformed enum/policy/numeric/order/capability/metadata values, or a declared enablement/fallback path that is absent or not boolean on the supplied typed configuration.

## Correction or recovery
Correct the registry definition or its typed configuration path before constructing the resolved registry.

## Operational effect
Registry resolution is rejected; no consumer can observe an ambiguous or silently defaulted segment policy.

## Rationale
First-wins, last-wins, or silent defaulting would make segment behavior depend on construction order or hide a configuration/schema mismatch.

## Alternatives or migration
Use one stable key, an existing executable builder seam, valid policy values, and valid boolean fields on the typed configuration object. The registry does not parse YAML or own configuration generations.

## Related diagnostics
See `SWSEG2001` for contradictory ordering or freshness policy.
