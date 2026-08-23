# SWSEG8001 — Segment publication requires reconciliation

## Meaning
The controller cannot prove whether a segment publication committed, so normal refresh for that key remains deferred.

## Trigger
Segment-store publication or its commandless reconciliation returns ambiguous or incomplete evidence.

## Correction or recovery
Inspect the segment-store evidence and complete the bounded reconciliation path before allowing the key to refresh normally.

## Operational effect
The affected segment is isolated from duplicate publication until durable evidence resolves the ambiguity.

## Rationale
Ambiguous publication evidence must remain isolated and repairable; treating it as an ordinary refresh failure could duplicate or incorrectly replace on-air content.

## Alternatives or migration
Use the existing bounded segment reconciliation path and do not retry publication blindly while evidence remains ambiguous.

## Related diagnostics
See `SWSEG3001` for an ordinary refresh dependency failure and `SWSEG4001` for fallback operation.
