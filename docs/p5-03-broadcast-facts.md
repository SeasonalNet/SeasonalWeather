# P5-03 derived broadcast facts

`BroadcastFactService.compute()` is a controller-owned, on-demand boundary.
It admits a current archive reference and at most one previous reference, with
an explicit comparison kind, target identity and evaluation time. It stores
only an eligible candidate. There is no background history search, selection,
airing, synthesized audio, or change to SAME/UGC targeting. PostgreSQL remains
optional; the existing broadcast path never calls this service.

## Evidence and comparison

Use immutable archive table/UUID references. The source adapter recognizes
local alert outbox snapshots (exact alert ID, issued/expiration times and
SAME coverage) and NWS station observations (exact station URL, observation
time, temperature in Celsius or sea-level pressure in pascals). It also admits
an explicitly normalized v1 `fact_evidence` object from the appropriate
archive table: `version`, `kind`, `identity`, `measured_at`, `valid_until`,
`areas`, `value`, and `period_start`/`period_end` for forecasts. The producer
must establish that the samples mean the same thing, including forecast wind
metric and gauge datum. Times include offsets; numeric values use the canonical
units below. A normalized object is not inferred from arbitrary raw payloads.
P4-06 zone metadata, for example, cannot supply forecast facts. No forecast,
marine, or gauge history producer is introduced here, and the installed sources
may not retain two compatible samples.

| Comparison | Material change | Maximum history gap | Candidate lifetime |
| --- | --- | --- | --- |
| Alert coverage | Strict superset or subset; report the changed codes | 24 h | 1 h |
| Alert expiration | Later expiration for the same coverage | 24 h | 1 h |
| Forecast precipitation start | At least 1 h earlier or later | 24 h | 6 h |
| Forecast wind | Increase of at least 5 mph | 24 h | 6 h |
| Observed temperature | Drop of at least 3 °F | 3 h | 1 h |
| Observed sea-level pressure | Drop of at least 2 hPa | 3 h | 1 h |
| Marine forecast wind | Increase of at least 5 knots | 24 h | 6 h |
| Gauge stage | Rise of at least 0.5 feet | 3 h | 1 h |

Forecasts must target the same exact valid period. Alert versions must overlap
in validity. A mixed alert area change is neither an expansion nor a
contraction. No polygon comparison or inferred spatial equivalence is done;
code/identity evidence suffices for these rules without PostGIS. A future
caller requiring geographic enrichment must supply evidence from a validated
version rather than substituting pending or rejected geometry. Candidates are
valid from the new sample time until the earlier of source expiry and the
lifetime above. Priority is advisory (alerts 10, forecasts 20, observations
and gauge 30); confidence means the inputs passed comparability checks, not
that a forecast is certain. Speech is fixed, with UTC times and no raw source
labels. No-change, expired, unsupported, and conflicting comparisons do not
create a candidate.

## Storage and diagnostics

Migration 4 adds `broadcast_fact_candidates`. It does not touch the migration
1 fact table linked to broadcast history; storing a candidate is not evidence
of airtime. The stable candidate UUID uses the derivation version, kind,
ordered source references and target identity. Retries verify identical stored
content; conflicting content fails closed. The database enforces source links
and candidate lookups respect expiry. Source records referenced by candidates
cannot be removed until the candidates are removed.

The requesting controller may supply a diagnostic sink. Insufficient
comparable history uses `SWFACT4001`; impossible explicit archive linkage
or a deterministic-ID collision with different content uses `SWFACT5001` and
fails the operation. Source, geographic, and database failures retain their
originating ownership; ordinary absence of optional capability is not a
fatal failure and no segment diagnostic is emitted here.
