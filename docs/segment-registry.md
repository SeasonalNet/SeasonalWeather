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

Builder references identify the exact execution seam. Station status and ID
retain their existing refresher paths, live time remains conductor-owned, and
the remaining static definitions point to one independent `CycleBuilder`
method each. A per-segment refresh therefore cannot invoke
`CycleBuilder.build_segments` or rebuild unrelated content. Dynamic alert and
scheduled-insert keys remain runtime-owned.
