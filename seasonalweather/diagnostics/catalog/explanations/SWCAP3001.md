# SWCAP3001 — CAP source request failed

## Meaning

A bounded request to an external CAP or API source failed.

## Trigger

Connection, response, transport, or dependency failure occurs before product
normalization.

## Correction or recovery

Apply the configured bounded retry or recovery policy and inspect source health.

## Operational effect

That source delivery is unavailable; existing durable alert state remains the
authority.

## Rationale

External ingest failure must be visible without fabricating a product.

## Alternatives or migration

Use a permitted alternate source path or await the next bounded poll.

## Related diagnostics

- `SWCAP1001` reports invalid product content.
