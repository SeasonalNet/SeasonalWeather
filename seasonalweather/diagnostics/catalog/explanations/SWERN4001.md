# SWERN4001 — ERN stream is degraded

## Meaning

The ERN stream remains available only under degraded recovery or relay policy.

## Trigger

Health evidence crosses the configured degradation threshold without a total
loss of the controlled stream boundary.

## Correction or recovery

Continue bounded recovery and require fresh health evidence before clearing the
condition.

## Operational effect

ERN-dependent behavior may be limited; unrelated alert processing remains under
its own authority.

## Rationale

Degradation is distinct from transport failure and from a fabricated success.

## Alternatives or migration

Temporarily disable the affected optional relay according to configuration.

## Related diagnostics

- `SWERN3001` reports a failed transport or decoder.
