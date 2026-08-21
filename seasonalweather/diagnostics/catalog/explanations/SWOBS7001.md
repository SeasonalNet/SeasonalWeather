# SWOBS7001 — Optional observability queue dropped a record

## Meaning

A bounded optional-output queue was full and dropped a noncanonical record.

## Trigger

Destination delivery remained slower than event production until the configured queue reached capacity.

## Correction or recovery

Inspect sink drop metrics, destination latency, and queue sizing. Do not make the queue unbounded or block broadcast callers.

## Operational effect

The optional record is not delivered. Canonical local logging and broadcast processing continue immediately.

## Rationale

Bounded loss in an optional sink is safer than unbounded memory growth or alert-path backpressure.

## Alternatives or migration

Improve the destination or use a local collector that can absorb bursts without changing controller ownership.

## Related diagnostics

Related condition is SWOBS4001.
