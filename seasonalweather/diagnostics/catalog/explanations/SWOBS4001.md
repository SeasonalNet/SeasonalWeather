# SWOBS4001 — Optional observability destination is degraded

## Meaning

An optional destination is unavailable while the controller continues through its canonical local observability path.

## Trigger

Repeated delivery failures or a disabled optional destination leave the configured output unavailable.

## Correction or recovery

Repair the destination or remove its optional configuration. Confirm that local JSON logs and `/metrics` remain available.

## Operational effect

Only the optional destination's visibility is degraded. Broadcast work is not delayed for retries.

## Rationale

Degraded optional visibility is safer than introducing blocking retry behavior into alert processing.

## Alternatives or migration

Route the canonical local stream through a separately managed collector while the destination is repaired.

## Related diagnostics

Related conditions are SWOBS3001 and SWOBS7001.
