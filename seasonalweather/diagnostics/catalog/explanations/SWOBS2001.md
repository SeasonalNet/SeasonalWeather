# SWOBS2001 — Optional observability output configuration was rejected

## Meaning

An optional observability destination does not satisfy the bounded configuration contract.

## Trigger

The configured endpoint, queue, TLS setting, or deployment-provided protocol adapter is missing or invalid.

## Correction or recovery

Disable the optional destination or correct its bounded endpoint and trust configuration. Keep the canonical local stream enabled.

## Operational effect

The destination is not started. Broadcast processing and canonical stdout/stderr logging continue.

## Rationale

Optional integrations must never turn an invalid destination into a broadcast dependency.

## Alternatives or migration

Use the controller `/metrics` endpoint and external collectors while the optional destination is being provisioned.

## Related diagnostics

Related conditions are SWOBS3001 and SWOBS6001.
