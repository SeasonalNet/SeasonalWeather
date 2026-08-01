# SWCFG4002 — Compatible capability is degraded

## Meaning
An authorized, compatible capability is accepting work but reports degraded operational health.

## Trigger
Compatibility analysis receives a controller-qualified capability snapshot whose operational state is `DEGRADED` and whose admission state remains usable.

## Correction or recovery
Inspect the capability owner’s health evidence and restore full dependency health.

## Operational effect
The capability remains usable and the finding is advisory unless a separately configured policy blocks warnings.

## Rationale
Usability does not imply full health. Preserving the degraded state prevents a qualified but impaired capability from being represented as satisfied.

## Alternatives or migration
Use a configured viable fallback when policy requires a fully healthy capability.

## Related diagnostics
`SWCFG2003` represents an unsupported or incompatible capability.
