# SWNWWS2002 — NWWS UGC and VTEC routing metadata disagreed

The product supplied both a UGC expiration and a parseable VTEC expiration,
but the two independently derived routing times differed beyond the bounded
minute-level tolerance.

## Meaning

This is a metadata consistency warning. It does not mean that a product with
no VTEC is invalid: many supported NWS products legitimately use UGC without
VTEC.

## Trigger

SeasonalWeather emits this diagnostic only when both UGC and VTEC are present,
both expiration values parse successfully, and their UTC values disagree by
more than the allowed one-minute precision tolerance.

## Correction or recovery

Keep the product's normal routing decision. Inspect the raw product and the
two parsed timestamps. VTEC remains authoritative for VTEC lifecycle expiry;
UGC remains available for products and routing paths that do not carry VTEC.

## Operational effect

The product is not blocked, downgraded, replayed, or silently rewritten. The
warning adds bounded evidence for operator review.

## Rationale

UGC and VTEC are complementary metadata channels. Treating absent VTEC as a
conflict would incorrectly flag products whose format does not define VTEC.

## Alternatives or migration

Do not infer a VTEC record for a VTEC-less product. A future parser may improve
the comparison tolerance or evidence without changing the normal UGC-only path.

## Related diagnostics

See `SWNWWS1001` and `SWNWWS2001` for malformed products and incompatible
source exchanges.
