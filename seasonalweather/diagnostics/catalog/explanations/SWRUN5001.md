# SWRUN5001 — Controller terminated after an uncaught fatal failure

## Meaning

An uncaught startup, supervisor, event-loop, lifespan, or top-level controller failure made continued execution unsafe.

## Trigger

The original exception reaches the controller fatal boundary or a required supervised task fails unexpectedly.

## Correction or recovery

Use the preserved redacted traceback and chained evidence to correct the originating failure, then allow the service manager to start a fresh instance.

## Operational effect

The current process reports a bounded emergency diagnostic and exits nonzero without claiming a clean shutdown.

## Rationale

Process consequence is distinct from the subsystem condition while retaining the original Python failure evidence.

## Alternatives or migration

Expected operator shutdown and bounded offline command failures use their existing nonfatal outcomes.

## Related diagnostics

- `SWRUN4001` reports optional task degradation.
- `SWRUN8001` reports next-start evidence of an incomplete prior shutdown.
