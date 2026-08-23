# Diagnostic namespace implementation map

The namespace registry is an architectural ownership map. An active namespace
is implemented only when it has governed catalog definitions, one curated
explanation per code, typed code bindings, and tests that keep the catalog and
binding surfaces synchronized. Runtime occurrence emission remains owned by
the subsystem that observes the condition; this map does not authorize a
generic exception-to-diagnostic translator.

## Active namespaces

| Namespace | Owner | Current binding surface | Catalog coverage |
| --- | --- | --- | --- |
| `SWBUILD` | release engineering | build identity and compatibility contracts | identity-invalid and compatibility-rejected |
| `SWCAP` | alert ingestion | CAP/IPAWS/API normalization boundary | product-invalid and source-failed |
| `SWCFG` | configuration | compiler, semantic validation, preflight, and reload | complete current configuration rule/reload set |
| `SWDB` | persistence | database, durable repository, and recovery authorities | operation-failed and reconciliation-required |
| `SWERN` | ERN ingestion | continuous audio, FFmpeg, SAME decoding, and relay lifecycle | transport/decoder-failed and stream-degraded |
| `SWJOB` | job orchestration | command, job, lease, result, and reconciliation authorities | contract-incompatible and result-reconciliation |
| `SWLQS` | broadcast publication | Liquidsoap control and publication authority | control-failed and publication-reconciliation |
| `SWNWWS` | NWWS ingestion | normalized controller-owned source adapter | complete current source lifecycle set |
| `SWOBS` | observability | optional sink and telemetry boundaries | complete current optional-output set |
| `SWRUN` | runtime | supervisor, fatal boundary, and prior-shutdown recovery | complete current runtime set |
| `SWSEG` | broadcast segments | authoritative registry, refresh, and publication evidence | registry-invalid, refresh-failed, fallback, and publication-reconciliation |
| `SWTTS` | speech synthesis | backend-neutral service and media/fallback boundary | response, provider, fallback, trust, and bound conditions |
| `SWWP` | worker protocol | diagnostic envelope and controller compatibility boundary | envelope-rejected and contract-incompatible |

`SWCACHE` and `SWREDIS` remain reserved. They intentionally have no catalog
codes or runtime bindings until their architecture is approved.

## Binding and emission rules

Configuration and segment validation use the rule bindings in
`seasonalweather.diagnostics.bindings`. Runtime, NWWS, observability, reload,
and foundation subsystem codes are kept in named immutable binding maps. A
subsystem may promote one of its codes into a runtime occurrence only at its
existing authority boundary, with bounded redacted evidence and the normal
active/resolved lifecycle. A catalog definition by itself does not permit a
caller, worker, route, or generic logger to claim that condition.

The current runtime emission paths are concrete: CAP/IPAWS pollers emit
`SWCAP`; ERN process and queue supervision emits `SWERN`; job admission and
startup reconciliation emit `SWJOB`; database housekeeping emits `SWDB`;
Liquidsoap control/publication emits `SWLQS`; the required segment refresher
emits `SWSEG` for refresh failure, fallback, and ambiguous publication; and the
backend-neutral TTS service emits `SWTTS`. The controller attaches these ports to the existing
runtime objects through `RuntimeDiagnosticSink`, which validates namespace
ownership and promotes through the controller occurrence repository. Optional
observability callbacks emit `SWOBS` for queue degradation, bounded queue
drops, transport failures, and authorization/trust failures. Configuration
validation and segment-registry diagnostics remain report-boundary emissions;
segment refresh and publication diagnostics are promoted by the required
segment runtime. NWWS, reload, runtime, and worker-protocol diagnostics retain
their existing owners.

`SWBUILD1001` is emitted at startup when build identity cannot be loaded.
`SWBUILD2001` is emitted when the loaded identity is well formed but cannot
enter the selected runtime role: for example, a controller is started from a
worker profile, a worker profile does not match its selected image, or the
embedded software/protocol/schema versions have no supported overlap. Startup
is rejected before normal controller or worker work begins.

Codes preserve the universal condition-class bands: invalid input (`1xxx`),
unsupported state (`2xxx`), dependency (`3xxx`), degradation (`4xxx`),
permanent/fatal (`5xxx`), security/trust (`6xxx`), resource/deadline (`7xxx`),
and lifecycle/recovery (`8xxx`). The `x000` boundaries and `9xxx` remain
unassignable.

## Packet ownership

The catalog and binding groundwork is complete, and the runtime paths listed
above are wired to existing subsystem authorities. This does not create
implementations for the reserved `SWCACHE` or `SWREDIS` namespaces.
