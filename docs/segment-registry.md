# Authoritative segment registry

`seasonalweather.broadcast.segment_registry` is the single authority for
static segment identity, titles, existing builder references, configuration
enablement mappings, normal/focus ordering, refresh and freshness policy,
minimum airing intervals, failure policy, capability requirements, and segment
policy metadata.

The registry maps to typed configuration values; it does not parse YAML or own
configuration generations. Runtime capability availability and health remain
owned by the capability subsystem. Dynamic alert and scheduled-insert keys are
runtime state derived by their existing owners and are not static registry
definitions.

Duplicate/invalid definitions and contradictory ordering/freshness policies
fail closed with the governed `SWSEG1001` and `SWSEG2001` diagnostics.

HWO is always enabled when a real HWO product is available. Its
`hwo.speak_unavailable` configuration field is a registry-owned fallback
policy only; it suppresses or permits the synthetic unavailable speech and is
not base HWO enablement.

Builder references identify current execution seams. Most static content uses
the shared `CycleBuilder.build_segments` path; status and station ID retain
their existing refresher paths, and live time remains conductor-owned. P1-20
independent builders are not introduced here.
