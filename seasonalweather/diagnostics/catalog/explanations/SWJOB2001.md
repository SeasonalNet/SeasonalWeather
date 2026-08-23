# SWJOB2001 — Job contract is incompatible

## Meaning

A command, job, lease, result, or capability contract cannot be admitted.

## Trigger

The payload is bounded but conflicts with the current job policy or release
contract.

## Correction or recovery

Use the current typed contract and compatible controller/worker release family.

## Operational effect

The job is not leased or executed under an incompatible interpretation.

## Rationale

Job state is controller-owned and cannot be inferred from an incompatible
message.

## Alternatives or migration

Allow normal reconciliation to reject the old attempt and submit a new typed
command when policy permits.

## Related diagnostics

- `SWJOB8001` reports a result that needs durable reconciliation.
