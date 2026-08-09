# SWCFG2005 — Configuration reload change is not classified

## Meaning
A typed path is absent from the versioned reload policy.

## Trigger
The controller computes an effective change with no exact supported classification.

## Correction or recovery
Upgrade to a release with a complete policy or correct the candidate.

## Operational effect
Nothing is applied and the old generation remains active.

## Rationale
New schema paths must never silently become live reloadable.

## Alternatives or migration
Apply the candidate through a normal reviewed restart if supported by its release documentation.

## Related diagnostics
See `SWCFG0004` for valid candidates requiring operator action.
